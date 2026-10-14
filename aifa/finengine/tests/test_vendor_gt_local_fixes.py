"""Наши правки чужого эталона переживают пересинхронизацию (#254).

ЗАЧЕМ ЭТОТ ФАЙЛ. `benchmarks/vendor/` — вендоренная копия эталона Finengine, и
правило проекта: не править, источник истины у Алексея. 16.08.2026 владелец
решил иначе — поправить у себя и попросить разметчиков перепроверить. Решение
его, но у правки есть тихая смерть: `benchmarks/sync_vendor.sh` копирует
разметку через `rsync -a --delete`, то есть СОТРЁТ её без единого слова.

Этот файл — единственное, что стоит между правкой и её молчаливой пропажей.
Покраснел — значит вендор пересинхронизировали, а правку не перенесли.

ЧТО ИМЕННО ПОПРАВЛЕНО. `ifrs_nf_en/gt/nestle-2024.csv`, две строки таблицы 21
(географическая разбивка выручки) шли с ПУСТЫМ именем сегмента. Пустое имя в
ключе скорера означает «итог компании», поэтому они сталкивались с настоящим
итогом (91 354 и 92 998) и `benchmark_v2` отказывался считать документ ЦЕЛИКОМ:

    ValueError: Reference dataset contains conflicting duplicate keys:
    IFRS_NF_EN|nestle-2024.pdf|SALES_REVENUE|FY 2024|

Что это зона AMS (Америки) — не догадка, а арифметика по той же таблице:
    FY2024: United States 29 682 + Canada 2 441 = 32 123
    FY2023: United States 30 034 + Canada 2 519 = 32 553

⚠️ ФАЙЛ БЫЛ ОДИНАКОВ ВО ВСЕХ ВЕТКАХ Finengine (main, develop, grooming,
multidoc-dev) — md5 совпадал. То есть это не расхождение веток, а дефект самой
разметки, и перепроверить его должны разметчики (задача #254).

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в этих двух строках пустое имя сегмента —
`test_the_americas_zone_is_named` обязан покраснеть. Проделано 21.08.2026 на
КОПИИ файла (сам `benchmarks/vendor/**` править нельзя): стёртое имя зоны даёт
2 безымянные строки и 2 конфликтующих ключа — краснеют первый и третий тесты.

ЧЕГО НЕ ЛОВИТ (ревизия 21.08.2026, решение «О»). Сторож стоит РОВНО у одной
нашей правки в одном файле из 112. `sync_vendor.sh` делает `rsync -a --delete`
по всему каталогу — любая следующая правка чужого эталона умрёт так же тихо, и
этот файл о ней не узнает. Он также ничего не говорит о ПРАВИЛЬНОСТИ разметки:
что зона названа «AMS», доказано арифметикой США+Канада, а не источником, —
перепроверить обязаны разметчики (#254).
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

GT = (Path(__file__).resolve().parents[1] / "benchmarks" / "vendor" / "tests" /
      "test_benchmark" / "data" / "golden" / "ifrs_nf_en" / "gt" / "nestle-2024.csv")


def _rows() -> list[dict]:
    raw = GT.read_text(encoding="utf-8-sig")
    head = raw.split("\n", 1)[0]
    delim = ";" if head.count(";") > head.count(",") else ","
    return list(csv.DictReader(raw.splitlines(), delimiter=delim))


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    if not GT.is_file():
        pytest.skip(f"вендоренного эталона нет: {GT}")
    return _rows()


def test_the_americas_zone_is_named(rows):
    """ГЛАВНОЕ: у строк зоны AMS есть имя, иначе документ не посчитается."""
    nameless = [r for r in rows
                if r["table"] == "21" and r["segment_type"] == "required"
                and not r["segment_name"]]
    assert not nameless, (
        f"в таблице 21 снова {len(nameless)} строк без имени сегмента — "
        "скорер откажется считать nestle-2024 целиком. Похоже, вендор "
        "пересинхронизировали (sync_vendor.sh делает rsync --delete), а правку "
        "не перенесли. См. #254")
    ams = {(r["period"], r["value"]) for r in rows if r["segment_name"] == "AMS"}
    assert ams == {("FY 2024", "32123.0"), ("FY 2023", "32553.0")}, (
        f"строки AMS выглядят не так, как мы их правили: {sorted(ams)}")


def test_the_zone_still_equals_its_countries(rows):
    """Арифметика, которой доказано имя, обязана сходиться и после правок.

    Если разметчики поправят значения стран или зоны, а имя оставят — тест
    покажет, что основание для имени «AMS» рассыпалось, и его надо перепроверять
    заново, а не наследовать.
    """
    def val(segment: str, period: str) -> float:
        hit = [r for r in rows
               if r["table"] == "21" and r["segment_name"] == segment
               and r["period"] == period and r["enum"] == "SALES_REVENUE"]
        assert len(hit) == 1, f"{segment} {period}: строк {len(hit)}, ждали одну"
        return float(hit[0]["value"])

    for period in ("FY 2024", "FY 2023"):
        assert val("United States", period) + val("Canada", period) == val("AMS", period), (
            f"{period}: США + Канада больше не дают AMS — основание для имени "
            "зоны рассыпалось, перепроверить разметку")


def test_the_document_has_no_conflicting_keys_left(rows):
    """Больше ни одна пара строк не спорит о значении под одним ключом.

    Проверяем ключом скорера (документ, показатель, период, сегмент): именно на
    нём он и падал.
    """
    seen: dict[tuple, set] = {}
    for r in rows:
        key = (r["doc_name"], r["enum"], r["period"], r["segment_name"] or "")
        seen.setdefault(key, set()).add(r["value"])
    conflicts = {k: v for k, v in seen.items() if len(v) > 1}
    assert not conflicts, f"снова конфликтующие ключи: {sorted(conflicts)[:3]}"
