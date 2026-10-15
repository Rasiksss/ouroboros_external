"""Полка везёт методику, а не только скрипты (09.08.2026).

ЗАЧЕМ ЭТОТ ФАЙЛ. С 09.08 база методики — файлы
`station-skills/aifa-analyze/method/**.md`, и читает их КОД СТАНЦИИ. Значит
полка (наш код, который гейтвей кладёт в комнату станции) обязана везти не
только `scripts/analyze.py`, но и весь каталог `method/`. Забудь мы про него —
получили бы ровно ту болезнь, которой уже пять случаев за 05.08: сделанное не
доезжает до места работы.

С 15.08 то же самое верно для СПРАВОЧНИКА ПОКАЗАТЕЛЕЙ
(`station-skills/aifa-extract/method/catalogue/<ячейка>/<ENUM>.md`, #250): состав
показателей перестал быть константой в коде и стал файлами навыка. Недоехавшая
запись здесь выглядит не как отказ, а как молча не извлечённый показатель —
и списывается на качество модели. Сегодня оба сборщика копируют `station-skills`
целиком и справочник доезжает; сторож нужен, чтобы это не сломала первая же
попытка сузить копирование до `scripts/`.

Полку собирают ДВА разных места, и это само по себе риск «двух мест»:
  • `station_driver._build_shelf()` — боевой путь, каждый прогон;
  • `deploy/skill-hub/pack.sh`      — выкатка, кладёт shelf.zip на гейтвей.
Проверяются оба.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать `aifa_station_skills` из `_shelf_roots()` (или
заменить в pack.sh `cp -R station-skills` на копирование одних scripts/) —
соответствующий тест обязан покраснеть.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from finengine.skills import station_driver

ROOT = Path(__file__).resolve().parents[1]
METHOD_DIR = ROOT / "station-skills" / "aifa-analyze" / "method"
CATALOGUE_DIR = ROOT / "station-skills" / "aifa-extract" / "method" / "catalogue"

# Лаунчер станции отказывается работать без этого файла — см.
# tests/test_station_launcher_is_loud.py и station-skills/aifa-analyze/run.sh.
REQUIRED = {
    "aifa_station_skills/aifa-analyze/scripts/analyze.py",
    "aifa_station_skills/aifa-extract/scripts/extract.py",
    "aifa_station_skills/aifa-analyze/method/_order.md",
    # Обёртки, которые запускает МОДЕЛЬ по телу навыка. Недоехавшая обёртка —
    # это не отказ, а тихая потеря того, что она доставляет: у разбора это
    # личная методика пользователя (#123).
    "aifa_station_skills/aifa-analyze/scripts/analyze_run.sh",
    "aifa_station_skills/aifa-extract/scripts/extract_run.sh",
}


def _method_arcnames() -> set[str]:
    """Все файлы методики в репозитории — в терминах имён внутри полки."""
    return {
        f"aifa_station_skills/aifa-analyze/method/{p.relative_to(METHOD_DIR)}"
        for p in METHOD_DIR.rglob("*") if p.is_file()
    }


def _catalogue_arcnames() -> set[str]:
    """Все записи справочника показателей — в терминах имён внутри полки."""
    return {
        f"aifa_station_skills/aifa-extract/method/catalogue/{p.relative_to(CATALOGUE_DIR)}"
        for p in CATALOGUE_DIR.rglob("*") if p.is_file()
    }


def _must_be_on_the_shelf() -> set[str]:
    return REQUIRED | _method_arcnames() | _catalogue_arcnames()


def test_repository_actually_has_a_methodology():
    """Пустой каталог сделал бы все проверки ниже бессмысленно зелёными."""
    names = _method_arcnames()
    assert len(names) >= 5, f"в {METHOD_DIR} нашлось {len(names)} файлов — тест устарел"


def test_repository_actually_has_a_catalogue():
    """Тот же отрицательный контроль для справочника показателей (#250).

    Считаются ЗАПИСИ, а не файлы: с 15.08 ячейка — один файл, и проверка «файлов
    больше N» на нём стала бы вечно зелёной при любом содержимом.
    """
    cells = [p for p in CATALOGUE_DIR.glob("*.md") if not p.name.startswith("_")]
    assert cells, f"в {CATALOGUE_DIR} нет ни одного файла ячейки"
    for p in cells:
        records = p.read_text(encoding="utf-8").count("\n## ")
        assert records >= 40, (
            f"{p.name}: записей {records} — справочник ячейки это десятки "
            "показателей, значит проверка доставки ниже ничего не доказывает")


def test_runtime_shelf_carries_every_methodology_file(monkeypatch):
    """Боевой путь: то, что уезжает в станцию каждым прогоном."""
    monkeypatch.setattr(station_driver, "_shelf_cache", None, raising=False)
    with zipfile.ZipFile(io.BytesIO(station_driver._build_shelf())) as z:
        names = set(z.namelist())

    missing = _must_be_on_the_shelf() - names
    assert not missing, (
        "полка уезжает в станцию без этих файлов — анализ откажется стартовать, "
        "а недоехавшая запись справочника молча снимет показатель с извлечения:\n  "
        + "\n  ".join(sorted(missing))
    )


@pytest.mark.skipif(shutil.which("zip") is None, reason="нет утилиты zip")
def test_deploy_shelf_carries_every_methodology_file(tmp_path):
    """Путь выкатки: shelf.zip, который ship.sh кладёт на гейтвей."""
    out = tmp_path / "shelf.zip"
    r = subprocess.run(["bash", str(ROOT / "deploy" / "skill-hub" / "pack.sh"), str(out)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f"pack.sh упал:\n{r.stdout}\n{r.stderr}"

    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())

    missing = _must_be_on_the_shelf() - names
    assert not missing, (
        "pack.sh собирает полку без этих файлов:\n  " + "\n  ".join(sorted(missing))
    )
