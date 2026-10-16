"""Сравнение двух опорных записей (F5 (б), C1.2; ADR-0005).

Обе записи нормализуются (`golden_normalize.py`), затем сверяются: документы, реплики, события
каждого хода, текст ответа, файлы-результаты, снимок. Результат — равны / список различий
«ход · событие · поле · было → стало».

Разрешённые различия — со ссылкой на ADR, равенства не ломают:
- `thinking` пропал, нарезка `text_delta` изменилась — ADR-0015;
- XLS/DOC на новой системе отклоняются при загрузке — ADR-0026.

Файлы-результаты сверяются по имени, числу и статусу скачивания; байты — только у csv/md/txt
и только в режиме `exact`. Байты xlsx и pdf содержат время создания, json — случайные id
результата; их содержимое проверяют шаги 18 и 20.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Literal

from golden_format import DocumentRef, FileRef, Recording, TurnRecord
from golden_normalize import IdLabels, JsonObject, normalize_turn, scrub

TextMode = Literal["exact", "presence"]
ABSENT = "<нет>"
PRESENT_TEXT = "<текст>"
# ADR-0026: старые форматы Office принимает только старая система.
REFUSED_FORMATS = (".xls", ".doc")
REFUSED_FROM_STATUS = 400
# Без времени создания и случайных id: другие байты — другой файл у пользователя. Текст в них
# пишет и модель, поэтому сверяются, как и ответ, только в режиме `exact`.
STABLE_BYTES_FORMATS = (".csv", ".md", ".txt")


@dataclass(frozen=True)
class Difference:
    where: str  # «ход 2 · событие 5», «документ 1», «снимок»
    event_type: str
    field: str
    was: Any
    became: Any
    adr: str = ""  # номер ADR у разрешённого различия

    def line(self) -> str:
        field = f" · поле {self.field}" if self.field else ""
        adr = f" [{self.adr}]" if self.adr else ""
        return (
            f"{self.where} ({self.event_type}){field}: "
            f"было {_show(self.was)} → стало {_show(self.became)}{adr}"
        )


@dataclass(frozen=True)
class Comparison:
    differences: tuple[Difference, ...]
    allowed: tuple[Difference, ...]
    turns: int
    events: int

    @property
    def equal(self) -> bool:
        return not self.differences

    def report(self) -> str:
        if self.equal:
            lines = [f"равны: {self.turns} ход(ов), {self.events} событий после нормализации"]
        else:
            lines = [f"РАЗЛИЧИЯ ({len(self.differences)}):"]
            lines += [f"  {diff.line()}" for diff in self.differences]
        if self.allowed:
            lines.append(f"разрешённые различия ({len(self.allowed)}):")
            lines += [f"  {diff.line()}" for diff in self.allowed]
        return "\n".join(lines)


def compare(first: Recording, second: Recording, *, text: TextMode = "exact") -> Comparison:
    """`first` — опора (было), `second` — проверяемая запись (стало)."""
    differences: list[Difference] = []
    allowed: list[Difference] = []
    _compare_documents(first.documents, second.documents, differences, allowed)
    labels = (IdLabels(), IdLabels())
    events = 0
    if len(first.turns) != len(second.turns):
        differences.append(
            Difference("запись", "turns", "число ходов", len(first.turns), len(second.turns))
        )
    for number, (one, two) in enumerate(zip(first.turns, second.turns, strict=False), start=1):
        events += _compare_turn(number, one, two, labels, text, differences, allowed)
    snapshots = (scrub(first.snapshot, labels[0]), scrub(second.snapshot, labels[1]))
    for path, was, became in _field_diffs(snapshots[0], snapshots[1], ""):
        differences.append(Difference("снимок", "snapshot", path, was, became))
    return Comparison(tuple(differences), tuple(allowed), len(first.turns), events)


def _compare_turn(
    number: int,
    one: TurnRecord,
    two: TurnRecord,
    labels: tuple[IdLabels, IdLabels],
    text: TextMode,
    differences: list[Difference],
    allowed: list[Difference],
) -> int:
    where = f"ход {number}"
    if one.user != two.user:
        differences.append(Difference(where, "user", "text", one.user, two.user))
    left = normalize_turn(one.events, labels[0])
    right = normalize_turn(two.events, labels[1])
    thinking = (left.dropped.get("drop_thinking", 0), right.dropped.get("drop_thinking", 0))
    if thinking[0] != thinking[1]:
        allowed.append(Difference(where, "thinking", "число событий", *thinking, adr="ADR-0015"))
    if left.text_delta_count != right.text_delta_count:
        allowed.append(
            Difference(
                where,
                "text_delta",
                "число дельт",
                left.text_delta_count,
                right.text_delta_count,
                adr="ADR-0015",
            )
        )
    events = (_apply_text_mode(left.events, text), _apply_text_mode(right.events, text))
    differences.extend(_compare_events(where, events[0], events[1]))
    answers = (_answer(one.answer, text), _answer(two.answer, text))
    if answers[0] != answers[1]:
        differences.append(Difference(where, "answer", "text", answers[0], answers[1]))
    differences.extend(_compare_files(where, one.files, two.files, text))
    return len(left.events)


def _apply_text_mode(events: Sequence[JsonObject], text: TextMode) -> list[JsonObject]:
    if text == "exact":
        return list(events)
    return [
        {**event, "text": PRESENT_TEXT if event.get("text") else ""}
        if event.get("type") == "text_delta"
        else event
        for event in events
    ]


def _answer(answer: str, text: TextMode) -> str:
    if text == "exact":
        return answer
    return PRESENT_TEXT if answer.strip() else ""


def _compare_events(
    where: str, one: Sequence[JsonObject], two: Sequence[JsonObject]
) -> list[Difference]:
    found: list[Difference] = []
    matcher = SequenceMatcher(None, [_key(e) for e in one], [_key(e) for e in two], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        paired = min(i2 - i1, j2 - j1) if tag == "replace" else 0
        for offset in range(paired):
            found.extend(_event_pair(where, i1 + offset, one[i1 + offset], two[j1 + offset]))
        for index in range(i1 + paired, i2):
            found.append(_one_sided(where, index, one[index], missing_in_second=True))
        for index in range(j1 + paired, j2):
            found.append(_one_sided(where, index, two[index], missing_in_second=False))
    return found


def _event_pair(where: str, index: int, was: JsonObject, became: JsonObject) -> list[Difference]:
    place = f"{where} · событие {index + 1}"
    if was.get("type") != became.get("type"):
        return [
            _one_sided(where, index, was, missing_in_second=True),
            _one_sided(where, index, became, missing_in_second=False),
        ]
    return [
        Difference(place, str(was.get("type")), path, old, new)
        for path, old, new in _field_diffs(was, became, "")
    ]


def _one_sided(where: str, index: int, event: JsonObject, *, missing_in_second: bool) -> Difference:
    place = f"{where} · событие {index + 1}"
    kind = str(event.get("type"))
    if missing_in_second:
        return Difference(place, kind, "", event, None)
    return Difference(place, kind, "", None, event)


def _key(event: JsonObject) -> str:
    return json.dumps(event, sort_keys=True, ensure_ascii=False)


def _field_diffs(was: Any, became: Any, path: str) -> list[tuple[str, Any, Any]]:
    if isinstance(was, dict) and isinstance(became, dict):
        found: list[tuple[str, Any, Any]] = []
        for key in sorted(set(was) | set(became)):
            child = f"{path}.{key}" if path else str(key)
            found += _field_diffs(was.get(key, ABSENT), became.get(key, ABSENT), child)
        return found
    if isinstance(was, list) and isinstance(became, list):
        found = []
        for index in range(max(len(was), len(became))):
            child = f"{path}[{index}]"
            found += _field_diffs(
                was[index] if index < len(was) else ABSENT,
                became[index] if index < len(became) else ABSENT,
                child,
            )
        return found
    return [] if was == became else [(path, was, became)]


def _compare_files(
    where: str, one: Sequence[FileRef], two: Sequence[FileRef], text: TextMode
) -> list[Difference]:
    by_bytes = text == "exact"
    first, second = _by_name(one), _by_name(two)
    found: list[Difference] = []
    for name in sorted(set(first) | set(second)):
        was, became = first.get(name, []), second.get(name, [])
        if len(was) != len(became):
            found.append(Difference(where, "file", name, _count(len(was)), _count(len(became))))
            continue
        for old, new in zip(was, became, strict=True):
            if old.note != new.note:
                found.append(Difference(where, "file", f"{name} · скачивание", old.note, new.note))
            elif (
                by_bytes
                and name.lower().endswith(STABLE_BYTES_FORMATS)
                and old.sha256 != new.sha256
            ):
                found.append(
                    Difference(where, "file", f"{name} · содержимое", old.sha256, new.sha256)
                )
    return found


def _by_name(files: Sequence[FileRef]) -> dict[str, list[FileRef]]:
    grouped: dict[str, list[FileRef]] = {}
    for ref in files:
        grouped.setdefault(ref.name, []).append(ref)
    return grouped


def _count(number: int) -> str | None:
    return None if number == 0 else "есть" if number == 1 else f"{number} шт."


def _compare_documents(
    one: Sequence[DocumentRef],
    two: Sequence[DocumentRef],
    differences: list[Difference],
    allowed: list[Difference],
) -> None:
    if len(one) != len(two):
        differences.append(
            Difference("запись", "documents", "число документов", len(one), len(two))
        )
    for number, (was, became) in enumerate(zip(one, two, strict=False), start=1):
        where = f"документ {number} ({was.name})"
        for field in ("name", "sha256", "size"):
            old, new = getattr(was, field), getattr(became, field)
            if old != new:
                differences.append(Difference(where, "document", field, old, new))
        refusal = was.name.lower().endswith(REFUSED_FORMATS) and (
            max(was.upload_status, became.upload_status) >= REFUSED_FROM_STATUS
        )
        if refusal and was.upload_status != became.upload_status:
            allowed.append(
                Difference(
                    where,
                    "document",
                    "upload_status",
                    was.upload_status,
                    became.upload_status,
                    adr="ADR-0026",
                )
            )
        elif was.upload_status != became.upload_status:
            differences.append(
                Difference(
                    where, "document", "upload_status", was.upload_status, became.upload_status
                )
            )
        elif was.upload_detail != became.upload_detail:
            differences.append(
                Difference(
                    where, "document", "upload_detail", was.upload_detail, became.upload_detail
                )
            )


def _show(value: Any) -> str:
    if value is None:
        return "нет"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return repr(value)
