"""Полноконтекстное извлечение — боевой поток прогона.

Реестр ячейки режется на шарды по 20 показателей и уходит в модель
параллельно; рядом, тоже параллельно, идут две отдельные стадии — разрезы и
состав холдинга. На документе банка это 12 вызовов на показатели плюс два.
"""
from __future__ import annotations

from pathlib import Path
import asyncio
import contextlib
import os
from typing import AsyncGenerator, Callable, Optional
from finengine.llm import openrouter as llm
from finengine.skills.periods import POINT_KIND, sort_periods
from finengine.skills.metric_schema import merge_aliases
from ._log import _LOG
from .company_detection import _detect_company_from_text, _detect_company_type, _prefer_detected_company_name
from .consolidated_group import _extract_consolidated_group_full_context
from .debug_io import _write_llm_debug_file
from .formula_engine import _calculate_metrics_for_periods, _calculate_metrics_pair, _calculated_period_kind_map, _sanity_note, _with_point_values_for_duration_calculations, annualization_note, annualized, unit_for_calculated
from .json_parse import _parse_extract_json
from .period_selection import _filter_plausible_report_periods, _max_report_anchor_year, _select_display_periods, _select_run_periods
from .period_values import _coerce_period_values, _first_value, _main_total_fallback_event, _page_from_period_values, _value_for_period
from .segments_context import _detect_reporting_standard, _role_wants_segments, _segment_metric_scope
from .segments_llm import _extract_segment_breakdown_full_context
from .source_forms import SOURCE_FORM_BALANCE, SOURCE_FORM_CASH_FLOW, SOURCE_FORM_EQUITY, SOURCE_FORM_NOTES, SOURCE_FORM_PNL, _apply_metric_source_forms, _build_source_form_by_page
from .streams_metric_iter import _metric_block_iter
from .value_parsing import _default_unit_for_standard


def _merge_aliases_from_base(rows: list, company_type: str, base_rows: list | None = None) -> list:
    """Строки ячейки + синонимы из общего реестра.

    Ячейка — авторитет по составу, описаниям и подсказкам «где искать»: их
    команды правили осознанно под свою отчётность. Но синонимы там только
    русские, а общий реестр несёт ещё и латиницу. Замер 01.08.2026: переход на
    ячейку «в лоб» терял 169 латинских синонимов у всех 78 показателей
    ifrs_bank_ru и 192 у 75 в ifrs_nf_ru. Русские отчёты по МСФО часто
    двуязычные — терять узнавание английских заголовков нельзя.

    Порядок внутри списка стабильный (сначала свои, потом добавленные), иначе
    промпт менялся бы от запуска к запуску и эталон стал бы бесполезен.
    Показателя нет в общем реестре (новый, заведён ячейкой) — берётся как есть.

    `base_rows` — общий реестр СНАРУЖИ, а не из импорта модуля. Нужен паспорту
    прогона (#155): отпечаток правил снимается с копии `metrics.json` из полки,
    уехавшей в станцию, и повторять слияние вторым, похожим кодом нельзя — иначе
    паспорт называл бы состав синонимов, которого движок не видел. Пусто —
    поведение прежнее: берём общий реестр этого процесса по типу компании.
    """
    if base_rows is None:
        from finengine.skills.metric_registry import (  # noqa: PLC0415
            EXTRACTABLE_BANK,
            EXTRACTABLE_NON_FINANCIAL,
        )

        base_rows = EXTRACTABLE_BANK if company_type == "bank" else EXTRACTABLE_NON_FINANCIAL
    base = {str(m.get("enum", "")).strip().upper(): m for m in base_rows if isinstance(m, dict)}

    out = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        src = base.get(str(row.get("enum", "")).strip().upper())
        if src is None:
            out.append(row)
            continue
        merged = dict(row)
        for key in ("aliases_ru", "aliases_en", "aliases"):
            own = list(row.get(key) or [])
            seen = {str(a).strip().casefold() for a in own}
            for a in (src.get(key) or []):
                if str(a).strip().casefold() not in seen:
                    seen.add(str(a).strip().casefold())
                    own.append(a)
            if own:
                merged[key] = own
        out.append(merged)
    return out

async def _extract_stream_full_context(
    doc_id: str,
    company_type: str,
    index=None,
    limit: Optional[int] = None,
    model_id: Optional[str] = None,
    include_segments: bool = False,
    include_consolidated_group: bool = False,
    metric_enums: Optional[set[str]] = None,
    full_registry: bool = False,
    role: str = "",
    segment_mode: str = "extended",
    segment_context_mode: str = "full",
    debug_dir: Optional[Path] = None,
    system_prompt_override: Optional[str] = None,
    user_prompt_builder: Optional[Callable[..., str]] = None,
    segment_metrics_override: Optional[set[str]] = None,
    segment_rules_extra: Optional[str] = None,
    default_unit_override: Optional[str] = None,
    fallback_period_label_override: Optional[str] = None,
    registry_override: Optional[dict] = None,
) -> AsyncGenerator[dict, None]:
    """Full-context extraction: send whole PDF text + metric registry → JSON.

    Replaces the per-metric RAG loop (снят 13.08). Single LLM call returns a JSON
    object {METRIC_ENUM: {values: {period: raw_cell}, unit_text, page}} which
    is fanned out to the same per-metric event shape as the legacy stream plus
    `period_values` for multi-period consumers.

    Scope warning: the generic full-context entrypoint keeps the legacy
    AIFA-trained 55-metric subset when neither ``metric_enums`` nor
    ``full_registry`` is supplied. Finengine DEV callers must use
    ``extract_stream_finengine_dev`` so production and quality tests cannot
    silently drift back to that compatibility scope.
    """
    import json as _json

    if index is None:
        from finengine.ingestion.document_store import get_store

        index = get_store()

    doc = index.get_document(doc_id)
    if not doc:
        yield {"type": "error", "message": f"Document {doc_id} not found"}
        return

    # Текст пишется фоновой задачей после загрузки — ждём его, а не отбиваем
    # сразу. Признак жизни подаём ДО ожидания: иначе человек видит тишину.
    from finengine.store.blobs import document_text, document_text_when_ready  # noqa: PLC0415

    pdf_text = document_text(doc_id)
    if pdf_text is None:
        yield {
            "type": "status",
            "stage": "document_preparation",
            "message": "Подготавливаем документ к анализу",
        }
        pdf_text = await document_text_when_ready(doc_id, timeout_s=900, poll_s=1.5)
        if pdf_text is None:
            yield {
                "type": "error",
                "message": (
                    "Документ ещё распознаётся (превышено окно ожидания 15 минут). "
                    "Большие отчёты (100+ страниц) распознаются дольше — "
                    "подождите 1-2 минуты и нажмите «Извлечь» снова. "
                    "Повторно загружать документ не нужно."
                ),
            }
            return
    if "===PAGE " not in pdf_text:
        from finengine.store.blobs import document_idp_raw  # noqa: PLC0415

        raw_idp = document_idp_raw(doc_id)
        if raw_idp is not None:
            from finengine.skills.aifa_baseline.idp_ocr_client import (  # noqa: PLC0415
                markdown_from_saved_idp_response,
            )
            try:
                со_страницами = markdown_from_saved_idp_response(_json.loads(raw_idp))
                if со_страницами.strip():
                    pdf_text = со_страницами
            except (ValueError, KeyError, TypeError):
                # Плоский текст остаётся законным запасным вариантом для
                # старых загрузок — разбор без ссылок лучше отказа.
                pass

    # Try to rebuild a *paginated* version of the doc from stored chunks so
    # the LLM can cite a page per metric (`Источник` column on the UI).
    # Fallback to the flat sidecar text when no chunk metadata is available
    # — the prompt then drops the page-citation instructions and `page`
    # comes back as null.
    by_page: dict[int, list[str]] = {}
    for kind in ("text", "financial_table", "table"):
        try:
            chunks_p = index.get_chunks_by_type(kind, doc_id=doc_id)
        except Exception:
            chunks_p = []
        for c in chunks_p:
            p = c.metadata.get("page", 0) if hasattr(c, "metadata") else 0
            try:
                p = int(p or 0)
            except Exception:
                p = 0
            if p > 0:
                by_page.setdefault(p, []).append(c.content or "")
    paginated_text = ""
    if by_page:
        parts = []
        for p in sorted(by_page.keys()):
            parts.append(f"===PAGE {p}===\n" + "\n\n".join(by_page[p]))
        paginated_text = "\n\n".join(parts)
    has_sidecar_pages = "===PAGE " in pdf_text
    has_pages = bool(paginated_text) or has_sidecar_pages
    pdf_text_for_prompt = paginated_text or pdf_text

    effective_type = company_type
    if company_type in ("unknown", "auto", ""):
        effective_type = _detect_company_type(pdf_text)

    calc_meta: dict[str, dict] = {}
    if registry_override is not None:
        # Per-cell registry: the calling cell resolved its own base + overrides
        # (hints/aliases/add/drop) via registry_for_profile and passes the rows
        # in.
        #
        # ⚠️ СИНОНИМЫ ОБЪЕДИНЯЮТСЯ, А НЕ ЗАМЕЩАЮТСЯ (замер 01.08.2026). Файлы
        # ячеек команды правили под свою отчётность, и подсказки «где искать»
        # там ЛУЧШЕ общих. Но список синонимов в них — только русский: переход
        # «в лоб» терял 169 латинских синонимов у ВСЕХ 78 показателей
        # ifrs_bank_ru и 192 у 75 в ifrs_nf_ru — Total assets, PP&E,
        # Long-term borrowings, Qualified opinion. Русские отчёты по МСФО часто
        # ДВУЯЗЫЧНЫЕ, и половина крупных эмитентов печатает таблицы с
        # английскими заголовками, поэтому такая потеря бьёт по узнаванию.
        # Берём лучшее с обеих сторон: состав, описания и подсказки — из ячейки,
        # синонимы — объединением с общим реестром.
        extractable = _merge_aliases_from_base(
            registry_override.get("extractable") or [], effective_type
        )
        calculated_defs = registry_override.get("calculated") or []
        # Единица и приведение к году живут в ячейке рядом с формулой (#146) —
        # хардкод-реестры ниже такой подсказки не несут, там работает только
        # вывод по структуре формулы.
        calc_meta = registry_override.get("calculated_meta") or {}
    else:
        # ЯЧЕЙКА НЕ ПЕРЕДАНА — ячейка по умолчанию, а не легаси `data/metrics.json`
        # (21.08.2026). Копия расходилась молча: 12 формул у банка и 2 у
        # нефинансовой давали другое число под тем же именем, а после перевода
        # доходностей в проценты — в сто раз меньшее. Легаси при этом строгое
        # ПОДМНОЖЕСТВО ячейки, так что замена ничего не отнимает и приносит
        # `calculated_meta` — единицу и приведение к году, которых у хардкода
        # не было вовсе (#146).
        from finengine.skills.metric_schema import registry_for_profile

        _fallback = registry_for_profile(
            "ifrs_bank_ru" if effective_type == "bank" else "ifrs_nf_ru")
        extractable = list(_fallback["extractable"])
        calculated_defs = list(_fallback["calculated"])
        calc_meta = _fallback.get("calculated_meta") or {}
        if effective_type != "bank" and not (metric_enums or full_registry):
            # Сужение до 55 показателей, на которых обучался реранкер AIFA.
            # ⚠️ Само по себе это ОСТАТОК снятой архитектуры (первый эшелон,
            # «Остатки AIFA-архитектур, TRAINED_NF_55») — здесь только
            # сохранено прежнее поведение, решение о нём отдельное.
            from finengine.skills.aifa.scope import filter_to_trained
            extractable = filter_to_trained(extractable)

    if metric_enums:
        wanted = {str(item).strip().upper() for item in metric_enums if str(item).strip()}
        if wanted:
            # Показатель, которого нет в строках реестра, отсеивается ЗДЕСЬ и до
            # #147 исчезал молча: пользователь заводил его в свою ячейку, ждал
            # в таблице и не получал ни строки, ни объяснения. Молчание тут
            # хуже пропажи — правку не с чем связать. Считаем зазор ДО фильтра
            # и говорим о нём вслух (событие доезжает до чата через тул).
            _known = {str(m.get("enum", "")).strip().upper() for m in extractable}
            _known |= {str(m[0]).strip().upper() for m in calculated_defs}
            _missing = sorted(wanted - _known)
            extractable = [m for m in extractable if str(m.get("enum", "")).strip().upper() in wanted]
            calculated_defs = [m for m in calculated_defs if str(m[0]).strip().upper() in wanted]
            if _missing:
                yield {
                    "type": "status",
                    "stage": "registry_gap",
                    "missing_enums": _missing,
                    "message": (
                        f"В реестре нет описания для {len(_missing)} запрошенных показателей "
                        f"({', '.join(_missing[:8])}{'…' if len(_missing) > 8 else ''}) — "
                        "они не попадут в промпт. Добавьте строку показателя в файл своей "
                        "скомпилированного effective registry ячейки."
                    ),
                }

    segment_extractable = list(extractable)

    if limit is not None and limit > 0:
        extractable = extractable[:limit]
        calculated_defs = []

    # 🔴 ЗАПАСНОЙ ДВИЖОК СЧИТАЕТ НЕ ТО ЖЕ, ЧТО БОЕВОЙ (названо 20.08.2026).
    #
    # Здесь стоит безусловный снос Finengine: показатель, заведённый И
    # извлекаемым, И расчётным, теряет расчётную половину ДО прогона. В боевом
    # `two_stage_extractor` этот снос заменён правилом «извлечённое старше
    # расчётного» (`_dual_nature_enums` + `deferred_dual`, 19.08): обе половины
    # живут, формула считает, когда строку в отчёте не нашли.
    #
    # Значит на ЭТОМ пути формул двойной природы нет вовсе — 35 пар в девяти
    # ячейках, включая EBITDA, CAPEX, валовую прибыль и агрегат ценных бумаг
    # `ifrs_bank_ru/SECURITIES_ASSETS`. Путь не мёртвый: `config.py` объявляет
    # `FINENGINE_EXTRACT_ARCH=full_context` штатным откатом «одной переменной».
    # То есть откат чинит одно и молча теряет другое.
    #
    # Не переносим сюда правило намеренно: перенос тянет и вторую половину
    # механизма (придержанная пустая строка, `deferred_dual`), а без неё в
    # таблице появятся две строки с одним именем — ровно дефект #163, ради
    # которого этот снос и стоял. Откат — аварийный режим; чинить его вслепую
    # опаснее, чем знать про его цену. Знать — теперь можно.
    extracted_enum_set = {str(m.get("enum", "")).strip().upper() for m in extractable}
    if extracted_enum_set:
        calculated_defs = [
            m for m in calculated_defs
            if str(m[0]).strip().upper() not in extracted_enum_set
        ]

    total_metrics = len(extractable) + len(calculated_defs)

    periods_detected, period_current, period_prev = _select_run_periods(
        pdf_text,
        doc.period,
        fallback_period_label=fallback_period_label_override or "Текущий",
    )
    max_report_year = _max_report_anchor_year(periods_detected)
    reporting_standard = _detect_reporting_standard(pdf_text_for_prompt)
    default_unit = default_unit_override or _default_unit_for_standard(reporting_standard)
    source_form_by_page = _build_source_form_by_page(
        pdf_text_for_prompt,
        reporting_standard=reporting_standard,
        role=role,
    )

    # Auto-detect company name on every extract — covers documents uploaded
    # before the upload-time detector existed and the user-empty case.
    # Pick the best company name we can, then sanity-check against the
    # filename. The reconcile catches the subsidiary-vs-parent confusion
    # (e.g. ЧТПЗ 2024.pdf returning "ТМК" because the report repeats
    # "входит в Группу ТМК" on every page).
    detected_company = _detect_company_from_text(pdf_text)
    company_display = _prefer_detected_company_name(doc.company_name, detected_company, doc.filename)

    yield {
        "type": "start",
        "total_metrics": total_metrics,
        "total_extractable": len(extractable),
        "company_type": effective_type,
        "company_name": company_display,
        "unit": default_unit,
        "period_current": period_current,
        "period_prev": period_prev,
        "periods": periods_detected,
    }

    yield {
        "type": "status",
        "stage": "report_analysis",
        "message": "Анализируем отчет и сопоставляем показатели",
    }

    # Build the rich extraction prompt. Two sections:
    #   1) Информация о показателях — for each metric: name_ru/en, synonyms,
    #      description (что это), hint (где в отчёте искать).
    #   2) Схема ответа — JSON shape per ENUM.
    # The model gets concrete language variants AND search instructions, which
    # raises both recall (finds metric under non-canonical wording) and
    # precision (less likely to grab a wrong line that just happens to share a
    # word with the metric name).
    info_blocks: list[str] = []
    schema_lines: list[str] = []
    for m in extractable:
        # Info block
        block = [f"### {m['enum']}: {m['name_ru']}"]
        if m.get("name_en"):
            block.append(f"  (англ.: {m['name_en']})")
        aliases = merge_aliases(m, 6)
        if aliases:
            block.append(f"  Также может называться: {'; '.join(aliases)}")
        if m.get("description"):
            block.append(f"  Описание: {m['description']}")
        if m.get("hint"):
            block.append(f"  Где искать: {m['hint']}")
        # E-Reg-4: explicit gross/net contract. Renders only for metrics
        # the registry annotates — keeps prompt noise minimal.
        vb = m.get("value_basis")
        if vb == "gross":
            block.append("  ⚠️ БАЗИС: ВАЛОВОЕ значение (gross — ДО вычета резерва/амортизации). НЕ нетто.")
        elif vb == "net":
            block.append("  ⚠️ БАЗИС: НЕТТО значение (net — ПОСЛЕ вычета резерва/амортизации).")
        info_blocks.append("\n".join(block))

        # Schema line. Verbatim transcript schema (E-1 fix, 2026-05-05):
        # model returns the cell text AS-WRITTEN by period header.
        # Deterministic Python parser converts text→number, so the LLM
        # can never multiply by 1000 by accident (the Sber failure mode), and
        # deterministic period normalization handles shifted fiscal calendars.
        # `page` is the 1-based page number — present only when the prompt
        # is paginated with `===PAGE N===` markers.
        if has_pages:
            schema = (
                '{"values": {"<период как в шапке таблицы>": '
                '"<буквально как в ячейке или null>"}, '
                '"unit_text": "<буквально из шапки колонки/таблицы>", '
                '"page": <номер страницы или null>}'
            )
            schema_lines.append(
                f'"{m["enum"]}": {schema}'
            )
        else:
            schema = (
                '{"values": {"<период как в шапке таблицы>": '
                '"<буквально как в ячейке или null>"}, '
                '"unit_text": "<буквально из шапки колонки/таблицы>"}'
            )
            schema_lines.append(
                f'"{m["enum"]}": {schema}'
            )

    # Cell ownership hook: if a cell handed us its own system_prompt,
    # use it verbatim. Otherwise call into the shared legacy assembly —
    # same source of truth cells use, so RU-prompt strings live in one
    # place (_shared/legacy_assemblies.py) rather than being duplicated
    # here and there. Lazy import keeps bulk_extractor → extractors
    # acyclic.
    if system_prompt_override is not None:
        system_prompt = system_prompt_override
    else:
        from finengine.skills.extractors._shared.legacy_assemblies import (
            build_legacy_ru_system_prompt,
        )
        system_prompt = build_legacy_ru_system_prompt(reporting_standard)

    page_instructions = (
        "\n\n# Цитирование страниц\n"
        "Текст отчёта разбит на страницы маркерами вида `===PAGE N===`. Для "
        "каждой найденной метрики верни номер страницы N, на которой ты "
        "взял значение, в поле `page`. Если значение получено из нескольких "
        "страниц — укажи ту, где найден итог. Если значение не найдено — "
        "`page: null`."
        if has_pages
        else ""
    )

    # Same pattern as system_prompt: cell-provided builder → use it;
    # otherwise call the shared RU assembly so the prompt strings stay
    # in one place.
    if user_prompt_builder is not None:
        builder = user_prompt_builder
    else:
        from finengine.skills.extractors._shared.legacy_assemblies import (
            build_legacy_ru_user_prompt,
        )
        builder = build_legacy_ru_user_prompt
    # #117 Шардирование главного вызова. Замер показал: 240–334 с уходят на
    # СЕРИЙНУЮ генерацию выходных токенов одного вызова на весь реестр (75
    # показателей). Показатели друг от друга не зависят, поэтому реестр режется
    # на куски не больше FE_EXTRACT_SHARD_SIZE показателей (порядок реестра =
    # порядок форм отчётности, кусок остаётся тематически цельным) и вызовы
    # идут параллельно по тому же file-RPC, что уже используют сегменты и
    # консолидированная группа. Ручка именно РАЗМЕР куска, а не число кусков:
    # время вызова ≈ его выходные токены ≈ показатели в нём, поэтому потолок
    # размера — прямой потолок задержки и для полной ячейки (75 → 4 куска), и
    # для роли (21 → 2). Цена: текст отчёта уходит в модель N раз (вход
    # дороже), выигрыш — время. Лейн модели движка в infproxy поднят под это
    # (см. deploy/infproxy/README.md). 0 → прежнее поведение один-в-один.
    try:
        _shard_size = int(os.environ.get("FE_EXTRACT_SHARD_SIZE", "20"))
    except ValueError:
        _shard_size = 20
    if _shard_size > 0 and len(schema_lines) > _shard_size:
        shard_slices = [
            slice(i, min(i + _shard_size, len(schema_lines)))
            for i in range(0, len(schema_lines), _shard_size)
        ]
    else:
        shard_slices = [slice(0, len(schema_lines))]

    user_prompts = [
        builder(
            pdf_text_for_prompt=pdf_text_for_prompt,
            period_current=period_current,
            period_prev=period_prev,
            periods_detected=periods_detected,
            info_blocks=info_blocks[sl],
            page_instructions=page_instructions,
            schema_lines=schema_lines[sl],
            include_non_financial_industry_note=effective_type != "bank",
        )
        for sl in shard_slices
    ]
    _write_llm_debug_file(debug_dir, "main_system.md", system_prompt)
    for _i, _up in enumerate(user_prompts):
        _write_llm_debug_file(
            debug_dir,
            "main_prompt.md" if len(user_prompts) == 1 else f"main_prompt.{_i + 1}.md",
            _up,
        )

    def _coerce_num(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            s = v.strip().replace(" ", "").replace("\u00a0", "")
            # Russian decimal comma handling. If string has exactly one
            # comma followed by 1-2 digits → decimal: "951,5" → 951.5;
            # "12,34" → 12.34. If 3 digits after comma (or multiple commas)
            # → thousand grouping like "1,234,567" or "12,345" → strip.
            comma_count = s.count(",")
            if comma_count == 1:
                head, tail = s.split(",", 1)
                # 1-2 digits after the comma + plain digits before → decimal
                if 1 <= len(tail) <= 2 and tail.isdigit() and head.replace("-", "").replace(".", "").isdigit():
                    s = head + "." + tail
                else:
                    # Fall back: strip the comma (treat as thousand separator)
                    s = s.replace(",", "")
            elif comma_count > 1:
                # US-style thousands grouping like "1,234,567"
                s = s.replace(",", "")
            try:
                return float(s)
            except Exception:
                return None
        return None

    extracted_current: dict[str, Optional[float]] = {}
    extracted_prev: dict[str, Optional[float]] = {}
    extracted_by_period: dict[str, dict[str, Optional[float]]] = {}
    extractable_period_values_by_enum: dict[str, list[dict]] = {}
    metric_by_enum = {m["enum"]: m for m in extractable}
    valid_enums = set(metric_by_enum.keys())
    formula_default_abs_enums = {
        str(m.get("enum", "")).strip().upper()
        for m in extractable
        if m.get("formula_default_abs_behavior")
    }

    # Stage timings + first-tokens latency for the run logger.
    import time as _ttiming
    diag: dict = {
        "pdf_text_chars": len(pdf_text),
        "schema_lines": len(schema_lines),
        "llm_stream_s": 0.0,
        "first_token_s": None,
        "calc_s": 0.0,
        "include_consolidated_group": bool(include_consolidated_group),
        "source_forms_enabled": bool(source_form_by_page),
    }
    if source_form_by_page:
        diag["source_form_pages"] = {
            label: sum(1 for value in source_form_by_page.values() if value == label)
            for label in (
                SOURCE_FORM_BALANCE,
                SOURCE_FORM_PNL,
                SOURCE_FORM_CASH_FLOW,
                SOURCE_FORM_EQUITY,
                SOURCE_FORM_NOTES,
            )
        }
    usage_sink: dict = {"in": 0, "out": 0, "cached": 0}
    llm_t0 = _ttiming.time()
    consolidated_group_task: asyncio.Task[list[dict]] | None = None

    async def _collect_consolidated_group_events() -> list[dict]:
        events: list[dict] = []
        async for group_event in _extract_consolidated_group_full_context(
            pdf_text_for_prompt=pdf_text_for_prompt,
            period_current=period_current,
            period_prev=period_prev,
            company_display=company_display or doc.company_name or doc.filename,
            reporting_standard=reporting_standard,
            company_type=effective_type,
            model_id=model_id,
            debug_dir=debug_dir,
        ):
            events.append(group_event)
        return events

    if include_consolidated_group:
        consolidated_group_task = asyncio.create_task(_collect_consolidated_group_events())
        yield {
            "type": "status",
            "stage": "group_started",
            "message": "Ищем состав холдинга — отдельный вызов модели",
        }

    # Segment breakdown is a SEPARATE LLM call that re-sends the full OCR.
    # Launch it concurrently with the main extraction (same pattern as the
    # group task above) instead of running it sequentially afterwards — the
    # two/three LLM calls then overlap, cutting total extraction wall-time.
    # Output order is unchanged: segment rows are still emitted after the main
    # metrics (we await the task at the original consumption point below). The
    # helper collects locally and returns, so there's no concurrent write to
    # the shared `diag` dict (merge happens after await, single-threaded).
    segment_task: asyncio.Task[tuple[list[dict], dict, str]] | None = None
    _want_segments = include_segments or _role_wants_segments(role)

    async def _collect_segment_events() -> tuple[list[dict], dict, str]:
        events: list[dict] = []
        seg_diag: dict = {}
        seg_error = ""
        async for segment_event in _extract_segment_breakdown_full_context(
            pdf_text_for_prompt=pdf_text_for_prompt,
            extractable=segment_extractable,
            period_current=period_current,
            period_prev=period_prev,
            company_display=company_display or doc.company_name or doc.filename,
            effective_type=effective_type,
            model_id=model_id,
            segment_mode=segment_mode,
            segment_context_mode=segment_context_mode,
            debug_dir=debug_dir,
            segment_metrics_override=segment_metrics_override,
            segment_rules_extra=segment_rules_extra,
        ):
            if segment_event.get("type") == "segment_diag":
                seg_diag = segment_event.get("_diag_segment") or {}
                continue
            if segment_event.get("type") == "error":
                seg_error = segment_event.get("message", "")
                continue
            events.append(segment_event)
        return events, seg_diag, seg_error

    # ⚠️ ОТМЕТКИ ПАРАЛЛЕЛЬНЫХ СТАДИЙ (16.08.2026). Разбивка и состав группы —
    # ОТДЕЛЬНЫЕ вызовы модели, и они не потоковые: ответ разбирается целиком,
    # поэтому все их строки приезжают ОДНИМ всплеском в конце. Замер на Нестле
    # (ход 69f11315): плоские показатели шли с +137 по +175 с, а 43 сегментные
    # и 8 групповых строк пришли разом на +276 с — сто секунд, за которые от
    # движка не было НИ ОДНОГО признака жизни по этим вкладкам.
    #
    # Пользователь видел «строки этого вида пока не приходили» и читал это как
    # «ничего не нашлось». Отметка не ускоряет вызов — она перестаёт врать
    # молчанием: на экране видно, что стадия ИДЁТ и сколько строк принесла.
    #
    # Отметки идут ЗДЕСЬ, а не внутри генераторов стадий: старт задачи — это
    # факт оркестрации, а генератор о том, что его запустили параллельно, знать
    # не обязан.
    if _want_segments:
        segment_task = asyncio.create_task(_collect_segment_events())
        yield {
            "type": "status",
            "stage": "segments_started",
            "message": "Извлекаем разбивку по сегментам — отдельный вызов модели",
        }

    # Stream the response and emit metric events as soon as each enum block
    # parses successfully — gives the UI live progress instead of a 30s wait.
    buffers: list[str] = [""] * len(user_prompts)
    shard_sinks: list[dict] = [{"in": 0, "out": 0, "cached": 0} for _ in user_prompts]
    shard_errors: list[Exception] = []
    emitted: set[str] = set()
    shard_q: asyncio.Queue = asyncio.Queue()

    async def _run_shard(idx: int) -> None:
        """Один шард реестра: стримит свой кусок и толкает разобранные блоки
        в общую очередь, чтобы потребитель (генератор) отдавал метрики в UI по
        мере готовности любого шарда, а не после самого медленного."""
        local = ""
        seen: set[str] = set()
        try:
            async for delta in llm.stream(
                prompt=user_prompts[idx],
                system_prompt=system_prompt,
                model=model_id,
                response_format={"type": "json_object"},
                max_output=32000,
                usage_sink=shard_sinks[idx],
            ):
                if diag["first_token_s"] is None and delta:
                    diag["first_token_s"] = round(_ttiming.time() - llm_t0, 2)
                local += delta
                buffers[idx] = local
                for enum, block in _metric_block_iter(local, valid_enums):
                    if enum in seen:
                        continue
                    seen.add(enum)
                    await shard_q.put(("metric", enum, block))
        except Exception as exc:  # noqa: BLE001 — судьбу шарда решает потребитель
            await shard_q.put(("error", idx, exc))
            return
        await shard_q.put(("done", idx, None))

    shard_tasks = [asyncio.create_task(_run_shard(i)) for i in range(len(user_prompts))]
    _live = len(shard_tasks)
    try:
        while _live:
            _kind, _a, _b = await shard_q.get()
            if _kind in ("done", "error"):
                _live -= 1
                if _kind == "error":
                    shard_errors.append(_b)
                continue
            enum, block = _a, _b
            if enum in emitted or enum not in valid_enums:
                continue
            try:
                info = _json.loads(block)
            except Exception:
                continue
            emitted.add(enum)
            metric = metric_by_enum[enum]
            # Verbatim path (E-1 fix): prefer new `values` map by period,
            # fall back to legacy current/previous fields for older mocks.
            period_values, metric_unit = _coerce_period_values(
                info,
                enum=enum,
                period_current=period_current,
                period_prev=period_prev,
                default_unit=default_unit,
                abs_cost=False,
                max_report_year=max_report_year,
            )
            for pv in period_values:
                if pv.get("value") is not None:
                    extracted_by_period.setdefault(pv["period"], {})[enum] = pv.get("value")
            extractable_period_values_by_enum[enum] = period_values
            v_cur = _value_for_period(period_values, period_current)
            if v_cur is None:
                v_cur = _first_value(period_values)
            v_prv = _value_for_period(period_values, period_prev)
            extracted_current[enum] = v_cur
            extracted_prev[enum] = v_prv
            yield _apply_metric_source_forms({
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
                "page": _page_from_period_values(period_values),
            }, source_form_by_page)
    except Exception as e:
        for _t in shard_tasks:
            _t.cancel()
        if consolidated_group_task and not consolidated_group_task.done():
            consolidated_group_task.cancel()
            with contextlib.suppress(BaseException):
                await consolidated_group_task
        from finengine.llm.provider_errors import _user_facing_llm_error
        yield {"type": "error", "message": _user_facing_llm_error(str(e))}
        return

    # Все шарды упали — вести себя как раньше (терминальная ошибка). Упала
    # часть — отдаём то, что собрали: недостающие enum'ы пройдут финальным
    # добором ниже и станут «не найдено», это честнее, чем потерять всё.
    if shard_errors and len(shard_errors) == len(shard_tasks):
        _LOG.warning(
            "extract: ALL %d shards failed; raw errors: %s",
            len(shard_tasks), "; ".join(str(e)[:300] for e in shard_errors),
        )
        if consolidated_group_task and not consolidated_group_task.done():
            consolidated_group_task.cancel()
            with contextlib.suppress(BaseException):
                await consolidated_group_task
        from finengine.llm.provider_errors import _user_facing_llm_error
        yield {"type": "error", "message": _user_facing_llm_error(str(shard_errors[0]))}
        return

    for _s in shard_sinks:
        for _k in ("in", "out", "cached"):
            usage_sink[_k] = int(usage_sink.get(_k) or 0) + int(_s.get(_k) or 0)
        if _s.get("model"):
            usage_sink["model"] = _s["model"]

    diag["llm_stream_s"] = round(_ttiming.time() - llm_t0, 1)
    diag["raw_chars"] = sum(len(b) for b in buffers)
    diag["streamed_emitted"] = len(emitted)
    diag["shards"] = len(shard_tasks)
    if shard_errors:
        diag["shard_errors"] = [str(e) for e in shard_errors]
        _LOG.warning(
            "extract: %d/%d shards failed; continuing with partial coverage: %s",
            len(shard_errors), len(shard_tasks), shard_errors[0],
        )
    for _i, _b in enumerate(buffers):
        _write_llm_debug_file(
            debug_dir,
            "main_response.json" if len(buffers) == 1 else f"main_response.{_i + 1}.json",
            _b,
        )
    if not any(b.strip() for b in buffers):
        yield {
            "type": "error",
            "message": (
                "Модель не вернула ответ по извлечению. "
                "Пожалуйста, запустите извлечение ещё раз."
            ),
        }
        return

    yield {
        "type": "status",
        "stage": "table_build",
        "message": "Формируем таблицу показателей",
    }

    # If the streaming pass missed any enums (model format drift, partial
    # output), do one final pass over the full buffer with our robust parser.
    # Каждый шард — самостоятельный JSON, поэтому разбираем по отдельности и
    # сливаем (склейка буферов дала бы невалидный JSON).
    leftover_parsed: dict = {}
    for _b in buffers:
        leftover_parsed.update(_parse_extract_json(_b) or {})
    for metric in extractable:
        enum = metric["enum"]
        if enum in emitted:
            continue
        info = leftover_parsed.get(enum) or {}
        # Verbatim path (E-1 fix), same as streaming branch.
        period_values, metric_unit = _coerce_period_values(
            info,
            enum=enum,
            period_current=period_current,
            period_prev=period_prev,
            default_unit=default_unit,
            abs_cost=False,
            max_report_year=max_report_year,
        )
        for pv in period_values:
            if pv.get("value") is not None:
                extracted_by_period.setdefault(pv["period"], {})[enum] = pv.get("value")
        extractable_period_values_by_enum[enum] = period_values
        v_cur = _value_for_period(period_values, period_current)
        if v_cur is None:
            v_cur = _first_value(period_values)
        v_prv = _value_for_period(period_values, period_prev)
        extracted_current[enum] = v_cur
        extracted_prev[enum] = v_prv
        yield _apply_metric_source_forms({
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
            "page": _page_from_period_values(period_values),
        }, source_form_by_page)

    # Calculated metrics — same logic as the RAG path.
    calc_t0 = _ttiming.time()
    # ПРОИСХОЖДЕНИЕ ЕДЕТ ВТОРЫМ КАНАЛОМ (Б5, 19.08.2026): числа возвращаются
    # прежними словарями, а «по какой формуле и по каким числам» — этими тремя.
    # Каналов три, потому что значение строки берётся из трёх проходов, и
    # подписать строку происхождением другого прохода значило бы соврать.
    trace_current: dict[str, dict] = {}
    trace_calc_by_period: dict[str, dict] = {}
    trace_display_by_period: dict[str, dict] = {}
    calc_current, calc_prev = _calculate_metrics_pair(
        calculated_defs,
        extracted_current,
        extracted_prev,
        formula_default_abs_enums=formula_default_abs_enums,
        trace_current=trace_current,
        period_current=period_current,
        period_prev=period_prev,
    )
    diag["calc_s"] = round(_ttiming.time() - calc_t0, 2)
    from finengine.skills.pricing import aggregate_usage_to_diag, cost_usd
    usage_sink["calls"] = len(shard_tasks)
    aggregate_usage_to_diag(usage_sink, diag, model_id)
    usage_model = str(usage_sink.get("model") or model_id or "")
    diag["llm_requests"] = [
        {
            "stage": "main",
            "model": usage_model,
            "elapsed_s": diag["llm_stream_s"],
            "tokens_in": diag["tokens_in"],
            "tokens_out": diag["tokens_out"],
            "tokens_cached": diag["tokens_cached"],
            "cost_usd": round(
                cost_usd(
                    usage_model,
                    diag["tokens_in"],
                    diag["tokens_out"],
                    diag["tokens_cached"],
                ),
                4,
            ),
        }
    ]
    all_periods = _filter_plausible_report_periods(
        sort_periods(list(extracted_by_period.keys())) or periods_detected,
        max_report_year=max_report_year,
    )
    calculation_extracted_by_period = _with_point_values_for_duration_calculations(
        extracted_by_period,
        all_periods,
    )
    calc_by_period = _calculate_metrics_for_periods(
        calculated_defs,
        calculation_extracted_by_period,
        all_periods,
        formula_default_abs_enums=formula_default_abs_enums,
        trace_by_period=trace_calc_by_period,
    )
    display_calc_by_period = _calculate_metrics_for_periods(
        calculated_defs,
        extracted_by_period,
        all_periods,
        formula_default_abs_enums=formula_default_abs_enums,
        trace_by_period=trace_display_by_period,
    )
    calc_period_kinds = _calculated_period_kind_map(calculated_defs)
    for enum, name_ru, formula, _deps in calculated_defs:
        meta = calc_meta.get(str(enum).strip().upper(), {})
        unit_calc = unit_for_calculated(formula, default_unit, meta.get("unit_kind", ""))
        do_annualize = bool(meta.get("annualize"))
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
        # Приведение к году идёт ПОСЛЕ выбора значения периода и одинаково для
        # всех выходов показателя — иначе колонка периода и «текущий/предыдущий»
        # разъехались бы между собой на промежуточном отчёте.
        period_values = [
            {
                "period": p,
                "value": annualized(source_calc_by_period.get(p, {}).get(enum), p, do_annualize),
                "unit": unit_calc,
                "page": None,
                "source_text": "",
            }
            for p in all_periods
            if source_calc_by_period.get(p, {}).get(enum) is not None
        ]
        v_cur = calc_current.get(enum)
        v_cur = (
            _value_for_period(period_values, period_current)
            if v_cur is None
            else annualized(v_cur, period_current, do_annualize)
        )
        v_prv = calc_prev.get(enum)
        v_prv = (
            _value_for_period(period_values, period_prev)
            if v_prv is None
            else annualized(v_prv, period_prev, do_annualize)
        )
        # Приведение к году делается ПРИ ВЫДАЧЕ, а входы сняты за период: без
        # множителя в подписи арифметика «входы → напечатанное» не сходится, и
        # у 40 показателей реестра расхождение выглядит ошибкой расчёта.
        note = annualization_note(period_current) if do_annualize else ""
        formula_shown = prov.get("formula") or formula
        yield _apply_metric_source_forms({
            "type": "metric",
            "enum": enum,
            "name_ru": name_ru,
            "value_current": v_cur,
            "value_prev": v_prv,
            "period_values": period_values,
            "unit": unit_calc,
            "sanity": _sanity_note(enum, v_cur, unit_calc, calc_meta),
            "found": v_cur is not None,
            "is_calculated": True,
            "formula": f"({formula_shown}) * {note}" if note else formula_shown,
            "inputs": prov.get("inputs") or [],
        }, source_form_by_page)

    if segment_task is not None:
        # The segment LLM call was launched concurrently with the main
        # extraction above; await it here (it has been running in parallel,
        # so this usually returns immediately) and merge its diag.
        segment_events, segment_diag, segment_error = await segment_task
        if segment_diag:
            existing_requests = list(diag.get("llm_requests") or [])
            diag.update(segment_diag)
            diag["llm_requests"] = [
                *existing_requests,
                *(segment_diag.get("llm_requests") or []),
            ]
        if segment_error:
            diag["segment_error"] = segment_error

        segment_total_enums = {
            event.get("enum")
            for event in segment_events
            if event.get("type") == "metric" and str(event.get("segment") or "").upper() == "TOTAL"
        }
        if segment_metrics_override is not None:
            scoped = [m for m in segment_extractable if m.get("enum") in segment_metrics_override]
        else:
            segment_standard = _detect_reporting_standard(pdf_text_for_prompt)
            scoped = _segment_metric_scope(
                segment_extractable, segment_standard, effective_type,
            )
        segment_metric_by_enum = {metric["enum"]: metric for metric in scoped}
        total_fallback_events: list[dict] = []
        for enum, metric in segment_metric_by_enum.items():
            if enum in segment_total_enums:
                continue
            period_values = [
                dict(pv)
                for pv in extractable_period_values_by_enum.get(enum, [])
                if pv.get("value") is not None
            ]
            if not period_values:
                continue
            total_fallback_events.append(
                _main_total_fallback_event(
                    metric=metric,
                    period_values=period_values,
                    period_current=period_current,
                    period_prev=period_prev,
                    unit=default_unit,
                )
            )

        if total_fallback_events:
            diag["segment_total_from_main_fallback"] = [
                event.get("enum") for event in total_fallback_events
            ]
        # Счёт, а не факт: «стадия прошла и дала ноль» и «стадия не запускалась»
        # выглядят на экране одинаково пусто, а означают разное — первое повод
        # смотреть отчёт, второе повод смотреть настройки.
        yield {
            "type": "status",
            "stage": "segments_done",
            "message": (f"Разбивка по сегментам: строк "
                        f"{len(segment_events) + len(total_fallback_events)}"),
        }
        for segment_event in [*segment_events, *total_fallback_events]:
            yield _apply_metric_source_forms(segment_event, source_form_by_page)

    if consolidated_group_task is not None:
        group_rows_seen = 0
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
            group_rows_seen += 1
            yield _apply_metric_source_forms(group_event, source_form_by_page)
        yield {
            "type": "status",
            "stage": "group_done",
            "message": f"Состав холдинга: строк {group_rows_seen}",
        }

    display_period_current, display_period_prev = _select_display_periods(
        all_periods or periods_detected,
        period_current,
        period_prev,
    )

    yield {
        "type": "done",
        "period_current": display_period_current,
        "period_prev": display_period_prev,
        "periods": all_periods or periods_detected,
        "unit": default_unit,
        "company_type": effective_type,
        "company_name": company_display or doc.company_name,
        "ocr_engine": getattr(doc, "ocr_engine", "") or "",
        "_diag": diag,
    }
