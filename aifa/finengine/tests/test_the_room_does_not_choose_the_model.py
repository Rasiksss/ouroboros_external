"""Комната не выбирает модель — её знает демон моста (FE_RPC_DEFAULT_MODEL).

ЗАЧЕМ. 07.09.2026 стенд считался переведённым на DeepSeek через Cloud.ru:
FE_RPC_DEFAULT_MODEL станции и FINENGINE_OPENROUTER_MODEL бэкенда стояли верно,
а все вызовы извлечения шли на `qwen/qwen3.6-plus` через OpenRouter (лейн
`dev|qwen/qwen3.6-plus` в журнале infproxy). Ключ OpenRouter кончился в 12:13
(402), с 12:22 — 403, и каждый прогон падал на стадии сжатия. Причина — не
пины (их сняли тем же утром), а невидимость настройки в запечатанной комнате:
лаунчер экспортирует только нонс и FE_RPC_DIR, `settings.openrouter_model`
там равен умолчанию config.py, и экстрактор честно клал его в запрос как
«модель». Демон подставляет свой дефолт только на None.

ПРОВЕРКА ОТКАТОМ (исполнена 07.09): `_model_for_request` → `return model` —
первый тест красный.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest

from finengine.config import settings
from finengine.llm import station_bridge


def _fake_daemon(rpc_dir: Path, seen: list, stop: threading.Event) -> None:
    """Демон моста в одну строку: читает *.req.json, запоминает model, отвечает."""
    while not stop.is_set():
        for req in list(rpc_dir.glob("*.req.json")):
            body = json.loads(req.read_text(encoding="utf-8"))
            seen.append(body.get("model"))
            resp = rpc_dir / f"{body['id']}.resp.json"
            tmp = rpc_dir / f"{body['id']}.resp.tmp"
            tmp.write_text(json.dumps({
                "text": "ok",
                "usage": {"in": 1, "out": 1, "cached": 0,
                          "model": body.get("model") or "daemon-default"},
            }), encoding="utf-8")
            os.replace(tmp, resp)
            req.unlink()
        time.sleep(0.02)


@pytest.fixture
def room(tmp_path, monkeypatch):
    monkeypatch.setenv("FE_RPC_DIR", str(tmp_path))
    monkeypatch.setenv("FE_RPC_POLL_SEC", "0.02")
    monkeypatch.setenv("FE_RPC_TIMEOUT_SEC", "5")
    monkeypatch.delenv("FINENGINE_OPENROUTER_MODEL", raising=False)
    seen: list = []
    stop = threading.Event()
    t = threading.Thread(target=_fake_daemon, args=(tmp_path, seen, stop), daemon=True)
    t.start()
    yield seen
    stop.set()
    t.join(1)


def test_the_settings_default_is_not_a_choice(room):
    """Умолчание config.py в комнате означает «не настроено» — демону уходит None."""
    text, usage = asyncio.run(
        station_bridge.complete_with_usage("ping", model=settings.openrouter_model))
    assert text == "ok" and usage["model"] == "daemon-default"
    assert room == [None], f"комната навязала демону модель: {room}"


def test_an_explicit_room_setting_is_honoured(room, monkeypatch):
    """Задано в окружении комнаты явно — имя едет как есть."""
    monkeypatch.setenv("FINENGINE_OPENROUTER_MODEL", "some/explicit-model")
    asyncio.run(station_bridge.complete_with_usage("ping", model="some/explicit-model"))
    assert room == ["some/explicit-model"], room


def test_a_deliberate_other_model_is_kept(room):
    """Вызывающий выбрал НЕ умолчание — это выбор, а не эхо настройки."""
    asyncio.run(station_bridge.complete_with_usage("ping", model="other/model"))
    assert room == ["other/model"], room
