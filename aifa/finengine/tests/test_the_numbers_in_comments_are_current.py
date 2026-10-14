"""Числа, записанные в комментариях, стареют молча — здесь они не молчат.

ЦЕНА, КУПЛЕННАЯ ДВАЖДЫ. В `two_stage_extractor` количество показателей двойной
природы было записано словами «27 на десять ячеек» в ДВУХ местах — докстринге
`_dual_nature_enums` и комментарии у места вызова, двадцатью экранами ниже.
19.08 их стало 33, 20.08 — 35, и коммит, чьей ЕДИНСТВЕННОЙ целью было снять
устаревшее число, снял его в одном месте из двух. Второе нашла состязательная
проверка. Третий экземпляр той же величины («шесть показателей, семь ячеек из
восьми») жил в `calc.py` и врал с тех пор, как ячеек стало десять.

Поэтому число теперь ОДНО на файл и сверяется с прогоном. Ошибиться в нём
по-прежнему можно — нельзя ошибиться молча.

ЧТО ЗДЕСЬ НЕ СТЕРЕЖЁТСЯ. Состав пар (какие именно показатели) держит
`tests/test_registry_no_silent_duplicates.py` — этот файл только про то, что
цифра в тексте совпадает с цифрой в реестре.

ПРОВЕРКА ОТКАТОМ — мутации, прогнанные 20.08.2026:
* вернуть «27» в докстринг `_dual_nature_enums` — красен
  `test_the_dual_count_in_the_docstring_matches_the_registry` («в тексте 27,
  в реестре 35»);
* написать в `calc.py` «34 пары» — красен
  `test_the_dual_count_in_calc_matches_the_registry`.

ЧЕГО ЭТОТ ФАЙЛ НЕ ЛОВИТ (ревизия 21.08.2026, решение «О»). Сверяется РОВНО
одна величина в двух заранее названных местах. Всё остальное в этих же двух
комментариях — состав пар, ячейки, выводы — и любое число в любом другом
комментарии проекта стареют так же молча, как старело «27». Оставлен как
паритет двух мест: число в прозе против числа из прогона реестра; исполнить
«комментарий не врёт» иначе нечем.
"""
from __future__ import annotations

import re
from pathlib import Path

from finengine.skills.metric_schema import PROFILE_IDS, registry_for_profile
from finengine.skills.two_stage_extractor import _dual_nature_enums

SRC = Path(__file__).resolve().parents[1] / "src" / "finengine" / "skills"


def _dual_pairs_in_registry() -> int:
    """Пар «ячейка + показатель», где показатель и извлекаемый, и расчётный."""
    total = 0
    for cell in PROFILE_IDS:
        registry = registry_for_profile(cell)
        extractable = [{"enum": m["enum"]} for m in registry.get("extractable", [])]
        total += len(_dual_nature_enums(extractable, registry.get("calculated", [])))
    return total


def test_the_dual_count_in_the_docstring_matches_the_registry():
    """Число в докстринге `_dual_nature_enums` снято с реестра, а не с памяти."""
    doc = _dual_nature_enums.__doc__ or ""
    written = re.search(r"Таких пар (\d+)", doc)
    assert written, (
        "в докстринге `_dual_nature_enums` больше нет фразы «Таких пар N» — "
        "либо верните её, либо снимите этот сторож вместе с ней"
    )
    measured = _dual_pairs_in_registry()
    assert int(written.group(1)) == measured, (
        f"в докстринге `_dual_nature_enums` написано {written.group(1)} пар "
        f"двойной природы, а в реестре их {measured}. Число в комментарии "
        f"устарело — так уже было с «27»."
    )


def test_the_dual_count_in_calc_matches_the_registry():
    """Третий экземпляр той же величины — в `calc.py`, у предупреждения #163."""
    text = (SRC / "calc.py").read_text(encoding="utf-8")
    written = re.search(r"(\d+) пар\w* «ячейка \+", text)
    assert written, (
        "в `calc.py` больше нет фразы «N пар «ячейка + показатель»» — "
        "либо верните её, либо снимите этот сторож вместе с ней"
    )
    measured = _dual_pairs_in_registry()
    assert int(written.group(1)) == measured, (
        f"в `calc.py` написано {written.group(1)} пар двойной природы, а в "
        f"реестре их {measured}."
    )
