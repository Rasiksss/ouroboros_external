"""#35: cost guard прогона извлечения — защита от разгона по вызовам/токенам.

Единая точка входа всех LLM-вызовов движка — openrouter.complete_with_usage;
гвард живёт там, а бюджет объявляет ВХОДНАЯ точка прогона (extract_stream_
finengine_dev / extract_all_sync) через contextvar — наследуется всеми
вложенными задачами (сегменты/группа/ретраи), работает и в бэке, и в станции
(движок один и тот же). Чат-путь через гейтвей сюда не ходит — не задет.

Цель — не экономия на нормальном прогоне, а СТОП при паттерне разгона
(бесконечные «пустой ответ, повтор», циклы по сегментам и т.п.), поэтому
дефолты заведомо выше честного максимума большого отчёта.

Ручки (env, действуют и в станции):
  FINENGINE_LLM_MAX_CALLS_PER_RUN   (дефолт 600)
  FINENGINE_LLM_MAX_TOKENS_PER_RUN  (дефолт 60_000_000, in+out суммарно)
"""
from __future__ import annotations

import contextvars
import logging
import os

log = logging.getLogger(__name__)

_budget: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "finengine_llm_budget", default=None)


class BudgetExceeded(RuntimeError):
    """Прогон превысил бюджет LLM-вызовов — остановлен как разгон."""


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def start_run(label: str) -> contextvars.Token:
    """Объявить бюджет прогона. Вернуть token для reset() в finally."""
    state = {
        "label": label,
        "calls": 0,
        "in": 0,
        "out": 0,
        "max_calls": _int_env("FINENGINE_LLM_MAX_CALLS_PER_RUN", 600),
        "max_tokens": _int_env("FINENGINE_LLM_MAX_TOKENS_PER_RUN", 60_000_000),
    }
    return _budget.set(state)


def end_run(token: contextvars.Token) -> None:
    _budget.reset(token)


def precheck_and_count() -> None:
    """Вызывается перед КАЖДЫМ LLM-вызовом. Вне прогона (бюджет не объявлен) —
    no-op. Поверх лимита — BudgetExceeded с честной сводкой."""
    st = _budget.get()
    if st is None:
        return
    if st["calls"] >= st["max_calls"] or (st["in"] + st["out"]) >= st["max_tokens"]:
        msg = (
            f"Бюджет прогона исчерпан ({st['label']}): "
            f"{st['calls']} вызовов (лимит {st['max_calls']}), "
            f"{st['in'] + st['out']:,} токенов (лимит {st['max_tokens']:,}). "
            "Извлечение остановлено как разгон — проверьте логи прогона."
        )
        log.error("llm budget exceeded: %s", msg)
        raise BudgetExceeded(msg)
    st["calls"] += 1


def add_usage(usage: dict) -> None:
    st = _budget.get()
    if st is None:
        return
    st["in"] += int(usage.get("in", 0) or 0)
    st["out"] += int(usage.get("out", 0) or 0)


def snapshot() -> dict | None:
    """Текущее потребление прогона (для done-события/логов). None вне прогона."""
    st = _budget.get()
    if st is None:
        return None
    return {"calls": st["calls"], "in": st["in"], "out": st["out"]}
