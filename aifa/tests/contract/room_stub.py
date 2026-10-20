"""Комната станции для контрактных тестов без Workstation: та же точка входа, локальный
интерпретатор.

Только тестовая подмена `sealed_room.run_code` (PRINCIPLES.md §Б.5: запасного локального исполнения
кода атома
в продукте нет). Файлы из `out/` не собираются — атомам контрактных тестов они не нужны.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from finengine.skills.spec import sealed_room


def local_run_code(
    code: str, inputs: dict[str, Any], *, timeout: int = sealed_room.TIMEOUT_SECONDS, **_: Any
) -> sealed_room.CodeRun:
    entry = Path(sealed_room.__file__).with_name("_room_entry.py").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as workdir:
        done = subprocess.run(
            [sys.executable, "-X", "utf8", "-I", "-c", entry],
            input=json.dumps({"code": code, "inputs": inputs}, ensure_ascii=False).encode(),
            capture_output=True,
            timeout=30,
            check=False,
            cwd=workdir,
        )
    reply = {
        "exitCode": done.returncode,
        "stdout": done.stdout.decode("utf-8"),
        "stderr": done.stderr.decode("utf-8"),
        "timedOut": False,
    }
    return sealed_room._read_reply(reply, timeout)


def use_local_room(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sealed_room, "run_code", local_run_code)
