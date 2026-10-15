"""Расхождение нашего реестра с реестром Finengine — задекларировано, а не забыто.

ЗАЧЕМ. Реестр ячеек пришёл от команды Finengine, и мы его правим. Пока
расхождение живёт только в диффе, оно невидимо: при следующей синхронизации
вендорского снимка своё либо затрётся «наведением порядка», либо останется, но
никто не вспомнит почему. Бенчмарк тоже слепнет — разница по составу читается
как ошибка извлечения.

Здесь сверяется ТРИ вещи, и каждая ловит свой способ разъехаться молча:
  1. всё, что мы объявили своим, действительно есть у нас и действительно
     отсутствует у них (иначе список — художественный вымысел);
  2. всё, что есть у нас и нет у них, объявлено (иначе новое расхождение
     проедет мимо);
  3. всё, что есть у них и нет у нас, объявлено (иначе мы не узнаем, что
     отстали, — а отстать проще всего).

Вендорский снимок при этом НЕ правится: он якорь сопоставимости с апстримом.

⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ЛОВИТ (дописано ревизией 21.08.2026). Он про
БУХГАЛТЕРИЮ расхождения, а не про качество извлечения. Он сверяет три
списка имён между собой и НЕ запускает ни движок, ни расчёт: показатель,
заведённый, объявленный и при этом извлекаемый неверно, — для него в
полном порядке. Формулу он сравнивает как строку: `A / B * 100` и
`(A * 100) / B` для него разные, а `A/B` с перепутанными местами A и B —
«просто расхождение», о котором сказано словами в
`registry_divergence.FORMULAS`, а не проверено счётом. Единственное
исключение — `test_shareholder_roe_inputs_come_as_a_pair`: там проверяется
смысл, а не бухгалтерия.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from finengine.skills.registry_divergence import FORMULAS, OURS, THEIRS
from finengine.skills.metric_schema import PROFILE_IDS
from finengine.skills.registry_composition import RegistrySelection, compile_registry

REPO = Path(__file__).resolve().parents[1]
VENDOR_DIR = REPO / "benchmarks" / "vendor" / "src" / "finengine" / "data" / "registry"


def _ours(profile_id: str) -> dict:
    return compile_registry(
        RegistrySelection.from_profile_id(profile_id)
    ).to_effective_data()


def _data(source: dict | Path) -> dict:
    return source if isinstance(source, dict) else json.loads(source.read_text(encoding="utf-8"))


def _enums(source: dict | Path) -> set[str]:
    d = _data(source)
    return {
        str(m.get("enum") or "").strip()
        for key in ("extractable", "calculated")
        for m in d.get(key, [])
    } - {""}


def _formula(source: dict | Path, enum: str) -> str:
    d = _data(source)
    for m in d.get("calculated", []):
        if m.get("enum") == enum:
            return " ".join(str(m.get("formula") or "").split())
    return ""


def _pairs() -> list[tuple[str, dict, Path]]:
    """Ячейки, которые есть и у нас, и в снимке. Ячейку, которой у них нет,
    сравнивать не с чем — она целиком наша и в сверку не идёт."""
    return [
        (profile_id, _ours(profile_id), VENDOR_DIR / f"{profile_id}.json")
        for profile_id in PROFILE_IDS
        if (VENDOR_DIR / f"{profile_id}.json").is_file()
    ]


def test_vendor_snapshot_is_present():
    """Без снимка все проверки ниже стали бы зелёными ни о чём."""
    assert _pairs(), "нет вендорского снимка реестра — сверять не с чем (git lfs pull?)"


def test_everything_we_declared_as_ours_really_is_ours():
    """Расхождение считается ПО ЯЧЕЙКАМ, а не по объединению.

    Иначе список врёт в обе стороны: ROA у Finengine есть в нефинансовых
    ячейках, а мы завели его в банковских — по объединению «он у них есть», по
    факту в банковской ячейке его нет и анализ банка без него считал в уме.
    Объявление оправдано, если найдётся ХОТЯ БЫ ОДНА ячейка, где показатель у
    нас есть, а у них нет.
    """
    justified: set[str] = set()
    ours_all: set[str] = set()
    for _profile_id, mine, vend in _pairs():
        ours_all |= _enums(mine)
        justified |= _enums(mine) - _enums(vend)
    for enum in OURS:
        assert enum in ours_all, f"«{enum}» объявлен нашим, но в наших ячейках его нет"
        assert enum in justified, (
            f"«{enum}» объявлен нашим, но НИ В ОДНОЙ ячейке расхождения нет — "
            f"строку надо убрать")


def test_everything_we_added_is_declared():
    """Новый показатель, заведённый мимо списка, — молчаливый разъезд."""
    undeclared: dict[str, list[str]] = {}
    for profile_id, mine, vend in _pairs():
        for enum in sorted(_enums(mine) - _enums(vend)):
            if enum not in OURS:
                undeclared.setdefault(enum, []).append(profile_id)
    assert not undeclared, (
        "показатели есть у нас и нет у Finengine, но не объявлены в "
        f"registry_divergence.OURS: {undeclared}")


def test_everything_we_lack_is_declared():
    """Отстать проще всего: апстрим добавил, мы не заметили."""
    undeclared: dict[str, list[str]] = {}
    for profile_id, mine, vend in _pairs():
        for enum in sorted(_enums(vend) - _enums(mine)):
            if enum not in THEIRS:
                undeclared.setdefault(enum, []).append(profile_id)
    assert not undeclared, (
        "показатели есть у Finengine и нет у нас, но не объявлены в "
        f"registry_divergence.THEIRS: {undeclared}")


def test_declared_lacks_are_really_absent():
    ours_all: set[str] = set()
    for _profile_id, mine, _vend in _pairs():
        ours_all |= _enums(mine)
    for enum in THEIRS:
        assert enum not in ours_all, (
            f"«{enum}» объявлен отсутствующим, но он у нас ЕСТЬ — строку надо убрать")


@pytest.mark.parametrize("enum", sorted(FORMULAS))
def test_declared_formula_divergence_is_real(enum):
    """Одинаковое имя и разное число — опаснее разного состава: заметить это
    можно только сравнив выдачи, а списком — сразу."""
    same = []
    for profile_id, mine, vend in _pairs():
        f_mine, f_vend = _formula(mine, enum), _formula(vend, enum)
        if f_mine and f_vend and f_mine == f_vend:
            same.append(profile_id)
    assert not same, (
        f"«{enum}» объявлен расходящимся по формуле, но в {same} формулы совпали — "
        f"либо расхождение исчезло, либо правку потеряли при синхронизации")


def test_shareholder_roe_inputs_come_as_a_pair():
    """Числитель и знаменатель обязаны быть с ОДНОГО уровня консолидации.
    Прибыль акционеров, делённая на капитал ГРУППЫ, — не ROE акционеров, а
    бессмыслица, которую никто не заметит: число выглядит правдоподобно."""
    for profile_id, mine, _vend in _pairs():
        enums = _enums(mine)
        if "ROE_SHAREHOLDERS" not in enums:
            continue
        assert {"NET_INCOME_SHAREHOLDERS", "EQUITY_SHAREHOLDERS"} <= enums, (
            f"{profile_id}: ROE акционеров есть, а его входов нет")
        formula = _formula(mine, "ROE_SHAREHOLDERS")
        assert "TOTAL_EQUITY" not in formula, (
            f"{profile_id}: ROE акционеров считается по капиталу ГРУППЫ — "
            f"уровни числителя и знаменателя разошлись")
