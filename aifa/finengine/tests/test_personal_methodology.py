"""Личная методика: хранение правок и их горизонт (#123).

Проверяем свойства, где ошибка была бы ТИХОЙ: правка одного не должна доехать до
другого; сломанная правка не должна лечь на диск; разовая правка не должна
превратиться в постоянную; а потерянный якорь не должен ни уронить анализ, ни
раствориться без следа.
"""
from __future__ import annotations

import pytest

from finengine.config import settings
from finengine.skills.analysis import methodology as md
from finengine.store import state
from finengine.skills.analysis import personal


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    personal._turn_deltas.clear()
    yield
    personal._turn_deltas.clear()


NIM_MINE = {"op": "replace", "id": "bank.formula.nim",
            "text": "NIM = NII / средние работающие активы ЗА КВАРТАЛ.",
            "why": "методика нашего банка"}


def test_saved_delta_applies_to_every_next_run():
    personal.save_delta(7, NIM_MINE)
    block = md.analytical_block(personal.effective(7), "bank")
    assert "ЗА КВАРТАЛ" in block
    # И на следующем прогоне тоже — это и значит «считай так ВСЕГДА».
    assert "ЗА КВАРТАЛ" in md.analytical_block(personal.effective(7), "bank")


def test_broken_delta_is_never_stored():
    """Иначе одна кривая правка сломала бы все последующие анализы.

    ⚠️ Проверяем СВОЙСТВО («не сохранилась»), а не место хранения. До переезда
    здесь стояло `not overlay_path(7).exists()` — тест знал про файл, и смена
    хранилища его сломала бы, ничего не поймав по существу.
    """
    with pytest.raises(md.MethodologyError):
        personal.save_delta(7, {"op": "replace", "id": "bank.formula.nonexistent", "text": "x"})
    assert personal.load(7) == []
    assert state.get(state.METHODOLOGY, owner=7, key="overlay") is None


def test_second_edit_of_same_rule_replaces_the_first():
    personal.save_delta(7, NIM_MINE)
    personal.save_delta(7, {**NIM_MINE, "text": "NIM = NII / активы на конец периода."})
    saved = personal.load(7)
    assert len(saved) == 1, "правки одного правила не должны копиться стопкой"
    assert "на конец периода" in md.analytical_block(personal.effective(7), "bank")


def test_forget_returns_the_base_rule():
    personal.save_delta(7, NIM_MINE)
    assert personal.forget(7, "bank.formula.nim") == 1
    assert "процентно-приносящие активы" in md.analytical_block(personal.effective(7), "bank")
    assert personal.forget(7, "bank.formula.nim") == 0, "убирать нечего — говорим честно"


def test_base_edit_is_remembered_for_later_merge():
    """Правка помнит, ПРОТИВ ЧЕГО делалась — иначе нечего будет сравнивать."""
    personal.save_delta(7, NIM_MINE)
    assert personal.load(7)[0]["base_was"] == md.base().elements["bank.formula.nim"].text


def test_users_do_not_see_each_others_methodology():
    personal.save_delta(7, NIM_MINE)
    assert "ЗА КВАРТАЛ" not in md.analytical_block(personal.effective(8), "bank")
    assert personal.load(8) == []


# ─────────────────────── «сейчас» против «всегда» ───────────────────────


def test_turn_delta_is_eaten_by_the_first_run():
    """«В этот раз посчитай так» — это не новая методика."""
    personal.remember_for_turn(7, {"op": "replace", "id": "nf.rule.profit_quality",
                                   "text": "В этот раз исключи продажу оборудования."})
    assert "продажу оборудования" in md.analytical_block(
        personal.effective(7), "non_financial")
    assert "продажу оборудования" not in md.analytical_block(
        personal.effective(7), "non_financial"), "разовая правка осталась на второй прогон"


def test_turn_delta_is_not_persisted():
    personal.remember_for_turn(7, {"op": "replace", "id": "nf.rule.profit_quality",
                                   "text": "разово"})
    assert personal.load(7) == []


def test_turn_delta_expires_if_no_run_comes():
    personal.remember_for_turn(7, {"op": "replace", "id": "nf.rule.profit_quality",
                                   "text": "разово"})
    ts, deltas = personal._turn_deltas[7]
    personal._turn_deltas[7] = (ts - personal._TURN_TTL_SEC - 1, deltas)
    assert "разово" not in md.analytical_block(personal.effective(7), "non_financial")


def test_broken_turn_delta_is_refused_immediately():
    with pytest.raises(md.MethodologyError):
        personal.remember_for_turn(7, {"op": "replace", "id": "no.such.rule", "text": "x"})


# ─────────────────────── база уехала из-под правки ───────────────────────


def test_lost_anchor_does_not_kill_the_run_but_is_visible(monkeypatch):
    """Мы сами переписали базу — ронять из-за этого чужой анализ нельзя.

    Но и молча посчитать по базовой методике, пока пользователь уверен в своей,
    нельзя тем более: потеря обязана доехать до провенанса карточки.
    """
    personal.save_delta(7, NIM_MINE)
    trimmed = md.base()
    del trimmed.elements["bank.formula.nim"]  # как будто правило убрали из продукта
    monkeypatch.setattr(md, "base", lambda: trimmed)

    m = personal.effective(7)
    assert m.notices and m.notices[0]["kind"] == "lost_anchor"
    assert m.notices[0]["yours"].startswith("NIM = NII")
    assert not m.changes, "потерянная правка не должна числиться применённой"


def test_describe_shows_changes_without_eating_turn_deltas():
    personal.save_delta(7, NIM_MINE)
    personal.remember_for_turn(7, {"op": "replace", "id": "nf.rule.liquidity",
                                   "text": "разово"})
    got = personal.describe(7)
    assert [c["id"] for c in got["changes"]] == ["bank.formula.nim"]
    # Показ не должен съедать разовую правку — иначе она пропадёт до анализа.
    assert "разово" in md.analytical_block(personal.effective(7), "non_financial")


def test_a_garbled_overlay_does_not_crash_the_run():
    """Мусор вместо списка правок — прогон идёт по базе, а не падает."""
    state.put(state.METHODOLOGY, owner=7, key="overlay", doc={"deltas": "не список"})
    assert personal.load(7) == []
    assert md.analytical_block(personal.effective(7), "bank")
