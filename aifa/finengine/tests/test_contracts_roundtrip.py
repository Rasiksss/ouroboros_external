"""Every A0 contract type: importable, immutable, round-trips through JSON.

Пишется ДО реализации `src/finengine/contracts/` (методология C0: сначала
критерий готовности и тесты, потом код). Критерий для Фазы 0: все 30 сущностей
из A0 существуют как типы, каждая сериализуется/десериализуется без потери
данных, и ни одну нельзя мутировать после создания — исправление обязано
породить новый объект, а не изменить старый (нерушимое решение №4).
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from finengine.contracts import (
    AdHocConceptSpec,
    AdHocFormulaSpec,
    AmbiguityNote,
    ApproachPolicy,
    ApproachReport,
    ApproachSignal,
    Basis,
    BasisComparison,
    CalculatedMetric,
    CandidateLocator,
    CaseDocumentRef,
    CaseIdentity,
    CaseSnapshot,
    CaseTarget,
    ClassificationResult,
    ConditionEvidence,
    CorrectionIntent,
    DecompositionComponent,
    DecompositionResult,
    DerivedFact,
    DynamicArtifact,
    EphemeralAtomSource,
    DocumentCandidate,
    DocumentCard,
    DocumentIndex,
    DocumentIndexEntry,
    DocumentProbe,
    DocumentSet,
    DocumentSetMember,
    DocumentSetRejection,
    DocumentText,
    EntityIdentity,
    EntityRelation,
    EvidenceBundleV2,
    EvidenceRef,
    Explanation,
    ExplanationFactor,
    ExternalFactRef,
    ExtractedMetric,
    ExtractedStructure,
    FactDelta,
    FileArtifact,
    FormOutput,
    FormRow,
    FormulaDefinition,
    GoalClause,
    GoalIR,
    HumanOverride,
    Judgment,
    MetricDefinition,
    MonitoringCapacity,
    MonitorResult,
    MonitorRunTrace,
    NarrativeOutput,
    NumericClaimRecord,
    ChoicePoint,
    InvalidGoal,
    NodeKey,
    NodeRun,
    NodeRunEvent,
    OutputGap,
    OutputRunMetadata,
    OutputWarning,
    PeriodAxis,
    PeriodChangeReport,
    PeriodChangeRow,
    PeerCandidate,
    PeerExclusion,
    PeerMetricValue,
    PeerPolicy,
    PeerReferenceSet,
    PlanBuildResult,
    PlanFeasibility,
    PlanGraph,
    PlanNode,
    PlanPolicy,
    PlanScenario,
    PlanRunRecord,
    QuoteEvidence,
    ReferenceValue,
    ReportOutput,
    ReportSection,
    ReportThesis,
    PublicationChecks,
    ResultEvent,
    ResultReference,
    RevisionInfo,
    RuleComparison,
    RunPassport,
    ScalarFact,
    SourceLocator,
    SeriesChange,
    SeriesFact,
    SeriesGap,
    SeriesPoint,
    StructuredFact,
    SubjectAccess,
    SubjectDiff,
    SubjectKey,
    SubjectProfile,
    SubjectSnapshot,
    TargetNode,
    TypedError,
    ErrorEvent,
    CompletedEvent,
    HeartbeatEvent,
    LegacyCompatibilityEvent,
    Unreachable,
    UserProvidedMetric,
    VerificationFinding,
    WatchSchedule,
    WatchSubscription,
    OverrideAuditEvent,
    OverrideChoicePoint,
)

_NODE_KEY = dict(atom_name="extract.scalar", atom_version="1.0.0", args_fingerprint="abc123")
_PERIOD = dict(period_id="p-2025-q1", kind="duration", start="2025-01-01", end="2025-03-31")
_TARGET = dict(target_id="t-1", concept="SALES_REVENUE", output_kind="scalar")
_CLASSIFICATION = dict(
    profile_id="ifrs_nf_ru",
    standard="ifrs",
    company_type="non_financial",
    language="ru",
    confidence_low=False,
    reason="strict IFRS basis phrase",
)
_ENTITY = dict(entity_id="ent-1", canonical_name="ПАО Ромашка", raw_name="ПАО Ромашка")

# Одна минимальная валидная выборка kwargs на каждый тип из A0. Явные словари,
# а не генератор из аннотаций — 30 типов не настолько много, чтобы прятать
# форму данных за абстракцией, а явный список легче свести с самим A0.
SAMPLES: dict[type[BaseModel], dict] = {
    RunPassport: dict(
        goal_ir_id="goal-1",
        classification=ClassificationResult(**_CLASSIFICATION),
        entity=EntityIdentity(**_ENTITY),
        registry_version="registry-2026.09",
        methodology_version="methodology-1.0",
        model_version="claude-sonnet-5",
        prompt_version="prompt-1.0",
        sandbox_image_version="sandbox-2026.09",
    ),
    ClassificationResult: _CLASSIFICATION,
    EntityIdentity: _ENTITY,
    DocumentProbe: dict(
        sha256="a" * 64,
        byte_size=1024,
        technical_status="ok",
        total_pages=10,
        pdf_origin="digital",
        needs_ocr=False,
    ),
    GoalIR: dict(
        goal_id="goal-1",
        raw_query="какая выручка за 1кв2025",
        intent_kind="extract_registered_metric",
        targets=[TargetNode(**_TARGET)],
    ),
    GoalClause: dict(
        goal_ref="goal-2",
        raw_query="посчитай чистую рентабельность",
        intent_kind="calculate_registered_metric",
        targets=[TargetNode(
            target_id="t-2", concept="NET_PROFIT_MARGIN", output_kind="scalar",
        )],
        depends_on=["goal-1"],
    ),
    TargetNode: _TARGET,
    AmbiguityNote: dict(term="прибыль", candidates=["NET_INCOME", "EBITDA_MARGIN"], reason="не указан вид прибыли"),
    DocumentCard: dict(
        document_id="doc-1",
        filename="report.pdf",
        company_name="ПАО Ромашка",
        language="ru",
        document_version="v1",
    ),
    DocumentText: dict(document_id="doc-1", document_version="v1", page=12, text="Выручка ..."),
    DocumentIndex: dict(
        document_id="doc-1", document_version="v1", indexer_version="1.0.0", total_pages=120,
        entries=[DocumentIndexEntry(kind="table", label="balance_sheet", page_start=10, page_end=10)],
    ),
    DocumentIndexEntry: dict(kind="table", label="balance_sheet", page_start=10, page_end=10),
    SourceLocator: dict(page=12, table_or_section="T001", text_fragment="Выручка | 100 000"),
    CandidateLocator: dict(
        produced_by_atom="locate", produced_by_atom_version="1.0.0",
        document_id="doc-1", document_version="v1", concept="SALES_REVENUE",
        locator=SourceLocator(page=12, table_or_section="T001", text_fragment="Выручка | 100 000"),
        confidence=0.6, origin="model",
    ),
    ScalarFact: dict(
        produced_by_atom="extract.scalar",
        produced_by_atom_version="1.0.0",
        source_ref="doc-1#p12",
        value=123.45,
        unit="RUB_MLN",
        period_ref="p-2025-q1",
    ),
    StructuredFact: dict(
        produced_by_atom="extract.structured",
        produced_by_atom_version="1.0.0",
        source_ref="doc-1#p40",
        table_kind="segments",
        rows=[{"segment": "Retail", "revenue": 100.0}],
    ),
    QuoteEvidence: dict(
        produced_by_atom="extract.quote",
        produced_by_atom_version="1.0.0",
        source_ref="doc-1#p3",
        quote_text="Совет директоров рекомендовал не выплачивать дивиденды.",
        page=3,
    ),
    ConditionEvidence: dict(
        produced_by_atom="read.condition",
        produced_by_atom_version="1.0.0",
        source_ref="doc-1#p50",
        concept="RATIO_NET_DEBT_EBITDA",
        condition_text="Net Debt to EBITDA shall not exceed 3.5x.",
        page=50,
        operator="<=",
        threshold_value=3.5,
        unit="x",
        subject="the Borrower",
        definition_text="Net Debt means total borrowings less cash and cash equivalents.",
        exceptions=["excluding lease liabilities"],
    ),
    EntityRelation: dict(
        produced_by_atom="extract.entities",
        produced_by_atom_version="1.0.0",
        source_ref="doc-1#p2",
        subject_entity="ООО Дочка",
        relation="subsidiary_of",
        object_entity="ПАО Ромашка",
    ),
    HumanOverride: dict(
        produced_by_atom="human.review",
        produced_by_atom_version="n/a",
        source_ref="review-session-1",
        overridden_evidence_id="ev-1",
        new_value="124.00",
        reviewer="analyst@example.com",
        justification="опечатка в исходном скане, сверено со страницей 12",
    ),
    DerivedFact: dict(
        produced_by_atom="calc.formula",
        produced_by_atom_version="1.0.0",
        source_ref="formula:net_debt_ebitda",
        formula_id="net_debt_ebitda",
        formula_version="1.0.0",
        inputs=["ev-1", "ev-2"],
        value=1.8,
    ),
    Basis: dict(standard="ifrs", perimeter="consolidated", currency="RUB"),
    BasisComparison: dict(status="not_comparable", reason="стандарт не совпадает: ifrs против rsbu"),
    MetricDefinition: dict(concept="EBITDA", version="registry-1", unit_kind="money", source="registry"),
    FormulaDefinition: dict(
        formula_id="net_debt_ebitda", version="registry-1", expression="NET_DEBT / ANNUAL(EBITDA)",
        dependencies=["NET_DEBT", "EBITDA"], source="registry",
    ),
    VerificationFinding: dict(
        produced_by_atom="verify.arithmetic",
        produced_by_atom_version="1.0.0",
        source_ref="ev-3",
        check_kind="arithmetic",
        status="pass",
        detail="сумма сегментов совпала с итогом",
    ),
    EvidenceBundleV2: dict(
        run_id="run-1",
        scalar_facts=[ScalarFact(
            produced_by_atom="extract.scalar", produced_by_atom_version="1.0.0",
            source_ref="doc-1#p12", value=123.45, unit="RUB_MLN", period_ref="p-2025-q1",
        )],
        structured_facts=[StructuredFact(
            produced_by_atom="extract.structured", produced_by_atom_version="1.0.0",
            source_ref="doc-1#p40", table_kind="segments",
            rows=[{"segment": "Retail", "revenue": 100.0}],
        )],
        derived_facts=[DerivedFact(
            produced_by_atom="calc.formula_engine", produced_by_atom_version="1.0.0",
            source_ref="formula:net_debt_ebitda", formula_id="net_debt_ebitda",
            formula_version="1.0.0", inputs=["ev-1", "ev-2"], value=1.8,
        )],
    ),
    PeriodAxis: _PERIOD,
    RevisionInfo: dict(revision_id="rev-1", document_id="doc-1", revision_kind="original"),
    DocumentCandidate: dict(
        document_id="doc-1", document_version="v1", entity=EntityIdentity(**_ENTITY),
        period_ref="p-2025-q1", doc_type="report",
    ),
    DocumentSetMember: dict(
        document_id="doc-1", document_version="v1", entity_id="ent-1",
        canonical_name="ПАО Ромашка", period_ref="p-2025-q1", doc_type="report",
    ),
    DocumentSetRejection: dict(
        document_id="doc-2", document_version="v1", reason="entity_mismatch",
        detail="другой подтверждённый эмитент",
    ),
    DocumentSet: dict(
        run_id="run-1",
        members=[DocumentSetMember(
            document_id="doc-1", document_version="v1", entity_id="ent-1",
            canonical_name="ПАО Ромашка", period_ref="p-2025-q1", doc_type="report",
        )],
        required_entities=1, completeness="complete",
    ),
    PlanFeasibility: dict(feasible=True),
    NodeKey: _NODE_KEY,
    PlanNode: dict(node_key=NodeKey(**_NODE_KEY), sandbox_profile="compute_no_network"),
    PlanGraph: dict(
        plan_id="plan-1",
        root=NodeKey(**_NODE_KEY),
        nodes=[PlanNode(node_key=NodeKey(**_NODE_KEY), sandbox_profile="compute_no_network")],
        graph_hash="sha256:deadbeef",
    ),
    PlanScenario: dict(
        goal_ref="goal-1",
        goal_type="extract_registered_metric",
        root=NodeKey(**_NODE_KEY),
    ),
    NodeRun: dict(
        node_key=NodeKey(**_NODE_KEY), run_id="run-1", status="planned",
        sandbox_profile="compute_no_network",
    ),
    PlanPolicy: dict(policy_version="policy-1", producer_priority={"CET1": ["extract.scalar"]}),
    Unreachable: dict(concept="BRAND_KARMA_SCORE", reason="нет атома-производителя", requested_by="root"),
    ChoicePoint: dict(
        concept="CET1", candidate_atoms=["extract.scalar", "extract.capital_prompt"],
        reason="PlanPolicy не называет приоритет производителя",
    ),
    InvalidGoal: dict(reason="TargetNode без concept"),
    PlanBuildResult: dict(
        graph=PlanGraph(
            plan_id="plan-1", root=NodeKey(**_NODE_KEY),
            nodes=[PlanNode(node_key=NodeKey(**_NODE_KEY), sandbox_profile="compute_no_network")],
            graph_hash="sha256:deadbeef",
        ),
        feasibility=PlanFeasibility(feasible=True),
    ),
    RuleComparison: dict(
        rule_id="rule-1",
        concept="RATIO_NET_DEBT_EBITDA",
        operator="<=",
        actual_value=3.0,
        actual_evidence_id="ev-1",
        threshold_value=3.5,
        threshold_reference_id="ref-1",
        unit="x",
        delta=-0.5,
        relative_delta=-0.142857,
        headroom=0.5,
        passed=True,
    ),
    Judgment: dict(
        judgment_id="j-1",
        concept="LIQUIDITY",
        verdict="stable",
        supporting_evidence_ids=["ev-1"],
        methodology_ref="methodology-1.0#liquidity",
    ),
    Explanation: dict(
        explanation_id="exp-1",
        judgment_id="j-1",
        text="Коэффициент текущей ликвидности не изменился год к году.",
        cited_evidence_ids=["ev-1"],
        factors=[ExplanationFactor(text="Рост запасов компенсировал рост обязательств.",
                                    causal_type="correlation", evidence_ids=["ev-1"])],
    ),
    ExplanationFactor: dict(
        text="Рост выручки совпал с запуском нового завода.",
        causal_type="co_movement",
        evidence_ids=["ev-1"],
    ),
    ExternalFactRef: dict(
        fact_id="extfact-1",
        text="Регулятор повысил ключевую ставку.",
        entity_id="ent-1",
        jurisdiction="RU",
        date="2025-03-01",
        source="cbr.ru",
        url="https://cbr.ru/press/pr/?id=1",
        period_ref="2025-Q1",
    ),
    ReferenceValue: dict(
        concept="REVENUE",
        current_period_ref="2025-Q1",
        current_evidence_id="ev-1",
        prior_period_ref="2024-Q1",
        prior_evidence_id="ev-0",
        prior_metric_id="metric-0",
        selection_reason="предыдущий период того же прогона извлечения",
    ),
    NumericClaimRecord: dict(
        text="выручка 1500 млн руб.",
        kind="financial",
        value=1500.0,
        period_ref="2025-Q1",
        unit="млн. руб.",
        evidence_id="ev-1",
    ),
    ReportOutput: dict(output_id="out-1", run_id="run-1", sections=[{"title": "Обзор", "body": "..."}]),
    FormOutput: dict(
        output_id="out-2",
        run_id="run-1",
        role="dgr",
        rows=[FormRow(kind="metric", enum="SALES_REVENUE", label="Выручка", value=100.0, available=True)],
    ),
    NarrativeOutput: dict(output_id="out-3", run_id="run-1", text="Выручка выросла.", cited_evidence_ids=["ev-1"]),
    FileArtifact: dict(artifact_id="art-1", run_id="run-1", kind="xlsx", storage_ref="s3://bucket/run-1.xlsx"),
    SubjectSnapshot: dict(
        snapshot_id="snap-1",
        subject_entity="ПАО Ромашка",
        as_of="2025-03-31",
        key_facts_evidence_ids=["ev-1"],
    ),
    # Retrofit v9 (R2): planning/domain wrappers вокруг evidence payload.
    ExtractedMetric: dict(
        scalar_fact=ScalarFact(
            produced_by_atom="read.value", produced_by_atom_version="1.0.0",
            source_ref="doc-1#p12", concept="SALES_REVENUE", value=123.45, unit="RUB_MLN",
            period_ref="p-2025-q1",
        ),
        concept="SALES_REVENUE", period_ref="p-2025-q1",
    ),
    CalculatedMetric: dict(
        derived_fact=DerivedFact(
            produced_by_atom="calc.formula_engine", produced_by_atom_version="1.0.0",
            source_ref="formula:net_debt_ebitda", concept="NET_DEBT_EBITDA",
            formula_id="net_debt_ebitda", formula_version="1.0.0",
            inputs=["ev-1", "ev-2"], value=1.8,
        ),
        concept="NET_DEBT_EBITDA", period_ref="p-2025-q1",
    ),
    UserProvidedMetric: dict(
        human_override=HumanOverride(
            produced_by_atom="human.override", produced_by_atom_version="1.0.0",
            source_ref="user-1", overridden_evidence_id="ev-1", new_value="123.45",
            reviewer="analyst-1", justification="исправлена опечатка в отчёте",
        ),
        concept="SALES_REVENUE", period_ref="p-2025-q1",
    ),
    ExtractedStructure: dict(
        structured_fact=StructuredFact(
            produced_by_atom="extract.structured", produced_by_atom_version="1.0.0",
            source_ref="doc-1#p40", table_kind="segments",
            rows=[{"segment": "Retail", "revenue": 100.0}],
        ),
        table_kind="segments",
    ),
    AdHocConceptSpec: dict(term="площадь торговых залов"),
    AdHocFormulaSpec: dict(
        term="выручка на квадратный метр торговой площади",
        expression="SALES_REVENUE / RETAIL_AREA",
        dependencies=["SALES_REVENUE", "RETAIL_AREA"],
    ),
}

# Срезы 10–11 расширили публичный каталог persistent Case и subject history.
# Samples остаются явными: так изменение обязательного provenance-поля ломает
# этот тест сразу, а не маскируется генератором по аннотациям.
_SUBJECT_KEY = SubjectKey(
    value="subject:lei:5493001KJTIIGC8Y1R12",
    entity_id="entity:5493001KJTIIGC8Y1R12",
    identifiers={"lei": "5493001KJTIIGC8Y1R12"},
)
_PROFILE_METRIC = ScalarFact(
    produced_by_atom="read.value",
    produced_by_atom_version="1.0.0",
    source_ref="doc-1#p12",
    concept="SALES_REVENUE",
    value=100.0,
    unit="RUB_MLN",
    period_ref="2025",
    basis="ifrs:consolidated",
)
_SUBJECT_DIFF = SubjectDiff(
    subject_key=_SUBJECT_KEY,
    previous_snapshot_id="snap-1",
    current_snapshot_id="snap-2",
    previous_plan_run_id="plan-run-1",
    current_plan_run_id="plan-run-2",
    comparability="comparable",
    changed_facts=[FactDelta(
        identity="SALES_REVENUE|2025|RUB_MLN",
        old_evidence_id="ev-old",
        new_evidence_id="ev-new",
        old_value=90.0,
        new_value=100.0,
        absolute_delta=10.0,
        relative_delta=10.0 / 90.0,
    )],
)
_SERIES_FACT = SeriesFact(
    produced_by_atom="calc.series",
    produced_by_atom_version="1.0.0",
    source_ref="subject-history:subject:lei:5493001KJTIIGC8Y1R12",
    concept="SALES_REVENUE",
    points=[SeriesPoint(
        period_ref="2025", value=100.0, unit="RUB_MLN",
        basis="ifrs:consolidated", evidence_id=_PROFILE_METRIC.evidence_id,
    )],
    input_evidence_ids=[_PROFILE_METRIC.evidence_id],
    methodology_version="series-1.0.0",
)
_APPROACH_REPORT = ApproachReport(
    policy_id="threshold-approach",
    policy_version="1.0.0",
    signals=[ApproachSignal(
        rule_id="rule-1",
        concept="RATIO_NET_DEBT_EBITDA",
        status="approaching",
        distance=0.1,
        previous_distance=0.2,
        direction="toward",
        approaching=True,
        policy_id="threshold-approach",
        policy_version="1.0.0",
        narrative="Расстояние до порога уменьшилось и находится в границе наблюдения.",
    )],
)

SAMPLES.update({
    CorrectionIntent: dict(original_wording="Исправь выручку на 100", concept="SALES_REVENUE", value=100.0),
    CaseIdentity: dict(case_id="case-1", owner_id="owner-1", tenant_id="tenant-1"),
    CaseTarget: dict(output_type="ScalarFact", concept_id="SALES_REVENUE", period_ref="2025"),
    CaseDocumentRef: dict(logical_name="report.pdf", document_id="doc-1", document_version="sha256:abc"),
    OverrideChoicePoint: dict(
        concept="SALES_REVENUE", candidate_evidence_ids=["ev-1", "ev-2"], reason="ambiguous correction",
    ),
    OverrideAuditEvent: dict(
        action="apply", owner_id="owner-1", case_id="case-1", root_evidence_id="ev-root",
        replaced_evidence_id="ev-1", override_evidence_id="ev-2", original_wording="Исправь на 100",
        idempotency_key="override-1", case_revision=2,
    ),
    PlanRunRecord: dict(
        plan_run_id="plan-run-1", case_id="case-1", owner_id="owner-1",
        goal_ir=GoalIR(raw_query="выручка", intent_kind="extract_registered_metric"),
        target=CaseTarget(output_type="ScalarFact", concept_id="SALES_REVENUE"),
        graph=PlanGraph(
            plan_id="plan-1", root=NodeKey(**_NODE_KEY),
            nodes=[PlanNode(node_key=NodeKey(**_NODE_KEY), sandbox_profile="compute_no_network")],
            graph_hash="sha256:deadbeef",
        ),
        graph_hash="sha256:deadbeef", registry_fingerprint="registry:abc",
        policy_version="policy-1", case_revision=1,
    ),
    CaseSnapshot: dict(identity=CaseIdentity(case_id="case-1", owner_id="owner-1")),
    SubjectKey: dict(
        value=_SUBJECT_KEY.value, entity_id=_SUBJECT_KEY.entity_id,
        identifiers=_SUBJECT_KEY.identifiers,
    ),
    SubjectAccess: dict(owner_id="owner-1", tenant_id="tenant-1", scope_id="portfolio-1"),
    SubjectProfile: dict(
        snapshot_id="snap-1", subject_key=_SUBJECT_KEY,
        access=SubjectAccess(owner_id="owner-1"), version=1,
        plan_run_id="plan-run-1", graph_hash="sha256:deadbeef", case_id="case-1",
        registry_version="registry-1", methodology_version="method-1", period_refs=["2025"],
        document_versions=["sha256:abc"],
        metrics=[ExtractedMetric(
            scalar_fact=_PROFILE_METRIC, concept="SALES_REVENUE", period_ref="2025",
        )],
        evidence_ids=[_PROFILE_METRIC.evidence_id],
    ),
    SeriesPoint: dict(
        period_ref="2025", value=100.0, unit="RUB_MLN", basis="ifrs:consolidated",
        evidence_id=_PROFILE_METRIC.evidence_id,
    ),
    SeriesChange: dict(
        from_period_ref="2024", to_period_ref="2025", absolute_change=10.0,
        relative_change=10.0 / 90.0, rate=10.0 / 90.0,
        input_evidence_ids=["ev-old", "ev-new"],
    ),
    SeriesGap: dict(after_period_ref="2023", before_period_ref="2025", missing_period_refs=["2024"]),
    SeriesFact: _SERIES_FACT.model_dump(),
    DecompositionComponent: dict(
        component_id="price", label="Price effect", value=6.0,
        derived_fact=DerivedFact(
            produced_by_atom="calc.decompose", produced_by_atom_version="1.0.0",
            source_ref="method:additive_v1", concept="SALES_CHANGE", formula_id="price",
            formula_version="additive_v1", inputs=["ev-price"], value=6.0,
        ),
    ),
    DecompositionResult: dict(
        concept="SALES_CHANGE", methodology_id="additive_v1", methodology_version="1.0.0",
        target_change=10.0, target_input_evidence_ids=["ev-target"],
        components=[DecompositionComponent(**{
            "component_id": "price", "label": "Price effect", "value": 6.0,
            "derived_fact": DerivedFact(
                produced_by_atom="calc.decompose", produced_by_atom_version="1.0.0",
                source_ref="method:additive_v1", concept="SALES_CHANGE", formula_id="price",
                formula_version="additive_v1", inputs=["ev-price"], value=6.0,
            ),
        })],
        residual=4.0, tolerance=0.000001, complete=True,
    ),
    PeerMetricValue: dict(
        concept="SALES_REVENUE", value=80.0, unit="RUB_MLN", period_ref="2025",
        basis="ifrs:consolidated", source_ref="peer-report#p10",
    ),
    PeerCandidate: dict(
        stable_peer_id="lei:peer-1", display_name="Peer One", company_type="non_financial",
        business_model="retail", standard="ifrs", currency="RUB",
        values=[PeerMetricValue(
            concept="SALES_REVENUE", value=80.0, unit="RUB_MLN", period_ref="2025",
            basis="ifrs:consolidated", source_ref="peer-report#p10",
        )],
    ),
    PeerPolicy: dict(
        policy_id="peer-retail", version="1.0.0", company_type="non_financial",
        business_model="retail", standard="ifrs", currency="RUB",
        basis="ifrs:consolidated", period_ref="2025", required_concepts=["SALES_REVENUE"],
    ),
    PeerExclusion: dict(stable_peer_id="lei:peer-2", reasons=["missing_source"]),
    PeerReferenceSet: dict(
        scenario_id="scenario-1", subject_key=_SUBJECT_KEY,
        policy_id="peer-retail", policy_version="1.0.0", peers=[], sufficient=False,
    ),
    WatchSchedule: dict(kind="daily", at_time="09:00"),
    WatchSubscription: dict(
        subscription_id="watch-1", subject_key=_SUBJECT_KEY, owner_id="owner-1", case_id="case-1",
        check_type="snapshot_and_threshold", schedule=WatchSchedule(kind="daily", at_time="09:00"),
        allowed_scope=["subject_history"], policy_version="1.0.0", idempotency_key="watch-key-1",
    ),
    FactDelta: _SUBJECT_DIFF.changed_facts[0].model_dump(),
    SubjectDiff: _SUBJECT_DIFF.model_dump(),
    ApproachPolicy: dict(
        policy_id="threshold-approach", version="1.0.0",
        distance_kind="relative_to_threshold", approach_boundary=0.1,
    ),
    ApproachSignal: _APPROACH_REPORT.signals[0].model_dump(),
    ApproachReport: _APPROACH_REPORT.model_dump(),
    MonitorRunTrace: dict(
        monitor_plan_run_id="plan-run-monitor", source_snapshot_ids=["snap-1", "snap-2"],
        source_plan_run_ids=["plan-run-1", "plan-run-2"],
    ),
    MonitorResult: dict(
        subject_key=_SUBJECT_KEY, diff=_SUBJECT_DIFF, series=[_SERIES_FACT],
        approaches=_APPROACH_REPORT,
        trace=MonitorRunTrace(
            monitor_plan_run_id="plan-run-monitor", source_snapshot_ids=["snap-1", "snap-2"],
            source_plan_run_ids=["plan-run-1", "plan-run-2"],
        ),
    ),
    MonitoringCapacity: dict(
        subject_count=1, snapshot_count=2, serialized_bytes=4096,
        active_subscriptions=1, estimated_runs_per_day=1.0,
    ),
})

# Slice 12: supporting schema/event contracts are public in exactly the same
# sense as the original A0 entities, so they participate in the package-wide
# JSON round-trip and immutability checks instead of living in a separate,
# weaker test universe.
_NODE_RUN_V1 = NodeRun(
    node_run_id="node-run-v1",
    node_key=NodeKey(atom_name="out.form", atom_version="1.0.0", args_fingerprint="abc"),
    run_id="run-1",
    status="succeeded",
    sandbox_profile="compute_no_network",
)
_NARRATIVE_V1 = NarrativeOutput(
    output_id="narrative-v1", run_id="run-1", text="Подтверждённый вывод",
)
_CHANGE_ROW_V1 = PeriodChangeRow(
    row_id="changes:SALES_REVENUE", order=0, concept="SALES_REVENUE", label="Выручка",
    status="computed", unit="млн. руб.", current_period_ref="FY 2025", prior_period_ref="FY 2024",
    current_value=1500.0, prior_value=1200.0, absolute_change=300.0, relative_change_pct=25.0,
    evidence_refs=[EvidenceRef(evidence_id="ev-cur"), EvidenceRef(evidence_id="ev-prior")],
)
SAMPLES.update({
    PeriodChangeRow: dict(
        row_id="changes:NET_INCOME", order=0, concept="NET_INCOME", label="Чистая прибыль",
        status="no_prior_period", reason="предыдущий период для сравнения не найден в отчёте",
    ),
    PeriodChangeReport: dict(
        run_id="run-1", role="other", rows=[_CHANGE_ROW_V1], explanation=_NARRATIVE_V1,
        explanation_status="ready",
    ),
    EvidenceRef: dict(evidence_id="ev-1", concept="SALES_REVENUE", period_ref="2025"),
    OutputGap: dict(code="missing_value", message="Нет значения", concept="RWA"),
    OutputWarning: dict(code="limited_scope", message="Неполный охват"),
    OutputRunMetadata: dict(run_id="run-1", evidence_version="1.0"),
    PublicationChecks: dict(grounding="pass", citation="not_applicable"),
    ReportThesis: dict(text="Выручка подтверждена", evidence_refs=[]),
    ReportSection: dict(section_id="summary", title="Итог", theses=[]),
    ResultReference: dict(
        result_type="NarrativeOutput", result_id="narrative-v1", schema_version="1.0",
        run_id="run-1", evidence_version="1.0", result_hash="a" * 64,
    ),
    TypedError: dict(code="failed_node", message="Узел завершился с ошибкой"),
    ResultEvent: dict(run_id="run-1", sequence=1, result=_NARRATIVE_V1.model_dump(mode="json")),
    NodeRunEvent: dict(
        run_id="run-1", sequence=2, plan_graph_hash="sha256:graph",
        node_run=_NODE_RUN_V1.model_dump(mode="json"),
    ),
    ErrorEvent: dict(
        run_id="run-1", sequence=3,
        error={"code": "failed_node", "message": "Узел завершился с ошибкой"},
    ),
    CompletedEvent: dict(run_id="run-1", sequence=4, status="succeeded"),
    HeartbeatEvent: dict(run_id="run-1", sequence=4),
    LegacyCompatibilityEvent: dict(
        run_id="run-1", sequence=5, legacy_event={"type": "text_delta", "text": "legacy"},
    ),
})


SAMPLES.update({
    DynamicArtifact: dict(
        output_type="dyn:ebitda_vs_brent", payload={"correlation": 0.82},
        origin=["generated"], producer="eph.ebitda_vs_brent@abc123",
    ),
    EphemeralAtomSource: dict(
        evidence_id="abc123", name="ebitda_vs_brent", request={"name": "ebitda_vs_brent"},
    ),
})


def test_every_a0_entity_has_a_sample():
    """Список выборок выше покрывает ровно то, что экспортирует пакет контрактов."""
    from finengine import contracts as C

    exported = {getattr(C, name) for name in C.__all__ if isinstance(getattr(C, name), type)}
    missing = exported - set(SAMPLES) - {FormRow}
    assert not missing, f"нет минимальной выборки для: {sorted(t.__name__ for t in missing)}"


@pytest.mark.parametrize("model_cls", list(SAMPLES), ids=lambda c: c.__name__)
def test_round_trips_through_json_without_loss(model_cls):
    instance = model_cls(**SAMPLES[model_cls])
    restored = model_cls.model_validate_json(instance.model_dump_json())
    assert restored == instance


@pytest.mark.parametrize("model_cls", list(SAMPLES), ids=lambda c: c.__name__)
def test_cannot_be_mutated_after_construction(model_cls):
    instance = model_cls(**SAMPLES[model_cls])
    field = next(iter(model_cls.model_fields))
    with pytest.raises(ValidationError, match="frozen"):
        setattr(instance, field, getattr(instance, field))
