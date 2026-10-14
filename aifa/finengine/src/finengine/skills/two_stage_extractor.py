"""Experimental two-stage full-context extractor.

The production registry extractor asks the LLM to extract values directly from
the full report. This module keeps the same public event contract but splits
the LLM work into:

1. context compression: choose likely table ids and useful exact quotes per
   metric;
2. value extraction from that structured evidence.

The implementation deliberately reuses the existing bulk_extractor
normalizers, calculated-metric evaluator and segment event shape so benchmark
comparisons stay apples-to-apples.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional

from finengine.config import settings
from finengine.llm import openrouter as llm
from finengine.llm import station_bridge as _cache
from finengine.skills import bulk_extractor as bulk
from finengine.skills.finengine2_prompts import (
    build_compression_prompt as _build_finengine2_compression_prompt,
    build_extraction_prompt as _build_finengine2_extraction_prompt,
    build_segment_compression_prompt as _build_finengine2_segment_compression_prompt,
    build_segment_extraction_prompt as _build_finengine2_segment_extraction_prompt,
    build_system_prompt as _build_finengine2_system_prompt,
    prompt_profile_for_cell,
)
from finengine.skills.periods import FiscalPeriodContext, POINT_KIND, sort_periods
from finengine.skills.metric_schema import merge_aliases


# 🔴 ПИНОВ МОДЕЛЕЙ ЗДЕСЬ БОЛЬШЕ НЕТ (07.09.2026, решение владельца). До этого
# стояли `qwen/qwen3.6-plus-04-02` (стадия 1) и `deepseek/deepseek-v4-pro`
# (стадия 2) — скопированы из Finengine 16.08 ради бенча «сравнивать схемы на
# одинаковых моделях». Бенч закончился 17.08, а пины остались и молча вели
# боевое извлечение мимо `settings.openrouter_model`: на dev 03.09 все 23
# реальных вызова ушли на OpenRouter, хотя стенд считался переведённым на
# Cloud.ru. Теперь обе стадии берут модель из настройки; явный аргумент
# вызова по-прежнему сильнее. Сторож: test_extractor_has_no_model_pins.


# Finengine 2-only aliases learned from Stage-1 evidence misses. Keep these
# local: the shared registry is also consumed by other extractor architectures.
_FINENGINE2_ALIAS_OVERRIDES: dict[str, tuple[str, ...]] = {
    "CAPITALIZED_INTEREST": ("За вычетом сумм, включенных в состав квалифицируемых активов",),
    "CASH_AND_CASH_BALANCES_WITH_CB": ("Cash and balances at central banks",),
    "CET1_RATIO_DISCLOSED": ("Показатель достаточности базового капитала",),
    "CHANGE_IN_EXPECTED_CREDIT_LOSSES": (
        "Net allowances to impairment",
        "Создание резерва под ожидаемые кредитные убытки",
    ),
    "CLIENT_DEPOSITS": ("Deposits from customers",),
    "COMMON_EQUITY_TIER_ONE_CAPITAL": ("Базовый капитал",),
    "GOVERNMENTAL_DEPOSITS": ("Государственные и общественные организации",),
    "INDIVIDUALS_DEPOSITS": ("Средства частных клиентов",),
    "INSURANCE_LIABILITIES": ("Assets and liabilities related to insurance contracts",),
    "INTERBANK_DEPOSITS": ("Срочные средства, размещенные в банках",),
    "INTEREST_PAID_LEASE_LIABILITIES": ("Interest on lease liabilities",),
    "NET_INCOME": ("Прибыль за период", "Прибыль за год", "Прибыль за квартал"),
    "NET_INTEREST_INCOME": ("Чистые процентные доходы",),
    "SHARE_CAPITAL": ("Уставный капитал",),
    "TIER1_RATIO_DISCLOSED": ("Показатель достаточности основного капитала",),
}

_RELAXED_ALIAS_STOPWORDS = frozenset(
    {
        "a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with",
        "в", "во", "для", "и", "из", "или", "к", "на", "над", "от", "по", "под", "с", "за",
    }
)


@dataclass(frozen=True)
class ReportTable:
    table_id: str
    page: int | None
    title: str
    text: str
    context_before: str = ""
    context_after: str = ""
    ocr_table_index: int | None = None


def _write_debug_file(debug_dir: Path | None, name: str, content: object) -> None:
    """Write a mandatory Finengine 2 replay artifact without hiding failures."""
    if debug_dir is None:
        return
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / name
    text = content if isinstance(content, str) else _json_dump(content)
    path.write_text(text, encoding="utf-8")


def _as_int(value: object, default: int = 1) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except Exception:
        return default
    return max(1, parsed)


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _json_loads_object(value: str) -> dict[str, Any]:
    parsed = bulk._parse_extract_json(value) or {}
    return parsed if isinstance(parsed, dict) else {}


def _retry_enum_order(
    enum_batch: list[str],
    *,
    document: str,
    batch_no: int,
    attempt: int,
) -> list[str]:
    """Change only enum order on retries to avoid deterministic bad outputs."""
    ordered = list(enum_batch)
    if attempt <= 1 or len(ordered) < 2:
        return ordered
    salt = f"{document}|{batch_no}|{attempt}"
    shuffled = sorted(
        ordered,
        key=lambda enum: hashlib.sha256(f"{salt}|{enum}".encode()).hexdigest(),
    )
    return shuffled if shuffled != ordered else ordered[1:] + ordered[:1]


def _validate_stage2_json_object(
    parsed: Any,
    expected_enums: list[str],
) -> tuple[dict[str, Any], str]:
    if not isinstance(parsed, dict):
        return {}, f"invalid_shape: expected JSON object, got {type(parsed).__name__}"
    normalized: dict[str, Any] = {}
    for key, value in parsed.items():
        enum = str(key or "").strip().upper()
        if not enum:
            return {}, "invalid_keys: empty enum key"
        if enum in normalized:
            return {}, f"invalid_keys: duplicate enum after normalization: {enum}"
        normalized[enum] = value
    expected = {
        str(enum or "").strip().upper() for enum in expected_enums if str(enum or "").strip()
    }
    missing = sorted(expected - set(normalized))
    extra = sorted(set(normalized) - expected)
    errors: list[str] = []
    if missing:
        errors.append(f"missing_enums={','.join(missing)}")
    if extra:
        errors.append(f"unexpected_enums={','.join(extra)}")
    return ({}, "; ".join(errors)) if errors else (normalized, "")


def _recover_stage2_json_object(
    raw_response: str,
    expected_enums: list[str],
) -> tuple[dict[str, Any], str, bool]:
    """Accept exact batch JSON, including one complete object around text junk."""
    raw = raw_response or ""
    try:
        parsed = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        decode_error = f"invalid_json: {exc.msg} at char {exc.pos}"
    else:
        validated, error = _validate_stage2_json_object(parsed, expected_enums)
        return validated, error, False

    decoder = json.JSONDecoder()
    expected = {
        str(enum or "").strip().upper() for enum in expected_enums if str(enum or "").strip()
    }
    candidate_errors: list[str] = []
    for start, char in enumerate(raw):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(raw, start)
        except json.JSONDecodeError:
            continue
        validated, error = _validate_stage2_json_object(candidate, expected_enums)
        if not error:
            return validated, "", True
        keys = (
            {str(key or "").strip().upper() for key in candidate}
            if isinstance(candidate, dict)
            else set()
        )
        if keys & expected:
            candidate_errors.append(error)
    return {}, candidate_errors[0] if candidate_errors else decode_error, False


def _validate_segment_stage2_json_object(
    parsed: Any,
    expected_enums: list[str],
) -> tuple[dict[str, Any], str]:
    if not isinstance(parsed, dict) or not isinstance(parsed.get("metrics"), dict):
        return {}, "invalid_shape: expected JSON object with a metrics object"
    metrics = parsed["metrics"]
    normalized: dict[str, Any] = {}
    for key, value in metrics.items():
        enum = str(key or "").strip().upper()
        if not enum or enum in normalized:
            return {}, f"invalid_keys: empty or duplicate enum: {enum or '<empty>'}"
        # Срез 30: `null`/"" вместо массива — ответ модели «сегментной раскладки
        # в отчёте нет» (промежуточные отчёты её не раскрывают). Это пустой
        # результат, а не нарушение формата: иначе узел падал после ретраев и
        # блокировал весь FULL_ANALYSIS.
        rows = value.get("rows") if isinstance(value, dict) else None
        if isinstance(value, dict) and (rows is None or rows == ""):
            rows = []
        if not isinstance(value, dict) or not isinstance(rows, list):
            return {}, f"invalid_shape: {enum} must contain a rows array"
        normalized[enum] = {"rows": rows}
    expected = {
        str(enum or "").strip().upper() for enum in expected_enums if str(enum or "").strip()
    }
    missing = sorted(expected - set(normalized))
    extra = sorted(set(normalized) - expected)
    errors: list[str] = []
    if missing:
        errors.append(f"missing_enums={','.join(missing)}")
    if extra:
        errors.append(f"unexpected_enums={','.join(extra)}")
    return ({}, "; ".join(errors)) if errors else (normalized, "")


def _recover_segment_stage2_json_object(
    raw_response: str,
    expected_enums: list[str],
) -> tuple[dict[str, Any], str, bool]:
    raw = raw_response or ""
    try:
        parsed = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        decode_error = f"invalid_json: {exc.msg} at char {exc.pos}"
    else:
        validated, error = _validate_segment_stage2_json_object(parsed, expected_enums)
        return validated, error, False

    decoder = json.JSONDecoder()
    candidate_errors: list[str] = []
    for start, char in enumerate(raw):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(raw, start)
        except json.JSONDecodeError:
            continue
        validated, error = _validate_segment_stage2_json_object(candidate, expected_enums)
        if not error:
            return validated, "", True
        if isinstance(candidate, dict) and isinstance(candidate.get("metrics"), dict):
            candidate_errors.append(error)
    return {}, candidate_errors[0] if candidate_errors else decode_error, False


def _balanced_batches(
    items: list[str],
    count: int,
    capacity: int,
    weights: dict[str, float],
) -> list[list[str]]:
    """Раскладка по ОЖИДАЕМОМУ ВЕСУ: тяжёлый показатель — в самый лёгкий батч.

    ЗАЧЕМ. Батчи расходятся по времени на порядок: замер стенда 20.08 (ПСБ,
    `ifrs_bank_ru`, пять батчей, темп 4) — 22,2 с у самого лёгкого против
    148,3 с у самого тяжёлого при сумме 369,5 с. Освободившееся место семафора
    занимается сразу, поэтому ступень — это не «пик × число кругов», а
    списочная раскладка на `темп` мест.

    🔴 СКОЛЬКО ЭТО ДАЁТ — ЗАВИСИТ ОТ ЧИСЛА БАТЧЕЙ, И ЗНАК ТОЖЕ. Волн выходит
    `ceil(N/темп)`; ровные батчи дают `ceil(N/темп) × S/N`, предел любого
    деления — `S/темп`. Значит ровные батчи оптимальны ТОЛЬКО там, где их число
    кратно темпу или не больше его. На восьми батчах при темпе 4 — выигрыш
    около 6 процентов (было заявлено 8–9: разбор калибровки ниже); на пяти при
    темпе 4 — ЗАМЕДЛЕНИЕ около 6 процентов: лучшее деление пятёрки нарочно
    неровное (два лёгких по S/8 и три по S/4 укладываются ровно в предел), и
    балансировка его как раз уничтожает. Полный пересчёт с симуляцией — в
    докстроке `tests/test_the_heaviest_batch_sets_the_pace.py`.

    ⚠️ Стоявшее здесь до 21.08 «ровные пятеро дали бы 92,4 с вместо 148,3»
    брало предел `S/темп` за то, что дают ровные батчи. Ровные пятеро дают
    2×S/5 = 147,8 с — полсекунды, а не полторы минуты.

    ЧЕМ ЭТО НЕ ОБЫЧНЫЙ LPT И ПОЧЕМУ. Свободная жадность «тяжёлый в самый
    лёгкий» ломает свойство, ради которого заведена `spread`: соседи по
    реестру уезжают в РАЗНЫЕ батчи, чтобы срыв одного батча дырявил таблицу
    равномерно, а не выедал целый раздел отчёта. Мягкая охрана соседства
    («батч с соседом беру последним») этого НЕ держит: к концу раскладки
    свободен один батч, и выбора у последних показателей нет — на случайных
    весах свойство ломалось в 42 прогонах из 186 при трёх батчах. Поэтому
    раскладка идёт по БЛОКАМ реестра.

    КАК. Реестр режется на подряд идущие блоки по `count` показателей, и
    каждый блок раздаёт РОВНО ПО ОДНОМУ показателю в каждый батч. Отсюда сразу
    три свойства, и все три — по построению, а не по везению на удобных весах:
    * соседи внутри блока всегда в разных батчах;
    * соседи через границу блоков — тоже: голове блока запрещён батч, куда ушёл
      хвост предыдущего (`forbidden`), а когда свободных мест остаётся два и
      одно из них запрещённое, туда сажают текущий показатель — иначе голове
      досталось бы единственное запрещённое место;
    * в батче не больше `capacity` показателей: блоков `ceil(len/count)`, а
      `count = ceil(len/capacity)`, значит блоков не больше `capacity`.
    Свобода, которая при этом остаётся, — КОМУ из батчей достанется какой
    показатель блока, — и тратится на вес: внутри блока тяжёлый садится в самый
    лёгкий на эту минуту батч.

    ЧЕМ ПЛАТИМ. Раскладка жадная и блочная, то есть не оптимальная: свободный
    LPT кладёт пиковый батч заметно ниже, но ломает соседство. На случайных
    весах (3000 раскладок, вес логнормальный σ=1,5) пиковый батч выходит 0,79
    от `spread` при восьми батчах, 0,81 при пяти, 0,85 при трёх. В части
    раскладок он оказывался ТЯЖЕЛЕЕ, чем при делении по порядку (9–14 % точек),
    обычно на проценты: жадность иногда проигрывает случайности.
    ⚠️ Эти числа — про ПИКОВЫЙ БАТЧ, и ступенью он управляет только в одну
    волну; в двух её задаёт сумма двух батчей на занятом месте, поэтому
    выигрыш ступени меньше падения пика вдвое и больше.

    🔴 ЭТИ ЧИСЛА ОТКАЛИБРОВАНЫ НА РАЗБРОСЕ, КОТОРОГО НЫНЕШНЯЯ МЕРА НЕ ДАЁТ —
    И ОБЕЩАТЬ ИХ ВЛАДЕЛЬЦУ НЕЛЬЗЯ. Логнормаль σ=1,5 — это CV весов около 2,1,
    калибровка под ПРЕЖНЮЮ меру (длина карточки в символах, разброс в сотни
    раз). Мера с 21.08 имеет форму `1 + Σ колонок + цитаты`, то есть единицы, а
    не сотни: на Северстали Q1-2025 у 44 показателей из 47 ровно по два периода
    (посчитано по golden), значит колонок у всякой улики около двух, и весь
    разброс создают число улик и цитат. Пересчёт теми же 3000 раскладок на
    формах этой меры (число улик 1…3 и 1…6, колонок 2 и 2…12):

        разброс весов          CV     пик bal/spread   ступень bal/spread
        логнормаль σ=1,5      2,1          0,79              0,92
        форма `1 + Σкол + Q`  0,3…0,45     0,86…0,90         0,93…0,95

    То есть на восьми батчах при темпе 4 ждать надо около 6 процентов ступени,
    а не 8–9, а на ПЯТИ при темпе 4 — ЗАМЕДЛЕНИЯ на те же 6 (1,06 в среднем,
    хуже `spread` в 83 % раскладок против 46 % на логнормали). Форма стенда —
    как раз пять батчей при темпе 4.
    Ровно поэтому стратегия — настройка, а не константа:
    `FINENGINE_EXTRACT_BATCH_STRATEGY=spread` возвращает прежнее деление одной
    переменной окружения, без пересборки.

    При РАВНЫХ весах раскладка вырождается в `spread` побайтово: порядок обхода
    блока сводится к порядку реестра, все батчи пусты, и блок раздаётся по
    кругу. То есть `balanced` — не «другая» стратегия, а надстройка над прежней
    там, где веса различимы.
    """
    place = {item: idx for idx, item in enumerate(items)}
    batches: list[list[str]] = [[] for _ in range(count)]
    load = [0.0] * count
    forbidden = -1
    for start in range(0, len(items), count):
        block = items[start : start + count]
        head, tail = block[0], block[-1]
        free = set(range(count))
        head_seated = False
        tail_seat = -1
        for item in sorted(block, key=lambda name: (-weights.get(name, 0.0), place[name])):
            if count < 2:
                allowed = free
            elif item == head:
                allowed = free - {forbidden}
            elif not head_seated and len(free) == 2 and forbidden in free:
                # Иначе голова блока осталась бы с единственным местом, и это
                # место — запрещённое: сосед через границу блоков слипся бы.
                allowed = {forbidden}
            else:
                allowed = free
            seat = min(allowed, key=lambda idx: (load[idx], idx))
            free.discard(seat)
            batches[seat].append(item)
            load[seat] += weights.get(item, 0.0)
            head_seated = head_seated or item == head
            if item == tail:
                tail_seat = seat
        # Запрет снимается ТОЛЬКО после блока: обнови `forbidden` внутри цикла —
        # и голова блока, если она тяжелее хвоста и села позже, получила бы
        # запрет на СВОЙ ЖЕ блок вместо предыдущего.
        forbidden = tail_seat
    return [batch for batch in batches if batch]


def _batch_items(
    items: list[str],
    batch_size: int,
    *,
    strategy: str = "spread",
    weights: dict[str, float] | None = None,
) -> list[list[str]]:
    size = max(1, int(batch_size or 1))
    if not items:
        return []
    if strategy == "contiguous":
        return [items[idx : idx + size] for idx in range(0, len(items), size)]
    count = (len(items) + size - 1) // size
    if strategy == "balanced":
        # Молчаливое вырождение в `spread` здесь было бы худшим исходом:
        # настройка стоит на «balanced», замер показывает прежнюю неровность,
        # и искать причину пришлось бы в модели, а не в забытом аргументе.
        if weights is None:
            raise ValueError("Balanced batch strategy needs weights")
        return _balanced_batches(items, count, size, weights)
    if strategy != "spread":
        raise ValueError(f"Unknown batch strategy: {strategy}")
    batches: list[list[str]] = [[] for _ in range(count)]
    for idx, item in enumerate(items):
        batches[idx % count].append(item)
    return [batch for batch in batches if batch]



def _progress_nonce() -> str:
    """Нонс хода — пропуск комнаты к бэкенду. Пусто вне станции: кэша нет."""
    import os as _os

    return _os.environ.get("FE_PROGRESS_NONCE", "").strip()


def _prompt_fingerprint(prompt: str, system: str | None) -> str:
    """Отпечаток ВСЕГО вопроса к модели — ключ кэша первой стадии.

    Берём и системную часть: правила извлечения живут там, и правка правил
    обязана обесценить кэш. Отпечаток по документу этого не умеет — он отдал бы
    вчерашний ответ на сегодняшний вопрос, и молча.
    """
    import hashlib

    h = hashlib.sha256()
    h.update((system or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((prompt or "").encode("utf-8"))
    return h.hexdigest()


def _has_stage1_evidence(compression: dict[str, Any], enum: str) -> bool:
    item = _compression_payload_for_prompt(compression).get(str(enum or "").strip().upper())
    if not isinstance(item, dict):
        return False
    return bool(item.get("tables") or item.get("quotes") or item.get("text_quotes"))


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _is_page_marker(line: str) -> bool:
    return bool(re.match(r"\s*===PAGE\s+\d+===\s*", line))


def _nearby_non_table_context_lines(
    lines: list[str],
    start_idx: int,
    *,
    step: int,
    count: int = 2,
) -> str:
    """Return nearby non-table lines without crossing page markers or tables."""
    idx = start_idx
    selected: list[str] = []
    while 0 <= idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        if not stripped:
            if selected:
                break
            idx += step
            continue
        if _is_page_marker(line) or _is_table_line(line):
            break
        selected.append(stripped)
        if len(selected) >= count:
            break
        idx += step
    if step < 0:
        selected.reverse()
    return "\n".join(selected)


def _extract_tables_from_text(text: str, *, max_tables: int = 220) -> list[ReportTable]:
    """Find markdown-like tables in the OCR text and assign stable ids."""
    tables: list[ReportTable] = []
    page = None
    current_heading: list[str] = []
    lines = (text or "").splitlines()
    idx = 0
    while idx < len(lines):
        page_match = re.match(r"\s*===PAGE\s+(\d+)===\s*", lines[idx])
        if page_match:
            try:
                page = int(page_match.group(1))
            except Exception:
                page = None
            current_heading = []
            idx += 1
            continue
        line = lines[idx]
        stripped = line.strip()
        if _is_table_line(line):
            start = idx
            while idx < len(lines):
                if not _is_table_line(lines[idx]):
                    break
                idx += 1
            body = "\n".join(lines[start:idx]).strip()
            if len(body) >= 60:
                title = " ".join(current_heading[-4:])[:300]
                context_before = _nearby_non_table_context_lines(lines, start - 1, step=-1, count=2)
                context_after = _nearby_non_table_context_lines(lines, idx, step=1, count=2)
                tables.append(
                    ReportTable(
                        table_id=f"T{len(tables) + 1:03d}",
                        page=page,
                        title=title,
                        text=body,
                        context_before=context_before,
                        context_after=context_after,
                        ocr_table_index=None,
                    )
                )
                if len(tables) >= max_tables:
                    break
            continue
        if stripped and not stripped.startswith("#"):
            current_heading.append(stripped)
            current_heading = current_heading[-6:]
        idx += 1
    return tables


def _read_ocr_payload(сырьё: bytes | None) -> dict[str, Any] | None:
    """Полезная часть сохранённого ответа OCR. Едет БАЙТАМИ, а не путём:
    сайдкар живёт в хранилище объектов, и пути у него не всегда есть."""
    if not сырьё:
        return None
    try:
        data = json.loads(сырьё.decode("utf-8"))
    except UnicodeDecodeError:
        data = json.loads(сырьё.decode("utf-8-sig"))
    except (ValueError, TypeError):
        return None
    payload = data.get("ocr_result") if isinstance(data.get("ocr_result"), dict) else data
    return payload if isinstance(payload, dict) else None


def _ocr_table_body(table: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("caption", "annotation", "formatted_str", "text", "markdown"):
        value = table.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts).strip()


def _extract_tables_from_ocr_payload(payload: dict[str, Any] | None, *, max_tables: int = 220) -> list[ReportTable]:
    if not payload:
        return []
    raw_tables = payload.get("tables") or []
    if not isinstance(raw_tables, list):
        return []
    from finengine.skills.loan_sector_table_ranker import table_neighbor_context_lines

    tables: list[ReportTable] = []
    for idx, raw_table in enumerate(raw_tables):
        if not isinstance(raw_table, dict):
            continue
        body = _ocr_table_body(raw_table)
        if len(body) < 60:
            continue
        before, after = table_neighbor_context_lines(payload, idx, pre_lines=2, post_lines=2)
        tables.append(
            ReportTable(
                table_id=f"T{len(tables) + 1:03d}",
                page=raw_table.get("page") if isinstance(raw_table.get("page"), int) else None,
                title=str(raw_table.get("caption") or raw_table.get("annotation") or "")[:300],
                text=body,
                context_before="\n".join(before),
                context_after="\n".join(after),
                ocr_table_index=idx,
            )
        )
        if len(tables) >= max_tables:
            break
    return tables


def _build_report_tables_for_two_stage(pdf_text: str, raw_idp: bytes | None) -> list[ReportTable]:
    """Build the experimental extractor table catalogue once per document."""
    ocr_payload = _read_ocr_payload(raw_idp)
    return _extract_tables_from_ocr_payload(ocr_payload) or _extract_tables_from_text(pdf_text)


# Сколько строк тела таблицы оставлять в каталоге в коротком режиме. Шапка плюс
# первые строки — этого хватает, чтобы УЗНАТЬ таблицу в тексте, который у модели
# и так весь перед глазами. Меньше — и таблицы станут неразличимы между собой.
_SHORT_TABLE_LINES = 6


def _table_catalog(tables: list[ReportTable]) -> str:
    """Каталог таблиц для первой стадии: адресация, а при полном режиме — тела.

    ЗАЧЕМ КАТАЛОГ ВООБЩЕ. Модель называет улики идентификатором таблицы, и взять
    его неоткуда, кроме как отсюда: в тексте отчёта идентификаторов нет. Поэтому
    `table_id`, страница, заголовок и соседний текст остаются В ЛЮБОМ режиме.

    ЧТО ДУБЛИРУЕТСЯ. Тело таблицы — буквальный повтор того, что уже есть в
    тексте отчёта, который едет тем же промптом. Замер на трёх настоящих
    отчётах: каталог занимает 47–73 % длины текста, и 60–90 % его объёма —
    повтор. Разбор и включение — `config.compression_table_bodies`.
    """
    from finengine.config import settings

    short = str(getattr(settings, "compression_table_bodies", "full")).strip().lower() == "short"
    rows = []
    for table in tables:
        body = table.text or ""
        if short:
            lines = body.splitlines()
            if len(lines) > _SHORT_TABLE_LINES:
                body = "\n".join(lines[:_SHORT_TABLE_LINES]) + (
                    f"\n… ещё {len(lines) - _SHORT_TABLE_LINES} строк — "
                    "таблица целиком есть в тексте отчёта выше")
        rows.append(
            f"## table_id={_display_table_id(table.table_id)} | page={table.page or 'unknown'}\n"
            f"title: {table.title or ''}\n"
            f"context_before:\n{table.context_before or ''}\n"
            f"table:\n{body}\n"
            f"context_after:\n{table.context_after or ''}"
        )
    return "\n\n".join(rows)


def _metric_info_blocks(metrics: list[dict]) -> list[str]:
    blocks: list[str] = []
    for metric in metrics:
        block = [f"### {metric['enum']}: {metric.get('name_ru') or ''}"]
        if metric.get("name_en"):
            block.append(f"  (англ.: {metric['name_en']})")
        aliases = merge_aliases(metric, 8)
        if aliases:
            block.append(f"  Также может называться: {'; '.join(aliases)}")
        if metric.get("description"):
            block.append(f"  Описание: {metric['description']}")
        if metric.get("hint"):
            block.append(f"  Где искать: {metric['hint']}")
        blocks.append("\n".join(block))
    return blocks


def _metric_aliases_for_language(metric: dict[str, Any], language: str) -> list[str]:
    """Aliases used for automatic table fallback; language-specific first."""
    language = (language or "").strip().lower()
    primary_key = "aliases_ru" if language == "ru" else "aliases_en"
    fallback_key = "aliases_en" if primary_key == "aliases_ru" else "aliases_ru"
    values: list[str] = []
    for key in (primary_key, fallback_key):
        for item in metric.get(key) or []:
            text = str(item or "").strip()
            if text and text.lower() != "нет" and text not in values:
                values.append(text)
    for key in ("name_ru", "name_en"):
        text = str(metric.get(key) or "").strip()
        if text and text.lower() != "нет" and text not in values:
            values.append(text)
    return values


def _finengine2_aliases_for_language(metric: dict[str, Any], language: str) -> list[str]:
    values = _metric_aliases_for_language(metric, language)
    enum = str(metric.get("enum") or "").strip().upper()
    for alias in _FINENGINE2_ALIAS_OVERRIDES.get(enum, ()):
        if alias not in values:
            values.append(alias)
    return values


def _metric_registry_for_prompt(metrics: list[dict], *, language: str) -> str:
    rows: list[dict[str, Any]] = []
    for metric in metrics:
        rows.append(
            {
                "enum": metric.get("enum"),
                "name": metric.get("name_ru") if language == "ru" else metric.get("name_en") or metric.get("name_ru"),
                "name_ru": metric.get("name_ru") or "",
                "name_en": metric.get("name_en") or "",
                "aliases": _metric_aliases_for_language(metric, language)[:12],
                "description": metric.get("description") or "",
                "hint": metric.get("hint") or "",
                "period_type": metric.get("period_type") or "",
            }
        )
    return _json_dump(rows)


def _main_compression_output_template(metrics: list[dict]) -> str:
    template = {
        "metrics": {
            str(metric["enum"]): {
                "tables": [
                    {
                        "table_id": "0",
                        "location": "statement of cash flows / liquidity note / tax note",
                    }
                ],
                "quotes": [
                    {
                        "page": 1,
                        "text": (
                            "Exact 1-2 sentence narrative quote outside tables, only when it "
                            "contains evidence for this metric."
                        ),
                    }
                ],
            }
            for metric in metrics
        }
    }
    return _json_dump(template)


def _main_financial_metric_system_prompt(
    standard: str,
    language: str = "en",
    *,
    prompt_profile_id: str | None = None,
) -> str:
    profile = prompt_profile_for_cell(prompt_profile_id)
    if not prompt_profile_id:
        profile = type(profile)(
            profile_id="generic",
            family=profile.family,
            reporting_standard=str(standard or "financial reporting"),
            language="ru" if str(language or "").lower().startswith("ru") else "en",
        )
    return _build_finengine2_system_prompt(profile)


def _schema_lines(metrics: list[dict], *, has_pages: bool) -> list[str]:
    lines: list[str] = []
    for metric in metrics:
        if has_pages:
            schema = (
                '{"values": {"__RAW_PERIOD_HEADER__": '
                '"__RAW_CELL_VALUE_OR_NULL__"}, '
                '"unit_text": "__RAW_UNIT_TEXT__", '
                '"page": null}'
            )
        else:
            schema = (
                '{"values": {"__RAW_PERIOD_HEADER__": '
                '"__RAW_CELL_VALUE_OR_NULL__"}, '
                '"unit_text": "__RAW_UNIT_TEXT__"}'
            )
        lines.append(f'"{metric["enum"]}": {schema}')
    return lines


def _split_evenly(items: list[dict], parts: int) -> list[list[dict]]:
    parts = max(1, parts)
    if not items:
        return []
    buckets: list[list[dict]] = [[] for _ in range(min(parts, len(items)))]
    for idx, item in enumerate(items):
        buckets[idx % len(buckets)].append(item)
    return buckets


def _compression_payload_for_prompt(compression: dict[str, Any]) -> dict[str, Any]:
    raw_metrics = compression.get("metrics")
    if isinstance(raw_metrics, dict):
        return raw_metrics
    if isinstance(raw_metrics, list):
        out: dict[str, Any] = {}
        for item in raw_metrics:
            if not isinstance(item, dict):
                continue
            enum = str(item.get("Enum") or item.get("enum") or "").strip().upper()
            if enum:
                out[enum] = item
        return out
    return compression if isinstance(compression, dict) else {}


def _normalize_compression_payload(compression: dict[str, Any], metrics: list[dict]) -> dict[str, Any]:
    raw_metrics = _compression_payload_for_prompt(compression)
    normalized: dict[str, Any] = {"metrics": {}}
    for metric in metrics:
        enum = str(metric.get("enum") or "").strip().upper()
        if not enum:
            continue
        raw = raw_metrics.get(enum) if isinstance(raw_metrics, dict) else {}
        item = deepcopy(raw) if isinstance(raw, dict) else {}
        item.setdefault("tables", [])
        item.setdefault("quotes", [])
        item.setdefault("reasoning", "")
        normalized["metrics"][enum] = item
    return normalized


def _alias_pattern(alias: str) -> re.Pattern[str] | None:
    alias = re.sub(r"\s+", " ", str(alias or "").strip())
    if len(alias) < 3:
        return None
    escaped = re.escape(alias).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![\w]){escaped}(?![\w])", re.IGNORECASE)


def _relaxed_alias_tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return [
        token
        for token in re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
        if token not in _RELAXED_ALIAS_STOPWORDS
    ]


def _english_singular_alias_token(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("es") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _relaxed_alias_token_equal(left: str, right: str) -> bool:
    if left == right:
        return True
    if left.isascii() and right.isascii() and min(len(left), len(right)) >= 5:
        return _english_singular_alias_token(left) == _english_singular_alias_token(right)
    if left.isascii() or right.isascii() or min(len(left), len(right)) < 6:
        return False
    common = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        common += 1
    return common >= 5 and max(len(left), len(right)) - common <= 4


def _relaxed_alias_matches(aliases: list[str], table: ReportTable) -> tuple[list[str], list[str]]:
    matched_aliases: list[str] = []
    matched_in: list[str] = []
    locations = (
        ("title", table.title),
        ("context_before", table.context_before),
        ("body", table.text),
        ("context_after", table.context_after),
    )
    for alias in aliases:
        alias_tokens = _relaxed_alias_tokens(alias)
        if len(alias_tokens) < 3 or sum(map(len, alias_tokens)) < 15:
            continue
        matched_on_one_line = False
        for location, location_text in locations:
            lines = str(location_text or "").splitlines()
            if not any(
                all(
                    any(_relaxed_alias_token_equal(alias_token, token) for token in _relaxed_alias_tokens(line))
                    for alias_token in alias_tokens
                )
                for line in lines
            ):
                continue
            if alias not in matched_aliases:
                matched_aliases.append(alias)
            if location not in matched_in:
                matched_in.append(location)
            matched_on_one_line = True
        if matched_on_one_line:
            continue

        # Notes often encode the metric as a table heading plus a row suffix:
        # title ``Таблица 12. Выручка`` + row ``по посреднической деятельности``.
        # Requiring one body line for the non-heading part keeps this stricter
        # than collecting unrelated words from arbitrary rows of a long table.
        title_tokens = _relaxed_alias_tokens(table.title)
        for line in str(table.text or "").splitlines():
            body_tokens = _relaxed_alias_tokens(line)
            title_hits = [
                token for token in alias_tokens
                if any(_relaxed_alias_token_equal(token, candidate) for candidate in title_tokens)
            ]
            body_hits = [
                token for token in alias_tokens
                if any(_relaxed_alias_token_equal(token, candidate) for candidate in body_tokens)
            ]
            if not title_hits or not body_hits or len(set(title_hits + body_hits)) < len(alias_tokens):
                continue
            matched_aliases.append(alias)
            matched_in.extend(location for location in ("title", "body") if location not in matched_in)
            break
    return matched_aliases, matched_in


def _has_composite_parts(text: str, *parts: str) -> bool:
    return all(re.search(part, text, flags=re.IGNORECASE) for part in parts)


def _composite_alias_match(enum: str, table: ReportTable) -> str:
    text = _table_search_text(table)
    stage_match = re.search(r"STAGE(\d)", enum)
    stage = stage_match.group(1) if stage_match else ""
    if enum.startswith("LOANS_STAGE") and enum.endswith("_GROSS"):
        if _has_composite_parts(text, r"loans and advances to customers", r"gross value", rf"stage {stage}"):
            return f"customer loans + gross value + stage {stage}"
        if _has_composite_parts(
            text,
            r"кредиты и авансы.{0,40}клиент",
            r"валовая балансовая стоимость",
            r"12-месячные ожидаемые кредитные убытки",
            r"за весь срок жизни",
        ):
            return "customer loans + gross carrying amount + ECL stage columns"
    if enum.startswith("ECL_ALLOWANCE_STAGE"):
        if _has_composite_parts(text, r"loans and advances to customers", r"impairment", rf"stage {stage}"):
            return f"customer loans + impairment + stage {stage}"
        if _has_composite_parts(
            text,
            r"кредиты и авансы.{0,40}клиент",
            r"резерв под ожидаемые кредитные убытки",
            r"12-месячные ожидаемые кредитные убытки",
            r"за весь срок жизни",
        ):
            return "customer loans + ECL allowance + ECL stage columns"
    if enum == "DOUBTFUL_ACCOUNTS_ALLOWANCE" and _has_composite_parts(
        text,
        r"торговая дебиторская задолженность",
        r"резерв[а-я ]{0,25}ожидаемым кредитным убыткам",
    ):
        return "trade receivables + expected credit loss allowance"
    if enum == "ROU_DEPRECIATION" and _has_composite_parts(
        text,
        r"новые арендные договор",
        r"накопленная амортизация",
        r"начисленная амортизация",
    ):
        return "lease asset roll-forward + accumulated depreciation"
    return ""


def _table_search_text(table: ReportTable) -> str:
    return "\n".join(
        part
        for part in (table.title, table.context_before, table.text, table.context_after)
        if part
    )


def _display_table_id(table_id: str) -> str:
    idx = _table_index(table_id)
    if idx is None:
        return str(table_id or "").strip()
    return str(idx - 1)


def _resolve_table_id(raw: str, table_by_id: dict[str, ReportTable]) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value in table_by_id:
        return value
    upper = value.upper()
    if upper in table_by_id:
        return upper
    if re.fullmatch(r"\d+", value):
        candidate = f"T{int(value) + 1:03d}"
        return candidate if candidate in table_by_id else ""
    match = re.fullmatch(r"T0*(\d+)", upper)
    if match:
        candidate = f"T{int(match.group(1)):03d}"
        return candidate if candidate in table_by_id else ""
    return ""


def _enrich_compression_with_alias_table_matches(
    compression: dict[str, Any],
    *,
    metrics: list[dict],
    tables: list[ReportTable],
    language: str,
    max_added_tables_per_metric: int | None = None,
) -> dict[str, Any]:
    """Add deterministic alias-hit tables absent from the LLM compression.

    These rows are intentionally labelled as automatic fallback evidence; the
    second stage can use them, while diagnostics can still distinguish them
    from tables selected and reasoned about by the LLM.
    """
    enriched = _normalize_compression_payload(compression, metrics)
    table_by_id = {table.table_id: table for table in tables}
    table_texts = {table.table_id: _table_search_text(table) for table in tables}
    for metric in metrics:
        enum = str(metric.get("enum") or "").strip().upper()
        if not enum:
            continue
        item = enriched["metrics"].setdefault(enum, {"tables": [], "quotes": [], "reasoning": ""})
        table_refs = item.setdefault("tables", [])
        if not isinstance(table_refs, list):
            table_refs = []
            item["tables"] = table_refs
        existing_ids = set()
        for ref in table_refs:
            if not isinstance(ref, dict):
                continue
            resolved = _resolve_table_id(str(ref.get("table_id") or ref.get("id") or ""), table_by_id)
            if resolved:
                existing_ids.add(resolved)
        aliases = _finengine2_aliases_for_language(metric, language)
        patterns = [
            (alias, pattern)
            for alias in aliases
            for pattern in [_alias_pattern(alias)]
            if pattern
        ]
        if not patterns:
            continue
        candidates: list[tuple[ReportTable, tuple[int, int, int, int, int], list[str], list[str], str]] = []
        has_strong_match = False
        for table_index, table in enumerate(tables):
            table_id = table.table_id
            search_text = table_texts.get(table.table_id, "")
            matched_aliases = [alias for alias, pattern in patterns if pattern.search(search_text)]
            composite_match = _composite_alias_match(enum, table)
            if not matched_aliases and not composite_match:
                continue
            has_strong_match = True
            if table_id in existing_ids:
                continue
            matched_in = [
                location
                for location, location_text in (
                    ("title", table.title),
                    ("context_before", table.context_before),
                    ("body", table.text),
                    ("context_after", table.context_after),
                )
                if any(pattern.search(location_text or "") for _alias, pattern in patterns)
            ]
            match_type = "exact"
            if composite_match and not matched_aliases:
                matched_aliases = [composite_match]
                matched_in = ["body"]
                match_type = "composite"
            max_words = max(len(re.findall(r"\w+", alias, flags=re.UNICODE)) for alias in matched_aliases)
            max_chars = max(len(alias) for alias in matched_aliases)
            candidates.append(
                (
                    table,
                    (2 if match_type == "exact" else 1, max_words, max_chars, len(matched_aliases), -table_index),
                    matched_aliases,
                    matched_in,
                    match_type,
                )
            )

        if not has_strong_match:
            relaxed_candidates = []
            for table_index, table in enumerate(tables):
                if table.table_id in existing_ids:
                    continue
                matched_aliases, matched_in = _relaxed_alias_matches(aliases, table)
                if not matched_aliases:
                    continue
                max_words = max(len(_relaxed_alias_tokens(alias)) for alias in matched_aliases)
                max_chars = max(len(alias) for alias in matched_aliases)
                relaxed_candidates.append(
                    (
                        table,
                        (0, max_words, max_chars, len(matched_aliases), -table_index),
                        matched_aliases,
                        matched_in,
                        "relaxed",
                    )
                )
            relaxed_candidates.sort(key=lambda item: item[1], reverse=True)
            candidates.extend(relaxed_candidates[:1])

        if max_added_tables_per_metric is not None:
            candidates.sort(key=lambda item: item[1], reverse=True)
            candidates = candidates[: max(0, max_added_tables_per_metric)]

        for table, _score, matched_aliases, matched_in, match_type in candidates:
            table_id = table.table_id
            ref = {
                "table_id": _display_table_id(table.table_id),
                "page": table.page,
                "location": "Unknown",
                "reasoning": "Automatic alias match; not selected by the LLM ranker.",
                "evidence_source": "alias_expansion",
                "matched_aliases": matched_aliases,
                "matched_in": matched_in,
            }
            if match_type != "exact":
                ref["alias_match_type"] = match_type
            table_refs.append(ref)
            existing_ids.add(table_id)
    return enriched


def _table_index(table_id: str) -> int | None:
    match = re.fullmatch(r"T(\d+)", str(table_id or "").strip().upper())
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _table_header_signature(table: ReportTable) -> str:
    for line in table.text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 2:
            return re.sub(r"\s+", " ", stripped).lower()
    return ""


def _expand_related_table_ids(table_ids: list[str], table_by_id: dict[str, ReportTable]) -> list[str]:
    """Include adjacent OCR table fragments that look like the same table.

    IDP/OCR often splits one logical table into separate markdown tables by
    period. If the selected evidence table has an adjacent table with the same
    header row, include it so comparative periods are not lost between stages.
    """
    selected: set[str] = {tid for tid in table_ids if tid in table_by_id}
    queue = list(selected)
    while queue:
        current_id = queue.pop()
        current = table_by_id.get(current_id)
        current_idx = _table_index(current_id)
        if current is None or current_idx is None:
            continue
        signature = _table_header_signature(current)
        if not signature:
            continue
        for adjacent_idx in (current_idx - 1, current_idx + 1):
            adjacent_id = f"T{adjacent_idx:03d}"
            adjacent = table_by_id.get(adjacent_id)
            if adjacent is None or adjacent_id in selected:
                continue
            if _table_header_signature(adjacent) == signature:
                selected.add(adjacent_id)
                queue.append(adjacent_id)
    return sorted(selected, key=lambda tid: _table_index(tid) or 10**9)


def _table_refs_from_metric_item(
    item: dict[str, Any],
    table_by_id: dict[str, ReportTable],
    *,
    include_table_text: bool = False,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    raw_ids: list[str] = []
    location_by_id: dict[str, str] = {}
    for table_ref in item.get("tables") or []:
        location = ""
        if isinstance(table_ref, str):
            table_id = _resolve_table_id(table_ref.strip(), table_by_id)
        elif isinstance(table_ref, dict):
            table_id = _resolve_table_id(str(table_ref.get("table_id") or table_ref.get("id") or ""), table_by_id)
            location = str(table_ref.get("location") or "").strip()
        else:
            continue
        if not table_id:
            continue
        raw_ids.append(table_id)
        if location:
            location_by_id[table_id] = location
    for table_id in _expand_related_table_ids(raw_ids, table_by_id):
        table = table_by_id[table_id]
        ref = {
            "table_id": _display_table_id(table.table_id),
            "page": table.page,
            "title": table.title,
            "location": location_by_id.get(table_id, ""),
        }
        if include_table_text:
            ref.update(
                {
                    "context_before": table.context_before,
                    "text": table.text,
                    "context_after": table.context_after,
                }
            )
        refs.append(ref)
    return refs


def _evidence_for_metric(
    enum: str,
    compression: dict[str, Any],
    table_by_id: dict[str, ReportTable],
    *,
    include_table_text: bool = False,
) -> dict[str, Any]:
    metrics = _compression_payload_for_prompt(compression)
    raw = metrics.get(enum) if isinstance(metrics, dict) else {}
    if raw is None and isinstance(metrics, dict):
        # Полезная нагрузка сжатия ключуется `.strip().upper()`
        # (`_normalize_compression_payload`), а enum сюда приезжает из реестра
        # как есть. Разойдись они на первом же показателе с пробелом — улик не
        # нашлось бы НИ промпту, ни мере, причём мера схлопнулась бы в пол
        # (`1.0`) и выглядела бы как честно безулиочный показатель. Сырой ключ
        # пробуется первым: сжатие может приехать словарём прямо от модели, и
        # там ключи бывают в нижнем регистре.
        raw = metrics.get(str(enum or "").strip().upper())
    item = raw if isinstance(raw, dict) else {}
    quotes = item.get("quotes") or item.get("text_quotes") or []
    if not isinstance(quotes, list):
        quotes = []
    return {
        "tables": _table_refs_from_metric_item(
            item,
            table_by_id,
            include_table_text=include_table_text,
        ),
        "quotes": quotes[:6],
    }


def _dual_nature_enums(extractable: list, calculated_defs: list) -> set[str]:
    """Показатели, заведённые в ячейке И извлекаемыми, И расчётными.

    Таких пар 35 в девяти ячейках из десяти (11 разных показателей), и среди
    них EBITDA, CAPEX, валовая прибыль, чистые комиссионные. Для них в прогоне
    действует правило «извлечённое старше расчётного»: строку нашли в отчёте —
    формула молчит; не нашли — считает. Строка в таблице при этом одна.

    ЧИСЛО СНЯТО ПРОГОНОМ 20.08.2026 (`registry_for_profile` по всем ячейкам,
    пересечение `extractable` и `calculated`). Стоявшие здесь «27» устарели
    ещё до того. Число в комментарии стареет молча, поэтому состав пар
    стережёт `tests/test_registry_no_silent_duplicates.py`, а не оно.
    """
    return (
        {str(item[0]).strip().upper() for item in (calculated_defs or ())}
        & {str(m.get("enum", "")).strip().upper() for m in (extractable or ())}
    )


def _page_from_evidence(
    enum: str,
    compression: dict[str, Any],
    table_by_id: dict[str, "ReportTable"],
) -> int | None:
    """Страница показателя — СО СТАДИИ СЖАТИЯ, когда модель её не назвала.

    ДЕФЕКТ (прогон владельца по Россельхозбанку, 19.08.2026): колонка «Источник»
    пуста у ВСЕХ 110 строк, `page=None` и `source_form=None`. Текст при этом
    размечен страницами — их 46, — и шаблон ответа страницу спрашивает. Но
    спрашивает так:

        '{"values": {...}, "unit_text": "__RAW_UNIT_TEXT__", "page": null}'

    У каждого поля стоит плашка «сюда подставь», а у страницы — литеральный
    `null`. Модель честно возвращает то, что ей показали.

    ЧИНИМ НЕ ПРОМПТОМ, А ТЕМ, ЧТО УЖЕ ЗНАЕМ. Стадия сжатия выбрала для этого
    показателя КОНКРЕТНЫЕ таблицы, и у каждой таблицы страница известна нам
    самим (`table.page`), без участия модели. Это и есть «где нашли» — ответ
    надёжнее самоотчёта модели и не стоит ни одного лишнего вызова.

    Правку шаблона промпта здесь СОЗНАТЕЛЬНО не делаем: она меняет форму ответа
    модели, а значит и результат, и проверить это можно только живым прогоном.
    Детерминированный запасной путь такого риска не несёт.
    """
    evidence = _evidence_for_metric(enum, compression, table_by_id)
    # Ссылка на таблицу УЖЕ несёт страницу: её кладёт `_table_refs_from_metric_item`,
    # разрешив идентификатор в саму таблицу. Разрешать второй раз здесь нельзя —
    # наружу едет ОТОБРАЖАЕМЫЙ идентификатор («0»), а не внутренний, и повторный
    # поиск по нему не находит ничего. Поймано тестом сразу.
    for ref in evidence.get("tables") or []:
        page = ref.get("page") if isinstance(ref, dict) else None
        if isinstance(page, int) and page > 0:
            return page
    for quote in evidence.get("quotes") or []:
        page = quote.get("page") if isinstance(quote, dict) else None
        if isinstance(page, int) and page > 0:
            return page
    return None


def _evidence_bundle(
    metrics: list[dict],
    compression: dict[str, Any],
    table_by_id: dict[str, ReportTable],
    *,
    include_table_text: bool = False,
) -> dict[str, Any]:
    return {
        metric["enum"]: _evidence_for_metric(
            metric["enum"],
            compression,
            table_by_id,
            include_table_text=include_table_text,
        )
        for metric in metrics
    }


def _metric_prompt_block(metric: dict, evidence: dict[str, Any], *, enum: str) -> dict[str, Any]:
    """Карточка ОДНОГО показателя в промпте второй стадии.

    Была двумя побайтово одинаковыми словарями — в основной сборке промпта и в
    сегментной. Расхождение таких близнецов не ловится ничем: промпты собираются
    в разных функциях, правятся по отдельности, а разъехавшись, дают у сегментов
    другой вопрос при том же реестре.

    ⚠️ ВЕС ПОКАЗАТЕЛЯ ЗДЕСЬ БОЛЬШЕ НЕ СЧИТАЕТСЯ. С 20.08 по 21.08 у карточки был
    ТРЕТИЙ читатель: `_prompt_card_weights` меряла вес её длиной. Замер 20.08 по
    Северстали показал, что длина карточки — это ВХОД, а время идёт за ВЫХОДОМ
    (разбор числами — в докстроке `_prompt_card_weights`), и мера переехала на
    ожидаемое число клеток ответа. Общей у веса и промпта осталась ОДНА вещь —
    улики: обе стороны берут их у `_evidence_bundle` с `include_table_text=True`.
    Разойдись этот источник — балансировка считала бы колонки в таблицах, каких
    модель не увидит. Держит `test_the_weight_reads_the_evidence_the_prompt_shows`.

    `enum` передаётся отдельно: основная стадия кладёт его как есть, сегментная —
    приведённым к верхнему регистру, и промпты обеих обязаны остаться прежними.
    """
    return {
        "enum": enum,
        "name_ru": metric.get("name_ru") or "",
        "name_en": metric.get("name_en") or "",
        "description": metric.get("description") or "",
        "aliases_ru": metric.get("aliases_ru") or [],
        "aliases_en": metric.get("aliases_en") or [],
        "exclusions_ru": metric.get("exclusions_ru") or [],
        "exclusions_en": metric.get("exclusions_en") or [],
        "hint": metric.get("hint") or "",
        "period_type": metric.get("period_type") or "",
        "evidence": evidence or {},
    }


def _widest_row_columns(table_text: str) -> int:
    """ВЕРХНЯЯ ОЦЕНКА числа колонок значений: все колонки самой широкой строки.

    ⚠️ ИМЯ ЗДЕСЬ ЧЕСТНЕЕ ПРЕЖНЕГО, И ЭТО НЕ КОСМЕТИКА. До 21.08 функция звалась
    `_value_columns` — «колонки значений», — а считала любые: разбивку по
    валютам, срокам, видам инструмента. Ключи ответа при этом только периодные
    (`__RAW_PERIOD_HEADER__` в `_schema_lines`), непериодных модель не пишет
    вовсе. Имя обещало отбор, которого в теле нет, — и на этом обещании в
    докстроке веса стояло неверное оправдание (разбор — в `_prompt_card_weights`,
    четвёртое завышение). Отобрать периодные колонки честно мы не умеем: шапку
    в теле улики не опознать, `_ocr_table_body` склеивает подпись, аннотацию и
    разметку в один текст.

    ПОЧЕМУ САМАЯ ШИРОКАЯ СТРОКА, А НЕ ПЕРВАЯ. Не потому, что первой стоит
    подпись: подпись до сравнения не доживает — `_is_table_line` требует
    ведущий `|`, а `caption`/`annotation` пайпами не начинаются. Причина в
    ОБЪЕДИНЁННЫХ ШАПКАХ: у первой строки markdown-таблицы объединённая ячейка
    съедает разделители, и шапка выходит уже тела («| Показатель | 2025 |» над
    строками в пять колонок). Держит `test_the_widest_row_wins_over_the_first`.

    Первая колонка — подпись строки, значений в ней нет, отсюда `- 1`. Снятие
    этой поправки меняет ПОРЯДОК весов между «мало широких улик» и «много
    узких» — держит `test_the_row_label_column_is_not_a_value`.
    """
    widest = 0
    for line in (table_text or "").splitlines():
        if not _is_table_line(line):
            continue
        widest = max(widest, len(line.strip().strip("|").split("|")) - 1)
    return max(0, widest)


def _expected_answer_cells(evidence: dict[str, Any]) -> float:
    """Сколько клеток ответа модель обязана заполнить по этим уликам.

    Клетка — одна пара `"сырая шапка периода": "сырое значение"` в шаблоне
    ответа второй стадии (`_schema_lines`). Единица в начале — конверт
    показателя (`unit_text`, `page`, сам ключ enum'а): его модель пишет всегда,
    даже когда ни одного значения не нашла. Держит
    `test_the_metric_envelope_is_a_cell`; цитаты считаются отдельным
    слагаемым — `test_a_quote_is_a_cell_too`.

    🔴 МЕРА СЧИТАЕТ ФОРМУ ОТВЕТА ОСНОВНОЙ ЦЕПОЧКИ И ТОЛЬКО ЕЁ.
    `_segment_extraction_output_template` просит не `values`, а
    `rows: [{segment, dimension, values{...}}]` — там выход это СТРОКИ × колонки,
    а строк эта мера не видит намеренно. На Северстали Q1-2025
    (`benchmarks/vendor/.../ifrs_nf_ru/gt/severstal-Q1-2025.csv`, посчитано
    прямо по файлу) SALES_REVENUE даёт 8 сегментов × 2 периода = 16 клеток,
    OPERATING_PROFIT — 4 × 2 = 8, а мера ставит обоим одно и то же. Это
    ИЗВЕСТНЫЙ ОТКАЗ, а не недосмотр, и он назван вслух потому, что чинить его
    вслепую дороже: перемножь строки на колонки — и вес сегментного показателя
    поедет за числом строк ВСЕЙ таблицы, а не своих; проверить это можно только
    замером по golden.

    ЧЕМ ОТКАЗ ОГРАНИЧЕН, ЧИСЛОМ: сегментных показателей самое большее ПЯТЬ
    (`_SEGMENT_METRICS_IFRS_RU` / `_US_GAAP_EN` / `_BANK_RU` — по пять в каждом),
    а размер сегментного батча по умолчанию тоже пять
    (`extract_segment_batch_size`). Значит `_batch_items` отдаёт сегментной
    цепочке ОДИН батч, и раскладывать там нечего: слепота меры на неё сегодня
    не влияет вовсе. Утверждение УСЛОВНОЕ — и размер, и состав меняются снаружи
    (`FINENGINE_EXTRACT_SEGMENT_BATCH_SIZE`, `segment_metrics_override`).
    Оба условия держит `test_the_segment_stage_is_still_one_batch`
    (`tests/test_the_batch_pace_has_something_to_pace.py`) вместе с
    отрицательным контролем «состав шире размера — батчей больше одного».
    Покраснеет он — меру придётся разводить по цепочкам по-настоящему.
    """
    cells = 1.0
    for table in evidence.get("tables") or []:
        cells += _widest_row_columns(str(table.get("text") or ""))
    return cells + len(evidence.get("quotes") or [])


def _prompt_card_weights(
    metrics: list[dict],
    compression: dict[str, Any],
    table_by_id: dict[str, ReportTable],
) -> dict[str, float]:
    """Вес показателя = ожидаемое число КЛЕТОК ЕГО ОТВЕТА, а не длина вопроса.

    🔴 ЧЕМ МЕРА БЫЛА ДО 21.08 И ПОЧЕМУ ЕЁ СМЕНИЛИ. Здесь стояла длина карточки
    показателя в промпте, в символах. Это ВХОД. Замер стенда 20.08 (Северсталь
    Q1-2025, `ifrs_nf_ru`, девять вызовов второй стадии) показал, что мера
    делает ровно то, что обещала, и что этого мало:

        вход  батчей   18 275…20 346 токенов  — разброс в 1,1 раза
        выход батчей    1 591…7 101  токенов  — разброс в 4,5 раза
        время батчей     19,1…230,3  с        — разброс в 12 раз

        корреляция секунд с ВХОДОМ   0,20
        корреляция секунд с ВЫХОДОМ  0,83

    Балансировка по длине карточки выровняла вход почти в точку — и ступень
    осталась двенадцатикратной. Это не «мера слабовата», это законченный опыт:
    величину, которую мера равняла, она выровняла, а темп за ней не пошёл.
    ⚠️ Число 0,20 при этом само по себе НИЧЕГО не доказывает: на диапазоне в
    1,1 раза корреляция задавлена сужением размаха. Доказывает пара фактов —
    вход выровнен, время нет.

    ЧТО СЧИТАЕМ ТЕПЕРЬ. Шаблон ответа второй стадии просит по показателю
    `{"values": {сырая шапка периода: сырое значение}, "unit_text", "page"}`,
    а правило промпта — «scan all period columns in that row and all evidence
    tables, return every explicitly disclosed period». Значит длину ответа
    задаёт число КОЛОНОК в отобранных уликах, а не число строк в них: по
    каждой колонке модель обязана выдать пару, по строкам — только выбрать
    одну. Отсюда `1 + Σ колонок улик + число цитат`.

    ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ ДЛИНЫ КАРТОЧКИ — ДВУМЯ РАЗВОРОТАМИ, И ОБА РАБОЧИЕ:
    * отчёт о финансовом положении на сорок строк в две колонки против
      сегментной росписи на шесть строк в двенадцать: карточка первого длиннее
      втрое, ответ второго длиннее вшестеро;
    * шесть коротких таблиц против одной длинной: по символам выигрывает
      длинная, по клеткам ответа — шестёрка.
    Ровно эти два разворота и стережёт `test_the_weight_counts_the_answer`.

    ⚠️ ВЕСЬ СМЫСЛ МЕРЫ ПО-ПРЕЖНЕМУ НЕСЁТ `include_table_text=True` НИЖЕ. Без
    текста таблицы колонки считать не по чему: в ссылке остаются только
    идентификатор, страница и заголовок. Переведи в `False` — вес станет
    `1 + число цитат`, то есть почти константой, и балансировка тихо
    выродится в прежнее деление. Стенд-харнесс этого не ловит: он отдаёт
    `tables: []`.

    ЧЕМ ПЛАТИМ, ЧЕТЫРЕ ИЗВЕСТНЫХ ЗАВЫШЕНИЯ И ОДНО НЕЗНАНИЕ:
    * `_expand_related_table_ids` добавляет продолжения таблицы с ТОЙ ЖЕ
      шапкой, а в ответе одинаковые шапки схлопываются в один ключ `values` —
      такие улики считаются дважды. Считать вместо этого объединение шапок
      честнее, но шапку в теле улики не опознать: OCR склеивает подпись,
      аннотацию и разметку в один текст (`_ocr_table_body`);
    * широкая таблица редко раскрывает показатель во всех ПЕРИОДНЫХ колонках —
      часть клеток модель вернёт как `null` (шаблон это разрешает:
      `__RAW_CELL_VALUE_OR_NULL__`), но написать их всё равно обязана;
    * 🔴 НЕПЕРИОДНЫЕ КОЛОНКИ СЧИТАЮТСЯ, А В ОТВЕТЕ ИХ НЕТ ВОВСЕ, и это
      завышение не «одинаковое у всех». Ключи ответа — только периодные шапки
      (`_schema_lines`), а `_widest_row_columns` складывает любые: разбивку по
      валютам, срокам, видам инструмента. Улика-примечание на шесть-десять
      колонок даёт в ответе те же две пары, что строка баланса, — и весит
      впятеро больше. Бьёт по показателям, чьи улики лежат в примечаниях.
      ⚠️ До 21.08 здесь стояло, что «часть клеток модель вернёт как null, но
      написать их всё равно обязана», — про непериодные колонки это НЕВЕРНО:
      их модель не пишет ни значением, ни нулём;
    * цитата считается за клетку, хотя значения может не нести вовсе.
    Первое, второе и четвёртое завышают ОДИНАКОВО у показателей с похожими
    уликами, а раскладке важен порядок весов, не масштаб; третье — нет, и это
    известная кривизна меры, а не её свойство.

    ⚠️ МЕРА УМЕЕТ ВЫРОЖДАТЬСЯ В ПОЛНУЮ РОВНОСТЬ, И ТОГДА `balanced` — ЭТО
    ПОБАЙТОВО `spread`. Улике не обязательно быть markdown-таблицей:
    `_extract_tables_from_ocr_payload` берёт любое тело длиннее 60 символов,
    пайпов не требует. На плоской OCR-таблице `_widest_row_columns` даёт ноль у
    ВСЕХ улик, веса схлопываются в `1 + цитаты`, и раскладка возвращает прежнее
    деление — молча, потому что равные веса это законный случай
    (`test_equal_weights_degenerate_to_prior_division`). Отличить «балансировка
    не помогла» от «балансировки не было» на замере нечем, поэтому разброс
    весов уезжает в диагностику прогона: `stage2_batch_weight_spread` и
    `stage2_batch_weights_degenerate`. Смягчает то, что наш IDP-путь
    конвертирует HTML→markdown (`idp_ocr_client.py`), но защиты в мере нет.

    ⚠️ СЕГМЕНТНАЯ ЦЕПОЧКА ЗОВЁТ ЭТУ ЖЕ МЕРУ, а форма ответа у неё другая —
    разбор и число, которым отказ ограничен, в докстроке
    `_expected_answer_cells`.

    ⚠️ ЧЕГО МЫ НЕ ЗНАЕМ И ЧЕМ ЭТО ОГРАНИЧЕНО. В измеренный «выход» входят и
    токены рассуждения модели, а отделить их от JSON без сырых ответов прогона
    мы не умеем. Растёт ли рассуждение по КОЛОНКАМ (что мерим теперь) или по
    СТРОКАМ (что мерили раньше) — прямого замера у нас НЕТ, а поимённый состав
    девяти батчей замера живёт в артефактах стенда, и без него корреляцию новой
    меры с теми секундами не посчитать. Проверено здесь только то, что
    проверяется без стенда: механика двух разворотов на фикстурах и негодность
    снятой меры. Поэтому стратегия остаётся настройкой:
    `FINENGINE_EXTRACT_BATCH_STRATEGY=spread` возвращает деление по порядку
    реестра одной переменной окружения, без пересборки.

    Мера считается ДО вызова модели: улики уже отобраны первой стадией.
    """
    evidence_by_enum = _evidence_bundle(
        metrics,
        compression,
        table_by_id,
        include_table_text=True,
    )
    weights: dict[str, float] = {}
    for metric in metrics:
        raw_enum = metric.get("enum")
        weights[str(raw_enum or "").strip().upper()] = _expected_answer_cells(
            evidence_by_enum.get(raw_enum) or {}
        )
    return weights


def _weight_spread(items: list[str], weights: dict[str, float]) -> float:
    """Разброс весов по составу, который реально ушёл в деление.

    ЗАЧЕМ ЭТО В ДИАГНОСТИКЕ. Раскладка по равным весам — это `spread`
    побайтово (`test_equal_weights_degenerate_to_prior_division`), и вырождение
    законно: равные веса бывают. Но замер владельца тогда показывает прежнюю
    неровность при настройке `balanced`, и отличить «балансировка не помогла»
    от «балансировки не было» по исходу прогона нечем. Ноль здесь — это второе,
    и молчать о нём нельзя: мера схлопывается целиком на плоской OCR-таблице
    без markdown-пайпов, где `_widest_row_columns` даёт ноль у всех улик.

    Громкой ОШИБКОЙ это не сделано намеренно, в отличие от `weights=None`
    (`_batch_items`): забытый аргумент — дефект кода, а одинаковые веса —
    свойство документа, и падать на нём значило бы отказывать в извлечении
    отчёту, который просто плоско распознан.
    """
    values = [weights.get(item, 0.0) for item in items]
    return round(max(values) - min(values), 1) if values else 0.0


def _evidence_table_ids(
    metrics: list[dict],
    compression: dict[str, Any],
    table_by_id: dict[str, ReportTable],
) -> list[str]:
    seen: set[str] = set()
    for item in _evidence_bundle(metrics, compression, table_by_id).values():
        for table in item.get("tables") or []:
            table_id = _resolve_table_id(str(table.get("table_id") or table.get("internal_table_id") or ""), table_by_id)
            if table_id in table_by_id:
                seen.add(table_id)
    return sorted(seen, key=lambda tid: _table_index(tid) or 10**9)


def _table_evidence_block(table_ids: list[str], table_by_id: dict[str, ReportTable]) -> str:
    tables = []
    for table_id in table_ids:
        table = table_by_id.get(table_id)
        if table is None:
            continue
        tables.append(
            {
                "table_id": _display_table_id(table.table_id),
                "page": table.page,
                "title": table.title,
                "context_before": table.context_before,
                "text": table.text,
                "context_after": table.context_after,
            }
        )
    return _json_dump(tables)


def _llm_request_record(
    *,
    stage: str,
    model_id: str | None,
    started_at: float,
    elapsed_s: float,
    usage: dict[str, Any],
) -> dict[str, Any]:
    """Запись одного вызова модели для посекундного разреза прогона.

    `started_at` — время НАЧАЛА вызова (epoch, как `time.time()`). Пока стадии
    стояли в очередь, разрез читался сложением: сумма `elapsed_s` совпадала со
    временем прогона — значит очередь. С параллельным запуском сегментной
    цепочки сумма стала больше времени прогона всегда, и сложение говорит лишь
    «что-то перекрылось», но не что с чем. Пара `started_at` + `elapsed_s` даёт
    отрезок каждого вызова, и по отрезкам перекрытие видно поимённо — без этого
    следующий замер скорости мерить нечем.
    """
    from finengine.skills.pricing import cost_usd

    usage_model = str(usage.get("model") or model_id or "")
    tokens_in = int(usage.get("in") or usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    tokens_out = int(usage.get("out") or usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    tokens_cached = int(usage.get("cached") or 0)
    estimated_cost = cost_usd(usage_model, tokens_in, tokens_out, tokens_cached)
    actual_cost = usage.get("cost")
    return {
        "stage": stage,
        "model": usage_model,
        "started_at": round(started_at, 1),
        "elapsed_s": round(elapsed_s, 1),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_cached": tokens_cached,
        "cost_usd": round(float(actual_cost if actual_cost is not None else estimated_cost), 6),
        "cost_source": "openrouter_usage" if actual_cost is not None else "local_pricing_estimate",
        "estimated_cost_usd": round(estimated_cost, 6),
        "generation_id": usage.get("generation_id") or "",
    }


async def _call_json_llm(
    *,
    prompt: str,
    system: str,
    model_id: str | None,
    stage: str,
    debug_dir: Path | None,
    prompt_name: str,
    response_name: str,
    max_output: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    if "json" not in f"{system}\n{prompt}".lower():
        prompt = prompt.rstrip() + "\n\nReturn valid JSON only."
    _write_debug_file(debug_dir, f"{prompt_name}_system.md", system)
    _write_debug_file(debug_dir, f"{prompt_name}.md", prompt)
    t0 = time.time()
    raw, usage = await llm.complete_with_usage(
        prompt=prompt,
        system=system,
        model=model_id,
        response_format={"type": "json_object"},
        max_output=max_output,
        omit_max_output=max_output is None,
    )
    elapsed = time.time() - t0
    _write_debug_file(debug_dir, response_name, raw)
    request = _llm_request_record(
        stage=stage, model_id=model_id, started_at=t0, elapsed_s=elapsed, usage=usage or {}
    )
    usage_name = f"{Path(response_name).stem.removesuffix('_response')}_usage.json"
    _write_debug_file(
        debug_dir,
        usage_name,
        _json_dump(request),
    )
    return _json_loads_object(raw), request, raw


def _build_main_compression_prompt(
    *,
    pdf_text_for_prompt: str,
    tables: list[ReportTable],
    metrics: list[dict],
    period_current: str,
    period_prev: str,
    periods_detected: list[str],
    language: str = "en",
    prompt_profile_id: str | None = None,
) -> str:
    profile = prompt_profile_for_cell(prompt_profile_id)
    if not prompt_profile_id and str(language or "").lower().startswith("ru"):
        profile = type(profile)(
            profile.profile_id, profile.family, profile.reporting_standard, "ru"
        )
    return _build_finengine2_compression_prompt(
        profile,
        ocr_text=pdf_text_for_prompt,
        table_catalog=_table_catalog(tables),
        registry_json=_metric_registry_for_prompt(metrics, language=profile.language),
        output_template=_main_compression_output_template(metrics),
    )


def _build_main_extraction_prompt(
    *,
    all_metrics: list[dict],
    target_metrics: list[dict],
    compression: dict[str, Any],
    table_by_id: dict[str, ReportTable],
    period_current: str,
    period_prev: str,
    periods_detected: list[str],
    has_pages: bool,
    split_request: bool,
    company_type: str = "",
    reporting_standard: str = "",
    language: str = "en",
    prompt_profile_id: str | None = None,
) -> str:
    target_enums = [metric["enum"] for metric in target_metrics]
    blocks = []
    evidence_by_enum = _evidence_bundle(
        target_metrics,
        compression,
        table_by_id,
        include_table_text=True,
    )
    for metric in target_metrics:
        enum = str(metric.get("enum") or "")
        blocks.append(
            _metric_prompt_block(metric, evidence_by_enum.get(enum) or {}, enum=enum)
        )
    profile = prompt_profile_for_cell(prompt_profile_id)
    if not prompt_profile_id:
        profile = type(profile)(
            profile_id="generic",
            family=profile.family,
            reporting_standard=str(reporting_standard or "financial reporting"),
            language="ru" if str(language or "").lower().startswith("ru") else "en",
        )
    return _build_finengine2_extraction_prompt(
        profile,
        target_enums=target_enums,
        metric_blocks_json=_json_dump(blocks),
        schema_lines=_schema_lines(target_metrics, has_pages=has_pages),
        split_request=split_request,
        company_type=company_type,
    )


def _segment_compression_output_template(metrics: list[dict]) -> str:
    return _json_dump(
        {
            "metrics": {
                str(metric["enum"]): {
                    "tables": [
                        {
                            "table_id": "0",
                            "location": "business breakdown / segment disclosure note",
                        }
                    ],
                    "quotes": [
                        {
                            "page": 1,
                            "text": (
                                "Exact 1-2 sentence narrative quote outside tables that "
                                "describes a quantitative business breakdown or relevant table."
                            ),
                        }
                    ],
                }
                for metric in metrics
            }
        }
    )


def _segment_extraction_output_template(metrics: list[dict]) -> str:
    return _json_dump(
        {
            "metrics": {
                str(metric["enum"]): {
                    "rows": [
                        {
                            "segment": "SEGMENT_NAME",
                            "dimension": "operating_segment|product|geography|other",
                            "values": {"RAW_PERIOD_HEADER": "RAW_CELL_VALUE"},
                            "unit_text": "RAW_UNIT_TEXT",
                            "page": 1,
                        }
                    ]
                }
                for metric in metrics
            }
        }
    )


def _build_segment_compression_prompt(
    *,
    pdf_text_for_prompt: str,
    tables: list[ReportTable],
    metrics: list[dict],
    prompt_profile_id: str | None,
    language: str,
) -> str:
    profile = prompt_profile_for_cell(prompt_profile_id)
    if not prompt_profile_id:
        profile = type(profile)(
            profile_id="generic",
            family=profile.family,
            reporting_standard=profile.reporting_standard,
            language="ru" if str(language or "").lower().startswith("ru") else "en",
        )
    return _build_finengine2_segment_compression_prompt(
        profile,
        ocr_text=pdf_text_for_prompt,
        table_catalog=_table_catalog(tables),
        registry_json=_metric_registry_for_prompt(metrics, language=profile.language),
        output_template=_segment_compression_output_template(metrics),
    )


def _build_segment_extraction_prompt(
    *,
    target_metrics: list[dict],
    compression: dict[str, Any],
    table_by_id: dict[str, ReportTable],
    segment_mode: str,
    company_type: str,
    prompt_profile_id: str | None,
) -> str:
    blocks: list[dict[str, Any]] = []
    evidence_by_enum = _evidence_bundle(
        target_metrics,
        compression,
        table_by_id,
        include_table_text=True,
    )
    for metric in target_metrics:
        enum = str(metric.get("enum") or "").strip().upper()
        blocks.append(
            _metric_prompt_block(metric, evidence_by_enum.get(enum) or {}, enum=enum)
        )
    profile = prompt_profile_for_cell(prompt_profile_id)
    return _build_finengine2_segment_extraction_prompt(
        profile,
        mode=bulk._normalize_segment_mode(segment_mode),
        target_enums=[str(metric["enum"]).strip().upper() for metric in target_metrics],
        metric_blocks_json=_json_dump(blocks),
        output_template=_segment_extraction_output_template(target_metrics),
        company_type=company_type,
    )


async def _prepare_common_scope(
    *,
    doc_id: str,
    company_type: str,
    index: Any,
    metric_enums: Optional[set[str]],
    full_registry: bool,
    limit: int | None,
    registry_override: dict | None,
    fallback_period_label_override: str | None,
    default_unit_override: str | None,
    role: str,
    period_calendar: str,
    reporting_standard_override: str | None,
) -> dict[str, Any] | AsyncGenerator[dict, None]:
    doc = index.get_document(doc_id)
    if not doc:
        async def _err():
            yield {"type": "error", "message": f"Document {doc_id} not found"}
        return _err()

    from finengine.store.blobs import (  # noqa: PLC0415
        document_idp_raw, document_text_when_ready,
    )

    pdf_text = await document_text_when_ready(doc_id, timeout_s=900, poll_s=1.5)
    if pdf_text is None:
        async def _err():
            yield {
                "type": "error",
                "message": "Документ ещё парсится (превышено окно ожидания 15 минут).",
            }
        return _err()
    raw_idp = document_idp_raw(doc_id)
    if "===PAGE " not in pdf_text and raw_idp is not None:
        # Разбивка по страницам нужна ссылкам «PDF стр. N»: без неё показатель
        # не на что навести. Не разобралось — идём с тем, что есть: разбор без
        # ссылок лучше отказа.
        from finengine.skills.aifa_baseline.idp_ocr_client import (  # noqa: PLC0415
            markdown_from_saved_idp_response,
        )
        try:
            со_страницами = markdown_from_saved_idp_response(json.loads(raw_idp))
            if со_страницами.strip():
                pdf_text = со_страницами
        except (ValueError, KeyError, TypeError):
            # Логгера в этом модуле нет, а заводить его ради одной строки —
            # менять то, к чему переезд отношения не имеет. Прежний код тут
            # молчал вовсе (голый `except: pass`); теперь хотя бы видно в
            # выводе прогона, что разбивка по страницам не собралась.
            print(f"… сырой ответ OCR документа {doc_id} не разбирается — "
                  f"иду без разбивки по страницам", file=sys.stderr, flush=True)

    by_page: dict[int, list[str]] = {}
    for kind in ("text", "financial_table", "table"):
        try:
            chunks_p = index.get_chunks_by_type(kind, doc_id=doc_id)
        except Exception:
            chunks_p = []
        for chunk in chunks_p:
            page = chunk.metadata.get("page", 0) if hasattr(chunk, "metadata") else 0
            try:
                page = int(page or 0)
            except Exception:
                page = 0
            if page > 0:
                by_page.setdefault(page, []).append(chunk.content or "")
    paginated_text = "\n\n".join(
        f"===PAGE {page}===\n" + "\n\n".join(by_page[page])
        for page in sorted(by_page)
    )
    pdf_text_for_prompt = paginated_text or pdf_text
    has_pages = bool(paginated_text) or "===PAGE " in pdf_text

    effective_type = company_type if company_type not in ("unknown", "auto", "") else bulk._detect_company_type(pdf_text)
    # Приведение к году живёт в ячейке рядом с формулой (#146) и едет ОТДЕЛЬНОЙ
    # картой: четвёрка `(enum, name_ru, formula, deps)` — это конвейер, пятого
    # поля у неё нет. Хардкод-реестры ниже такой подсказки не несут, поэтому
    # пустая карта — их штатное состояние, а не потеря.
    calc_meta: dict[str, dict] = {}
    if registry_override is not None:
        # 🔧 НАША ПРАВКА (#257, 16.08): берём состав из ячейки, но СИНОНИМЫ
        # ОБЪЕДИНЯЕМ С ОБЩИМ РЕЕСТРОМ — как на нашем пути
        # (`streams_full_context`, замер 01.08). Файлы ячеек команда правила
        # под свою отчётность, и подсказки «где искать» там лучше общих, но
        # список синонимов в них ТОЛЬКО РУССКИЙ: переход «в лоб» терял 169
        # латинских синонимов у всех 78 показателей ifrs_bank_ru и 192 у 75 в
        # ifrs_nf_ru — Total assets, PP&E, Long-term borrowings. Русские отчёты
        # по МСФО часто двуязычные, и половина крупных эмитентов печатает
        # таблицы с английскими заголовками.
        extractable = list(bulk._merge_aliases_from_base(
            registry_override.get("extractable") or [], effective_type))
        calculated_defs = list(registry_override.get("calculated") or [])
        calc_meta = registry_override.get("calculated_meta") or {}
    else:
        # ЯЧЕЙКА НЕ ПЕРЕДАНА — берём ячейку по умолчанию для типа компании,
        # а не легаси-реестр `data/metrics.json` (21.08.2026). Копия расходилась
        # с ячейками молча: 12 формул у банка и 2 у нефинансовой давали другое
        # число под тем же именем, а после перевода доходностей в проценты —
        # в сто раз меньшее. Замерено: легаси — строгое ПОДМНОЖЕСТВО ячейки,
        # замена ничего не отнимает.
        from finengine.skills.metric_schema import registry_for_profile

        _fallback = registry_for_profile(
            "ifrs_bank_ru" if effective_type == "bank" else "ifrs_nf_ru")
        extractable = list(_fallback["extractable"])
        calculated_defs = list(_fallback["calculated"])
        calc_meta = _fallback.get("calculated_meta") or {}
        if not metric_enums and not full_registry:
            from finengine.skills.aifa.scope import filter_to_trained

            extractable = filter_to_trained(extractable)

    if metric_enums:
        wanted = {str(item).strip().upper() for item in metric_enums if str(item).strip()}
        if wanted:
            extractable = [m for m in extractable if str(m.get("enum", "")).strip().upper() in wanted]
            calculated_defs = [m for m in calculated_defs if str(m[0]).strip().upper() in wanted]

    # 🔧 НАША ПРАВКА, ВТОРОЕ ОТСТУПЛЕНИЕ ОТ ПОБАЙТОВОГО ПЕРЕНОСА (#257, 16.08).
    #
    # `top_borrowers` — отдельная возможность Finengine (крупнейшие заёмщики
    # банка), 495 строк, и у нас её НЕТ. Показателя `TOP_BORROWERS` нет ни в
    # одной из десяти наших ячеек и нет в справочнике навыка — проверено
    # грепом. Переносить модуль ради показателя, которого мы не извлекаем,
    # значит тащить мёртвый вес.
    #
    # Здесь имя нужно ровно для одного: найти этот показатель в составе и
    # обработать отдельно. Нет модуля — нет и показателя, поиск ниже вернёт
    # None, ветка не сработает. Заглушка НЕ ОТКЛЮЧАЕТ возможность, а честно
    # сообщает, что её тут не бывает.
    #
    # ⚠️ Появится `TOP_BORROWERS` в наших ячейках — перенести модуль целиком,
    # иначе показатель будет молча пропускаться.
    try:
        from finengine.skills.top_borrowers import TOP_BORROWERS_ENUM
    except ModuleNotFoundError:
        TOP_BORROWERS_ENUM = "__TOP_BORROWERS_NOT_IN_OUR_REGISTRY__"

    top_borrowers_metric = next(
        (m for m in extractable if str(m.get("enum", "")).strip().upper() == TOP_BORROWERS_ENUM),
        None,
    )
    extractable = [
        m for m in extractable
        if str(m.get("enum", "")).strip().upper() != TOP_BORROWERS_ENUM
    ]
    segment_extractable = list(extractable)
    if limit is not None and limit > 0:
        extractable = extractable[:limit]
        calculated_defs = []
    # 🔧 НАША ПРАВКА, ОТСТУПЛЕНИЕ ОТ ПОБАЙТОВОГО ПЕРЕНОСА (19.08, Б0).
    #
    # Здесь у Finengine стоял безусловный снос: enum, заведённый И извлекаемым,
    # И расчётным, терял расчётную половину ДО прогона. Смысл понятен — не
    # положить в таблицу две строки с одним именем (#163).
    #
    # Цена оказалась выше смысла. Сколько таких пар и где — сказано ОДИН раз,
    # в докстринге `_dual_nature_enums`; второй экземпляр числа здесь прожил
    # ровно до первой правки реестра и врал, пока его не поймала состязательная
    # проверка. Среди них EBITDA, CAPEX, валовая прибыль. Печатной строки
    # «EBITDA» в отчётности по МСФО обычно нет вовсе — формула достала бы её,
    # но формулы к тому моменту уже нет. Прогон Северстали 19.08: операционная
    # прибыль 28 355 и амортизация 9 495 извлечены, EBITDA пуста.
    #
    # Держим обе половины и решаем ПО ФАКТУ: нашли строку в отчёте — формула
    # молчит, не нашли — считает. Двух строк с одним именем не появляется:
    # пустая строка извлечения придерживается и уходит только тогда, когда и
    # формула не дала значения (см. `deferred_dual` ниже по файлу).
    dual_enums = _dual_nature_enums(extractable, calculated_defs)

    periods_detected, period_current, period_prev = bulk._select_run_periods(
        pdf_text,
        getattr(doc, "period", ""),
        fallback_period_label=fallback_period_label_override or "Текущий",
        period_calendar=period_calendar,
    )
    reporting_standard = reporting_standard_override or bulk._detect_reporting_standard(pdf_text_for_prompt)
    try:
        from finengine.skills.extractors._classifier import detect_language

        language = detect_language(pdf_text_for_prompt)
    except Exception:
        language = "ru" if re.search(r"[А-Яа-яЁё]", pdf_text_for_prompt[:50_000]) else "en"
    default_unit = default_unit_override or bulk._default_unit_for_standard(reporting_standard)
    detected_company = bulk._detect_company_from_text(pdf_text)
    company_display = bulk._prefer_detected_company_name(
        getattr(doc, "company_name", None),
        detected_company,
        getattr(doc, "filename", ""),
    )
    return {
        "doc": doc,
        "pdf_text": pdf_text,
        "pdf_text_for_prompt": pdf_text_for_prompt,
        "raw_idp": raw_idp,
        "has_pages": has_pages,
        "effective_type": effective_type,
        "extractable": extractable,
        "calculated_defs": calculated_defs,
        "calc_meta": calc_meta,
        "dual_enums": dual_enums,
        "segment_extractable": segment_extractable,
        "top_borrowers_metric": top_borrowers_metric,
        "periods_detected": periods_detected,
        "period_current": period_current,
        "period_prev": period_prev,
        "period_calendar": period_calendar,
        "max_report_year": bulk._max_report_anchor_year(periods_detected),
        "reporting_standard": reporting_standard,
        "language": language,
        "default_unit": default_unit,
        "company_display": company_display,
        "source_form_by_page": bulk._build_source_form_by_page(
            pdf_text_for_prompt,
            reporting_standard=reporting_standard,
            role=role,
        ),
    }


async def _extract_segment_breakdown_two_stage(
    *,
    pdf_text_for_prompt: str,
    extractable: list[dict],
    period_current: str,
    period_prev: str,
    company_display: str,
    effective_type: str,
    model_id: str | None,
    compression_model_id: str | None = None,
    extraction_model_id: str | None = None,
    segment_mode: str = "extended",
    segment_context_mode: str = "full",
    debug_dir: Path | None = None,
    segment_metrics_override: set[str] | None = None,
    extraction_batch_size: int = 5,
    extraction_batch_jobs: int = 1,
    extraction_batch_attempts: int = 3,
    extraction_batch_strategy: str = "balanced",
    report_tables: list[ReportTable] | None = None,
    prompt_profile_id: str | None = None,
    period_calendar: str = "calendar",
    compression_override: dict[str, Any] | None = None,
    compression_request_override: dict[str, Any] | None = None,
    compression_max_output: int | None = None,
    extraction_max_output: int | None = None,
    progress_callback: Any = None,
) -> AsyncGenerator[dict, None]:
    """Вторая стадия: разрезы по сегментам для отобранных показателей.

    🔧 РАСХОЖДЕНИЕ С FINENGINE, ПЕРЕНЕСЕНО ИЗ #252. Здесь стоял
    `if effective_type == "bank": return` — стадия молча выходила для любого
    банка. У нас этот запрет снят ещё вчера в нашем движке (`fd03bd8`), и
    снятие дало на Ак Барсе 0 → 20 строк с настоящими именами разрезов
    (Розница / КБ / Казначейство / TOTAL).

    Пока запрет стоял здесь, замер A/B отвечал не на тот вопрос: наш путь
    сегменты банка отдавал, перенесённая схема — нет, и разница «30 против 0»
    объяснялась не схемой извлечения, а этой строкой.

    Второй такой же запрет — в `segments_supported` у вызывающей стороны;
    снят вместе с этим, иначе сюда просто не заходят.
    """
    standard = bulk._detect_reporting_standard(pdf_text_for_prompt)
    default_unit = bulk._default_unit_for_standard(standard)
    max_report_year = bulk._max_report_anchor_year([period_current, period_prev])
    fiscal_context = FiscalPeriodContext.from_text(pdf_text_for_prompt)
    target_metrics = (
        [m for m in extractable if m.get("enum") in segment_metrics_override]
        if segment_metrics_override is not None
        # тип компании обязателен: у банка свой набор сегментных показателей
        # (`_SEGMENT_METRICS_BANK_RU`), без него сюда приедет набор
        # нефинансовой компании и не совпадёт ни с чем
        else bulk._segment_metric_scope(extractable, standard, effective_type)
    )
    if not target_metrics:
        return

    context_mode = bulk._normalize_segment_context_mode(segment_context_mode)
    context = bulk._build_segment_prompt_context(pdf_text_for_prompt, mode=context_mode)
    if not context.strip():
        return
    tables = list(report_tables or []) if context_mode == "full" else _extract_tables_from_text(context)
    table_by_id = {table.table_id: table for table in tables}
    profile = prompt_profile_for_cell(prompt_profile_id)
    system_prompt = _build_finengine2_system_prompt(profile)
    stage1_model_id = compression_model_id or settings.openrouter_model
    stage2_model_id = extraction_model_id or model_id or settings.openrouter_model
    if compression_override is None:
        compress_prompt = _build_segment_compression_prompt(
            pdf_text_for_prompt=context,
            tables=tables,
            metrics=target_metrics,
            prompt_profile_id=prompt_profile_id,
            language=profile.language,
        )
        try:
            compression, compression_request, raw_compression = await _call_json_llm(
                prompt=compress_prompt,
                system=system_prompt,
                model_id=stage1_model_id,
                stage="segment_compression",
                debug_dir=debug_dir,
                prompt_name="segment_compression_prompt",
                response_name="segment_compression_response.json",
                max_output=compression_max_output,
            )
        except Exception as exc:
            yield {"type": "error", "message": f"Two-stage segment compression LLM error: {exc}"}
            return
    else:
        compression = dict(compression_override)
        raw_compression = _json_dump(compression)
        compression_request = dict(
            compression_request_override
            or {
                "stage": "segment_compression",
                "mode": "replay",
                "model": "",
                "elapsed_s": 0.0,
                "tokens_in": 0,
                "tokens_out": 0,
                "tokens_cached": 0,
                "cost_usd": 0.0,
                "estimated_cost_usd": 0.0,
            }
        )
        _write_debug_file(
            debug_dir,
            "segment_compression_response.json",
            raw_compression,
        )
    _write_debug_file(
        debug_dir,
        "segment_compression_parsed.json",
        _json_dump(compression),
    )

    metric_by_enum = {
        str(metric["enum"]).strip().upper(): metric for metric in target_metrics
    }
    target_enums = list(metric_by_enum)
    skipped_empty_evidence = [
        enum for enum in target_enums if not _has_stage1_evidence(compression, enum)
    ]
    skipped = set(skipped_empty_evidence)
    batches = _batch_items(
        [enum for enum in target_enums if enum not in skipped],
        max(1, int(extraction_batch_size or 5)),
        strategy=extraction_batch_strategy,
        weights=_prompt_card_weights(target_metrics, compression, table_by_id),
    )
    batch_sem = asyncio.Semaphore(max(1, int(extraction_batch_jobs or 1)))
    attempts = max(1, int(extraction_batch_attempts or 1))

    async def _run_batch(batch_no: int, enum_batch: list[str]) -> dict[str, Any]:
        requests: list[dict[str, Any]] = []
        last_error = ""
        last_raw = ""
        for attempt in range(1, attempts + 1):
            attempt_enums = _retry_enum_order(
                enum_batch,
                document=company_display,
                batch_no=batch_no,
                attempt=attempt,
            )
            metrics = [metric_by_enum[enum] for enum in attempt_enums]
            prompt = _build_segment_extraction_prompt(
                target_metrics=metrics,
                compression=compression,
                table_by_id=table_by_id,
                segment_mode=segment_mode,
                company_type=effective_type,
                prompt_profile_id=prompt_profile_id,
            )
            stage_name = f"segment_extract_batch_{batch_no:03d}_attempt_{attempt:02d}"
            stem = stage_name
            if progress_callback is not None:
                progress_callback(
                    {
                        "batch_no": batch_no,
                        "batch_count": len(batches),
                        "attempt": attempt,
                        "attempt_count": attempts,
                        "status": "running" if attempt == 1 else "retrying",
                        "enums": attempt_enums,
                    }
                )
            try:
                async with batch_sem:
                    _parsed, request, raw = await _call_json_llm(
                        prompt=prompt,
                        system=system_prompt,
                        model_id=stage2_model_id,
                        stage=stage_name,
                        debug_dir=debug_dir,
                        prompt_name=f"{stem}_prompt",
                        response_name=f"{stem}_response.json",
                        max_output=extraction_max_output,
                    )
            except Exception as exc:
                last_error = f"llm_error: {type(exc).__name__}: {exc}"
                if progress_callback is not None:
                    progress_callback(
                        {
                            "batch_no": batch_no,
                            "batch_count": len(batches),
                            "attempt": attempt,
                            "attempt_count": attempts,
                            "status": "invalid",
                            "error": last_error,
                            "enums": attempt_enums,
                        }
                    )
                continue
            last_raw = raw
            parsed, last_error, recovered = _recover_segment_stage2_json_object(
                raw,
                enum_batch,
            )
            request.update(
                {
                    "attempt": attempt,
                    "batch_no": batch_no,
                    "batch_valid": not bool(last_error),
                    "batch_validation_error": last_error,
                    "batch_json_recovered": recovered,
                    "batch_enum_order": attempt_enums,
                    "batch_enum_order_shuffled": attempt_enums != enum_batch,
                }
            )
            requests.append(request)
            if not last_error:
                if progress_callback is not None:
                    progress_callback(
                        {
                            "batch_no": batch_no,
                            "batch_count": len(batches),
                            "attempt": attempt,
                            "attempt_count": attempts,
                            "status": "completed",
                            "recovered": recovered,
                            "enums": attempt_enums,
                        }
                    )
                return {
                    "batch_no": batch_no,
                    "parsed": parsed,
                    "requests": requests,
                    "attempts": attempt,
                    "status": "completed",
                    "raw_chars": len(raw),
                    "error": "",
                }
            if progress_callback is not None:
                progress_callback(
                    {
                        "batch_no": batch_no,
                        "batch_count": len(batches),
                        "attempt": attempt,
                        "attempt_count": attempts,
                        "status": "invalid",
                        "error": last_error,
                        "enums": attempt_enums,
                    }
                )
        return {
            "batch_no": batch_no,
            "parsed": {},
            "requests": requests,
            "attempts": attempts,
            "status": "failed",
            "raw_chars": len(last_raw),
            "error": last_error or "unknown_invalid_response",
        }

    batch_results = (
        await asyncio.gather(
            *(
                _run_batch(batch_no, enum_batch)
                for batch_no, enum_batch in enumerate(batches, start=1)
            )
        )
        if batches
        else []
    )
    rows_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    requests = [compression_request]
    failed_batches: list[dict[str, Any]] = []
    raw_chars = len(raw_compression)
    for result in sorted(batch_results, key=lambda item: item["batch_no"]):
        requests.extend(result["requests"])
        raw_chars += int(result["raw_chars"] or 0)
        if result["status"] != "completed":
            failed_batches.append(result)
            continue
        for enum, payload in result["parsed"].items():
            for raw_row in payload.get("rows") or []:
                if not isinstance(raw_row, dict):
                    continue
                segment = str(raw_row.get("segment") or "").strip()
                if not segment:
                    continue
                if bulk._is_total_segment_label(segment):
                    segment = "TOTAL"
                dimension = str(raw_row.get("dimension") or "other").strip().lower() or "other"
                if dimension not in {"operating_segment", "product", "geography", "other"}:
                    dimension = "other"
                key = (enum, segment.casefold(), dimension)
                row = rows_by_key.setdefault(
                    key,
                    {
                        "enum": enum,
                        "segment": segment,
                        "dimension": dimension,
                        "values": {},
                        "unit_text": raw_row.get("unit_text") or "",
                        "page": raw_row.get("page"),
                    },
                )
                values = raw_row.get("values")
                if isinstance(values, dict):
                    for period, value in values.items():
                        row["values"].setdefault(str(period), value)

    sent = 0
    for (enum, _segment_key, _dimension_key), row in rows_by_key.items():
        metric = metric_by_enum.get(enum)
        if metric is None:
            continue
        period_values, metric_unit = bulk._coerce_period_values(
            row,
            enum=enum,
            period_current=period_current,
            period_prev=period_prev,
            default_unit=default_unit,
            abs_cost=False,
            dash_as_zero=True,
            max_report_year=max_report_year,
            fiscal_context=fiscal_context,
            period_calendar=period_calendar,
        )
        if not period_values:
            continue
        value_current = bulk._value_for_period(
            period_values,
            period_current,
            period_calendar,
        )
        if value_current is None:
            value_current = bulk._first_value(period_values)
        yield {
            "type": "metric",
            "enum": enum,
            "name_ru": metric.get("name_ru") or metric.get("name_en") or enum,
            "value_current": value_current,
            "value_prev": bulk._value_for_period(
                period_values,
                period_prev,
                period_calendar,
            ),
            "period_values": period_values,
            "unit": period_values[0].get("unit") or metric_unit,
            "found": any(item.get("value") is not None for item in period_values),
            "is_calculated": False,
            "formula": "",
            "page": bulk._page_from_period_values(period_values),
            "segment": row["segment"],
            "dimension": row["dimension"],
        }
        sent += 1
    yield {
        "type": "segment_diag",
        "_diag_segment": {
            "segment_rows": sent,
            "segment_mode": bulk._normalize_segment_mode(segment_mode),
            "segment_context_mode": context_mode,
            "segment_context_chars": len(context),
            "segment_standard": standard,
            "segment_two_stage_raw_chars": raw_chars,
            "segment_stage1_target_count": len(target_enums),
            "segment_stage1_model_id": stage1_model_id,
            "segment_stage2_model_id": stage2_model_id,
            "segment_stage2_batch_size": max(1, int(extraction_batch_size or 5)),
            "segment_stage2_batch_jobs": max(1, int(extraction_batch_jobs or 1)),
            "segment_stage2_batch_count": len(batches),
            "segment_stage2_retry_count": sum(
                max(0, int(item["attempts"]) - 1) for item in batch_results
            ),
            "segment_stage2_invalid_batches": [
                {"batch_no": item["batch_no"], "error": item["error"]}
                for item in failed_batches
            ],
            "segment_stage2_skipped_empty_evidence": skipped_empty_evidence,
            "llm_requests": requests,
        },
    }


async def _extract_stream_two_stage_full_context(
    doc_id: str,
    company_type: str,
    index=None,
    limit: Optional[int] = None,
    model_id: Optional[str] = None,
    compression_model_id: Optional[str] = None,
    extraction_model_id: Optional[str] = None,
    include_segments: bool = False,
    include_consolidated_group: bool = False,
    metric_enums: Optional[set[str]] = None,
    full_registry: bool = False,
    role: str = "",
    segment_mode: str = "extended",
    segment_context_mode: str = "full",
    debug_dir: Optional[Path] = None,
    system_prompt_override: Optional[str] = None,
    user_prompt_builder: Any = None,
    segment_metrics_override: Optional[set[str]] = None,
    segment_rules_extra: Optional[str] = None,
    default_unit_override: Optional[str] = None,
    fallback_period_label_override: Optional[str] = None,
    registry_override: Optional[dict] = None,
    include_top_borrowers: bool = False,
    extraction_parts: int = 1,
    extraction_batch_size: int | None = None,
    extraction_batch_jobs: int = 1,
    extraction_batch_attempts: int = 3,
    extraction_batch_strategy: str = "balanced",
    extraction_ask_without_evidence: bool = True,
    segment_extraction_batch_size: int = 5,
    compression_max_output: int | None = None,
    extraction_max_output: int | None = None,
    use_alias_enrichment: bool = True,
    period_calendar: str = "calendar",
    disable_segments: bool = False,
    prompt_profile_id: str | None = None,
    reporting_standard_override: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    run_t0 = time.time()
    stage1_model_id = compression_model_id or settings.openrouter_model
    stage2_model_id = extraction_model_id or model_id or settings.openrouter_model
    if index is None:
        from finengine.ingestion.document_store import get_store

        index = get_store()

    prepared = await _prepare_common_scope(
        doc_id=doc_id,
        company_type=company_type,
        index=index,
        metric_enums=metric_enums,
        full_registry=full_registry,
        limit=limit,
        registry_override=registry_override,
        fallback_period_label_override=fallback_period_label_override,
        default_unit_override=default_unit_override,
        role=role,
        period_calendar=period_calendar,
        reporting_standard_override=reporting_standard_override,
    )
    if not isinstance(prepared, dict):
        async for event in prepared:
            yield event
        return

    extractable = prepared["extractable"]
    calculated_defs = prepared["calculated_defs"]
    calc_meta = prepared.get("calc_meta") or {}
    dual_enums = prepared.get("dual_enums") or set()
    # Пустые строки двойных enum'ов ждут здесь свою формулу — см. `dual_enums`.
    deferred_dual: dict[str, dict] = {}
    period_current = prepared["period_current"]
    period_prev = prepared["period_prev"]
    periods_detected = prepared["periods_detected"]
    default_unit = prepared["default_unit"]
    effective_type = prepared["effective_type"]
    company_display = prepared["company_display"]
    source_form_by_page = prepared["source_form_by_page"]
    pdf_text_for_prompt = prepared["pdf_text_for_prompt"]
    raw_idp = prepared["raw_idp"]
    has_pages = prepared["has_pages"]
    max_report_year = prepared["max_report_year"]
    doc = prepared["doc"]
    top_borrowers_metric = prepared["top_borrowers_metric"]
    # 🔧 Второй запрет сегментов банкам, снят вместе с первым (#252 → #257).
    # Стоял `and effective_type != "bank"`. Пара запретов и делала сегменты
    # банка недостижимыми: этот не пускал в стадию, тот выходил из неё сразу.
    segments_supported = (
        not disable_segments
        and (segment_metrics_override is None or bool(segment_metrics_override))
    )

    yield {
        "type": "start",
        # Двойные enum'ы считаем ОДИН раз: строка в таблице у них одна, и
        # знаменатель «найдено из скольких» не должен их удваивать.
        "total_metrics": len(extractable) + len(calculated_defs) - len(dual_enums),
        "total_extractable": len(extractable),
        "company_type": effective_type,
        "company_name": company_display,
        "unit": default_unit,
        "period_current": period_current,
        "period_prev": period_prev,
        "periods": periods_detected,
        "period_calendar": period_calendar,
    }
    yield {"type": "status", "stage": "two_stage_compression", "message": "Сжимаем контекст отчета"}

    tables = _build_report_tables_for_two_stage(pdf_text_for_prompt, raw_idp)
    table_by_id = {table.table_id: table for table in tables}
    system_prompt = (
        _main_financial_metric_system_prompt(
            prepared["reporting_standard"],
            prepared["language"],
            prompt_profile_id=prompt_profile_id,
        )
        if prompt_profile_id
        else system_prompt_override
        or _main_financial_metric_system_prompt(
            prepared["reporting_standard"],
            prepared["language"],
        )
    )
    compression_prompt = _build_main_compression_prompt(
        pdf_text_for_prompt=pdf_text_for_prompt,
        tables=tables,
        metrics=extractable,
        period_current=period_current,
        period_prev=period_prev,
        periods_detected=periods_detected,
        language=prepared["language"],
        prompt_profile_id=prompt_profile_id,
    )
    diag: dict[str, Any] = {
        "two_stage": True,
        "two_stage_parts": _as_int(extraction_parts),
        "two_stage_table_count": len(tables),
        "pdf_text_chars": len(prepared["pdf_text"]),
        "llm_requests": [],
        "prompt_profile_id": prompt_profile_for_cell(prompt_profile_id).profile_id,
        "prompt_family": prompt_profile_for_cell(prompt_profile_id).family,
        "segments_supported": segments_supported,
        "stage1_alias_enrichment": bool(use_alias_enrichment),
        "stage1_model_id": stage1_model_id,
        "stage2_model_id": stage2_model_id,
        "include_consolidated_group": bool(include_consolidated_group),
    }
    consolidated_group_task: asyncio.Task[list[dict]] | None = None

    async def _collect_consolidated_group_events() -> list[dict]:
        events: list[dict] = []
        profile = prompt_profile_for_cell(prompt_profile_id)
        async for group_event in bulk._extract_consolidated_group_full_context(
            pdf_text_for_prompt=pdf_text_for_prompt,
            period_current=period_current,
            period_prev=period_prev,
            company_display=company_display
            or getattr(doc, "company_name", "")
            or getattr(doc, "filename", ""),
            reporting_standard=(
                profile.reporting_standard if prompt_profile_id else prepared["reporting_standard"]
            ),
            company_type=effective_type,
            model_id=stage2_model_id,
            debug_dir=debug_dir,
        ):
            events.append(group_event)
        return events

    if include_consolidated_group:
        consolidated_group_task = asyncio.create_task(_collect_consolidated_group_events())
    # СЕГМЕНТНАЯ ЦЕПОЧКА ИДЁТ ОДНОВРЕМЕННО С ОСНОВНОЙ, А НЕ ПОСЛЕ НЕЁ.
    # Замер на стенде (ПСБ, `ifrs_bank_ru`, 20.08) по разрезу `run_stages`:
    # main_compression 229 с, main_extract_1 178 с, segment_compression 83 с,
    # segment_extract_batch_001 12 с, consolidated_group 3 с — сумма 505 с
    # ровно равна времени прогона, то есть стадии стояли в очередь.
    # ⚠️ Имена стадий извлечения приведены как в ТОМ замере; с 21.08 они несут
    # номер попытки (`main_extract_1_attempt_01`,
    # `segment_extract_batch_001_attempt_01`) — искать в свежем разрезе по
    # префиксу.
    #
    # Ждать сегментной стадии незачем: свой промпт она строит сама, по полному
    # тексту отчёта (`_build_segment_compression_prompt` ←
    # `bulk._build_segment_prompt_context`), а `compression_override` ей никто
    # не передаёт. Восемьдесят три секунды уходили на ожидание того, что можно
    # было считать параллельно.
    #
    # Порядок событий на выходе НЕ меняется: строки копятся в списке, а
    # разбираются на прежнем месте, ниже плоских показателей. Общий `diag`
    # задача не трогает — он наполняется только после `await`.
    segment_task: asyncio.Task[list[dict]] | None = None

    async def _collect_segment_events() -> list[dict]:
        events: list[dict] = []
        async for segment_event in _extract_segment_breakdown_two_stage(
            pdf_text_for_prompt=pdf_text_for_prompt,
            extractable=prepared["segment_extractable"],
            period_current=period_current,
            period_prev=period_prev,
            company_display=company_display or getattr(doc, "company_name", "") or getattr(doc, "filename", ""),
            effective_type=effective_type,
            model_id=stage2_model_id,
            compression_model_id=stage1_model_id,
            extraction_model_id=stage2_model_id,
            segment_mode=segment_mode,
            segment_context_mode=segment_context_mode,
            debug_dir=debug_dir,
            segment_metrics_override=segment_metrics_override,
            extraction_batch_size=max(1, int(segment_extraction_batch_size or 5)),
            extraction_batch_jobs=max(1, int(extraction_batch_jobs or 1)),
            extraction_batch_attempts=max(1, int(extraction_batch_attempts or 1)),
            extraction_batch_strategy=extraction_batch_strategy,
            report_tables=tables,
            prompt_profile_id=prompt_profile_id,
            period_calendar=period_calendar,
        ):
            events.append(segment_event)
        return events

    if segments_supported and (include_segments or bulk._role_wants_segments(role)):
        segment_task = asyncio.create_task(_collect_segment_events())

    # ВСЁ ТЕЛО НИЖЕ — ПОД `finally` В КОНЦЕ ФУНКЦИИ, см. объяснение у самого
    # `finally`: генератор могут бросить недочитанным, а фоновые задачи это не
    # гасит.
    try:
        try:
            # КЭШ ПЕРВОЙ СТАДИИ (21.08.2026). 216 секунд из 318 — здесь, и для
            # одного и того же промпта ответ один и тот же. Ключ — отпечаток
            # ПРОМПТА, а не документа: у одного отчёта промпт разный при разной
            # ячейке, разном составе показателей и после любой нашей правки
            # правил. Разбор — `api/internal_compression_cache`.
            _cache_key = _prompt_fingerprint(compression_prompt, system_prompt)
            _cached = _cache.compression_cached(_progress_nonce(), _cache_key)
            if _cached:
                compression = json.loads(_cached)
                diag["main_compression_cache_hit"] = True
                diag["main_compression_raw_chars"] = len(_cached)
                yield {"type": "status", "stage": "two_stage_compression",
                       "message": "Отчёт уже разобран — беру готовую раскладку"}
            else:
                compression, request, raw_compression = await _call_json_llm(
                    prompt=compression_prompt,
                    system=system_prompt,
                    model_id=stage1_model_id,
                    stage="main_compression",
                    debug_dir=debug_dir,
                    prompt_name="main_compression_prompt",
                    response_name="main_compression_response.json",
                    max_output=compression_max_output,
                )
                diag["llm_requests"].append(request)
                diag["main_compression_raw_chars"] = len(raw_compression)
                # Кладём ТОЛЬКО разобранное: положи мы сырой ответ, переспрос
                # получил бы из кэша тот же мусор, и три попытки стали бы одной.
                _cache.remember_compression(
                    _progress_nonce(), _cache_key, _json_dump(compression))
            _write_debug_file(
                debug_dir,
                "main_compression_parsed.json",
                _json_dump(compression),
            )
            if use_alias_enrichment:
                compression = _enrich_compression_with_alias_table_matches(
                    compression,
                    metrics=extractable,
                    tables=tables,
                    language=prepared["language"],
                )
            _write_debug_file(
                debug_dir,
                "main_compression_enriched.json",
                _json_dump(compression),
            )
        except Exception as exc:
            # Гасим СРАЗУ, до отдачи ошибки наружу. Потребитель может не
            # вернуться за следующим событием вовсе — тогда уборка в `finally`
            # не наступит, пока генератор не соберёт сборщик мусора, а всё это
            # время сегментная цепочка жжёт токены на прогон, который уже
            # объявлен отказом.
            if consolidated_group_task is not None:
                consolidated_group_task.cancel()
            if segment_task is not None:
                segment_task.cancel()
            yield {"type": "error", "message": f"Two-stage compression LLM error: {exc}"}
            return

        extracted_current: dict[str, Optional[float]] = {}
        extracted_prev: dict[str, Optional[float]] = {}
        extracted_by_period: dict[str, dict[str, Optional[float]]] = {}
        extractable_period_values_by_enum: dict[str, list[dict]] = {}
        formula_default_abs_enums = {
            str(metric.get("enum", "")).strip().upper()
            for metric in extractable
            if metric.get("formula_default_abs_behavior")
        }
        # `.strip()` здесь не украшение: ключом этого словаря состав уходит в
        # `_batch_items`, а вес того же показателя `_prompt_card_weights` кладёт
        # под `.strip().upper()`. Разойдись нормализации на первом же enum'е с
        # пробелом — вес молча стал бы нулём, и `balanced` выродилась бы в
        # `spread`, не уронив ничего. Ровно та тихая деградация, ради которой
        # деление без весов сделано громкой ошибкой.
        metric_by_enum = {str(metric["enum"]).strip().upper(): metric for metric in extractable}
        skipped_empty_evidence: list[str] = []
        asked_without_evidence: list[str] = []
        # `None` — «деление шло не по весу вовсе» (устаревший путь без размера
        # батча). Ноль — «вес посчитан и оказался одинаковым у всех», а это
        # совсем другое: см. `_weight_spread`.
        weight_spread: float | None = None
        if extraction_batch_size is not None:
            target_enums = list(metric_by_enum)
            starved = [
                enum for enum in target_enums if not _has_stage1_evidence(compression, enum)
            ]
            # ПОЧЕМУ БЕЗУЛИОЧНЫЕ ЕДУТ В ОБЩИЕ БАТЧИ, А НЕ В СВОЙ ОТДЕЛЬНЫЙ.
            # Единственный канал содержимого отчёта в промпте второй стадии —
            # поле `evidence` внутри карточки показателя, и собирается оно
            # ТОЛЬКО по `target_metrics` (`_build_main_extraction_prompt` →
            # `_evidence_bundle(target_metrics, ...)`; ни таблиц, ни текста
            # страницы в промпте больше нет — см. `build_extraction_prompt`).
            # У безулиочного показателя это поле пустое. Значит батч, набранный
            # из ОДНИХ безулиочных, уехал бы к модели вообще без единой таблицы
            # отчёта: определения есть, читать нечего. Найти он может только
            # выдуманное — то есть ровно тот риск, ради которого upstream их и
            # выбрасывает.
            # До шардирования эти показатели находились ровно потому, что ехали
            # в одном промпте с соседями, у которых улики были, и смотрели в ИХ
            # таблицы. Подмешивание восстанавливает это условие: `spread`
            # разносит соседей по реестру в разные батчи, так что непрерывный
            # отрезок безулиочных в комок не сбивается. ⚠️ ГАРАНТИИ ЗДЕСЬ НЕТ,
            # и утверждать обратное нельзя: раскладка идёт по `idx % count`
            # (`_batch_items`), поэтому позиции, сравнимые по модулю `count`,
            # попадут в ОДИН батч. Целиком безулиочный батч остаётся достижим —
            # маловероятен, но возможен; поймать это можно только замером, по
            # поимённому списку `metrics_asked_without_evidence`.
            # Числом это тот же «дополнительный батч»: размер батча фиксирован,
            # и лишние показатели удлиняют не батч, а их список
            # (`_batch_items`: count = ceil(len / size)).
            if extraction_ask_without_evidence:
                asked_without_evidence = starved
            else:
                skipped_empty_evidence = starved
            dropped = set(skipped_empty_evidence)
            asked_enums = [enum for enum in target_enums if enum not in dropped]
            batch_weights = _prompt_card_weights(extractable, compression, table_by_id)
            enum_batches = _batch_items(
                asked_enums,
                extraction_batch_size,
                strategy=extraction_batch_strategy,
                weights=batch_weights,
            )
            weight_spread = _weight_spread(asked_enums, batch_weights)
            split_request = False
        else:
            legacy_groups = _split_evenly(extractable, _as_int(extraction_parts))
            enum_batches = [
                [str(metric["enum"]).upper() for metric in group] for group in legacy_groups
            ]
            split_request = len(enum_batches) > 1

        diag["stage2_batch_size"] = extraction_batch_size
        diag["stage2_batch_strategy"] = extraction_batch_strategy
        diag["stage2_batch_jobs"] = max(1, int(extraction_batch_jobs or 1))
        diag["stage2_batch_attempts"] = max(1, int(extraction_batch_attempts or 1))
        diag["stage2_batch_count"] = len(enum_batches)
        diag["stage2_batch_weight_spread"] = weight_spread
        diag["stage2_batch_weights_degenerate"] = weight_spread == 0.0
        diag["stage2_skipped_empty_evidence"] = skipped_empty_evidence
        diag["stage2_asked_without_evidence"] = asked_without_evidence
        yield {
            "type": "status",
            "stage": "two_stage_extraction",
            "message": f"Извлекаем показатели из сжатого контекста: {len(enum_batches)} батчей",
        }

        batch_sem = asyncio.Semaphore(diag["stage2_batch_jobs"])

        async def _run_stage2_batch(batch_no: int, enum_batch: list[str]) -> dict[str, Any]:
            requests: list[dict[str, Any]] = []
            last_error = ""
            last_raw = ""
            attempts = diag["stage2_batch_attempts"]
            for attempt in range(1, attempts + 1):
                attempt_enums = _retry_enum_order(
                    enum_batch,
                    document=str(getattr(doc, "filename", "") or doc_id),
                    batch_no=batch_no,
                    attempt=attempt,
                )
                target_metrics = [metric_by_enum[enum] for enum in attempt_enums]
                prompt = _build_main_extraction_prompt(
                    all_metrics=(target_metrics if extraction_batch_size is not None else extractable),
                    target_metrics=target_metrics,
                    compression=compression,
                    table_by_id=table_by_id,
                    period_current=period_current,
                    period_prev=period_prev,
                    periods_detected=periods_detected,
                    has_pages=has_pages,
                    split_request=split_request,
                    company_type=effective_type,
                    reporting_standard=prepared["reporting_standard"],
                    language=prepared["language"],
                    prompt_profile_id=prompt_profile_id,
                )
                batch_stage = (
                    f"main_extract_batch_{batch_no:03d}"
                    if extraction_batch_size is not None
                    else f"main_extract_{batch_no}"
                )
                # ПОЧЕМУ НОМЕР ПОПЫТКИ ЖИВЁТ В ИМЕНИ СТАДИИ (21.08.2026).
                # Разрез прогона (`extract_result.run_stages`) проецирует вызов
                # модели на `_STAGE_FIELDS` — а `attempt`, который добавляется
                # к записи ниже, в этот список не входит и до читателя не
                # доезжает. Пока имя было общим на все попытки, повтор батча
                # выглядел в разрезе ВТОРЫМ БАТЧЕМ С ТЕМ ЖЕ НОМЕРОМ: замер
                # Северстали 20.08 дал `main_extract_batch_008` дважды — 19,1 с
                # и 35,5 с, — и отличить «батч шёл 54 секунды, потому что его
                # переспросили» от «батчей было девять» было нечем.
                # Цена не в отчётности: попыток четыре, и незамеченный повтор
                # уводит поиск медленного места в размер батча вместо разбора
                # ответа.
                stage_name = f"{batch_stage}_attempt_{attempt:02d}"
                stem = stage_name
                try:
                    async with batch_sem:
                        _parsed, request, raw = await _call_json_llm(
                            prompt=prompt,
                            system=system_prompt,
                            model_id=stage2_model_id,
                            stage=stage_name,
                            debug_dir=debug_dir,
                            prompt_name=f"{stem}_prompt",
                            response_name=f"{stem}_response.json",
                            max_output=extraction_max_output,
                        )
                except Exception as exc:
                    last_error = f"llm_error: {type(exc).__name__}: {exc}"
                    continue
                last_raw = raw
                parsed, last_error, recovered = _recover_stage2_json_object(raw, enum_batch)
                request.update(
                    {
                        "attempt": attempt,
                        "batch_no": batch_no,
                        "batch_valid": not bool(last_error),
                        "batch_validation_error": last_error,
                        "batch_json_recovered": recovered,
                        "batch_enum_order": attempt_enums,
                        "batch_enum_order_shuffled": attempt_enums != enum_batch,
                    }
                )
                requests.append(request)
                if not last_error:
                    return {
                        "batch_no": batch_no,
                        "parsed": parsed,
                        "requests": requests,
                        "attempts": attempt,
                        "status": "completed",
                        "raw_chars": len(raw),
                        "error": "",
                    }
            return {
                "batch_no": batch_no,
                "parsed": {},
                "requests": requests,
                "attempts": attempts,
                "status": "failed",
                "raw_chars": len(last_raw),
                "error": last_error or "unknown_invalid_response",
            }

        batch_results = (
            await asyncio.gather(
                *(
                    _run_stage2_batch(batch_no, enum_batch)
                    for batch_no, enum_batch in enumerate(enum_batches, start=1)
                )
            )
            if enum_batches
            else []
        )
        parsed_by_enum: dict[str, Any] = {}
        failed_batches: list[dict[str, Any]] = []
        for result in sorted(batch_results, key=lambda item: item["batch_no"]):
            diag["llm_requests"].extend(result["requests"])
            diag[f"main_extract_batch_{result['batch_no']}_raw_chars"] = result["raw_chars"]
            if result["status"] != "completed":
                failed_batches.append(result)
                continue
            parsed_by_enum.update(result["parsed"])
        diag["stage2_retry_count"] = sum(max(0, int(item["attempts"]) - 1) for item in batch_results)
        diag["stage2_invalid_batches"] = [
            {"batch_no": item["batch_no"], "error": item["error"]} for item in failed_batches
        ]
        if failed_batches:
            yield {
                "type": "status",
                "stage": "two_stage_partial",
                "message": f"Не удалось восстановить батчей: {len(failed_batches)}",
            }

        detected_max_report_year = max_report_year
        max_report_year, model_period_support, model_period_threshold = (
            bulk._model_supported_max_report_year(
                parsed_by_enum,
                detected_max_report_year=detected_max_report_year,
                total_metrics=len(extractable),
            )
        )
        if model_period_support:
            diag["model_period_year_support"] = {
                str(year): support for year, support in model_period_support.items()
            }
            diag["model_period_support_threshold"] = model_period_threshold
        if max_report_year != detected_max_report_year:
            diag["model_supported_report_year_override"] = {
                "detected": detected_max_report_year,
                "trusted": max_report_year,
            }

        for metric in extractable:
            enum = str(metric["enum"]).upper()
            info = parsed_by_enum.get(enum) or {}
            period_values, metric_unit = bulk._coerce_period_values(
                info,
                enum=enum,
                period_current=period_current,
                period_prev=period_prev,
                default_unit=default_unit,
                abs_cost=False,
                max_report_year=max_report_year,
                period_calendar=period_calendar,
            )
            for pv in period_values:
                if pv.get("value") is not None:
                    extracted_by_period.setdefault(pv["period"], {})[enum] = pv.get("value")
            extractable_period_values_by_enum[enum] = period_values
            v_cur = bulk._value_for_period(period_values, period_current, period_calendar)
            if v_cur is None:
                v_cur = bulk._first_value(period_values)
            v_prv = bulk._value_for_period(period_values, period_prev, period_calendar)
            extracted_current[enum] = v_cur
            extracted_prev[enum] = v_prv
            row = bulk._apply_metric_source_forms(
                {
                    "type": "metric",
                    "enum": enum,
                    "name_ru": metric["name_ru"],
                    "value_current": v_cur,
                    "value_prev": v_prv,
                    "period_values": period_values,
                    "unit": (period_values[0].get("unit") if period_values else None) or metric_unit,
                    "found": any(p.get("value") is not None for p in period_values),
                    "is_calculated": False,
                    "formula": "",
                    # Страница: сначала та, что назвала модель, потом — та, что мы
                    # знаем сами со стадии сжатия. Второе срабатывает всегда, когда
                    # первое пусто, а пусто оно по устройству шаблона (см.
                    # `_page_from_evidence`). Без этого колонка «Источник» пуста при
                    # полностью удачном извлечении.
                    "page": (bulk._page_from_period_values(period_values)
                             or _page_from_evidence(enum, compression, table_by_id)),
                },
                source_form_by_page,
            )
            # Пустую строку двойного enum'а придерживаем: у него есть формула, и
            # она ещё не считалась. Уйдёт эта строка или расчётная — решится ниже.
            if enum in dual_enums and not row.get("found"):
                deferred_dual[enum] = row
                continue
            yield row

        if include_top_borrowers:
            from finengine.skills import top_borrowers as _top_borrowers

            try:
                top_n = max(1, int(getattr(settings, "top_borrowers_count", 5) or 5))
            except Exception:
                top_n = 5
            top_borrowers_t0 = time.time()
            # ⚠️ Этой ветке нужен ПУТЬ, а сайдкар живёт объектом — кладём его во
            # временный файл на время вызова. `document_idp_file` отдаёт `None`,
            # когда ответа OCR нет: прежний код передавал `None` в том же
            # случае, поведение то же.
            from finengine.store.blobs import document_idp_file  # noqa: PLC0415
            try:
                with document_idp_file(doc_id) as _ocr_файл:
                    top_period_values, top_diag = await _top_borrowers.extract_top_borrowers_period_values(
                        pdf_text_for_prompt,
                        ocr_json_path=_ocr_файл,
                        top_n=top_n,
                        periods_detected=periods_detected,
                        period_current=period_current,
                        period_prev=period_prev,
                        default_unit=default_unit,
                        model_id=stage2_model_id,
                        debug_dir=debug_dir,
                        stream_fn=llm.stream,
                    )
                diag.update(top_diag)
                if "top_borrowers_llm_s" in top_diag:
                    diag["llm_requests"].append(
                        _llm_request_record(
                            stage="top_borrowers",
                            model_id=stage2_model_id,
                            started_at=top_borrowers_t0,
                            elapsed_s=float(top_diag.get("top_borrowers_llm_s") or 0.0),
                            usage={
                                "model": stage2_model_id,
                                "in": int(top_diag.get("top_borrowers_tokens_in") or 0),
                                "out": int(top_diag.get("top_borrowers_tokens_out") or 0),
                                "cached": int(top_diag.get("top_borrowers_tokens_cached") or 0),
                                "cost": top_diag.get("top_borrowers_cost_usd"),
                            },
                        )
                    )
            except Exception as exc:
                top_period_values = []
                diag["top_borrowers_error"] = str(exc)[:500]
            if top_period_values:
                from finengine.skills.top_borrowers import TOP_BORROWERS_ENUM

                extractable_period_values_by_enum[TOP_BORROWERS_ENUM] = top_period_values
                for pv in top_period_values:
                    if pv.get("value") is not None:
                        extracted_by_period.setdefault(pv["period"], {})[TOP_BORROWERS_ENUM] = pv.get(
                            "value"
                        )
                top_current = bulk._value_for_period(top_period_values, period_current, period_calendar)
                if top_current is None:
                    top_current = bulk._first_value(top_period_values)
                extracted_current[TOP_BORROWERS_ENUM] = top_current
                extracted_prev[TOP_BORROWERS_ENUM] = bulk._value_for_period(
                    top_period_values,
                    period_prev,
                    period_calendar,
                )
                yield {
                    "type": "hidden_metric",
                    "enum": TOP_BORROWERS_ENUM,
                    "name_ru": (top_borrowers_metric or {}).get("name_ru")
                    or "Топ заемщиков по секторам экономики",
                    "value_current": top_current,
                    "value_prev": extracted_prev[TOP_BORROWERS_ENUM],
                    "period_values": top_period_values,
                    "unit": (top_period_values[0].get("unit") if top_period_values else None)
                    or default_unit,
                    "found": True,
                    "is_calculated": False,
                    "formula": "",
                    "page": bulk._page_from_period_values(top_period_values),
                }
            elif top_borrowers_metric:
                extracted_current.setdefault(TOP_BORROWERS_ENUM, None)
                extracted_prev.setdefault(TOP_BORROWERS_ENUM, None)

        yield {"type": "status", "stage": "table_build", "message": "Формируем таблицу показателей"}
        calc_t0 = time.time()
        # ПРОИСХОЖДЕНИЕ ЕДЕТ ВТОРЫМ КАНАЛОМ (Б5, 19.08.2026): числа возвращаются
        # прежними словарями, а «по какой формуле и по каким числам» — этими тремя.
        # Каналов три, потому что значение строки берётся из трёх проходов, и
        # подписать строку происхождением другого прохода значило бы соврать.
        trace_current: dict[str, dict] = {}
        trace_calc_by_period: dict[str, dict] = {}
        trace_display_by_period: dict[str, dict] = {}
        calc_current, calc_prev = bulk._calculate_metrics_pair(
            calculated_defs,
            extracted_current,
            extracted_prev,
            formula_default_abs_enums=formula_default_abs_enums,
            trace_current=trace_current,
            period_current=period_current,
            period_prev=period_prev,
        )
        diag["calc_s"] = round(time.time() - calc_t0, 2)
        all_periods = bulk._filter_plausible_report_periods(
            sort_periods(list(extracted_by_period.keys())) or periods_detected,
            max_report_year=max_report_year,
        )
        calc_by_period = bulk._calculate_metrics_for_periods(
            calculated_defs,
            bulk._with_point_values_for_duration_calculations(extracted_by_period, all_periods),
            all_periods,
            formula_default_abs_enums=formula_default_abs_enums,
            period_calendar=period_calendar,
            trace_by_period=trace_calc_by_period,
        )
        display_calc_by_period = bulk._calculate_metrics_for_periods(
            calculated_defs,
            extracted_by_period,
            all_periods,
            formula_default_abs_enums=formula_default_abs_enums,
            period_calendar=period_calendar,
            trace_by_period=trace_display_by_period,
        )
        calc_period_kinds = bulk._calculated_period_kind_map(calculated_defs)
        for enum, name_ru, formula, _deps in calculated_defs:
            # Двойной enum: строку нашли в отчёте — формула молчит, извлечённое
            # значение старше расчётного. Не нашли — строка ждёт в `deferred_dual`.
            enum = str(enum).strip().upper()
            if enum in dual_enums and enum not in deferred_dual:
                continue
            # Единицу расчётной строки называет РЕЕСТР (`unit_kind`), а не догадка
            # по виду формулы: «* 100» узнаёт проценты, но безразмерное отношение
            # и дни оборачиваемости от денег не отличает. Та же функция, что у
            # двух других производителей расчётных строк.
            unit_calc = bulk.unit_for_calculated(
                formula, default_unit, calc_meta.get(enum, {}).get("unit_kind", "")
            )
            do_annualize = bool(calc_meta.get(enum, {}).get("annualize"))
            source_calc_by_period = (
                display_calc_by_period
                if calc_period_kinds.get(str(enum).strip().upper()) == POINT_KIND
                else calc_by_period
            )
            source_trace = (
                trace_display_by_period
                if calc_period_kinds.get(str(enum).strip().upper()) == POINT_KIND
                else trace_calc_by_period
            )
            # Происхождение берём из ТОГО ЖЕ прохода, что дал значение строки:
            # парный проход отметился в `trace_current` ровно тогда, когда вернул
            # число, а иначе значение пришло колонкой `period_current`.
            prov = trace_current.get(enum) or source_trace.get(period_current, {}).get(enum) or {}
            # КАЖДАЯ КОЛОНКА ПОДНИМАЕТСЯ ПО СВОЕЙ ДЛИНЕ, а не по длине основного
            # периода: в одной таблице стоят и Q1, и 9М, и год, и общий множитель
            # завысил бы годовую колонку вчетверо.
            period_values = [
                {"period": p, "value": bulk.annualized(source_calc_by_period.get(p, {}).get(enum), p, do_annualize), "unit": unit_calc, "page": None, "source_text": ""}
                for p in all_periods
                if source_calc_by_period.get(p, {}).get(enum) is not None
            ]
            # Приведение накладывается ТОЛЬКО на число парного прохода: запасной
            # выход уже читает `period_values`, поднятые выше, и второй множитель
            # дал бы шестнадцатикратную рентабельность на квартале.
            v_cur = calc_current.get(enum)
            v_cur = (bulk._value_for_period(period_values, period_current, period_calendar)
                     if v_cur is None
                     else bulk.annualized(v_cur, period_current, do_annualize))
            v_prv = calc_prev.get(enum)
            v_prv = (bulk._value_for_period(period_values, period_prev, period_calendar)
                     if v_prv is None
                     else bulk.annualized(v_prv, period_prev, do_annualize))
            # Приведение к году делается ПРИ ВЫДАЧЕ, а входы сняты за период: без
            # множителя в подписи арифметика «входы → напечатанное» не сходится, и
            # у 40 показателей реестра расхождение выглядит ошибкой расчёта.
            note = bulk.annualization_note(period_current) if do_annualize else ""
            formula_shown = prov.get("formula") or formula
            row = bulk._apply_metric_source_forms(
                {
                    "type": "metric",
                    "enum": enum,
                    "name_ru": name_ru,
                    "value_current": v_cur,
                    "value_prev": v_prv,
                    "period_values": period_values,
                    "unit": unit_calc,
                    "found": (calc_current.get(enum) is not None) or bool(period_values),
                    "is_calculated": True,
                    "formula": f"({formula_shown}) * {note}" if note else formula_shown,
                    "inputs": prov.get("inputs") or [],
                },
                source_form_by_page,
            )
            held = deferred_dual.pop(enum, None)
            # Формула тоже не дала числа — отдаём придержанную строку извлечения:
            # пустая строка «искали и не нашли» пользователю нужна, а подменять её
            # пустой расчётной незачем, у той ещё и формула в подписи.
            yield row if (held is None or row.get("found")) else held

        if segment_task is not None:
            # НАДПИСЬ ПРО НАСТОЯЩЕЕ, А НЕ ПРО ПРОШЛОЕ. Сжатие сегментных
            # раскрытий идёт с первой секунды прогона, параллельно основному, и
            # к этому месту оно давно позади (на ПСБ: 83 с сегментной цепочки
            # против 407 с основной). Прежнее «Сжимаем контекст сегментных
            # раскрытий» стало бы враньём — пользователю сообщали бы о работе,
            # которая закончилась. Сдвинуть надпись к старту стадии нельзя, не
            # переставив порядок статусов на экране; значит правдой должно быть
            # слово. Здесь и правда идёт сбор.
            yield {
                "type": "status",
                "stage": "two_stage_segment_compression",
                "message": "Собираем разбивку по сегментам",
            }
            segment_events: list[dict] = []
            # Отказ сегментной стадии теперь может случиться РАНЬШЕ основного
            # извлечения, а не после него. Задача держит исключение при себе до
            # `await` — прогон не рушится, отказ приезжает сюда, в
            # `segment_error`, а плоские показатели пользователь получает.
            try:
                collected_segment_events = await segment_task
            except Exception as exc:
                collected_segment_events = []
                diag["segment_error"] = f"{type(exc).__name__}: {exc}"[:300]
            for segment_event in collected_segment_events:
                if segment_event.get("type") == "segment_diag":
                    segment_diag = segment_event.get("_diag_segment") or {}
                    main_llm_requests = list(diag.get("llm_requests") or [])
                    diag.update(segment_diag)
                    diag["llm_requests"] = [
                        *main_llm_requests,
                        *(segment_diag.get("llm_requests") or []),
                    ]
                    continue
                if segment_event.get("type") == "error":
                    diag["segment_error"] = segment_event.get("message", "")
                    continue
                segment_events.append(segment_event)
            for segment_event in segment_events:
                yield bulk._apply_metric_source_forms(segment_event, source_form_by_page)

        if consolidated_group_task is not None:
            try:
                consolidated_group_events = await consolidated_group_task
            except Exception as exc:
                consolidated_group_events = []
                diag["consolidated_group_error"] = f"{type(exc).__name__}: {exc}"[:300]
            for group_event in consolidated_group_events:
                if group_event.get("type") == "consolidated_group_diag":
                    group_diag = group_event.get("_diag_consolidated_group") or {}
                    existing_requests = list(diag.get("llm_requests") or [])
                    diag.update(group_diag)
                    diag["llm_requests"] = [
                        *existing_requests,
                        *(group_diag.get("llm_requests") or []),
                    ]
                    continue
                if group_event.get("type") == "error":
                    diag["consolidated_group_error"] = group_event.get("message", "")
                    continue
                yield bulk._apply_metric_source_forms(group_event, source_form_by_page)

        display_period_current, display_period_prev = bulk._select_display_periods(
            all_periods or periods_detected,
            period_current,
            period_prev,
            period_calendar=period_calendar,
        )
        llm_requests = list(diag.get("llm_requests") or [])
        diag.update(
            {
                "llm_calls": len(llm_requests),
                "tokens_in": sum(int(item.get("tokens_in") or 0) for item in llm_requests),
                "tokens_out": sum(int(item.get("tokens_out") or 0) for item in llm_requests),
                "tokens_cached": sum(int(item.get("tokens_cached") or 0) for item in llm_requests),
                "cost_usd": round(sum(float(item.get("cost_usd") or 0.0) for item in llm_requests), 6),
                "estimated_cost_usd": round(
                    sum(float(item.get("estimated_cost_usd") or 0.0) for item in llm_requests),
                    6,
                ),
                "llm_s": round(sum(float(item.get("elapsed_s") or 0.0) for item in llm_requests), 1),
                "wall_s": round(time.time() - run_t0, 1),
            }
        )
        yield {
            "type": "done",
            "period_current": display_period_current,
            "period_prev": display_period_prev,
            "periods": all_periods or periods_detected,
            "period_calendar": period_calendar,
            "unit": default_unit,
            "company_type": effective_type,
            "company_name": company_display or getattr(doc, "company_name", ""),
            "ocr_engine": getattr(doc, "ocr_engine", "") or "",
            "_diag": diag,
        }
    finally:
        # ГЕНЕРАТОР МОГУТ БРОСИТЬ НЕДОЧИТАННЫМ: пользователь нажал «Стоп», ход
        # кончился по таймауту, потребитель упал ниже по цепочке. Фоновые
        # задачи это не гасит — они переживают и генератор, и прогон. Пробник
        # на брошенном генераторе: после `aclose()` сегментная стадия доезжала
        # до КОНЦА, сходив в модель ещё дважды (`segment_compression` с полным
        # текстом отчёта и `segment_extract_batch_001`). Это деньги владельца и
        # место в очереди провайдера за работу, которую уже некому прочитать.
        #
        # До параллельного запуска изъяна не было: сегментную стадию крутил сам
        # генератор, и `aclose()` останавливал её на первом же `await`. Уборка
        # здесь — цена параллельности.
        for background_task in (segment_task, consolidated_group_task):
            if background_task is None:
                continue
            if not background_task.done():
                background_task.cancel()
            elif not background_task.cancelled():
                # Забрать исключение у доделанной задачи, иначе asyncio пишет в
                # лог «Task exception was never retrieved» и путает разбор.
                background_task.exception()


async def extract_stream_finengine_dev_two_stage(
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
    extraction_parts: int = 1,
    registry_override: Optional[dict] = None,
    extraction_batch_jobs: int = 1,
    extraction_batch_size: int | None = None,
    extraction_batch_attempts: int = 3,
    extraction_batch_strategy: str = "balanced",
    extraction_ask_without_evidence: bool = True,
    segment_extraction_batch_size: int = 5,
) -> AsyncGenerator[dict, None]:
    # 🔧 НАША ПРАВКА, ПЕРВОЕ ОТСТУПЛЕНИЕ ОТ ПОБАЙТОВОГО ПЕРЕНОСА (#257, 16.08).
    #
    # `registry_override` внутри УЖЕ поддержан (см. приём на ~1858 и разбор на
    # ~1386), но публичный вход его не объявлял и не пробрасывал. Наш боевой
    # путь передаёт сюда реестр ЯЧЕЙКИ — правимый аналитиком файл, ради
    # которого сделаны #147 и #224/#250 (8 ячеек, 600/600 записей). Без этого
    # параметра двухстадийная схема читала бы СТАРЫЕ глобальные реестры, правки
    # аналитика молча переставали бы доезжать до промпта, а замер сравнивал бы
    # «наш движок с реестром ячейки» против «их движка без него» — то есть
    # отвечал бы не на тот вопрос.
    #
    # ⚠️ `calculated_meta` ячейки (признак `annualize`, слово `unit_kind`) едет
    # отдельно — собирается в `_prepare_two_stage_run` и применяется в цикле
    # выдачи расчётных строк. Здесь — только проброс.
    async for event in _extract_stream_two_stage_full_context(
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
        extraction_parts=extraction_parts,
        registry_override=registry_override,
        # 🔧 ВТОРОЕ ОТСТУПЛЕНИЕ ОТ ПОБАЙТОВОГО ПЕРЕНОСА (17.08). Внутри ручка
        # была, публичный вход её не объявлял — и батчи второй стадии шли по
        # одному. Одно значение задаёт темп ОБЕИМ стадиям: основной (здесь) и
        # сегментной (она получает его от `_extract_stream_two_stage_full_context`).
        extraction_batch_jobs=extraction_batch_jobs,
        # 🔧 ТРЕТЬЕ ОТСТУПЛЕНИЕ (20.08). Темп сдерживал одну задачу: без
        # `extraction_batch_size` внутренняя реализация уходит в ветку
        # `_split_evenly(extractable, extraction_parts=1)` — ровно один батч на
        # весь состав ячейки. Размер батча включает шардирование, попытки и
        # сегментный размер идут с ним одним набором.
        extraction_batch_size=extraction_batch_size,
        extraction_batch_attempts=extraction_batch_attempts,
        # 🔧 ЧЕТВЁРТОЕ ОТСТУПЛЕНИЕ (20.08). Стратегия деления состава по
        # батчам — настройка, а не константа: балансировка по ожидаемому
        # выходу меняет СОСТАВ батчей, то есть качество, и владелец обязан иметь
        # возможность вернуть прежнее деление одной переменной окружения
        # (`FINENGINE_EXTRACT_BATCH_STRATEGY=spread`), без пересборки.
        extraction_batch_strategy=extraction_batch_strategy,
        # 🔧 ЧЕТВЁРТОЕ ОТСТУПЛЕНИЕ (20.08). Upstream вместе с размером батча
        # включает и выбрасывание показателей, которым первая стадия не нашла
        # улик; у нас это стоило пяти показателей на замере (разбор — в
        # `config.extract_ask_without_evidence`). Ручка разводит два решения,
        # которые у upstream слиты в одно.
        extraction_ask_without_evidence=extraction_ask_without_evidence,
        segment_extraction_batch_size=segment_extraction_batch_size,
    ):
        yield event
