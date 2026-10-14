"""Путь станции не тянет пакетов, которых в запечатанной комнате нет.

🔴 КУПЛЕНО ПРОГОНОМ БСПБ 26.08.2026. В комнату едет ПОЛКА — исходники пакета
`finengine` и тела навыков. Сторонних библиотек там нет: комната запечатана,
сети наружу у неё тоже нет, доставить их некому.

Хранилища перевели на SQLAlchemy, и в `finengine/store/__init__.py` появились
реэкспорты `dialogs` и `runs`. После этого ОДНОЙ строки в пути извлечения
(`from finengine.store import blobs`) хватило, чтобы питон исполнил пакетный
`__init__` целиком:

    extract_result → skills.sources → finengine.store → store.runs → sqlalchemy

Извлечение падало с `ModuleNotFoundError: No module named 'sqlalchemy'` — уже
ПОСЛЕ распаковки полки и доставки 515 КБ текста, то есть потратив минуты
работы. Модель диагностировала верно и предложила поставить пакет в песочницу;
правильный ответ обратный — не тащить.

⚠️ ЧТО ПРОВЕРЯЕТСЯ. Не список запрещённых имён по памяти, а ФАКТ: после импорта
модулей, которые исполняет комната, названные пакеты не оказались в
`sys.modules`. Ленивый импорт внутри функции (как у `redis` в `store/cache` и
`boto3` в `store/blobs`) законен — он не срабатывает, пока ветку не позвали.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Модули, которые исполняет комната: то, что импортируют скрипты навыков.
ВХОДЫ = [
    "finengine.skills.station_guard",
    "finengine.skills.extract_text",
    "finengine.skills.extract_result",
    "finengine.skills.extract_present",
    "finengine.skills.metric_schema",
    "finengine.skills.requested_metrics",
    "finengine.skills.versioning",
    "finengine.skills.ratios",
    "finengine.skills.calc",
    "finengine.skills.extractors._classifier",
    "finengine.skills.extraction.tables",
    "finengine.roles.scope",
    "finengine.roles.access",
]

# Пакетов нет в комнате: полка возит только исходники finengine.
НЕТ_В_КОМНАТЕ = ("sqlalchemy", "psycopg", "boto3", "botocore", "redis", "fastapi", "uvicorn")


def test_the_entry_points_exist():
    """Сторож без предмета — не сторож: опечатка в имени молча зеленела бы."""
    for имя in ВХОДЫ:
        assert importlib.util.find_spec(имя) is not None, f"нет такого модуля: {имя}"


@pytest.mark.parametrize("модуль", ВХОДЫ)
def test_importing_it_pulls_nothing_the_room_lacks(модуль):
    """🔴 ГЛАВНОЕ. Мутация: вернуть реэкспорты в `store/__init__.py` — краснеет
    `extract_result` (через `skills.sources`).

    Проверка идёт В ОТДЕЛЬНОМ ПРОЦЕССЕ: в текущем `sqlalchemy` уже импортирован
    самим набором тестов, и `sys.modules` соврал бы про любой модуль.
    """
    корень = Path(__file__).resolve().parents[1] / "src"
    код = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(корень)!r})
        import {модуль}  # noqa: F401
        лишние = [и for и in {НЕТ_В_КОМНАТЕ!r} if и in sys.modules]
        print(",".join(лишние))
    """)
    итог = subprocess.run([sys.executable, "-c", код], capture_output=True, text=True)
    assert итог.returncode == 0, f"{модуль} не импортируется вовсе:\n{итог.stderr[-800:]}"

    лишние = [и for и in итог.stdout.strip().split(",") if и]
    assert not лишние, (
        f"{модуль} тянет в комнату пакеты, которых там нет: {лишние}. "
        "Полка возит только исходники finengine — в запечатанной комнате это "
        "ModuleNotFoundError уже ПОСЛЕ доставки текста, то есть потраченные "
        "минуты работы. Импортируй подмодуль явно или сделай импорт ленивым.")
