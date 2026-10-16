"""Нормализатор потока событий хода (F5 (б), C1.2; ADR-0005, ADR-0015).

Приводит сырой поток `GET /chat/runs/{id}/events` к тому, что видит экран: два прогона одного
сценария после нормализации равны, а различие, которое пользователь заметит, остаётся.

Что экрану важно, выведено из `aifa/web/src/lib/api.ts` (`unwrapRunEvents`),
`aifa/web/src/lib/projects.ts` (`applyChatEvent`) и `use-chat-run.ts`. Каждое правило названо в
`RULES` с причиной; счётчики `dropped` и `text_delta_count` нужны сравнению, чтобы показать
«разрешённые различия» с номером ADR (`golden_compare.py`).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class Rule:
    name: str
    why: str
    adr: str = ""  # номер ADR, если правило — разрешённое различие


def _rules(*rules: Rule) -> dict[str, Rule]:
    return {rule.name: rule for rule in rules}


RULES: dict[str, Rule] = _rules(
    Rule(
        "strip_envelope",
        "run_id, event_id, occurred_at, sequence, schema_version — паспорт доставки: у каждого "
        "прогона свои; экран разворачивает конверт в событие (`unwrapRunEvents`) и их не читает.",
    ),
    Rule(
        "drop_heartbeat",
        "Биение каждые 15 с — не запись журнала: число кадров зависит от длительности хода, "
        "экран глотает его до всякой обработки (`use-chat-run.ts`: ping → continue).",
    ),
    Rule(
        "drop_thinking",
        "Строка «размышляю» допускается отсутствующей; экран берёт из неё только подпись "
        "состояния, ответ и лента от неё не зависят.",
        adr="ADR-0015",
    ),
    Rule(
        "drop_segment_end",
        "Экран не режет текст по этому событию (`applyChatEvent`: text_segment_end → msg): "
        "на живых прогонах оно приходит ненадёжно, границу прозы даёт вызов инструмента.",
    ),
    Rule(
        "merge_text_delta",
        "Экран склеивает дельты (`content + ev.text`): нарезка не видна, а новая служба шлёт "
        "ответ одной дельтой. Порядок текста относительно вызовов инструментов — сохраняется.",
        adr="ADR-0015",
    ),
    Rule(
        "fold_tool_output",
        "Экран закрывает последний открытый такт с тем же именем (`tool_output`): порядок "
        "результатов разных инструментов не виден, виден итог такта — `ok` и причина.",
    ),
    Rule(
        "collapse_progress",
        "`tool_progress` — только строка состояния; число тиков зависит от длительности, а "
        "числа в тексте — это секунды и счётчики.",
    ),
    Rule(
        "sort_independent",
        "Состояния разных узлов (`node_run`) и шагов разных линий (`plan_step`) экран держит "
        "по ключу — порядок между ключами не виден; внутри одного ключа порядок сохраняется.",
    ),
    Rule(
        "strip_time_fields",
        "Времена и длительности (`*_at`, `elapsed_*`, `duration_*`) отличаются в каждом "
        "прогоне и не относятся к содержимому ответа.",
    ),
    Rule(
        "mask_usage_numbers",
        "Токены и стоимость хода меняются от прогона к прогону; важно, что событие пришло и "
        "какая модель.",
    ),
    Rule(
        "label_identifiers",
        "Идентификаторы (UUID, отпечатки узлов и графов) случайны или зависят от них; "
        "заменяются порядковыми метками по первому появлению, поэтому подмена ссылки "
        "(одно значение вместо двух разных) остаётся различием.",
    ),
)

# Состояния, о порядке которых экран не знает: окно подряд идущих таких событий сортируется.
INDEPENDENT_TYPES = frozenset({"node_run", "plan_step"})
ENVELOPE_META = frozenset({"schema_version", "event_id", "run_id", "sequence", "occurred_at"})
ID_KEYS = frozenset(
    {
        "run_id",
        "session_id",
        "file_id",
        "doc_id",
        "nonce",
        "node_run_id",
        "metric_id",
        "evidence_id",
        "source_ref",
        "output_id",
        "args_fingerprint",
        "graph_hash",
        "plan_graph_hash",
    }
)
TIME_KEYS = frozenset({"elapsed_ms", "elapsed_s", "duration_ms", "duration_s", "latency_ms"})
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
DIGITS_RE = re.compile(r"\d+")
TOOL_OUTPUT_FIELDS = ("ok", "status", "reason")


class IdLabels:
    """Порядковые метки идентификаторов: одно значение — одна метка в пределах записи."""

    def __init__(self) -> None:
        self._labels: dict[str, str] = {}

    def label(self, value: str) -> str:
        if value not in self._labels:
            self._labels[value] = f"<id:{len(self._labels) + 1}>"
        return self._labels[value]


@dataclass(frozen=True)
class NormalizedTurn:
    events: tuple[JsonObject, ...]
    dropped: Mapping[str, int] = field(default_factory=dict)
    text_delta_count: int = 0  # дельт до склейки: нарезка — разрешённое различие (ADR-0015)


def normalize_turn(
    raw_events: Sequence[Mapping[str, Any]], labels: IdLabels | None = None
) -> NormalizedTurn:
    labels = labels if labels is not None else IdLabels()
    dropped: Counter[str] = Counter()
    screen = _screen_events(raw_events, dropped)
    text_delta_count = sum(1 for event in screen if event.get("type") == "text_delta")
    merged = _merge_text(screen)
    folded = _fold_tool_outputs(merged)
    collapsed = _collapse_progress(folded)
    ordered = _sort_independent(collapsed)
    cleaned = tuple(_clean_event(event, labels) for event in ordered)
    return NormalizedTurn(cleaned, dict(dropped), text_delta_count)


def _screen_events(raw: Sequence[Mapping[str, Any]], dropped: Counter[str]) -> list[JsonObject]:
    """Конверты → события экрана (`unwrapRunEvents`); служебные отбрасываются со счётом."""
    events: list[JsonObject] = []
    for item in raw:
        event = unwrap_event(item)
        if event is None:
            dropped["drop_heartbeat"] += 1
        elif event.get("type") == "thinking":
            dropped["drop_thinking"] += 1
        elif event.get("type") == "ping":
            dropped["drop_heartbeat"] += 1
        elif event.get("type") == "text_segment_end":
            dropped["drop_segment_end"] += 1
        else:
            events.append(event)
    return events


def unwrap_event(item: Mapping[str, Any]) -> JsonObject | None:
    kind = item.get("event_type")
    if kind is None:
        return dict(item)  # уже событие экрана (поток `/stream` или синтетика)
    if kind == "heartbeat":
        return None
    if kind == "legacy":
        return dict(item["legacy_event"])
    if kind == "result":
        return {"type": "typed_result", "result": item["result"], "status": item["delivery_status"]}
    if kind == "node_run":
        return {
            "type": "node_run",
            "plan_graph_hash": item["plan_graph_hash"],
            "node_run": item["node_run"],
        }
    if kind == "error":
        return {
            "type": "typed_error",
            "error": item["error"],
            "partial_results": item.get("partial_results", []),
        }
    if kind == "completed":
        return {"type": "done", "status": item["status"]}
    rest = {k: v for k, v in item.items() if k not in ENVELOPE_META and k != "event_type"}
    return {"type": f"envelope:{kind}", **rest}  # новый тип конверта виден в сравнении


def _merge_text(events: list[JsonObject]) -> list[JsonObject]:
    merged: list[JsonObject] = []
    for event in events:
        if event.get("type") == "text_delta" and merged and merged[-1].get("type") == "text_delta":
            merged[-1] = {**merged[-1], "text": merged[-1]["text"] + event.get("text", "")}
        else:
            merged.append(event)
    return merged


def _fold_tool_outputs(events: list[JsonObject]) -> list[JsonObject]:
    folded: list[JsonObject] = []
    for event in events:
        if event.get("type") == "tool_output":
            call = _open_call(folded, event.get("name"))
            if call is not None:
                folded[call] = {
                    **folded[call],
                    "output": {k: event[k] for k in TOOL_OUTPUT_FIELDS if k in event},
                }
                continue
        folded.append(event)
    return folded


def _open_call(events: list[JsonObject], name: object) -> int | None:
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if event.get("type") == "tool_call" and event.get("name") == name and "output" not in event:
            return index
    return None


def _collapse_progress(events: list[JsonObject]) -> list[JsonObject]:
    collapsed: list[JsonObject] = []
    for event in events:
        if event.get("type") == "tool_progress":
            event = {**event, "text": DIGITS_RE.sub("<n>", str(event.get("text", "")))}
            if collapsed and collapsed[-1] == event:
                continue
        collapsed.append(event)
    return collapsed


def _sort_independent(events: list[JsonObject]) -> list[JsonObject]:
    ordered: list[JsonObject] = []
    window: list[JsonObject] = []
    for event in events:
        if event.get("type") in INDEPENDENT_TYPES:
            window.append(event)
            continue
        ordered.extend(sorted(window, key=_independent_key))  # sorted устойчива: порядок ключа цел
        window = []
        ordered.append(event)
    ordered.extend(sorted(window, key=_independent_key))
    return ordered


def _independent_key(event: JsonObject) -> tuple[str, str, str]:
    if event["type"] == "node_run":
        node = event.get("node_run", {}).get("node_key", {})
        return ("node_run", str(node.get("atom_name", "")), str(node.get("atom_version", "")))
    return ("plan_step", str(event.get("name") or "ход"), str(event.get("index", "")))


def _clean_event(event: JsonObject, labels: IdLabels) -> JsonObject:
    if event.get("type") == "usage":
        event = _mask_numbers(event)
    cleaned = _clean(event, "", labels)
    assert isinstance(cleaned, dict)
    return cleaned


def _clean(value: Any, key: str, labels: IdLabels) -> Any:
    if isinstance(value, dict):
        return {
            k: _clean(v, k, labels)
            for k, v in value.items()
            if not (k.endswith("_at") or k in TIME_KEYS)
        }
    if isinstance(value, list):
        return [_clean(item, key, labels) for item in value]
    if isinstance(value, str):
        if key in ID_KEYS:
            return labels.label(value)
        return UUID_RE.sub(lambda match: labels.label(match.group(0)), value)
    return value


def _mask_numbers(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _mask_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_numbers(item) for item in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "<n>"
    return value


def scrub(value: Any, labels: IdLabels | None = None) -> Any:
    """Те же правила идентификаторов и времени — для снимка артефактов, где нет событий."""
    return _clean(value, "", labels if labels is not None else IdLabels())
