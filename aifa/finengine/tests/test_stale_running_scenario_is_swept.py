"""Зависший «running» не переживает рестарт сервиса (18.08.2026).

ЖИВОЙ СЛУЧАЙ С ПРОДА. Сценарий ir_calendar (id accc83b6…) висел
last_status='running' с 17.08 16:55 UTC больше 4,5 часов — владелец видел в
админке вечное «выполняется». Первопричина: около 17:00 пересборка
backend-контейнера при деплое убила процесс посреди прогона; финализация
статуса в runner.py (ok/error в конце run_scenario) не выполнилась, и
'running' остался навсегда.

Прогоны сценариев живут в ТОМ ЖЕ процессе, что и API (планировщик — фоновый
asyncio-таск из startup, ручной запуск — asyncio.create_task в
run_orchestrator.start_run), поэтому после рестарта любой 'running' — труп.
Лекарство — store.sweep_stale_running() на старте: 'running' → 'error' с
текстом «прерван перезапуском сервиса». Статус именно 'error', не 'failed':
фронт знает только "" | "ok" | "error" | "running" (web/src/lib/scenarios.ts).

Отрицательный контроль проверен: с выключенным UPDATE в sweep_stale_running()
оба содержательных теста краснеют.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from finengine.skills.scenarios import store

# Файловых запасных путей у новостей, сценариев и реестра документов больше нет
# (снесены 26.08.2026) — файлу нужна настоящая база.
pytestmark = pytest.mark.usefixtures("бд_хранилищ")



@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "db_path", lambda: tmp_path / "scenarios.db")
    store.init_schema()


def test_stale_running_becomes_error_with_reason(tmp_db):
    scn = store.create(user_id=1, name="IR Календарь", kind="ir_calendar")
    store.set_status(scn.id, last_status="running", last_run_at=time.time())

    swept = store.sweep_stale_running()

    assert swept == 1
    fresh = store.get(scn.id)
    assert fresh.last_status == "error", "зависший 'running' обязан стать 'error'"
    assert fresh.last_error == "прерван перезапуском сервиса"


def test_finished_scenario_is_untouched(tmp_db):
    scn = store.create(user_id=1, name="IR Daily", kind="ir_daily")
    store.set_status(
        scn.id, last_status="ok", last_run_at=time.time(), last_run_id="run123"
    )

    swept = store.sweep_stale_running()

    assert swept == 0
    fresh = store.get(scn.id)
    assert fresh.last_status == "ok"
    assert fresh.last_error == ""
    assert fresh.last_run_id == "run123"


def test_startup_actually_calls_the_sweep():
    """Недоставка — главная болезнь: функция без вызова на старте не лечит
    ничего. Текст startup-хука читаем файлом, не импортом — импорт
    finengine.api.main поднимает всё приложение."""
    main_py = Path(__file__).resolve().parent.parent / "src/finengine/api/main.py"
    assert "sweep_stale_running()" in main_py.read_text(encoding="utf-8")
