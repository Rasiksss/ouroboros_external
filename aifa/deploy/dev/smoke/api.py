"""Дымовые проверки службы и фронтенда aifa-sym (шаг 12): запускаются на dev-хосте через `run.sh`.

Ходят на loopback-порт фронтенда (туннель к нему — тот же вход, что у пользователей), поэтому одна
проверка охватывает и фронтенд, и его перезапись `/api/*` в службу (P31). Клиент и разбор потока —
те же, что у опорной записи (`aifa/tests/golden`): второго клиента не заводим.

    api.py health   --base http://127.0.0.1:13000
    api.py build-id --base http://127.0.0.1:13000 --expect <коммит>
    api.py login    --base ... --user qa-a            # пароль — AIFA_SYM_QA_PASSWORD
    api.py hello    --base ... --user qa-a --prompt привет

Код возврата: 0 — проверка прошла, 1 — не прошла (причина в stdout).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

GOLDEN = Path(__file__).resolve().parents[3] / "tests" / "golden"
sys.path.insert(0, str(GOLDEN))

from golden_client import ApiSession, ClientError, ClientSettings  # noqa: E402
from golden_record import Scenario, record_scenario  # noqa: E402

TIMEOUT_S = 15


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as reply:  # noqa: S310 — loopback
            return reply.status, reply.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, str(exc)


def _health(base: str) -> bool:
    status, body = _get(f"{base}/api/health")
    print(f"службе через фронтенд: HTTP {status} {body[:120]}")
    return status == 200


def _build_id(base: str, expect: str) -> bool:
    status, body = _get(f"{base}/build-id")
    print(f"/build-id: HTTP {status} {body[:120]}")
    if status != 200:
        return False
    build = json.loads(body).get("build", "")
    print(f"сборка фронтенда {build!r}, ожидалась {expect!r}")
    return build == expect


def _settings(base: str, user: str) -> ClientSettings:
    password = os.environ.get("AIFA_SYM_QA_PASSWORD", "")
    if not password:
        raise ClientError("не задан AIFA_SYM_QA_PASSWORD (файл с паролями учёток, права 600)")
    return ClientSettings(base_url=f"{base}/api", username=user, password=password)


def _login(base: str, user: str) -> bool:
    session = ApiSession(_settings(base, user))
    session.login()
    me = session.request("GET", "/auth/me")
    print(f"вход {user}: HTTP {me.status}")
    return me.status == 200 and me.json().get("username") == user


def _hello(base: str, user: str, prompt: str) -> bool:
    scenario = Scenario(f"smoke-{uuid.uuid4().hex[:6]}", (), (prompt,), web_search=False)
    recording = record_scenario(scenario, _settings(base, user))
    turn = recording.turns[0]
    answer = turn.answer.strip()
    print(f"событий {len(turn.events)}, ответ: {answer[:200]!r}")
    last = turn.events[-1] if turn.events else {}
    return last.get("event_type") == "completed" and bool(answer)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("health", "build-id", "login", "hello"))
    parser.add_argument("--base", default="http://127.0.0.1:13000")
    parser.add_argument("--expect", default="")
    parser.add_argument("--user", default="qa-a")
    parser.add_argument("--prompt", default="привет")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    try:
        if args.command == "health":
            ok = _health(base)
        elif args.command == "build-id":
            ok = _build_id(base, args.expect)
        elif args.command == "login":
            ok = _login(base, args.user)
        else:
            ok = _hello(base, args.user, args.prompt)
    except ClientError as exc:
        print(f"отказ: {exc}")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
