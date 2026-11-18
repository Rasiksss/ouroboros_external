"""Financial-analysis skill registry.

`fe_analyze` dispatches by `analysis_type` into this registry. Its tool
description is generated from `menu_text()` so adding a skill here makes the
agent aware of it automatically. See `base.py` for the design rationale.

Today: `general` (document-oriented financial analysis) is ready with a `depth`
parameter — `full` (default) or `quick`. plan-fact / retrospective / comparative
are registered as `planned` so the agent knows the roadmap and can decline
gracefully (they become the future axis a per-user «analysis» button routes into;
for now the button just selects depth). xlsx plan/fact *factor* decomposition
lives in the separate `factor_analysis` tool (different input shape), not here.

The analysis is DOCUMENT-oriented: it reads the whole report text (statements +
notes + management commentary), the model computes ratios itself and cites the
source — NOT bound to a pre-extracted metric table (decision 08.07). Cell
(company_type × standard × language) is a prompt parameter (framework.py), not a
separate skill. Depth changes the text window, section framework and output size.
"""
from __future__ import annotations

from finengine.skills.analysis import run as _run
from finengine.skills.analysis.base import AnalysisSkill, build_menu

ANALYSIS_SKILLS: list[AnalysisSkill] = [
    AnalysisSkill(
        id="general",
        title="Финансовый анализ",
        when_to_use=(
            "ДЕФОЛТ для «проведи анализ / сделай выводы / оцени отчёт / разбери компанию»: "
            "одна отчётность → структурированный разбор (рентабельность, ликвидность, долг, "
            "денежные потоки, динамика, риски; для банков — капитал/качество активов/NPL). "
            "Глубина задаётся параметром depth: full (полный, дефолт) или quick (быстрая "
            "выжимка + ключевые коэффициенты, когда просят «быстро/коротко»)"
        ),
        min_docs=1,
        max_docs=1,
        doc_semantics="1 отчёт",
        company_variants=("nf", "bank"),
        status="ready",
        handler=_run.run,
    ),
    AnalysisSkill(
        id="planfact",
        title="План-факт анализ",
        when_to_use=(
            "пользователь даёт прежний прогноз и фактическую отчётность; "
            "оценить, насколько прогноз разошёлся с фактом и почему"
        ),
        min_docs=2,
        max_docs=2,
        doc_semantics="прогноз + факт",
        company_variants=("nf", "bank"),
        status="planned",
    ),
    AnalysisSkill(
        id="retrospective",
        title="Ретроспективный анализ (динамика)",
        when_to_use=(
            "несколько отчётностей одной компании за разные периоды; "
            "проследить динамику показателей во времени"
        ),
        min_docs=2,
        max_docs=0,
        doc_semantics="N периодов одной компании",
        company_variants=("nf", "bank"),
        status="planned",
    ),
    AnalysisSkill(
        id="comparative",
        title="Сравнительный анализ",
        when_to_use="две компании; сравнить их финансовые профили бок о бок",
        min_docs=2,
        max_docs=2,
        doc_semantics="2 компании",
        company_variants=("nf", "bank"),
        status="planned",
    ),
]

_BY_ID = {s.id: s for s in ANALYSIS_SKILLS}


def get_skill(skill_id: str) -> AnalysisSkill | None:
    return _BY_ID.get(skill_id)


def menu_text() -> str:
    return build_menu(ANALYSIS_SKILLS)
