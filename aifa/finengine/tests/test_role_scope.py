"""Состав извлечения сужается под роль (#141).

ЧТО БЫЛО. `_scope_with_calculated` доливал ВСЕ расчётные показатели ячейки
безусловно — поверх уже правильно суженного ролью набора. Роли ДИЭ на
нефинансовой ячейке к её 16 показателям приезжало ещё 43 чужих коэффициента.
Движок эмитит строку metric на КАЖДОЕ определение формулы, посчиталось оно или
нет, — отсюда «~56 строк» в рабочей области вместо 13 строк раскладки.

ДВЕ ГРАНИЦЫ, которые тест держит одновременно:
1. Роль сужает (иначе дефект вернётся).
2. Роль other/all по-прежнему получает ПОЛНУЮ ячейку С ФОРМУЛАМИ. Это грабля
   31.07.2026: из 47 формул выживало 6, и «долг/EBITDA» анализ считал в уме.
3. Анализ в том же ходу не должен обеднеть: диспетчер ставит basis_full, и тогда
   читается полный базис ячейки, а роль остаётся фильтром на ВЫДАЧЕ.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from finengine.roles.scope import build_engine_scope, build_role_plan
from finengine.skills.metric_schema import registry_for_profile

REPO = Path(__file__).resolve().parents[1]
TOOLS = (REPO / "src" / "finengine" / "llm" / "tools.py").read_text(encoding="utf-8")
AGENT = (REPO / "src" / "finengine" / "orchestrator" / "agent.py").read_text(encoding="utf-8")
# Бэкендовый решатель состава переехал: раньше он жил в `llm/tools.py`, внутри
# `drive_extract_in_station`. Тот тул снесён вместе с монолитом (aa13cb1), а
# решение теперь принимает приёмник станционного хода.
TURN = (REPO / "src" / "finengine" / "api" / "station_turn.py").read_text(encoding="utf-8")
DRIVER = (REPO / "src" / "finengine" / "skills" / "station_driver.py").read_text(encoding="utf-8")
STATION = (REPO / "station-skills" / "aifa-extract" / "scripts" / "extract.py").read_text(
    encoding="utf-8")
PROJECTS_TS = (REPO.parent / "web" / "src" / "lib" / "projects.ts").read_text(encoding="utf-8")
API_TS = (REPO.parent / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")


def _cell(profile_id: str) -> tuple[set[str], set[str]]:
    reg = registry_for_profile(profile_id)
    return ({m["enum"] for m in reg["extractable"]}, {c[0] for c in reg["calculated"]})


def _engine_enums(profile_id: str, company_type: str, role: str, *, basis_full: bool) -> set[str]:
    """РЕАЛЬНОЕ решение боевого пути — не копия: и бэкенд, и станция зовут ровно
    эту функцию (парность закреплена статикой в конце файла)."""
    return build_engine_scope(role, company_type, profile_id, basis_full=basis_full)[1]


# ── сужение под роль ────────────────────────────────────────────────────────

def test_die_reads_only_its_own_layout_and_formula_inputs():
    """Роль ДИЭ, нефинансовая ячейка: 16 показателей, а не 59."""
    plan = build_role_plan("die", "non_financial", "ifrs_nf_ru")
    engine = _engine_enums("ifrs_nf_ru", "non_financial", "die", basis_full=False)
    assert engine == set(plan.extraction_enums)
    assert plan.output_enums <= engine, "входы раскладки обязаны читаться"
    # Ровно «раскладка ∪ рекурсивно развёрнутые входы её формул» — ничего сверх.
    assert len(engine) < 20, f"в движок снова уезжает чужое: {len(engine)}"


@pytest.mark.parametrize(
    "profile_id, company_type, role",
    [("ifrs_nf_ru", "non_financial", "die"), ("ifrs_nf_ru", "non_financial", "risks"),
     ("ifrs_bank_ru", "bank", "die"), ("ifrs_bank_ru", "bank", "dgr")],
)
def test_narrow_role_never_gets_the_whole_cell(profile_id, company_type, role):
    """Движок читает РОВНО план роли — ни одного чужого коэффициента сверху."""
    plan = build_role_plan(role, company_type, profile_id)
    engine = _engine_enums(profile_id, company_type, role, basis_full=False)
    assert engine == set(plan.extraction_enums), \
        f"сверх плана роли приехало: {sorted(engine - set(plan.extraction_enums))[:10]}"


# ── границы, которые ломать нельзя ──────────────────────────────────────────

@pytest.mark.parametrize("role", ["other", "all"])
def test_other_and_all_keep_the_full_cell_with_formulas(role):
    """ГРАБЛЯ 31.07.2026: без долива формул из 47 выживало 6."""
    extractable, calculated = _cell("ifrs_nf_ru")
    engine = _engine_enums("ifrs_nf_ru", "non_financial", role, basis_full=False)
    assert engine == extractable | calculated
    assert {"NET_DEBT", "TOTAL_DEBT", "RATIO_NET_DEBT_EBITDA",
            "EBITDA_MARGIN", "FREE_CASH_FLOW"} <= engine


@pytest.mark.parametrize(
    "profile_id, company_type, role",
    [("ifrs_nf_ru", "non_financial", "dgr"), ("ifrs_bank_ru", "bank", "risks")],
)
def test_unsupported_role_falls_back_to_the_full_cell_not_to_nothing(profile_id, company_type, role):
    """У ДГР нет нефинансовой раскладки, у risks — банковской (так пришло из
    FinEngine, upstream идентичен). Пустой extraction_enums не должен означать
    «извлечь ничего»."""
    extractable, calculated = _cell(profile_id)
    assert build_role_plan(role, company_type, profile_id).format == "unsupported"
    assert _engine_enums(profile_id, company_type, role, basis_full=False) == extractable | calculated


def test_basis_full_restores_the_whole_cell_for_the_analysis_in_the_same_turn():
    """Главное свойство правки: сужение НЕ обедняет вердикт. Когда прогон
    кормит анализ через шину (#121), читаем полную ячейку, а роль показывается
    фильтром на выдаче."""
    extractable, calculated = _cell("ifrs_nf_ru")
    assert _engine_enums("ifrs_nf_ru", "non_financial", "die", basis_full=True) == \
        extractable | calculated


# ── парность боевых путей ───────────────────────────────────────────────────

def test_both_live_paths_use_the_same_decision():
    """Бэкенд и станция обязаны звать ОДНУ функцию. Две копии решения = однажды
    поправят одну и молча разведут dev с продом (ровно та беда, из-за которой
    сносили инлайн-ветку движка).

    🔴 СТОРОЖ СМОТРЕЛ НЕ ТУДА (починено 25.08.2026). Он проверял `tools.py`,
    где решатель жил внутри `drive_extract_in_station`. Тул снесён вместе с
    монолитом (aa13cb1) — и сторож стал красным навсегда, то есть перестал
    охранять что-либо, оставаясь в прогоне как шум.

    ⚠️ Срез 13.5.4: ПРЕДМЕТ ПАРЫ СМЕНИЛСЯ. `build_engine_scope` (что ДВИЖОК
    ЧИТАЕТ из отчёта) больше не вызывается ни бэкендом, ни станцией — состав
    чтения решает граф внутри `out.form` (`requested_concepts`), одним местом
    по устройству, а не соглашением звать одну функцию. Пара, которая
    осталась, — `build_role_plan` (что ПОКАЗАТЬ модели/человеку про саму РОЛЬ:
    сработала она для этой ячейки или нет): её зовут и станционный
    `extract.py` (`_role_notice_line`, для суб-агента, читающего stdout
    `exec`), и слушатель `station_turn.py` (`_canonical_role_plan`, для
    канона/истории хода) — те же места, что были, тот же класс риска.
    """
    for src, where in ((TURN, "api/station_turn.py"), (STATION, "station extract.py")):
        assert "build_role_plan(" in src, f"{where}: роль считается мимо общей функции"
        assert "_scope_with_calculated" not in src, f"{where}: вернулась локальная копия решения"
    assert "drive_extract_in_station" not in TOOLS, (
        "в tools.py вернулся снесённый монолитный драйвер — состав снова "
        "считается в двух местах")


# ── вторая половина дефекта: фронт выбрасывал белый список роли ─────────────

def test_front_keeps_the_roles_whitelist():
    """Бэкенд слал table_output_enums всегда, но тип события его не объявлял, а
    редьюсер не присваивал — фильтр таблицы на живом пути не включался никогда."""
    assert "table_output_enums?: string[] | null" in API_TS, "тип события не объявляет поле"
    assert "table_output_enums: msg.table_output_enums ?? ev.table_output_enums" in PROJECTS_TS, \
        "редьюсер выбрасывает белый список роли"


# ─────────── неподдерживаемая пара роль×отчётность — вслух, не молча ───────────
# ЧТО БЫЛО. Пары «ДГР × нефинансовая» и «Риски × банк» проваливались в полный
# набор ячейки МОЛЧА (119 и 107 показателей). Человек выбирал узкую роль,
# получал полный список и не мог понять, сработал ли его выбор. Данные при этом
# верные — отказывать не за что, но факт «роль не применилась» обязан быть сказан.


@pytest.mark.parametrize(("profile_id", "company_type", "role"), [
    ("ifrs_nf_ru", "non_financial", "dgr"),
    ("ifrs_bank_ru", "bank", "risks"),
])
def test_unsupported_pair_is_reported_not_hidden(profile_id, company_type, role):
    from finengine.roles.scope import role_not_applied

    plan = build_role_plan(role, company_type, profile_id)
    assert role_not_applied(plan), f"{role}×{company_type}: молчаливый полный набор вернулся"


@pytest.mark.parametrize(("profile_id", "company_type", "role"), [
    ("ifrs_bank_ru", "bank", "dgr"),      # роль СРАБОТАЛА — сообщать не о чем
    ("ifrs_bank_ru", "bank", "other"),    # «Общая» = полный набор ПО ЗАМЫСЛУ
    ("ifrs_bank_ru", "bank", "all"),
])
def test_working_roles_are_not_flagged(profile_id, company_type, role):
    """Ложное «роль не применилась» научит пользователя не читать предупреждения."""
    from finengine.roles.scope import role_not_applied

    assert not role_not_applied(build_role_plan(role, company_type, profile_id))


def test_notice_reaches_the_screen_channel_of_the_live_path():
    """Половина, которая РАБОТАЕТ: предупреждение доезжает до фронта.

    ⚠️ 13.08.2026, #190/К1. Тело события `extract_done` собирает
    `skills/extract_result.py::build_extract_done`. Прежняя проверка искала
    `role_notice` в `tools.py` ПОСЛЕ подстроки `extract_done` — после переезда
    она осталась ЗЕЛЁНОЙ, потому что в `tools.py` и то и другое ещё
    встречалось, только уже в других ролях. Проверка откатом — убрать
    `role_notice` из `build_extract_done`.
    """
    from finengine.skills import extract_result

    emit_src = inspect.getsource(extract_result.build_extract_done)
    assert '"role_notice": role_notice' in emit_src, "нет в событии для фронта"
    assert '"role_not_applied": role_not_applied' in emit_src, "нет в событии для фронта"


