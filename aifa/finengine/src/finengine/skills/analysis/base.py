"""Analysis-skill registry primitives.

The financial-analysis surface scales along three orthogonal axes:
  - analysis TYPE (general / plan-fact / retrospective / comparative / …)
    → this is what a `AnalysisSkill` represents (the user-visible "skill");
  - company TYPE (nf / bank) → a content variant *inside* a skill;
  - ROLE (other / risks / cib / …) → an emphasis lens, a parameter — NOT a
    separate skill.

The agent discovers skills by reading the `fe_analyze` tool description, which
is generated from the registry below (`build_menu`). Adding a new analysis type
= adding one `AnalysisSkill` entry; the tool description then advertises it
automatically. `status="planned"` entries are shown as a roadmap so the agent
can tell the user "this kind of analysis isn't available yet" instead of
improvising a poor one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional


@dataclass(frozen=True)
class AnalysisSkill:
    id: str                       # routing key, passed as analysis_type
    title: str                    # human title (RU)
    when_to_use: str              # one-liner that drives agent routing
    min_docs: int                 # required input documents
    max_docs: int                 # 0 = unbounded
    doc_semantics: str            # what the documents mean ("1 отчёт" / "прогноз + факт" / …)
    company_variants: tuple[str, ...]   # ("nf", "bank") or ("any",)
    status: str                   # "ready" | "planned"
    # handler(*, company_name, company_type, metrics, ocr_text, model_id, role, progress) -> markdown str
    handler: Optional[Callable[..., Awaitable[str]]] = None


def build_menu(skills: list[AnalysisSkill]) -> str:
    """Render the registry as a compact menu for the fe_analyze tool description.

    Ready skills become selectable `analysis_type` values with their
    when-to-use + input requirement; planned ones are listed as a roadmap.
    """
    ready = [s for s in skills if s.status == "ready"]
    planned = [s for s in skills if s.status == "planned"]
    lines = ["Виды анализа — значения analysis_type:"]
    for s in ready:
        lines.append(f"  • {s.id} — {s.title}: {s.when_to_use} (вход: {s.doc_semantics}).")
    if planned:
        names = ", ".join(f"{s.id} ({s.title})" for s in planned)
        lines.append(f"  В разработке, пока недоступно: {names}.")
    return "\n".join(lines)
