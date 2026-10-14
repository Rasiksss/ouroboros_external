"""Исполнение кода временного атома в запечатанной комнате станции (Workstation).

Комната — та же, где работают навыки станции: свой rootfs на сессию, ws-sandbox
(Landlock + seccomp + cgroup), сокетов нет, окружение backend не проходит.
Контракт HTTP-API — тот же, что у плагина гейтвея (deploy/workstation-ext/index.ts):
создать сессию → exec → закрыть. Одна комната на один запуск.
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from finengine.config import settings

_ENTRY = Path(__file__).with_name("_room_entry.py").read_text(encoding="utf-8")
TIMEOUT_SECONDS = 60
MAX_OUTPUT_CHARS = 1_000_000
_HTTP_SLACK_SECONDS = 15       # сверх таймаута кода: создание комнаты и сеть до сервиса

# Порт выхода (часть 16): всё, что код записал в `out/` рабочего каталога, клиент забирает по
# `files:list` / `files:download` до закрытия комнаты. Формат файла не ограничен.
OUT_DIR = "/workspace/out"
MAX_ROOM_FILES = 5
MAX_ROOM_FILE_BYTES = 20 * 1024 * 1024
MAX_ROOM_TOTAL_BYTES = 30 * 1024 * 1024


@dataclass(frozen=True)
class RoomFile:
    name: str                  # базовое имя, без путей
    content: bytes


@dataclass(frozen=True)
class CodeRun:
    output: dict | None
    error: str | None          # хвост traceback / причина, ≤ 2000 символов
    station_fault: bool = False   # сбой станции, а не кода: автору атома чинить нечего
    files: tuple[RoomFile, ...] = ()   # файлы из `out/`; только при `collect_files=True` и успешном исполнении


def run_code(code: str, inputs: dict, *, timeout: int = TIMEOUT_SECONDS,
             transport: httpx.BaseTransport | None = None, collect_files: bool = False) -> CodeRun:
    if not settings.workstation_url:
        return CodeRun(None, "комната станции не настроена: FINENGINE_WORKSTATION_URL пуст",
                       station_fault=True)
    session_id = f"eph-{uuid.uuid4().hex[:16]}"
    stdin = json.dumps({"code": code, "inputs": inputs}, ensure_ascii=False).encode()
    with httpx.Client(base_url=settings.workstation_url, timeout=timeout + _HTTP_SLACK_SECONDS,
                      transport=transport) as client:
        token = None
        try:
            token = _open_room(client, session_id)
            reply = _exec(client, session_id, token, stdin, timeout)
            run = _read_reply(reply, timeout)
            if collect_files and run.error is None:
                run = _with_files(client, session_id, token, run)
            return run
        except (httpx.HTTPError, ValueError, KeyError) as exc:   # ValueError — тело не JSON; KeyError — нет token
            return CodeRun(None, f"станция не исполнила код: {exc}", station_fault=True)
        finally:
            if token:                                            # нет токена — нет и комнаты
                _close_room(client, session_id, token)


def _open_room(client: httpx.Client, session_id: str) -> str:
    response = client.post("/v1/sessions", json={"idleTimeoutSec": 300},
                           headers={"X-Session-Id": session_id})
    response.raise_for_status()
    return response.json()["token"]


def _exec(client: httpx.Client, session_id: str, token: str, stdin: bytes, timeout: int) -> dict:
    response = client.post(
        f"/v1/sessions/{session_id}/exec",
        headers={"Authorization": f"Bearer {token}", "X-Session-Id": session_id},
        json={"command": ["python3", "-I", "-c", _ENTRY], "cwd": "/workspace", "env": {},
              "timeoutSec": timeout, "stdin": base64.b64encode(stdin).decode(),
              "stdinEncoding": "base64"},
    )
    response.raise_for_status()
    return response.json()


def _with_files(client: httpx.Client, session_id: str, token: str, run: CodeRun) -> CodeRun:
    """Файлы из `out/`. Нарушение пределов — ошибка кода атома (автору её чинить), сбой станции — исключение."""
    headers = {"Authorization": f"Bearer {token}", "X-Session-Id": session_id}
    listing = client.post(f"/v1/sessions/{session_id}/files:list", headers=headers,
                          json={"path": OUT_DIR, "recursive": True, "maxDepth": 2, "includeSha256": True})
    if listing.status_code == 404:        # каталога нет: код файлов не писал
        return run
    listing.raise_for_status()
    found = [node for node in listing.json().get("children") or [] if node.get("kind") == "file"]
    problem = _name_problem(found)       # до фильтра служебных: «../x» не прячется за точкой в начале имени
    nodes = [node for node in found if not _is_service_file(str(node.get("name") or ""))]
    problem = problem or _limits_problem(nodes)
    if problem:
        return CodeRun(None, problem)
    files = []
    for node in nodes:
        name = str(node["name"])
        response = client.post(f"/v1/sessions/{session_id}/files:download", headers=headers,
                               json={"path": f"{OUT_DIR}/{name}"})
        response.raise_for_status()
        content = response.content
        if not content:
            return CodeRun(None, f"файл out/{name} пуст: код должен записать содержимое")
        if node.get("sha256") and hashlib.sha256(content).hexdigest() != node["sha256"]:
            return CodeRun(None, f"файл out/{name} скачался повреждённым: хеш не совпал со списком")
        files.append(RoomFile(name, content))
    return replace(run, files=tuple(files))


def _is_service_file(name: str) -> bool:
    return name.startswith(".") or name.startswith("~$")


def _name_problem(nodes: list[dict]) -> str | None:
    for node in nodes:
        name = str(node.get("name") or "")
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            return f"недопустимое имя файла в out/: {name!r} — только имя, без пути"
    return None


def _limits_problem(nodes: list[dict]) -> str | None:
    if len(nodes) > MAX_ROOM_FILES:
        return f"в out/ записано {len(nodes)} файлов, максимум {MAX_ROOM_FILES}"
    for node in nodes:
        if int(node.get("bytes") or 0) > MAX_ROOM_FILE_BYTES:
            return f"файл out/{node['name']} больше {MAX_ROOM_FILE_BYTES // (1024 * 1024)} МБ"
    if sum(int(node.get("bytes") or 0) for node in nodes) > MAX_ROOM_TOTAL_BYTES:
        return f"файлы в out/ вместе больше {MAX_ROOM_TOTAL_BYTES // (1024 * 1024)} МБ"
    return None


def _close_room(client: httpx.Client, session_id: str, token: str) -> None:
    try:   # без Bearer станция отвечает 401 и комната живёт до idleTimeoutSec
        client.post(f"/v1/sessions/{session_id}:terminate",
                    headers={"Authorization": f"Bearer {token}", "X-Session-Id": session_id})
    except httpx.HTTPError:
        pass  # best-effort: незакрытая комната истечёт по idleTimeoutSec


def _read_reply(reply: dict, timeout: int) -> CodeRun:
    if reply.get("timedOut"):
        return CodeRun(None, f"код работал дольше {timeout} с")
    if reply.get("exitCode") != 0:
        stderr = (reply.get("stderr") or "")[-2000:]
        return CodeRun(None, stderr or f"код завершился с кодом {reply.get('exitCode')}")
    stdout = reply.get("stdout") or ""
    if len(stdout) > MAX_OUTPUT_CHARS:
        return CodeRun(None, "результат больше 1 МБ")
    try:
        output = json.loads(stdout, parse_constant=_reject_non_finite)
    except ValueError:
        return CodeRun(None, "комната вернула не JSON")
    if not isinstance(output, dict):
        return CodeRun(None, "комната вернула не объект JSON")
    return CodeRun(output, None)


def _reject_non_finite(constant: str) -> float:
    # Точка входа не пускает NaN, но код атома может писать в fd 1 мимо неё
    # (os.write + os._exit): последнюю границу держит клиент.
    raise ValueError(f"{constant} в результате недопустим")
