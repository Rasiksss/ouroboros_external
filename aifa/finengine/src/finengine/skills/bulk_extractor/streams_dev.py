"""Точка входа боевого прогона: собирает параметры и запускает полноконтекстный поток.
"""
from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator, Optional
# ⚠️ Импорт из КОНКРЕТНОГО модуля, а не из шима `.streams`. Разрез
# `streams_sync`/`streams_dev` прошёл РАНЬШЕ, чем `streams_full_context`,
# и помощник направил их на шим — тот теперь сам импортирует отсюда, и
# получался круг. Порядок разрезов имеет значение: модуль-потребитель
# режется ПОСЛЕ того, кого он зовёт.
from .streams_full_context import _extract_stream_full_context


async def extract_stream_finengine_dev(
    doc_id: str,
    company_type: str,
    index=None,
    limit: Optional[int] = None,
    model_id: Optional[str] = None,
    include_segments: bool = True,
    include_consolidated_group: bool = False,
    metric_enums: Optional[set[str]] = None,
    role: str = "",
    segment_mode: str = "extended",
    segment_context_mode: str = "full",
    debug_dir: Optional[Path] = None,
    registry_override: Optional[dict] = None,
) -> AsyncGenerator[dict, None]:
    """Canonical Finengine DEV registry pipeline used by UI and benchmarks.

    `registry_override` — строки реестра ЯЧЕЙКИ (`registry_for_profile`), то есть
    та самая редактируемая копия, которую правит аналитик. ДЫРА #147: параметра
    здесь не было, и живой путь (чат → станция → этот вход) файл ячейки
    использовал ТОЛЬКО как список enum'ов для фильтра, а сами строки показателей
    и формулы брал из сгенерированного `data/metrics.json`. Любая правка
    синонима/подсказки игнорировалась, а шесть коэффициентов из #137 не
    считались никогда. Дефолт None обязателен: этот же вход зовут бенчмарки.
    """
    # #35 cost guard: бюджет LLM-вызовов на прогон (contextvar наследуют все
    # вложенные задачи — сегменты/группа/ретраи). При разгоне openrouter кинет
    # BudgetExceeded — отдаём честную ошибку в стрим вместо тихого прожига.
    from finengine.llm import budget as _llm_budget
    _budget_token = _llm_budget.start_run(f"extract doc={doc_id} role={role or '-'}")
    try:
        async for event in _extract_stream_full_context(
            doc_id,
            company_type,
            index=index,
            limit=limit,
            model_id=model_id,
            include_segments=include_segments,
            include_consolidated_group=include_consolidated_group,
            metric_enums=metric_enums,
            full_registry=True,
            role=role,
            segment_mode=segment_mode,
            segment_context_mode=segment_context_mode,
            debug_dir=debug_dir,
            registry_override=registry_override,
        ):
            yield event
    except _llm_budget.BudgetExceeded as e:
        yield {"type": "error", "message": str(e)}
    finally:
        _llm_budget.end_run(_budget_token)
