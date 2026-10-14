"""Методика как адресуемые данные + персональные правки (#123).

Главное, что здесь защищается:
1) разбор методики на элементы НЕ изменил ни одного промпта (эталоны сняты ДО);
2) правка, потерявшая якорь, — ОШИБКА, а не тихий пропуск: молчание означало бы,
   что считаем по базовой методике, пока пользователь уверен в своей;
3) правки одного пользователя не протекают в базу и, значит, к другому.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from finengine.skills.analysis import framework as F
from finengine.skills.analysis import methodology as md

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "golden" / "methodology"
OCR = (
    "===PAGE 1===\nОтчет о прибылях и убытках\nВыручка 100\n\n"
    "===PAGE 2===\nПримечание 5. Коэффициент достаточности капитала\nН1.0 12,5%\n\n"
    "===PAGE 3===\nПрочее примечание\n"
)
CASES = [
    (ct, depth, tag, kw)
    for ct in ("non_financial", "bank")
    for depth in ("quick", "full")
    # Случай «bare» (промпт без таблицы заземления) снят 14.08 вместе с самой
    # возможностью (#238): такой прогон теперь отбивается отказом, а эталон на
    # него закреплял бы поведение, ради снятия которого правка и делалась.
    for tag, kw in (
        ("grounded", {"metrics_table": "SALES_REVENUE | Выручка | 100 | стр. 1\n"}),
        ("capfacts", {"metrics_table": "SALES_REVENUE | Выручка | 100 | стр. 1\n",
                      "capital_facts": "Н1.0: 12,5 / 11,8"}),
    )
]


@pytest.mark.parametrize(("ct", "depth", "tag", "kw"), CASES,
                         ids=[f"{c}.{d}.{t}" for c, d, t, _ in CASES])
def test_prompt_is_byte_identical_to_golden(ct, depth, tag, kw):
    """Промпт не должен меняться НЕЗАМЕТНО.

    Эталоны снимались дважды и по разным поводам:
    1) при разборе методики на элементы — там правка обязана была быть невидимой
       для модели, и совпадение байт-в-байт это доказало;
    2) при снятии лицензии на счёт в уме (#137) — там правка была НАМЕРЕННОЙ,
       эталон пересняли, и в диффе видно ровно четыре варианта «с таблицей» и
       ровно один абзац.

    То есть тест ловит не «изменение» само по себе, а изменение, которое автор не
    предъявил. Падение здесь — это приглашение посмотреть дифф, а не команда
    переснять эталон не глядя.
    """
    got = F.build_prompt(company_name="Тест", company_type=ct, standard="ifrs",
                         depth=depth, ocr_text=OCR, **kw)
    assert got == (GOLDEN / f"{ct}.{depth}.{tag}.txt").read_text(encoding="utf-8")


def test_every_referenced_id_exists():
    """Список сборки ссылается только на существующие элементы."""
    m = md.base()
    refs = set(m.comparability)
    for ids in m.analytical.values():
        refs |= set(ids)
    for ids in m.framework.values():
        refs |= set(ids)
    for el in m.elements.values():
        refs |= set(el.append)
    assert not (refs - set(m.elements)), "висячие ссылки в методике"


# ─────────────────────────── правки пользователя ───────────────────────────


def test_replace_changes_the_rendered_rule():
    """«NIM у нас считают по средним за квартал» — правка формулы доезжает."""
    m = md.apply([{"op": "replace", "id": "bank.formula.nim",
                   "text": "NIM = NII / средние активы ЗА КВАРТАЛ.", "why": "методика банка"}])
    block = md.analytical_block(m, "bank")
    assert "средние активы ЗА КВАРТАЛ" in block
    assert "процентно-приносящие активы" not in block, "базовая формулировка осталась"
    assert m.changes[0]["title"] == "NIM (чистая процентная маржа)"


def test_replace_of_unknown_id_is_an_error_not_silence():
    """Якорь потерян — говорим прямо. Тихий пропуск = считаем не по той методике."""
    with pytest.raises(md.MethodologyError, match="bank.formula.nim_old"):
        md.apply([{"op": "replace", "id": "bank.formula.nim_old", "text": "..."}])


def test_empty_replacement_is_refused():
    with pytest.raises(md.MethodologyError, match="пуст"):
        md.apply([{"op": "replace", "id": "bank.formula.nim", "text": "   "}])


def test_disable_removes_rule_from_the_block():
    m = md.apply([{"op": "disable", "id": "nf.rule.operating_leverage"}])
    assert "Операционный рычаг" not in md.analytical_block(m, "non_financial")


def test_disable_removes_section_from_the_framework():
    m = md.apply([{"op": "disable", "id": "nf.full.sec.dynamics"}])
    frame = md.framework_block(m, "non_financial", "full")
    assert "Динамика по периодам" not in frame
    # Нумерация обязана остаться сплошной, иначе модель получит дыру в каркасе.
    assert "\n8. " in frame and "\n9. " not in frame


def test_disable_of_unknown_id_is_an_error():
    with pytest.raises(md.MethodologyError):
        md.apply([{"op": "disable", "id": "nf.rule.no_such"}])


def test_added_rule_lands_in_the_block():
    """«Работающие активы считай так-то» — своё определение внутри методики."""
    m = md.apply([{
        "op": "add", "id": "user.def.working_assets", "kind": "definition",
        "title": "Работающие активы", "group": "analytical.bank",
        "text": "Работающие активы = кредиты клиентам + долговые ЦБ, БЕЗ средств в ЦБ.",
    }])
    assert "БЕЗ средств в ЦБ" in md.analytical_block(m, "bank")
    assert "БЕЗ средств в ЦБ" not in md.analytical_block(m, "non_financial")


def test_added_rule_can_be_placed_after_a_given_one():
    m = md.apply([{
        "op": "add", "id": "user.rule.horizon", "title": "Горизонт устойчивости",
        "group": "analytical.non_financial", "after": "nf.rule.liquidity",
        "text": "Горизонт устойчивости считай 8 месяцев, не 6.",
    }])
    ids = m.analytical["non_financial"]
    assert ids[ids.index("nf.rule.liquidity") + 1] == "user.rule.horizon"


def test_own_element_must_be_namespaced():
    """Без префикса «user.» правка однажды столкнётся с элементом базы."""
    with pytest.raises(md.MethodologyError, match="user."):
        md.apply([{"op": "add", "id": "bank.rule.mine", "group": "analytical.bank",
                   "title": "Своё", "text": "текст"}])


def test_add_into_unknown_group_names_the_valid_ones():
    with pytest.raises(md.MethodologyError, match="comparability"):
        md.apply([{"op": "add", "id": "user.rule.x", "group": "analytical.insurance",
                   "title": "Своё", "text": "текст"}])


def test_unknown_op_is_refused():
    with pytest.raises(md.MethodologyError, match="append"):
        md.apply([{"op": "append", "id": "bank.formula.nim", "text": "..."}])


# ────────────────────── расхождение с обновлённой базой ──────────────────────


def test_updated_base_is_reported_not_silently_overridden():
    """Мы поправили формулу, у пользователя своя — он должен УЗНАТЬ и решить."""
    m = md.apply([{"op": "replace", "id": "bank.formula.cor",
                   "text": "CoR = мой числитель / мой знаменатель.",
                   "base_was": "СТАРЫЙ текст базовой формулы, которого уже нет"}])
    assert m.notices and m.notices[0]["id"] == "bank.formula.cor"
    assert m.notices[0]["yours"].startswith("CoR = мой")
    assert "rollforward" in m.notices[0]["base_now"], "показываем, что стало у нас"
    # Правка всё равно применена: решение пользователя в силе до его же отмены.
    assert "мой числитель" in md.analytical_block(m, "bank")


def test_no_notice_when_base_did_not_move():
    base_text = md.base().elements["bank.formula.cor"].text
    m = md.apply([{"op": "replace", "id": "bank.formula.cor", "text": "своё",
                   "base_was": base_text}])
    assert not m.notices


# ─────────────────────────── изоляция пользователей ───────────────────────────


def test_deltas_do_not_leak_into_the_base():
    """Правка одного не должна доехать до другого — база общая и неизменяемая."""
    md.apply([{"op": "replace", "id": "bank.formula.nim", "text": "чужая формула"},
              {"op": "disable", "id": "bank.rule.wording"},
              {"op": "add", "id": "user.rule.x", "group": "comparability",
               "title": "Своё", "text": "текст"}])
    fresh = md.base()
    assert "процентно-приносящие активы" in fresh.elements["bank.formula.nim"].text
    assert "bank.rule.wording" in fresh.analytical["bank"]
    assert "user.rule.x" not in fresh.elements
    assert md.analytical_block(fresh, "bank") == md.analytical_block(md.base(), "bank")


def test_catalogue_lists_addressable_elements_with_titles():
    """Агент не может сослаться на правило, которого не видит по имени."""
    cat = {c["id"]: c for c in md.catalogue()}
    assert cat["bank.formula.nim"]["title"] == "NIM (чистая процентная маржа)"
    assert "анализ (bank)" in cat["bank.formula.nim"]["used_in"]
    # Правило флагов подмешивается в раздел, а не стоит отдельным пунктом —
    # адресовать его всё равно надо.
    assert "common.rule.flags" in cat
