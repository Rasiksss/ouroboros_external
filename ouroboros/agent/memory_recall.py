"""Relevance-based memory recall for durable named memory."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from ouroboros.storage.core_memory.memory.domain.models import KINDS, kinds_with_injection

if TYPE_CHECKING:
    from ouroboros.storage.core_memory.memory.domain.models import MemoryHeader
    from ouroboros.storage.core_memory.memory.domain.service import Memory

log = logging.getLogger(__name__)

_DEFAULT_MAX_RESULTS = 5
_RECALL_TIMEOUT_SEC = 8
_RECALLED_CONTENT_BUDGET_BYTES = 20_000

# Membership follows each kind's declared injection discipline, so adding a kind
# is adding a policy row rather than finding this module. ``knowledge`` declares
# ``index_only`` and is therefore in neither set: recall truncates each body to
# 3 000 characters and the whole set to 20 000 bytes, and feeding an
# 80 000-character knowledge memory through that would deliver a mutilated
# version of something the agent could have read whole with memory_read.
_AUTO_KINDS = kinds_with_injection("recall_auto")
_SELECTOR_KINDS = kinds_with_injection("recall_selector")
_AUTO_MAX_RESULTS = 2
_SELECTOR_CANDIDATE_LIMIT = 40

_SELECT_SYSTEM = """\
You select memories relevant to the current task query.
Given a query and a list of memories (with kind and description),
return the names of the memories that will clearly be useful for this task.

Rules:
- Include at most {max_results} memories.
- Only include memories you are confident are relevant to this specific query.
- The available list contains only {selector_kinds} memories.
- Include a memory only when it directly concerns the task topic.
- If nothing is clearly relevant, return an empty list.

Respond ONLY with valid JSON: {{"selected": ["name1", "name2"]}}
Do not include .md extensions. Do not add any explanation.
"""


def _auto_priority(kind: str) -> int:
    """Rank auto-injected kinds by their position in the closed enum."""
    return KINDS.index(kind) if kind in KINDS else len(KINDS)


def recall_relevant_memories(
    query: str,
    store: Memory,
    llm: Any,
    max_results: int = _DEFAULT_MAX_RESULTS,
    *,
    use_llm_selector: bool = True,
    event_queue: Any = None,
    task_id: str = "",
    headers: list[MemoryHeader] | None = None,
) -> list[tuple[str, float, float]]:
    """Select and read relevant memories for the current task.

    Kinds declaring ``recall_auto`` are injected deterministically within the
    budget. Kinds declaring ``recall_selector`` are injected only when the
    relevance selector chooses them for the current query. Selector failures
    skip those bodies.

    ``headers`` lets the caller hand over the listing it is about to build the
    prompt from, so selection and injection see one snapshot and read the header
    table once. Absent, the store is asked; what "eligible" means is the store's
    answer either way, not a predicate repeated here.
    """
    topics = store.list_prompt_headers() if headers is None else list(headers)
    if not topics:
        return []

    selected: list[str] = _auto_select(topics, max_results)
    remaining = max(0, max_results - len(selected))
    selector_candidates = _selector_candidates(query, topics, remaining)

    if use_llm_selector and remaining and selector_candidates:
        manifest = "\n".join(f"- [{h.kind}] {h.name}: {h.description}" for h in selector_candidates)
        selector_selected = _llm_select(
            query,
            manifest,
            llm,
            remaining,
            event_queue=event_queue,
            task_id=task_id,
        )
        if selector_selected:
            candidate_names = {h.name for h in selector_candidates}
            for name in selector_selected:
                if name in candidate_names and name not in selected:
                    selected.append(name)
                    if len(selected) >= max_results:
                        break
    if not selected:
        return []

    valid = {h.name for h in topics}
    selected = [n for n in selected if n in valid]
    if not selected:
        return []

    results, included_names = _read_facts(store, selected)
    try:
        store.mark_recalled(included_names)
    except Exception:
        log.debug("Failed to update memory recall counters", exc_info=True)
    return results


def _auto_select(topics: list[MemoryHeader], max_results: int) -> list[str]:
    limit = min(max(0, max_results), _AUTO_MAX_RESULTS)
    if limit <= 0:
        return []
    ranked = sorted(
        ((i, h) for i, h in enumerate(topics) if str(getattr(h, "kind", "") or "") in _AUTO_KINDS),
        key=lambda item: (
            _auto_priority(str(getattr(item[1], "kind", "") or "")),
            -float(getattr(item[1], "updated_at", 0.0) or 0.0),
            item[0],
        ),
    )
    return [topic.name for _, topic in ranked[:limit]]


def _query_terms(query: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z\u0410-\u044f0-9_]{3,}", str(query or "").lower())
        if token not in {"the", "and", "for", "with", "this", "that", "what", "about"}
    }


def _candidate_score(topic: MemoryHeader, terms: set[str]) -> int:
    if not terms:
        return 0
    haystack = f"{topic.name} {topic.description}".replace("_", " ").replace("-", " ").lower()
    return sum(1 for term in terms if term in haystack)


def _selector_candidates(query: str, topics: list[MemoryHeader], remaining_slots: int) -> list[MemoryHeader]:
    if remaining_slots <= 0:
        return []
    terms = _query_terms(query)
    ranked = sorted(
        (h for h in topics if str(getattr(h, "kind", "") or "") in _SELECTOR_KINDS),
        key=lambda h: (
            -_candidate_score(h, terms),
            -float(getattr(h, "updated_at", 0.0) or 0.0),
            h.name,
        ),
    )
    return ranked[: max(_SELECTOR_CANDIDATE_LIMIT, remaining_slots)]


def _fit_budget(text: str, remaining_bytes: int) -> str:
    raw = str(text or "")
    encoded = raw.encode("utf-8")
    if len(encoded) <= remaining_bytes:
        return raw
    note = "\n\n[MEMORY_CONTENT_OMITTED: recalled memory exceeded context budget]\n"
    note_bytes = note.encode("utf-8")
    if remaining_bytes <= len(note_bytes):
        return note_bytes[:remaining_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: remaining_bytes - len(note_bytes)].decode("utf-8", errors="ignore")
    return prefix + note


def _read_facts(store: Memory, names: list[str]) -> tuple[list[tuple[str, float, float]], list[str]]:
    results: list[tuple[str, float, float]] = []
    included_names: list[str] = []
    remaining = _RECALLED_CONTENT_BUDGET_BYTES
    for name in names:
        if remaining <= 0:
            break
        try:
            content, first_recorded_at, updated_at = store.read(name)
            fitted = _fit_budget(content, remaining)
            if not fitted:
                continue
            remaining -= len(fitted.encode("utf-8"))
            results.append((fitted, first_recorded_at, updated_at))
            included_names.append(name)
        except Exception:
            log.warning("Failed to read recalled memory %s", name, exc_info=True)
    return results, included_names


def _llm_select(
    query: str,
    manifest: str,
    llm: Any,
    max_results: int,
    *,
    event_queue: Any = None,
    task_id: str = "",
) -> list[str] | None:
    """Ask the main model which memory files are relevant.

    Returns None for selector failures so callers can use deterministic
    fallback; returns [] for a valid "nothing relevant" decision.
    """
    try:
        from ouroboros.app.config import get_main_model

        model = get_main_model()
        if not model:
            return None
        system = _SELECT_SYSTEM.format(
            max_results=max_results,
            selector_kinds=" and ".join(f'"{kind}"' for kind in sorted(_SELECTOR_KINDS)),
        )
        prompt = f"Query: {query[:600]}\n\nAvailable memories:\n{manifest}"
        msg, usage = llm.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            model=model,
            max_tokens=256,
            temperature=0.0,
            no_proxy=True,
            timeout_sec=_RECALL_TIMEOUT_SEC,
        )
        if usage:
            _record_recall_usage(event_queue, task_id, model, usage)
        text = str(msg.get("content") or "").strip()
        match = re.search(r'\{[^{}]*"selected"[^{}]*\}', text, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group())
        names = data.get("selected", [])
        if not isinstance(names, list):
            return None
        return [str(n).strip().removesuffix(".md") for n in names if isinstance(n, str) and n.strip()]
    except Exception:
        log.debug("Memory recall LLM selection failed", exc_info=True)
    return None


def _record_recall_usage(event_queue: Any, task_id: str, model: str, usage: dict[str, Any]) -> None:
    if event_queue is not None:
        try:
            from ouroboros.observability.pricing import emit_llm_usage_event

            event_model = str(usage.get("resolved_model") or model)
            provider = str(usage.get("provider") or "").strip() or None
            emit_llm_usage_event(
                event_queue,
                str(task_id or ""),
                event_model,
                usage,
                category="memory_recall",
                provider=provider,
                source="memory_recall",
            )
            return
        except Exception:
            log.debug("Failed to emit memory recall usage event", exc_info=True)
    try:
        from supervisor.state import update_usage_from_llm

        update_usage_from_llm(usage)
    except Exception:
        log.debug("Failed to record memory recall usage", exc_info=True)
