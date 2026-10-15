"""Мост к модели ставится и без лаунчера (17.08.2026, прогон ВТБ владельца).

ЧТО СЛУЧИЛОСЬ. Модель запустила `extract.py` НАПРЯМУЮ, минуя `run.sh`, — а
именно лаунчер выставляет `FE_RPC_DIR`. Мост ставился только по этой
переменной, прогресс же писал в тот же каталог ПО УМОЛЧАНИЮ. Вышло худшее из
сочетаний: ход выглядел живым (события шли на экран), а вызовы модели уходили
в HTTP, упирались в запечатанную сеть и возвращали пустой батч. Пользователь
получил таблицу из 110 прочерков и объяснение «нет доступа к модели через
bridge», сочинённое самой моделью.

Признаком станции сделан САМ КАТАЛОГ моста: его создаёт комната, вне станции
его нет. Обход лаунчера больше не обезоруживает движок.

⚠️ ПОДМЕНА ЗДЕСЬ ГЛОБАЛЬНАЯ — ЧЕТЫРЕ ФУНКЦИИ МОДУЛЯ. Первая версия теста
возвращала на место только `complete`, и соседние тесты про размышления падали
ЧЕРЕЗ РАЗ: порядок тестов случайный, и утечка проявлялась не всегда. Поэтому
восстановление отдано `monkeypatch` — он вернёт всё сам, чем бы тест ни
кончился.
"""
from __future__ import annotations

from finengine.llm import openrouter as _or
from finengine.llm import station_bridge

_PATCHED = ("complete", "complete_with_usage", "stream", "set_reasoning_effort")


def _arm(monkeypatch) -> dict:
    """Взять текущие функции под опеку monkeypatch и вернуть их для сравнения."""
    saved = {n: getattr(_or, n) for n in _PATCHED}
    for n, fn in saved.items():
        monkeypatch.setattr(_or, n, fn)
    return saved


def test_installs_when_only_the_directory_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("FE_RPC_DIR", raising=False)
    monkeypatch.setattr(station_bridge, "_rpc_dir", lambda: str(tmp_path))
    saved = _arm(monkeypatch)

    station_bridge.install_if_sealed()

    assert _or.complete is not saved["complete"], (
        "каталог моста есть, а движок остался на HTTP — это и был дефект ВТБ")


def test_stays_away_outside_the_station(tmp_path, monkeypatch):
    """Отрицательный контроль: на машине разработчика подмены быть не должно —
    иначе локальные замеры ушли бы в несуществующий мост."""
    monkeypatch.delenv("FE_RPC_DIR", raising=False)
    monkeypatch.setattr(station_bridge, "_rpc_dir", lambda: str(tmp_path / "нет-такого"))
    saved = _arm(monkeypatch)

    station_bridge.install_if_sealed()

    assert _or.complete is saved["complete"]
