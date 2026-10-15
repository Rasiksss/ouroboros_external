"""Атомы Среза 3 — адаптеры существующих КРУПНЫХ компонентов (B3: «`GraphPlanner`
строит настоящий `PlanGraph`, а `GraphExecutor` исполняет его через адаптеры
существующих крупных компонентов»). Это не финальная гранулярность продукта —
Срез 5a (`locate`/`read.value`) уже разобрал `extract.scalar` на настоящие
атомы (см. ниже); часть 2 (`read.structure`/`read.quote`/`read.entities`)
довершит это для сегментов/группы.

Имена атомов взяты из golden-фикстур Фазы 0 (`tests/golden/planner/manual/
*.json`), а не придуманы заново — те фикстуры уже анонсировали `ingest.
document`/`identify.classify`/`extract.scalar`/`calc.formula_engine`/`verify.
arithmetic`/`out.form` как форму, которую этот срез должен построить.
`extract.scalar` (одна модельная граница) заменена ДВУМЯ реальными узлами —
`locate`→`read.value` — с типизированной границей (`CandidateLocator`) между
ними вместо внутреннего питоновского словаря (нерушимое решение №10: один
источник числовой истины, старый монолитный путь не остаётся вторым
работающим маршрутом рядом с новым).

Упрощение, оставшееся от Среза 3 (см. `ExecutionContext.requires`/
`.dependencies()`): `locate`/`identify.classify` этого файла читают текст
документа из `ExecutionContext.inputs.document_text` напрямую, а не через
`dependency_evidence(doc.textify)`. `doc.textify`/`doc.index` (Срез 5a) ниже
УЖЕ реализованы и зарегистрированы как настоящие узлы графа — но не подключены
как `requires` этих трёх сценариев: три золотых интеграционных теста подают
УЖЕ готовый текст строкой (реальные excerpt'ы МСФО-отчётов), а не байты PDF, и
`tests/atoms/test_doc_probe.py` уже задокументировал, что кириллица, вписанная
в синтетический PDF через PyMuPDF, при обратном чтении не читается (нет
кириллического шрифта у встроенного Helvetica) — переводить эти три золотых
сценария на байтовый вход было бы риском, непропорциональным карточке 15a.
Значит `locate`/`read.value` НЕ зависят от `doc.textify`/`doc.index` как от
узлов графа в ЭТОЙ сессии (зависят от того же `document_text`, что раньше
`extract.scalar`) — но `doc.textify → doc.index` сами по себе ДВА связанных
РЕАЛЬНЫХ узла, и эта пара проверена как единое целое через настоящий
`GraphExecutor`, на маленьком ASCII-PDF без проблемы шрифта
(`tests/integration/test_doc_chain.py`).
"""
from __future__ import annotations

from finengine.contracts import (
    CalculatedMetric,
    ApproachPolicy,
    CandidateLocator,
    DocumentCandidate,
    DocumentText,
    EntityRelation,
    EvidenceBundleV2,
    ExtractedMetric,
    ExtractedStructure,
    Judgment,
    PlanPolicy,
    ReferenceValue,
    RunPassport,
    ScalarFact,
    UserProvidedMetric,
    VerificationFinding,
)
from finengine.skills.periods import normalize_period_label, period_kind_for_enum, split_period_labels
from finengine.config import settings as _settings
from finengine.policy.sandbox_profiles import external_enrichment, register_dynamic_profile
from finengine.roles.access import OTHER_ROLE
from finengine.roles.scope import (
    build_engine_scope,
    layouts,
    role_should_include_consolidated_group,
    role_should_include_segments,
)
from finengine.skills.atoms.ad_hoc_concepts import load_specs as load_ad_hoc_specs
from finengine.skills.atoms.ad_hoc_concepts import metric_row as ad_hoc_metric_row
from finengine.skills.atoms.assess_judgment import assess_judgment_batch, assess_judgment_requires
from finengine.skills.atoms.assess_rule import assess_rule_executor, assess_rule_requires
from finengine.skills.atoms.calc_formula import (
    calc_formula_executor,
    calc_formula_requirement_extra,
    calc_formula_requires,
)
from finengine.skills.atoms.calc_formula import ATOM_VERSION as CALC_FORMULA_VERSION
from finengine.skills.atoms.document_set import assemble_document_set
from finengine.skills.atoms.explain_external import explain_external_executor
from finengine.skills.atoms.web_search import ATOM_VERSION as WEB_SEARCH_ATOM_VERSION
from finengine.skills.atoms.web_search import WEB_SEARCH_TYPE, web_search_executor, web_search_step_problem
from finengine.skills.atoms.id_revision import detect_revision
from finengine.skills.atoms.locate import ATOM_VERSION as LOCATE_ATOM_VERSION
from finengine.skills.atoms.monitoring import (
    DEFAULT_APPROACH_POLICY,
    bind_watch_approach,
    calc_decompose_executor,
    calc_series_executor,
    monitor_compose_executor,
    ref_peer_executor,
    watch_diff_executor,
    watch_subscribe_executor,
)
from finengine.skills.atoms.explain_internal import explain_internal_batch, explain_internal_requires
from finengine.skills.atoms.norm_time import norm_time_gate
from finengine.skills.atoms.norm_unit import norm_unit_gate
from finengine.skills.atoms.out_changes import out_changes_executor, scope_concepts as changes_scope_concepts
from finengine.skills.atoms.out_narrative import out_narrative_executor
from finengine.skills.atoms.out_file import make_out_file_executor
from finengine.skills.atoms.out_form import out_form_executor
from finengine.skills.atoms.out_report import out_report_executor
from finengine.skills.atoms.ref_norm import ref_norm_executor, ref_norm_requires
from finengine.skills.atoms.read_override import read_override_executor, read_override_requires
from finengine.skills.atoms.ref_plan import ref_plan_executor, ref_plan_requires
from finengine.skills.atoms.ref_prior import ref_prior_executor, ref_prior_requires
from finengine.skills.atoms.run_passport import build_run_passport
from finengine.skills.atoms.verify_bounds import verify_bounds_gate
from finengine.skills.atoms.verify_citation import verify_citation_gate
from finengine.skills.atoms.verify_grounding import verify_grounding_gate, verify_grounding_requires
from finengine.skills.atoms.verify_identity_cross import (
    verify_cross_gate,
    verify_cross_requires,
    verify_identity_gate,
    verify_identity_requires,
)
from finengine.skills.metric_schema import registry_for_passport, registry_for_profile
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry, NodeArgs
from finengine.skills.spec.target import InputSpec, OutputSpec

ATOM_VERSION = "1.0.0"


# A4/решение №9: `doc.textify` ходит в сеть (облачный OCR) на один конкретный
# заранее известный узел — `external_enrichment` именно под это заведён
# («список узлов — обязанность вызывающего атома»). Регистрируем в
# `DYNAMIC_SANDBOX_PROFILES`, НЕ в `SANDBOX_PROFILES` — тот зарезервирован
# ровно под три фиксированных профиля (`test_registry_has_the_three_fixed_
# profiles`), а `external_enrichment(...)` всегда несёт одно и то же поле
# `.name`, так что ключ здесь — своя строка, не `.name` профиля.
_OCR_SANDBOX_PROFILE = "doc_textify_ocr_enrichment"
register_dynamic_profile(_OCR_SANDBOX_PROFILE, external_enrichment(
    allowed_hosts=(_settings.idp_ocr_url or "unconfigured",),
    description="doc.textify: постраничный OCR через облачный IDP-сервис.",
))

# `out.file` writes the already typed result to the configured object store.
# The renderer itself is deliberately calculation-free, but persistence is an
# I/O boundary and therefore must not masquerade as a deterministic atom.
_FILE_SANDBOX_PROFILE = "out_file_object_store"
register_dynamic_profile(_FILE_SANDBOX_PROFILE, external_enrichment(
    allowed_hosts=(_settings.s3_endpoint or "local-object-store",),
    description="out.file: запись готового артефакта в настроенное объектное хранилище.",
))

# Срез 6b: `explain.external` ходит в сеть на ОДИН заранее известный узел —
# `skills.web_search` (`api.tavily.com`), тот же принцип, что `doc.textify`
# выше (A4/решение №9: список узлов — обязанность вызывающего атома, не
# общая "сеть разрешена"). Тем же узлом и тем же профилем ходит `web.search`
# цикла гибридного планировщика (часть 19 доводки).
_WEB_SEARCH_SANDBOX_PROFILE = "explain_external_web_search"
register_dynamic_profile(_WEB_SEARCH_SANDBOX_PROFILE, external_enrichment(
    allowed_hosts=("api.tavily.com",),
    description="explain.external и web.search: поиск через web_search (Tavily).",
))


def _document_version(items, ctx) -> str:
    """Exact content version propagated by Planner/Case, never run_id when known.

    A graph run may have its own run_id, but Slice 10 reuse/invalidation is tied
    to immutable document content. All items of one physical batch must therefore
    refer to the same content version.
    """
    if isinstance(items, NodeArgs):
        versions = {items.document_version} if items.document_version else set()
    else:
        versions = {a.document_version for a in items if a.document_version}
    if len(versions) > 1:
        raise ValueError(f"batch mixes document versions: {sorted(versions)!r}")
    return next(iter(versions), ctx.run_id)


# ---------------------------------------------------------------------------
# ingest.document — граница со Срезом 5a (см. докстринг модуля).
def _ingest_document(args: NodeArgs, ctx) -> list:
    text = ctx.inputs.document_text
    if not text or not text.strip():
        raise ValueError("ingest.document: пустой document_text во входах прогона")
    return [DocumentText(
        document_id=args.entity_ref or "doc", document_version=_document_version(args, ctx), page=1, text=text,
    )]


# ---------------------------------------------------------------------------
# identify.classify — тонкая обёртка над Срезом 1 (build_run_passport),
# ГРАНИЦА двухпроходного раскрытия (A1.5): всё, что `needs_run_passport=True`,
# ждёт результата этого узла.
def _identify_classify(args: NodeArgs, ctx) -> list:
    text = ctx.inputs.document_text
    passport = build_run_passport(
        text, goal_ir_id=ctx.run_id,
        company_hint=args.entity_ref or "",
        cell=ctx.inputs.extra.get("cell", ""),
        customer_id=ctx.inputs.extra.get("customer_id", ""),
    )
    return [passport]


# ---------------------------------------------------------------------------
# doc.textify / doc.index — Срез 5a, реальные атомы (не адаптеры легаси-кода).
# Не подключены как `requires` трёх сценариев этого файла — см. докстринг
# модуля; зарегистрированы как настоящие узлы графа и проверены отдельной
# сквозной цепочкой (`tests/integration/test_doc_chain.py`).
def _bind_doc_textify(ocr_pages):
    """`ocr_pages=None` в реальном пути (`textify_document` берёт свой
    облачный дефолт); тесты подставляют дубль без сети — та же граница
    подмены, что у `locate_executor`/`read_value_executor`."""
    def _executor(args: NodeArgs, ctx) -> list:
        from finengine.skills.atoms.document import textify_document  # noqa: PLC0415

        return textify_document(
            args.entity_ref or "doc", args.period_ref or "v1", ctx.inputs.pdf_bytes, ocr_pages=ocr_pages,
        )
    return _executor


def _bind_doc_index(table_labeler):
    def _executor(args: NodeArgs, ctx) -> list:
        from finengine.skills.atoms.document_index import build_document_index  # noqa: PLC0415

        pages = [e for e in ctx.dependencies() if isinstance(e, DocumentText)]
        if not pages:
            raise ValueError("doc.index: нет страниц DocumentText во входах — doc.textify не отработал")
        return [build_document_index(
            pages, document_id=pages[0].document_id, document_version=pages[0].document_version,
            total_pages=max(p.page for p in pages), table_labeler=table_labeler,
        )]
    return _executor


# ---------------------------------------------------------------------------
# locate — РЕАЛЬНЫЙ путь: одна модельная граница на пакет концептов (та же
# пакетная экономика, что была у `extract.scalar`). Не вызывается
# автоматическими тестами этого среза без подмены (нужен живой модельный
# шлюз) — интеграционные тесты (`tests/integration/`) подставляют
# детерминированные дубли на границе `locate`/`read.value`, честно объявляя
# об этом в докстринге теста, а не молча.
def _ad_hoc_specs(items: list[NodeArgs]) -> dict:
    """Описания ad-hoc понятий, которые едут через `NodeArgs.extra` (Срез 31):
    `{КЛЮЧ_В_ВЕРХНЕМ_РЕГИСТРЕ: AdHocConceptSpec}` по всем узлам одного пакета."""
    specs: dict = {}
    for item in items:
        specs.update(load_ad_hoc_specs(item.extra))
    return specs


def _with_ad_hoc_fallback_rows(known_metrics: list[dict], enums: list[str], specs: dict | None = None) -> list[dict]:
    """R4/R5a: `locate`/`read.value` catch-all producer (`OutputSpec(...,
    concept=None)`, `atom_registry.AtomRegistry._producers_of_single`)
    обслуживает concept'ы вне реестра — `TargetFactory` (`atoms/intent.py`)
    ставит имя понятия как `Target.concept_id` для `extract_ad_hoc_metric`.
    `locate_batch`/`read_value_batch` (не переписаны) принимают `metrics:
    list[dict]` как есть.

    Срез 31 (П5): если модель описала понятие (имя, синонимы, определение,
    подсказку) — строка строится из этого описания и имеет ту же форму, что
    строка реестра; двухстадийный экстрактор ищет по синонимам. Без описания
    остаётся прежняя синтетическая строка `{"enum": term, "name_ru": term}`.
    Запись реестра описанием НЕ подменяется — она выверена."""
    known_enums = {str(m.get("enum", "")).strip().upper() for m in known_metrics}
    extra_rows = []
    for enum in enums:
        key = enum.strip().upper()
        if key in known_enums:
            continue
        spec = (specs or {}).get(key)
        extra_rows.append(
            {**ad_hoc_metric_row(spec), "enum": enum} if spec is not None
            else {"enum": enum, "name_ru": enum, "name_en": enum}
        )
    return known_metrics + extra_rows


def _locate_batch_real(items: list[NodeArgs], ctx) -> dict:
    from finengine.skills.atoms.locate import locate_batch  # noqa: PLC0415
    from finengine.skills import bulk_extractor as bulk  # noqa: PLC0415

    passport = ctx.run_passport
    if passport is None:
        raise ValueError("locate: RunPassport не зафиксирован — узел не должен был стать ready")
    # Retrofit v9 (R3/R5a): `NodeArgs.concept` узла `locate` — уже голый enum
    # (`OutputSpec(output_type="CandidateLocator", concept=enum)`, не строка
    # с префиксом `locator:` — тип артефакта различает `AtomRegistry`, не
    # текстовый префикс имени концепта).
    enums = sorted({a.concept for a in items if a.concept})
    registry = registry_for_passport(passport)
    text = ctx.inputs.document_text
    effective_type = passport.classification.company_type or bulk._detect_company_type(text)
    periods_detected, period_current, period_prev = bulk._select_run_periods(
        text, "", fallback_period_label="Текущий",
    )
    metrics = _with_ad_hoc_fallback_rows(registry.get("extractable", []), enums, _ad_hoc_specs(items))
    return locate_batch(
        enums, document_id=passport.entity.entity_id, document_version=_document_version(items, ctx),
        document_text=text, metrics=metrics, company_type=effective_type,
        reporting_standard=passport.classification.standard, language=passport.classification.language,
        period_current=period_current, period_prev=period_prev, periods_detected=periods_detected,
        model_id=_settings.locate_model or None,
    )


# ---------------------------------------------------------------------------
# read.value — РЕАЛЬНЫЙ путь: вторая модельная граница, берёт `CandidateLocator`
# из зависимости `locate` (та же пакетная экономика — один вызов на пакет).
def _read_value_dynamic_requires(node_args: NodeArgs, passport: "RunPassport | None") -> tuple[InputSpec, ...]:
    if not node_args.concept:
        return ()
    return (InputSpec(input_type="CandidateLocator", concept=node_args.concept),)


def _wrap_extracted_metric(raw_executor):
    """R2/R5a: `read.value` produces `ExtractedMetric`, ссылающийся на
    `ScalarFact` — не второй числовой источник, обёртка над ТЕМ ЖЕ payload.
    Обёрнут ЛЮБОЙ переданный `read_value_executor` (реальный или тестовый
    дубль, см. `build_registry`), не только `_read_value_batch_real` — так
    контракт выхода узла графа один для обоих путей, и тесты, подменяющие
    модельную границу, не обязаны знать про обёртку."""
    def _executor(items: list[NodeArgs], ctx) -> dict:
        raw = raw_executor(items, ctx) or {}
        specs = _ad_hoc_specs(items)
        origins = {
            item.concept: "ad_hoc" if item.extra.get("ad_hoc_spec_id") else "registry"
            for item in items
        }
        # Срез 30: «2024» для остатка (`PPE`) — это 31.12.2024, для потока — FY
        # 2024. Без вида показателя фильтр Среза 29a отбрасывал верно извлечённый
        # баланс: метка факта `31.12.2024` не равна нормализованному `FY 2024`.
        # Прежняя (безвидовая) метка остаётся допустимой — фильтр только
        # расширяется, поведение Среза 29a не сужается.
        requested_periods: dict[str, set[str]] = {}
        period_kinds: dict[str, str] = {}
        for item in items:
            if item.concept and item.period_ref:
                spec = specs.get(item.concept.strip().upper())
                period_kind = spec.value_kind if spec is not None else period_kind_for_enum(item.concept)
                period_kinds[item.concept] = period_kind
                # `GoalIR.period_ref` — скаляр, но пользователь мог назвать несколько
                # периодов: фильтр сверяет каждую метку отдельно.
                for label in split_period_labels(item.period_ref):
                    requested_periods.setdefault(item.concept, set()).update({
                        normalize_period_label(label),
                        normalize_period_label(label, value_kind=period_kind),
                    })
        # Срез 30: факт несёт `concept` в верхнем регистре, а `norm.unit`/
        # `norm.time`/`verify.bounds` сверяют `e.concept == args.concept` точно —
        # для ad-hoc фразы («средневзвешенное количество акций») цепочка рвалась
        # после `read.value`. Узел ждёт ЗАПРОШЕННЫЙ концепт; отличие только в
        # регистре — то же самое имя.
        def _metric_concept(fact, requested: str) -> str:
            own = fact.concept or requested
            return requested if own.strip().upper() == requested.strip().upper() else own

        return {
            concept: tuple(
                ExtractedMetric(
                    scalar_fact=fact, concept=_metric_concept(fact, concept), period_ref=fact.period_ref,
                    definition_origin=origins.get(concept, "registry"),
                )
                for fact in facts
                if not requested_periods.get(concept)
                or normalize_period_label(
                    fact.period_ref, value_kind=period_kinds.get(concept),
                ) in requested_periods[concept]
            )
            for concept, facts in raw.items()
        }
    return _executor


def _read_value_batch_real(items: list[NodeArgs], ctx) -> dict:
    from finengine.skills.atoms.read_value import read_value_batch  # noqa: PLC0415
    from finengine.skills import bulk_extractor as bulk  # noqa: PLC0415

    passport = ctx.run_passport
    if passport is None:
        raise ValueError("read.value: RunPassport не зафиксирован — узел не должен был стать ready")
    enums = sorted({a.concept for a in items if a.concept})
    registry = registry_for_passport(passport)
    text = ctx.inputs.document_text
    # Срез 30: `locate` ставит `CandidateLocator.concept` ВЕРХНИМ регистром, а
    # `read_value_batch` ключует кандидатов и ответ модели верхним регистром
    # (докстринг `read_value.py`). Для ASCII-enum реестра исходный регистр —
    # уже верхний; для ad-hoc фразы пользователя («средневзвешенное количество
    # акций») — нет: кандидаты не находились, модель не спрашивалась, узел
    # завершался `succeeded` с нулём фактов. Работаем внутри в верхнем регистре
    # и возвращаем результат под ЗАПРОШЕННЫМ концептом — под ним его ждёт узел.
    upper_of = {enum: enum.strip().upper() for enum in enums}
    candidates_by_concept: dict[str, list] = {upper: [] for upper in upper_of.values()}
    for evidence in ctx.dependencies():
        if isinstance(evidence, CandidateLocator):
            key = str(evidence.concept or "").strip().upper()
            if key in candidates_by_concept:
                candidates_by_concept[key].append(evidence)
    effective_type = passport.classification.company_type or bulk._detect_company_type(text)
    periods_detected, period_current, period_prev = bulk._select_run_periods(
        text, "", fallback_period_label="Текущий",
    )
    default_unit = bulk._default_unit_for_standard(passport.classification.standard)
    max_report_year = bulk._max_report_anchor_year(periods_detected)
    specs = _ad_hoc_specs(items)
    metrics = _with_ad_hoc_fallback_rows(
        registry.get("extractable", []), list(upper_of.values()), specs,
    )
    raw = read_value_batch(
        list(upper_of.values()), candidates_by_concept, metrics=metrics,
        document_version=_document_version(items, ctx), period_current=period_current, period_prev=period_prev,
        periods_detected=periods_detected, default_unit=default_unit,
        reporting_standard=passport.classification.standard, company_type=effective_type,
        language=passport.classification.language, max_report_year=max_report_year,
        model_id=_settings.read_value_model or None,
    )
    result = {}
    for enum, upper in upper_of.items():
        spec = specs.get(upper)
        period_kind = spec.value_kind if spec is not None else period_kind_for_enum(enum)
        result[enum] = [
            fact.model_copy(update={
                "period_ref": normalize_period_label(fact.period_ref, value_kind=period_kind),
            })
            for fact in raw.get(upper, [])
        ]
    return result


# ---------------------------------------------------------------------------
# read.structure — Срез 5b: многомерная структура (сегодня — только сегменты,
# см. докстринг `read_structure.py`) как `ExtractedStructure`/`StructuredFact`.
def _wrap_extracted_structure(raw_executor):
    """R2/5b: `read.structure` produces `ExtractedStructure`, ссылающийся на
    `StructuredFact` — та же роль обёртки, что `_wrap_extracted_metric` у
    `read.value`, для structured, а не скалярных доказательств."""
    def _executor(items: list[NodeArgs], ctx) -> dict:
        raw = raw_executor(items, ctx) or {}
        return {
            table_kind: tuple(
                ExtractedStructure(structured_fact=fact, table_kind=table_kind, definition_origin="registry")
                for fact in facts
            )
            for table_kind, facts in raw.items()
        }
    return _executor


def _read_structure_batch_real(items: list[NodeArgs], ctx) -> dict:
    from finengine.skills.atoms.read_structure import StructureSpec, read_structure_batch  # noqa: PLC0415
    from finengine.skills import bulk_extractor as bulk  # noqa: PLC0415

    passport = ctx.run_passport
    if passport is None:
        raise ValueError("read.structure: RunPassport не зафиксирован — узел не должен был стать ready")
    table_kinds = sorted({a.concept for a in items if a.concept})
    registry = registry_for_passport(passport)
    text = ctx.inputs.document_text
    effective_type = passport.classification.company_type or bulk._detect_company_type(text)
    standard = bulk._detect_reporting_standard(text)
    _periods_detected, period_current, period_prev = bulk._select_run_periods(
        text, "", fallback_period_label="Текущий",
    )
    specs = []
    for kind in table_kinds:
        # Сегодня единственный РЕАЛЬНЫЙ producer — `"segments"` (докстринг
        # `read_structure.py`: нет готового извлекателя сроков погашения/
        # возраста дебиторки/чувствительности). Неизвестный `table_kind`
        # получает пустой `target_metrics` — `read_structure_batch` вернёт
        # для него пустой список, не исключение (тот же принцип, что у
        # concept без кандидатов в `locate`).
        target_metrics = (
            bulk._segment_metric_scope(registry.get("extractable", []), standard, effective_type)
            if kind == "segments" else []
        )
        specs.append(StructureSpec(table_kind=kind, target_metrics=target_metrics))
    return read_structure_batch(
        specs, document_id=passport.entity.entity_id, document_version=_document_version(items, ctx), document_text=text,
        period_current=period_current, period_prev=period_prev, company_type=effective_type,
        reporting_standard=passport.classification.standard, language=passport.classification.language,
    )


# ---------------------------------------------------------------------------
# read.quote — Срез 5b: точная цитата с проверенным адресом (см. докстринг
# `read_quote.py`). `QuoteEvidence` — сама planning-семантика, без обёртки
# (`02-tekushee-sostoyanie-i-mekhanika.md`: `QuoteEvidence` в списке
# planning-типов наравне с `ExtractedMetric`, не payload под ним).
def _read_quote_batch_real(items: list[NodeArgs], ctx) -> dict:
    from finengine.skills.atoms.read_quote import read_quote_batch  # noqa: PLC0415

    topics = sorted({a.concept for a in items if a.concept})
    entity_ref = next((a.entity_ref for a in items if a.entity_ref), None) or "doc"
    return read_quote_batch(
        topics, document_id=entity_ref, document_version=_document_version(items, ctx), document_text=ctx.inputs.document_text,
    )


# ---------------------------------------------------------------------------
# read.condition — Срез 9: тот же паттерн извлечения с адресом, что
# read.quote (см. докстринг `read_condition.py`). Не обёрнут (как read.quote):
# `read_condition_batch` уже возвращает типизированный `ConditionEvidence`,
# `ConditionEvidence` — сама planning-семантика, без обёртки-wrapper.
def _read_condition_batch_real(items: list[NodeArgs], ctx) -> dict:
    from finengine.skills.atoms.read_condition import read_condition_batch  # noqa: PLC0415

    topics = sorted({a.concept for a in items if a.concept})
    entity_ref = next((a.entity_ref for a in items if a.entity_ref), None) or "doc"
    return read_condition_batch(
        topics, document_id=entity_ref, document_version=_document_version(items, ctx), document_text=ctx.inputs.document_text,
    )


# ---------------------------------------------------------------------------
# read.entities — Срез 5b: состав группы/доли участия как `EntityRelation`
# (см. докстринг `read_entities.py` — известное ограничение: сегодня только
# дочерние общества). Не батчируется по концепту — как `identify.classify`,
# один логический вызов на документ.
def _read_entities_real(args: NodeArgs, ctx) -> list:
    from finengine.skills.atoms.read_entities import read_entities_batch  # noqa: PLC0415
    from finengine.skills import bulk_extractor as bulk  # noqa: PLC0415

    passport = ctx.run_passport
    if passport is None:
        raise ValueError("read.entities: RunPassport не зафиксирован — узел не должен был стать ready")
    text = ctx.inputs.document_text
    effective_type = passport.classification.company_type or bulk._detect_company_type(text)
    _periods_detected, period_current, period_prev = bulk._select_run_periods(
        text, "", fallback_period_label="Текущий",
    )
    return read_entities_batch(
        document_id=passport.entity.entity_id, document_version=_document_version(args, ctx), document_text=text,
        period_current=period_current, period_prev=period_prev, company_display=passport.entity.canonical_name,
        reporting_standard=passport.classification.standard, company_type=effective_type,
    )


# ---------------------------------------------------------------------------
# calc.formula — Срез 6a: эволюция бывшего `calc.formula_engine` (см.
# `skills/atoms/calc_formula.py` — `calc_formula_requires`/
# `calc_formula_executor`, тот же `compute_ratios`, расширенный `DerivedFact`,
# новые mandatory_gates). Само имя переименовано на канонiческое из
# документации v9 — не второй атом рядом со старым.
def _calc_entry_enum(entry) -> str:
    # Две формы записи `calculated`: кортеж `(enum, name_ru, formula, deps)` из
    # `registry_for_profile()` и словарь `{"enum": ...}` из снимка паспорта
    # (`CompiledRegistry.to_effective_data()`) — именно его получает `build_registry`
    # в бою. Только кортежи → в бою у расчётных показателей не было производителей.
    if isinstance(entry, dict):
        return str(entry.get("enum") or "").strip().upper()
    return str(entry[0]).strip().upper() if isinstance(entry, (list, tuple)) and entry else ""


# ---------------------------------------------------------------------------
# verify.arithmetic — ЛЕГАСИ-ЭТАЛОН ДЛЯ РЕГРЕССИИ (C1/C7), БОЛЬШЕ НЕ
# MANDATORY: Срез 6a заменил его в цепочке `calc.formula` на `verify.bounds`
# (жёсткая/мягкая граница из версionированного определения, не только
# `isfinite`). Остаётся зарегистрированным атомом реестра — воспроизводимый
# эталон старого поведения для сравнения, не второй работающий маршрут.
def _verify_arithmetic(args: NodeArgs, ctx) -> list:
    """Гейт ПРОПУСКАЕТ дальше сам wrapper вместе со своей находкой (не только
    вердикт «прошло/не прошло»): потребитель узла зависит от ЭТОГО узла, не
    от `calc.formula_engine` напрямую (A1.7 — обход гейта невозможен по
    построению графа), значит расчётное значение обязано доехать до
    потребителя ЧЕРЕЗ гейт, иначе оно теряется за проверкой навсегда."""
    calculated = [e for e in ctx.dependencies() if isinstance(e, CalculatedMetric) and e.concept == args.concept]
    if not calculated:
        return [VerificationFinding(
            produced_by_atom="verify.arithmetic", produced_by_atom_version=ATOM_VERSION,
            source_ref=args.concept or "unknown", concept=args.concept,
            check_kind="arithmetic", status="fail", detail="нет расчётного факта для проверки",
        )]
    metric = calculated[0]
    fact = metric.derived_fact
    import math  # noqa: PLC0415

    ok = math.isfinite(fact.value) and bool(fact.inputs)
    finding = VerificationFinding(
        produced_by_atom="verify.arithmetic", produced_by_atom_version=ATOM_VERSION,
        source_ref=fact.evidence_id, concept=fact.concept, check_kind="arithmetic",
        status="pass" if ok else "fail",
        detail=f"inputs={fact.inputs}, value={fact.value}",
    )
    return [finding, metric]


# ---------------------------------------------------------------------------
# out.form — форма роли (решение №8: недоступная строка не выбрасывается).
def _dgr_role_rows(role: str, company_type: str) -> list[dict]:
    cfg = (layouts().get("roles") or {}).get(role) or {}
    fmt = (cfg.get("formats") or {}).get(company_type) or {}
    return [r for r in (fmt.get("rows") or []) if r.get("enum")]


def _out_form_scope(node_args: NodeArgs, passport: RunPassport) -> tuple[InputSpec, ...]:
    requested = tuple(
        value.strip() for value in (node_args.extra.get("requested_concepts") or "").split(",")
        if value.strip()
    )
    if requested:
        return tuple(InputSpec(input_type="MetricEvidence", concept=enum) for enum in sorted(set(requested)))
    role = node_args.extra.get("role") or OTHER_ROLE
    rows = _dgr_role_rows(role, passport.classification.company_type)
    return tuple(
        InputSpec(input_type="MetricEvidence", concept=enum)
        for enum in sorted({r["enum"] for r in rows})
    )


def _analyze_full_bundle_scope(node_args: NodeArgs, passport: RunPassport) -> tuple[InputSpec, ...]:
    """A1.5, второй проход: состав зависимостей «полного анализа» решает
    `roles/scope.py::build_engine_scope` — тот же вызов, что у станции, не
    вторая копия правила. Срез 31: состав — по РОЛИ (`basis_full=False`), см.
    комментарий в теле.

    Retrofit v9 (R3, A1.7): каждый показатель — своя запись `collection_scope`
    (`InputSpec(input_type="MetricEvidence", concept=enum)`), раскрываемая
    `GraphPlanner` ИЗОЛИРОВАННЫМ пробным проходом — недостающий показатель не
    портит `EvidenceBundleV2` соседей, а просто отсутствует в ней (решение
    №8/A1.5 для полного анализа, симметрично форме роли).

    Срез 6b (критерий 5 «Критерия готовности связки анализа», 16b): сегменты
    и состав группы входят в ТУ ЖЕ изолированную коллекцию, что показатели —
    `"segments"` сегодня единственный реальный `table_kind` `read.structure`
    (см. докстринг `read_structure.py`), `EntityRelation` не параметризован
    концептом (один логический вызов на документ, как `read.entities`)."""
    requested = tuple(
        value.strip() for value in (node_args.extra.get("requested_concepts") or "").split(",")
        if value.strip()
    )
    role = node_args.extra.get("role") or OTHER_ROLE
    company_type = passport.classification.company_type
    if requested:
        metric_enums = requested
    else:
        # Срез 31 (П2, решение владельца): состав анализа определяет РОЛЬ через
        # реестры («какие показатели каким ролям нужны») — `basis_full=False`:
        # раскладка роли ∪ рекурсивно развёрнутые входы её формул. Прежний
        # `basis_full=True` (полный базис ячейки «под заземление» старого
        # `grounding_bus`) сужал только ВЫДАЧУ, а анализ гонял всю ячейку (627 узлов
        # на реальном отчёте). Роли `other`/`all` и пары «роль × тип компании» без
        # раскладки по-прежнему получают полную ячейку — так устроен `build_role_plan`.
        _, metric_enums = build_engine_scope(
            role, company_type, passport.classification.profile_id, basis_full=False,
        )
    # Срез 31 (П2): сегменты и состав группы входят в полный анализ ПО РОЛИ
    # (`roles/scope.py`), а не для любой: раньше роль в граф не доезжала и обе
    # ветви добавлялись всем.
    structures: tuple[InputSpec, ...] = ()
    # MONITOR_CHANGES (карточка 32) сравнивает периоды по ПОКАЗАТЕЛЯМ: сегменты и состав
    # группы ему не нужны, а их извлечение — два лишних модельных вызова и отдельный
    # режим отказа (на живом отчёте `read.structure` упал и уронил весь отчёт).
    if node_args.extra.get("structures") != "off":
        if role_should_include_segments(role, company_type):
            structures += (InputSpec(input_type="ExtractedStructure", concept="segments"),)
        if role_should_include_consolidated_group(role, company_type):
            structures += (InputSpec(input_type="EntityRelation"),)
    return (
        tuple(InputSpec(input_type="MetricEvidence", concept=enum) for enum in sorted(metric_enums))
        + structures
    )


def _override_scalar(metric: UserProvidedMetric) -> ScalarFact:
    h = metric.human_override
    if h.numeric_value is None or not (h.unit or '').strip():
        raise ValueError('active UserProvidedMetric must be numeric and have a unit')
    return ScalarFact(
        evidence_id=h.evidence_id, produced_by_atom=h.produced_by_atom,
        produced_by_atom_version=h.produced_by_atom_version, source_ref=h.source_ref,
        document_version=h.document_version, concept=metric.concept, period_ref=metric.period_ref,
        basis=h.basis, supersedes=h.supersedes, created_at=h.created_at,
        value=h.numeric_value, unit=h.unit,
    )


def _analyze_full_bundle(args: NodeArgs, ctx) -> list:
    """Корень «полного анализа» для этого среза (B3) — проверенная
    `EvidenceBundleV2`, не повествовательный текст: `assess.judgment`/
    `explain.*` (нарративное суждение по методике) — Срез 6b/9, вне границ
    A1/A2. «Полный» здесь значит «полный применимый набор доказательств
    после фиксации контекста» (A1.5), а не «модель сочинила вывод».

    R2: `EvidenceBundleV2` остаётся payload'ом сырых `ScalarFact`/`DerivedFact`
    (не переписан) — planning-обёртки (`ExtractedMetric`/`CalculatedMetric`)
    разворачиваются здесь, на границе с бандлом, не хранятся в нём.

    Срез 6b: `structured_facts`/`entity_relations` разворачиваются тем же
    приёмом из `ExtractedStructure`/`EntityRelation` — те же planning-обёртки
    вокруг чужого payload, не второе извлечение."""
    deps = ctx.dependencies()
    scalar = [e.scalar_fact for e in deps if isinstance(e, ExtractedMetric)] + [_override_scalar(e) for e in deps if isinstance(e, UserProvidedMetric)]
    derived = [e.derived_fact for e in deps if isinstance(e, CalculatedMetric)]
    structured = [e.structured_fact for e in deps if isinstance(e, ExtractedStructure)]
    relations = [e for e in deps if isinstance(e, EntityRelation)]
    return [EvidenceBundleV2(
        run_id=ctx.run_id, scalar_facts=scalar, derived_facts=derived,
        structured_facts=structured, entity_relations=relations,
    )]


_compose_form_scope = _out_form_scope  # private compatibility for old imports; registry uses out.form


# ---------------------------------------------------------------------------
# assess.judgment / explain.internal — Срез 6b: батчируемые модельные узлы
# (см. докстринги `assess_judgment.py`/`explain_internal.py` — C9, один вызов
# на весь пакет концептов, не сто отдельных). Методика — та же `framework.
# framework_block`, которой сегодня пользуется промпт полного анализа
# (`md.base()` без личных правок пользователя: правки — станционный контур,
# вне периметра этой сессии графового planner'а).
def _methodology_text_for(passport: RunPassport, args: NodeArgs | None = None) -> str:
    from finengine.skills.analysis import methodology as _md  # noqa: PLC0415

    if args is not None and args.extra.get("methodology_text"):
        return args.extra["methodology_text"]
    depth = (args.extra.get("depth") if args is not None else "") or "full"
    return _md.framework_block(_md.base(), passport.classification.company_type, depth, ())


def _assess_judgment_batch_real(items: list[NodeArgs], ctx) -> dict:
    passport = ctx.run_passport
    if passport is None:
        raise ValueError("assess.judgment: RunPassport не зафиксирован — узел не должен был стать ready")
    concepts = sorted({a.concept for a in items if a.concept})
    bundle = next((e for e in ctx.dependencies() if isinstance(e, EvidenceBundleV2)), None) or EvidenceBundleV2(
        run_id=ctx.run_id,
    )
    return assess_judgment_batch(
        concepts, bundle=bundle, methodology_text=_methodology_text_for(passport, items[0] if items else None),
        methodology_ref=passport.methodology_version,
    )


def _explain_internal_batch_real(items: list[NodeArgs], ctx) -> dict:
    passport = ctx.run_passport
    if passport is None:
        raise ValueError("explain.internal: RunPassport не зафиксирован — узел не должен был стать ready")
    concepts = sorted({a.concept for a in items if a.concept})
    deps = ctx.dependencies()
    bundle = next((e for e in deps if isinstance(e, EvidenceBundleV2)), None)
    current_by_concept = {
        e.concept: e for e in deps if isinstance(e, (ExtractedMetric, CalculatedMetric, UserProvidedMetric)) and e.concept in concepts
    }
    reference_by_concept = {e.concept: e for e in deps if isinstance(e, ReferenceValue) and e.concept in concepts}
    judgment_by_concept = {e.concept: e for e in deps if isinstance(e, Judgment) and e.concept in concepts}
    return explain_internal_batch(
        concepts, current_by_concept=current_by_concept, reference_by_concept=reference_by_concept,
        judgment_by_concept=judgment_by_concept, bundle=bundle,
    )


# ---------------------------------------------------------------------------
# explain.external — Срез 6b: одна сущность, один вызов на прогон (не батч по
# концепту — внешний факт про КОМПАНИЮ, не про отдельный показатель).
def _explain_external_real(args: NodeArgs, ctx) -> list:
    return explain_external_executor(args, ctx)


# ---------------------------------------------------------------------------
# out.narrative — Срез 6b: корень аналитической связки. Изолированная
# коллекция — тот же приём, что `_analyze_full_bundle_scope`, но по
# `Judgment`/`Explanation` каждого показателя применимого набора, плюс
# `ExternalFactRef` ОДНОЙ записью (пропадает целиком, если внешний контекст
# недоступен — решение №8, не nullable-ребро).
def _out_narrative_scope(node_args: NodeArgs, passport: RunPassport) -> tuple[InputSpec, ...]:
    requested = tuple(
        value.strip()
        for value in (node_args.extra.get("requested_concepts") or "").split(",")
        if value.strip()
    )
    if requested:
        metric_enums = requested
    else:
        role = node_args.extra.get("role") or OTHER_ROLE
        # Тот же состав, что у `analyze.full_bundle` (см. `_analyze_full_bundle_scope`):
        # суждения и объяснения — по показателям РОЛИ, а не всей ячейки.
        _, metric_enums = build_engine_scope(
            role, passport.classification.company_type, passport.classification.profile_id,
            basis_full=False,
        )
    per_concept = tuple(
        spec
        for enum in sorted(metric_enums)
        for spec in (InputSpec(input_type="Judgment", concept=enum), InputSpec(input_type="Explanation", concept=enum))
    )
    # A scoped EXPLAIN_CHANGE is explicitly document-internal.  Requiring
    # ExternalFactRef here would add a web-search branch that the user did not
    # request and that is not needed to explain the report's comparative
    # columns.  Full analysis keeps the established external-context branch.
    # MONITOR_CHANGES (карточка 32) объясняет изменения по данным документа для
    # ВСЕГО ролевого набора, поэтому отключает внешний контекст явным флагом.
    document_internal = bool(requested) or node_args.extra.get("external_context") == "off"
    return per_concept if document_internal else per_concept + (InputSpec(input_type="ExternalFactRef"),)


def _out_changes_scope(node_args: NodeArgs, passport: RunPassport) -> tuple[InputSpec, ...]:
    """MONITOR_CHANGES (карточка 32): пара сравнения периодов — `ReferenceValue` по
    КАЖДОМУ показателю ролевого набора (изолированный пробный проход: показатель без
    значения в отчёте просто отсутствует, а не портит соседей)."""
    return tuple(
        InputSpec(input_type="ReferenceValue", concept=enum)
        for enum in changes_scope_concepts(node_args.extra, passport)
    )


def _out_narrative_real(args: NodeArgs, ctx) -> list:
    depth = "quick" if args.extra.get("depth") == "quick" else "detailed"
    return out_narrative_executor(args, ctx, depth=depth)


def _out_report_scope(node_args: NodeArgs, passport: RunPassport) -> tuple[InputSpec, ...]:
    """Use the same deterministic analysis scope as the narrative producer."""
    return _out_narrative_scope(node_args, passport)


def _out_file_requires(node_args: NodeArgs, _passport: RunPassport | None) -> tuple[InputSpec, ...]:
    source_type = node_args.extra.get("source_output_type") or "FormOutput"
    if source_type not in {"ReportOutput", "FormOutput", "NarrativeOutput"}:
        raise ValueError(f"out.file: unsupported source_output_type={source_type!r}")
    return (InputSpec(input_type=source_type),)


# ---------------------------------------------------------------------------
# id.revision — Срез 8 (B9): маркер пересмотра + модель под жёсткой схемой,
# per-документ (см. докстринг `id_revision.py`). `candidate_prior_documents`
# приходит как JSON-строка в `NodeArgs.extra` — тот же приём, каким уже
# передаются `role`/`registry_version` в других узлах этого файла, не новый
# канал: узел не открывает сеть/файлы сам, чтобы узнать про другие документы
# набора, ему их называет вызывающий.
def _id_revision_real(args: NodeArgs, ctx) -> list:
    import json as _json  # noqa: PLC0415

    text = ctx.inputs.document_text
    raw_candidates = args.extra.get("candidate_prior_documents") or "[]"
    try:
        candidates = _json.loads(raw_candidates)
    except ValueError:
        candidates = []
    document_id = args.entity_ref or ctx.run_id
    info = detect_revision(text, document_id=document_id, candidate_prior_documents=candidates)
    return [info] if info is not None else []


# ---------------------------------------------------------------------------
# set.assemble — Срез 8 (B9): DocumentSet по подтверждённой сущности/периоду/
# типу (см. докстринг `document_set.py`). Кандидаты — `DocumentCandidate`,
# уже вычисленные СОБСТВЕННЫМ однодокументным прогоном каждого документа, не
# читаемые заново здесь; `dynamic_collection_scope` раскрывает КАЖДЫЙ ключ
# кандидата ИЗОЛИРОВАННЫМ пробным проходом (тот же приём, что `analyze.
# full_bundle`/`out.narrative`) — декларативно, без хардкода «если два
# документа», по списку `candidate_keys`, который называет вызывающий (root
# `Target`/`root_extra`, этот узел — всегда корень своего графа: только
# корень получает `root_extra`, см. докстринг `graph_planner.py::plan_ex`).
def _set_assemble_scope(node_args: NodeArgs, _run_passport: "RunPassport | None") -> tuple[InputSpec, ...]:
    keys = [k for k in (node_args.extra.get("candidate_keys") or "").split(",") if k]
    return tuple(InputSpec(input_type="DocumentCandidate", concept=key) for key in keys)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(v for v in (value or "").split(",") if v)


def _set_assemble_real(args: NodeArgs, ctx) -> list:
    candidates = [e for e in ctx.dependencies() if isinstance(e, DocumentCandidate)]
    document_set = assemble_document_set(
        candidates, run_id=ctx.run_id,
        expected_entity_refs=_csv(args.extra.get("expected_entity_refs", "")),
        required_entities=int(args.extra.get("required_entities") or "1"),
        required_doc_types=_csv(args.extra.get("required_doc_types", "")),
        required_period_count=int(args.extra.get("required_period_count") or "1"),
    )
    return [document_set]


# ---------------------------------------------------------------------------
def build_registry(
    *, profile_id: str = "ifrs_nf_ru", locate_executor=None, read_value_executor=None,
    registry_data: dict | None = None,
    structure_executor=None, quote_executor=None, entities_executor=None,
    ocr_pages=None, table_labeler=None,
    judgment_executor=None, explanation_executor=None, external_executor=None, narrative_executor=None,
    report_executor=None, form_executor=None, file_writer=None,
    revision_executor=None, condition_executor=None,
    approach_policy: ApproachPolicy | None = None,
) -> AtomRegistry:
    """Собрать реестр трёх целевых сценариев (B3: точечный показатель, полный
    анализ, форма роли) плюс три атома Среза 5b (`read.structure`/`read.quote`/
    `read.entities`) — они регистрируются как СПОСОБНОСТИ реестра тем же
    образом, что `locate`/`read.value`, но (как и `doc.textify`/`doc.index`
    Среза 5a) не подключены как `requires` трёх сценариев ниже: разворот
    `GoalType.EXTRACT_STRUCTURE`/`DOCUMENT_QA` в корень графа — работа
    `root_from_goal` (intent-слой, вне периметра этой сессии), не этого
    реестра. `locate_executor`/`read_value_executor`/`structure_executor`/
    `quote_executor`/`entities_executor` — единственные замены, которые вправе
    делать вызывающий: граница с реальным модельным шлюзом по умолчанию; всё
    остальное — настоящий код.

    `profile_id` — ОДНА ячейка за раз (по умолчанию `ifrs_nf_ru`, самый частый
    случай трёх сценариев этого среза). Составление `produces` сразу по
    нескольким ячейкам рискует столкнуть одинаковое имя enum'а из «извлекаемых»
    одной ячейки с «расчётными» другой — это работа композиции реестра
    (Срез 13), не этого среза; здесь регистрируем СПОСОБНОСТИ ровно под одну
    ячейку, как и делает сегодняшний рабочий путь (`--cell`).
    """
    locate_executor = locate_executor or _locate_batch_real
    read_value_executor = read_value_executor or _read_value_batch_real
    structure_executor = structure_executor or _read_structure_batch_real
    quote_executor = quote_executor or _read_quote_batch_real
    entities_executor = entities_executor or _read_entities_real
    revision_executor = revision_executor or _id_revision_real
    condition_executor = condition_executor or _read_condition_batch_real
    approach_policy = approach_policy or DEFAULT_APPROACH_POLICY
    # Срез 6b: те же четыре модельные/сетевые границы, тем же приёмом —
    # `judgment_executor`/`explanation_executor` заменяют ПАРУ (батч-функцию
    # ЦЕЛИКОМ, как `locate_executor`), `external_executor`/`narrative_executor`
    # — единственный узел каждого. Реальная LLM-граница внутри `assess_
    # judgment_batch`/`explain_internal_batch`/`out_narrative_executor`
    # (`llm_json_call`) и `explain_external_executor` (`collector`) остаётся
    # доступной отдельно для юнит-тестов атомов — здесь же подменяется весь
    # узел графа, что и нужно интеграционным тестам без сети/модели.
    judgment_executor = judgment_executor or _assess_judgment_batch_real
    explanation_executor = explanation_executor or _explain_internal_batch_real
    external_executor = external_executor or _explain_external_real
    narrative_executor = narrative_executor or _out_narrative_real
    report_executor = report_executor or out_report_executor
    form_executor = form_executor or out_form_executor
    file_executor = make_out_file_executor(file_writer)
    # Production ingress may pass the exact effective snapshot used to create
    # RunPassport.  Re-reading layers here would create a small but real race:
    # registry files could change between passport freeze and AtomRegistry
    # construction.  Tests and direct library callers keep the existing lookup.
    reg = registry_data if registry_data is not None else registry_for_profile(profile_id)
    extractable_enums = tuple(sorted({m["enum"] for m in reg.get("extractable", [])}))
    # Retrofit v9 (R3.3): `locate` produces `CandidateLocator` typé по
    # концепту — не строка с префиксом `locator:` (тип артефакта различает
    # `OutputSpec.output_type`, не имя концепта). Catch-all
    # `OutputSpec(concept=None)` в конце каждого кортежа — R4/R5a: единственная
    # запись, через которую `AtomRegistry._producers_of_single` обслуживает
    # `Target.concept_id`, которого нет в реестре (`TargetFactory` ставит
    # исходную формулировку пользователя как `concept_id` для
    # `extract_ad_hoc_metric`/`calculate_ad_hoc_metric` — `AdHocConceptSpec`/
    # `AdHocFormulaSpec` живут в `Case`, не в этом реестре).
    locator_outputs = tuple(
        OutputSpec(output_type="CandidateLocator", concept=enum) for enum in extractable_enums
    ) + (OutputSpec(output_type="CandidateLocator"),)
    extracted_metric_outputs = tuple(
        OutputSpec(output_type="ExtractedMetric", concept=enum) for enum in extractable_enums
    ) + (OutputSpec(output_type="ExtractedMetric"),)
    calculated_enums = tuple(sorted({_calc_entry_enum(c) for c in reg.get("calculated", [])} - {""}))
    calculated_metric_outputs = tuple(
        OutputSpec(output_type="CalculatedMetric", concept=enum) for enum in calculated_enums
    ) + (OutputSpec(output_type="CalculatedMetric"),)
    # Срез 6b: `ref.prior`/`assess.judgment`/`explain.internal` — один
    # producer на весь союз extractable ∪ calculated (концепт может быть
    # любого рода MetricEvidence), плюс catch-all для ad-hoc.
    metric_concept_union = tuple(sorted(set(extractable_enums) | set(calculated_enums)))
    reference_value_outputs = tuple(
        OutputSpec(output_type="ReferenceValue", concept=enum) for enum in metric_concept_union
    ) + (OutputSpec(output_type="ReferenceValue"),)
    judgment_outputs = tuple(
        OutputSpec(output_type="Judgment", concept=enum) for enum in metric_concept_union
    ) + (OutputSpec(output_type="Judgment"),)
    explanation_outputs = tuple(
        OutputSpec(output_type="Explanation", concept=enum) for enum in metric_concept_union
    ) + (OutputSpec(output_type="Explanation"),)
    # Срез 9: `ref.plan`/`ref.norm` производят ТОТ ЖЕ Python-тип
    # `ReferenceValue`, что `ref.prior`, но под РАЗНЫМИ output_type-строками
    # (`ReferenceValue:plan`/`ReferenceValue:norm`, тот же приём, что
    # `Verified:citation`/`doc.textify:pages`) — иначе три разных producer'а
    # одного и того же голого `"ReferenceValue"` были бы неразличимым
    # `ChoicePoint` для любого потребителя (см. докстринг `reference.py`).
    reference_value_plan_outputs = tuple(
        OutputSpec(output_type="ReferenceValue:plan", concept=enum) for enum in metric_concept_union
    ) + (OutputSpec(output_type="ReferenceValue:plan"),)
    reference_value_norm_outputs = tuple(
        OutputSpec(output_type="ReferenceValue:norm", concept=enum) for enum in metric_concept_union
    ) + (OutputSpec(output_type="ReferenceValue:norm"),)
    rule_comparison_outputs = tuple(
        OutputSpec(output_type="RuleComparison", concept=enum) for enum in metric_concept_union
    ) + (OutputSpec(output_type="RuleComparison"),)
    def _goal_join(_args, _ctx):
        # Structural fan-in only. Scenario results remain typed outputs of
        # their own roots; the join establishes completion semantics/UI shape.
        return []

    return AtomRegistry([
        AtomDef(
            name="goal.join", version=ATOM_VERSION, produces=("CompoundResult",),
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="run_scoped", cost_class="free", executor=_goal_join,
            key_fields=("scenario_refs",),
        ),
        AtomDef(
            # `execution_kind="deterministic"`, не "io": он читает текст, уже
            # переданный во входах прогона (`ExecutionContext.inputs.
            # document_text`), а не ходит в сеть — `document_read` (A4)
            # намеренно не даёт сети вовсе («чтение файла разрешено, сеть —
            # нет»), и `execution_kind="io"` под этим профилем — ровно то
            # нарушение, которое `validate_sandbox_dispatch` (A2.1.8) обязан
            # ловить: узел получал бы «io», не имея права на сеть.
            name="ingest.document", version=ATOM_VERSION, produces=("document_text",),
            sandbox_profile="document_read", execution_kind="deterministic", cache_policy="run_scoped",
            cost_class="free", executor=_ingest_document, key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            name="identify.classify", version=ATOM_VERSION, produces=("RunPassport",),
            requires=("document_text",), sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="run_scoped", cost_class="free", executor=_identify_classify,
            key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            # Настоящий узел графа (Срез 5a) — не подключён как requires трёх
            # сценариев этого файла (см. докстринг модуля), проверен отдельной
            # сквозной цепочкой. `execution_kind="io"`: постраничный OCR ходит
            # в сеть на заранее известный узел (`_OCR_SANDBOX_PROFILE`).
            name="doc.textify", version=ATOM_VERSION, produces=("doc.textify:pages",),
            sandbox_profile=_OCR_SANDBOX_PROFILE, execution_kind="io", cache_policy="run_scoped",
            cost_class="cheap", executor=_bind_doc_textify(ocr_pages), key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            name="doc.index", version=ATOM_VERSION, produces=("doc.textify:index",),
            requires=("doc.textify:pages",), sandbox_profile="document_read", execution_kind="model",
            cache_policy="run_scoped", cost_class="cheap", executor=_bind_doc_index(table_labeler),
            key_fields=("entity_ref", "period_ref"),  # один документ на прогон, не по концепту — как ingest.document
        ),
        AtomDef(
            name="locate", version=LOCATE_ATOM_VERSION, produces=locator_outputs,
            requires=("document_text",), needs_run_passport=True, sandbox_profile="document_read",
            execution_kind="model", cache_policy="run_scoped", cost_class="model_call",
            executor=locate_executor, batch_key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            # Срез 6a: обязательные гейты после извлечения, до любого
            # потребителя (A1.7/A1.8) — норма единицы, норма времени, границы
            # правдоподобия. Порядок цепочки: read.value → norm.unit →
            # norm.time → verify.bounds; потребитель ExtractedMetric зависит
            # от ПОСЛЕДНЕГО узла цепочки, обойти нельзя по построению графа.
            name="read.value", version=ATOM_VERSION, produces=extracted_metric_outputs,
            needs_run_passport=True, dynamic_requires=_read_value_dynamic_requires,
            mandatory_gates=("norm.unit", "norm.time", "verify.bounds"),
            sandbox_profile="document_read", execution_kind="model", cache_policy="run_scoped",
            cost_class="model_call", executor=_wrap_extracted_metric(read_value_executor),
            batch_key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            # Срез 5b: сегодня единственный реальный `table_kind` — "segments"
            # (см. докстринг `read_structure.py`) — концепт этого `OutputSpec`,
            # не отдельный вход графа.
            name="read.structure", version=ATOM_VERSION,
            produces=(OutputSpec(output_type="ExtractedStructure", concept="segments"),),
            requires=("document_text",), needs_run_passport=True, sandbox_profile="document_read",
            execution_kind="model", cache_policy="run_scoped", cost_class="model_call",
            executor=_wrap_extracted_structure(structure_executor), batch_key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            # `QuoteEvidence` сама planning-семантика (без обёртки) — концепт
            # свободного текста темы цитаты, не enum реестра, поэтому нет
            # закрытого списка `OutputSpec` — единственная запись с
            # `concept=None` обслуживает любую тему (тот же catch-all
            # механизм, что у `locate`/`read.value` для ad-hoc концептов).
            name="read.quote", version=ATOM_VERSION, produces=(OutputSpec(output_type="QuoteEvidence"),),
            requires=("document_text",), mandatory_gates=("verify.citation",),
            sandbox_profile="document_read", execution_kind="model",
            cache_policy="run_scoped", cost_class="model_call", executor=quote_executor,
            batch_key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            # Не батчируется по концепту — как `identify.classify`: один
            # логический вызов на документ, `EntityRelation` без измерения
            # "какой концепт".
            name="read.entities", version=ATOM_VERSION, produces=("EntityRelation",),
            requires=("document_text",), needs_run_passport=True, sandbox_profile="document_read",
            execution_kind="model", cache_policy="run_scoped", cost_class="model_call",
            executor=entities_executor, key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            # Срез 10: явная пользовательская поправка — новый immutable
            # evidence wrapper. Exact replaced evidence id входит в NodeKey,
            # поэтому два override одного concept не дедуплицируются случайно.
            name="read.override", version=ATOM_VERSION,
            produces=(OutputSpec(output_type="UserProvidedMetric"),),
            dynamic_requires=read_override_requires,
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="none", cost_class="free", executor=read_override_executor,
            key_fields=("concept", "entity_ref", "period_ref", "basis", "document_version",
                        "policy_version", "overridden_evidence_id"),
        ),
        AtomDef(
            # Срез 6a: гейты в порядке identity → cross → time → bounds.
            # identity/cross сами объявляют СВОЙ `dynamic_requires` (вторая,
            # независимо полученная сторона сравнения — см. расширение
            # `GraphPlanner._apply_mandatory_gates`); norm.time/verify.bounds
            # получают только предыдущий узел цепочки, им хватает.
            name="calc.formula", version=CALC_FORMULA_VERSION, produces=calculated_metric_outputs,
            needs_run_passport=True, dynamic_requires=calc_formula_requires,
            mandatory_gates=("verify.identity", "verify.cross", "norm.time", "verify.bounds"),
            sandbox_profile="compute_no_network",
            execution_kind="deterministic", cache_policy="run_scoped", cost_class="free",
            executor=calc_formula_executor, requirement_extra=calc_formula_requirement_extra,
        ),
        AtomDef(
            name="verify.arithmetic", version=ATOM_VERSION, produces=("Verified:formula",),
            sandbox_profile="compute_no_network", execution_kind="deterministic", cache_policy="none",
            cost_class="free", executor=_verify_arithmetic,
        ),
        AtomDef(
            name="norm.unit", version=ATOM_VERSION, produces=("Verified:unit",),
            sandbox_profile="compute_no_network", execution_kind="deterministic", cache_policy="none",
            cost_class="free", executor=norm_unit_gate,
        ),
        AtomDef(
            name="norm.time", version=ATOM_VERSION, produces=("Verified:time",),
            sandbox_profile="compute_no_network", execution_kind="deterministic", cache_policy="none",
            cost_class="free", executor=norm_time_gate,
        ),
        AtomDef(
            name="verify.bounds", version=ATOM_VERSION, produces=("Verified:bounds",),
            sandbox_profile="compute_no_network", execution_kind="deterministic", cache_policy="none",
            cost_class="free", executor=verify_bounds_gate,
        ),
        AtomDef(
            # `dynamic_requires` — вторая сторона (ExtractedMetric) только для
            # concept'ов, зарегистрированных в `verification_rules` этой
            # ячейки; для остальных возвращает `()`, гейт становится no-op
            # `not_verified` (см. `verify_identity_cross.py`).
            name="verify.identity", version=ATOM_VERSION, produces=("Verified:identity",),
            dynamic_requires=verify_identity_requires,
            sandbox_profile="compute_no_network", execution_kind="deterministic", cache_policy="none",
            cost_class="free", executor=verify_identity_gate,
        ),
        AtomDef(
            # `dynamic_requires` — вторая сторона только для concept'ов
            # «двойной природы» (extractable ∩ calculated) этой ячейки;
            # безопасно для остальных (не создаёт `Unreachable`).
            name="verify.cross", version=ATOM_VERSION, produces=("Verified:cross",),
            dynamic_requires=verify_cross_requires,
            sandbox_profile="compute_no_network", execution_kind="deterministic", cache_policy="none",
            cost_class="free", executor=verify_cross_gate,
        ),
        AtomDef(
            name="verify.citation", version=ATOM_VERSION, produces=("Verified:citation",),
            sandbox_profile="compute_no_network", execution_kind="deterministic", cache_policy="none",
            cost_class="free", executor=verify_citation_gate,
        ),
        AtomDef(
            # Retrofit v9 (R3, A1.7): показатели реестра — коллекция
            # `collection_scope` (изолированный пробный проход на каждый), не
            # `optional_requires` — «полный анализ» это то, что реально
            # нашлось и посчиталось по всему применимому набору, а не
            # всё-или-ничего по 100+ показателям сразу.
            name="analyze.full_bundle", version=ATOM_VERSION, produces=("EvidenceBundleV2",),
            needs_run_passport=True, dynamic_collection_scope=_analyze_full_bundle_scope,
            sandbox_profile="compute_no_network", execution_kind="deterministic", cache_policy="none",
            cost_class="free", executor=_analyze_full_bundle,
        ),
        AtomDef(
            name="out.form", version=ATOM_VERSION, produces=("FormOutput",), needs_run_passport=True,
            dynamic_collection_scope=_out_form_scope, sandbox_profile="compute_no_network",
            execution_kind="deterministic", cache_policy="none", cost_class="free", executor=form_executor,
            key_fields=("entity_ref", "period_ref"),
        ),
        # -----------------------------------------------------------------
        # Срез 6b (B7): анализ как чистый потребитель проверенной доказательной
        # базы — `ref.prior → assess.judgment → explain.internal/external →
        # out.narrative`, с обязательным `verify.grounding` перед выдачей.
        AtomDef(
            name="ref.prior", version=ATOM_VERSION, produces=reference_value_outputs,
            needs_run_passport=True, dynamic_requires=ref_prior_requires,
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="run_scoped", cost_class="free", executor=ref_prior_executor,
        ),
        AtomDef(
            name="assess.judgment", version=ATOM_VERSION, produces=judgment_outputs,
            needs_run_passport=True, dynamic_requires=assess_judgment_requires,
            sandbox_profile="compute_no_network", execution_kind="model",
            cache_policy="run_scoped", cost_class="model_call", executor=judgment_executor,
            batch_key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            name="explain.internal", version=ATOM_VERSION, produces=explanation_outputs,
            needs_run_passport=True, dynamic_requires=explain_internal_requires,
            sandbox_profile="compute_no_network", execution_kind="model",
            cache_policy="run_scoped", cost_class="model_call", executor=explanation_executor,
            batch_key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            # Не "compute_no_network": `explain.external` реально ходит в сеть
            # (веб-поиск, `analysis.external.collect`) — тот же класс, что
            # `doc.textify` (A4/решение №9, узкий заранее известный узел).
            name="explain.external", version=ATOM_VERSION, produces=(OutputSpec(output_type="ExternalFactRef"),),
            needs_run_passport=True, sandbox_profile=_WEB_SEARCH_SANDBOX_PROFILE, execution_kind="io",
            cache_policy="run_scoped", cost_class="cheap", executor=external_executor,
            key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            # Часть 19 доводки: поиск для цикла. Запрос — концепт шага (по нему `Case` различает
            # результаты), режим — `extra["topic"]`; паспорт и документ узлу не нужны.
            name="web.search", version=WEB_SEARCH_ATOM_VERSION, produces=(OutputSpec(output_type=WEB_SEARCH_TYPE),),
            sandbox_profile=_WEB_SEARCH_SANDBOX_PROFILE, execution_kind="io",
            cache_policy="run_scoped", cost_class="cheap", executor=web_search_executor,
            key_fields=("concept", "entity_ref", "topic"), step_check=web_search_step_problem,
        ),
        AtomDef(
            name="out.narrative", version=ATOM_VERSION, produces=(OutputSpec(output_type="NarrativeOutput"),),
            needs_run_passport=True, requires=(InputSpec(input_type="EvidenceBundleV2"),),
            dynamic_collection_scope=_out_narrative_scope, mandatory_gates=("verify.grounding",),
            sandbox_profile="compute_no_network", execution_kind="model",
            cache_policy="none", cost_class="model_call", executor=narrative_executor,
            key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            # MONITOR_CHANGES по одному документу (карточка 32): детерминированная
            # таблица изменений между периодами + объяснение как необязательная часть
            # (заблокированный `verify.grounding` текст не убирает таблицу).
            name="out.changes", version=ATOM_VERSION, produces=("PeriodChangeReport",),
            needs_run_passport=True, requires=(
                InputSpec(input_type="EvidenceBundleV2"),
                InputSpec(input_type="NarrativeOutput"),
            ),
            dynamic_collection_scope=_out_changes_scope, sandbox_profile="compute_no_network",
            execution_kind="deterministic", cache_policy="none", cost_class="free",
            executor=out_changes_executor, key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            # Финальный отчёт не извлекает и не считает показатели: он
            # структурирует уже типизированные narrative/evidence результаты.
            # Оба publication gate обязательны и потому являются частью
            # графа, а не условием, которое может забыть HTTP-обработчик.
            name="out.report", version=ATOM_VERSION, produces=(OutputSpec(output_type="ReportOutput"),),
            needs_run_passport=True, requires=(
                InputSpec(input_type="NarrativeOutput"),
                InputSpec(input_type="EvidenceBundleV2"),
            ),
            dynamic_collection_scope=_out_report_scope,
            mandatory_gates=("verify.grounding", "verify.citation"),
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="none", cost_class="free", executor=report_executor,
            key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            # Единственный источник файла — один из типизированных outputs.
            # Формулы и финансовая арифметика остаются в domain atoms; этот
            # узел только сериализует/рисует и фиксирует lineage + hash.
            name="out.file", version=ATOM_VERSION, produces=(OutputSpec(output_type="FileArtifact"),),
            needs_run_passport=True, dynamic_requires=_out_file_requires,
            sandbox_profile=_FILE_SANDBOX_PROFILE, execution_kind="io",
            cache_policy="none", cost_class="free", executor=file_executor,
            key_fields=("entity_ref", "period_ref", "source_output_type", "file_kind", "template_version"),
        ),
        AtomDef(
            name="verify.grounding", version=ATOM_VERSION, produces=("Verified:grounding",),
            dynamic_requires=verify_grounding_requires,
            sandbox_profile="compute_no_network", execution_kind="deterministic", cache_policy="none",
            cost_class="free", executor=verify_grounding_gate,
        ),
        # -----------------------------------------------------------------
        # Срез 8 (B9): DocumentSet и id.revision (см. докстринги `document_
        # set.py`/`id_revision.py`).
        AtomDef(
            # Per-документ (как `read.quote`/`read.entities`) — не нужен
            # `RunPassport`: маркер пересмотра ищется в тексте, который каждый
            # документ уже несёт независимо от классификации.
            name="id.revision", version=ATOM_VERSION, produces=(OutputSpec(output_type="RevisionInfo"),),
            requires=("document_text",), sandbox_profile="compute_no_network", execution_kind="model",
            cache_policy="run_scoped", cost_class="model_call", executor=revision_executor,
            key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            # Не нужен `RunPassport` — кандидаты приходят уже классифицированными
            # своим собственным однодокументным прогоном (`DocumentCandidate`
            # несёт готовую `EntityIdentity`), `set.assemble` их не переклассифицирует.
            name="set.assemble", version=ATOM_VERSION, produces=(OutputSpec(output_type="DocumentSet"),),
            dynamic_collection_scope=_set_assemble_scope,
            sandbox_profile="compute_no_network", execution_kind="deterministic", cache_policy="none",
            cost_class="free", executor=_set_assemble_real, key_fields=("entity_ref", "period_ref"),
        ),
        # -----------------------------------------------------------------
        # Срез 9 (B10): договоры, нормативы, планы (см. докстринги
        # `read_condition.py`/`ref_plan.py`/`ref_norm.py`/`assess_rule.py`).
        AtomDef(
            # Свободная тема (как `read.quote`) — не enum реестра, поэтому
            # единственная catch-all запись `OutputSpec(concept=None)`.
            # `mandatory_gates=("verify.citation",)` — та же обязательная
            # проверка адреса, что `read.quote`; любой потребитель
            # `ConditionEvidence` получает уже прошедшую (или явно
            # провалившую) её версию, обойти нельзя по построению графа.
            name="read.condition", version=ATOM_VERSION, produces=(OutputSpec(output_type="ConditionEvidence"),),
            requires=("document_text",), mandatory_gates=("verify.citation",),
            sandbox_profile="document_read", execution_kind="model",
            cache_policy="run_scoped", cost_class="model_call", executor=condition_executor,
            batch_key_fields=("entity_ref", "period_ref"),
        ),
        AtomDef(
            name="ref.plan", version=ATOM_VERSION, produces=reference_value_plan_outputs,
            needs_run_passport=True, dynamic_requires=ref_plan_requires,
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="run_scoped", cost_class="free", executor=ref_plan_executor,
        ),
        AtomDef(
            name="ref.norm", version=ATOM_VERSION, produces=reference_value_norm_outputs,
            needs_run_passport=True, dynamic_requires=ref_norm_requires,
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="run_scoped", cost_class="free", executor=ref_norm_executor,
        ),
        AtomDef(
            name="assess.rule", version=ATOM_VERSION, produces=rule_comparison_outputs,
            needs_run_passport=True, dynamic_requires=assess_rule_requires,
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="none", cost_class="free", executor=assess_rule_executor,
        ),
        # -----------------------------------------------------------------
        # Slice 11 (B12): subject history and monitoring. SubjectProfile
        # inputs are immutable available leaves supplied by Case history;
        # no atom below extracts numbers or starts a parallel pipeline.
        AtomDef(
            name="calc.series", version=ATOM_VERSION,
            produces=(OutputSpec(output_type="SeriesFact"),),
            requires=(InputSpec(input_type="SubjectProfile:previous"),
                      InputSpec(input_type="SubjectProfile:current")),
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="run_scoped", cost_class="free", executor=calc_series_executor,
            key_fields=("entity_ref", "policy_version"),
        ),
        AtomDef(
            name="calc.decompose", version=ATOM_VERSION,
            produces=(OutputSpec(output_type="DecompositionResult"),),
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="run_scoped", cost_class="free", executor=calc_decompose_executor,
            key_fields=("entity_ref", "policy_version", "decomposition_json"),
        ),
        AtomDef(
            name="ref.peer", version=ATOM_VERSION,
            produces=(OutputSpec(output_type="PeerReferenceSet"),),
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="run_scoped", cost_class="free", executor=ref_peer_executor,
            key_fields=("entity_ref", "policy_version", "peer_request_json"),
        ),
        AtomDef(
            name="watch.subscribe", version=ATOM_VERSION,
            produces=(OutputSpec(output_type="WatchSubscription"),),
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="none", cost_class="free", executor=watch_subscribe_executor,
            key_fields=("entity_ref", "policy_version", "subscription_json"),
        ),
        AtomDef(
            name="watch.diff", version=ATOM_VERSION,
            produces=(OutputSpec(output_type="SubjectDiff"),),
            requires=(InputSpec(input_type="SubjectProfile:previous"),
                      InputSpec(input_type="SubjectProfile:current")),
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="run_scoped", cost_class="free", executor=watch_diff_executor,
            key_fields=("entity_ref", "policy_version"),
        ),
        AtomDef(
            name="watch.approach", version=ATOM_VERSION,
            produces=(OutputSpec(output_type="ApproachReport"),),
            requires=(InputSpec(input_type="SubjectProfile:previous"),
                      InputSpec(input_type="SubjectProfile:current")),
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="run_scoped", cost_class="free",
            executor=bind_watch_approach(approach_policy),
            key_fields=("entity_ref", "policy_version"),
        ),
        AtomDef(
            name="monitor.compose", version=ATOM_VERSION,
            produces=(OutputSpec(output_type="MonitorResult"),),
            requires=(InputSpec(input_type="SubjectDiff"), InputSpec(input_type="SeriesFact"),
                      InputSpec(input_type="ApproachReport")),
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="none", cost_class="free", executor=monitor_compose_executor,
            key_fields=("entity_ref", "policy_version"),
        ),
    ])


def default_policy(profile_id: str = "ifrs_nf_ru", *, policy_version: str = "1.0.0") -> PlanPolicy:
    """`PlanPolicy` для этой ячейки: единственное реальное правило приоритета,
    которое нужно трём сценариям этого среза.

    Retrofit v9 (R3.3): `read.value`/`calc.formula` НЕ конкурируют за
    корень `Target(EBITDA)` — «извлечь EBITDA» и «рассчитать EBITDA» разные
    `Target` (`ExtractedMetric`/`CalculatedMetric`), у каждого ровно один
    производитель. Этот tie-break нужен ТОЛЬКО для резолюции `MetricEvidence`
    (общий union — собственные входы формулы, полный анализ, форма роли):
    enum'ы «двойной природы» (извлекаемые И расчётные одновременно —
    `two_stage_extractor._dual_nature_enums`) достижимы там ДВУМЯ путями, и
    без правила это законный `ChoicePoint` (A1.6) — продуктовое решение здесь
    простое и уже принято самим движком: предпочесть то, что реально
    написано в отчёте, расчёту того же значения из других показателей."""
    reg = registry_for_profile(profile_id)
    extractable = {m["enum"] for m in reg.get("extractable", [])}
    calculated = {_calc_entry_enum(c) for c in reg.get("calculated", [])} - {""}
    dual_nature = extractable & calculated
    return PlanPolicy(
        policy_version=policy_version,
        producer_priority={enum: ["read.value", "calc.formula"] for enum in dual_nature},
    )


__all__ = ["ATOM_VERSION", "build_registry", "default_policy"]
