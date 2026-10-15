"""C5.2 вживую: разовая комната Workstation контура по протоколу `sealed_room` (CONTRACTS.md §5).

Создать сессию (`aifa-eph-<16 hex>`, секрет создания, версия протокола) → `exec` пробного кода как
у комнаты службы (`python3 -I -c`, `env: {}`) → закрыть. Пробный код печатает один JSON:
импорты пакетов комнаты, попытку открыть сокет и ключи окружения. Секрет создания берётся
из окружения контейнера, не из аргументов и не печатается.

    AIFA_SYM_WORKSTATION_CREATE_SECRET=... python room.py --base-url http://workstation:8080
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import uuid

import httpx

PROBE = r"""
import json, os, socket
mods = {}
for name in ("numpy", "pandas", "openpyxl", "pptx", "docx"):
    try:
        __import__(name)
        mods[name] = "ok"
    except Exception as exc:
        mods[name] = f"{type(exc).__name__}: {exc}"
try:
    s = socket.socket()
    s.settimeout(3)
    s.connect(("1.1.1.1", 53))
    sock = "connected"
except Exception as exc:
    sock = f"blocked: {type(exc).__name__}"
print(json.dumps({"imports": mods, "socket": sock, "env": sorted(os.environ)}))
"""

SERVICE_ENV_PREFIXES = (
    "FINENGINE_", "OUROBOROS_", "WORKSTATION_", "CLOUDRU_", "AWS_", "POSTGRES", "AIFA_SYM_",
)


def room(base_url: str, secret: str) -> dict[str, object]:
    session_id = f"aifa-eph-{uuid.uuid4().hex[:16]}"
    stdin = base64.b64encode(b"{}").decode()
    with httpx.Client(base_url=base_url, timeout=75) as client:
        opened = client.post(
            "/v1/sessions",
            json={"idleTimeoutSec": 300},
            headers={
                "X-Session-Id": session_id,
                "X-Session-Create-Secret": secret,
                "X-Workstation-Protocol": "1",
            },
        )
        opened.raise_for_status()
        token = opened.json()["token"]
        auth = {"Authorization": f"Bearer {token}", "X-Session-Id": session_id}
        try:
            done = client.post(
                f"/v1/sessions/{session_id}/exec",
                headers=auth,
                json={
                    "command": ["python3", "-I", "-c", PROBE],
                    "cwd": "/workspace",
                    "env": {},
                    "timeoutSec": 60,
                    "stdin": stdin,
                    "stdinEncoding": "base64",
                },
            )
            done.raise_for_status()
            reply: dict[str, object] = done.json()
        finally:
            client.post(f"/v1/sessions/{session_id}:terminate", headers=auth)
    return reply


def verdict(reply: dict[str, object]) -> list[str]:
    problems: list[str] = []
    if reply.get("exitCode") != 0:
        return [f"код завершился с {reply.get('exitCode')}: {str(reply.get('stderr'))[-400:]}"]
    probe = json.loads(str(reply.get("stdout") or "{}"))
    problems += [f"импорт {m}: {s}" for m, s in probe["imports"].items() if s != "ok"]
    if probe["socket"] == "connected":
        problems.append("сокет открылся — комната не запечатана")
    leaked = [k for k in probe["env"] if k.startswith(SERVICE_ENV_PREFIXES)]
    if leaked:
        problems.append(f"в окружении комнаты ключи службы: {leaked}")
    print(f"импорты: {probe['imports']}")
    print(f"сокет: {probe['socket']}; ключи окружения комнаты: {probe['env']}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://workstation:8080")
    args = parser.parse_args()
    secret = os.environ.get("AIFA_SYM_WORKSTATION_CREATE_SECRET", "")
    if not secret:
        print("FAIL нет AIFA_SYM_WORKSTATION_CREATE_SECRET в окружении контейнера")
        return 1
    problems = verdict(room(args.base_url, secret))
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        print("OK C5.2: комната открыта, пакеты импортируются, сокета нет, окружения службы нет")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
