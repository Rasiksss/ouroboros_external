import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { TypedResultView } from "@/components/typed-result-view";
import type {
  CalculatedMetric, ExtractedMetric, FormOutput, MonitorResult, PeriodChangeReport, PeriodChangeRow,
} from "@/lib/api";

const result: FormOutput = {
  schema_version: "1.0",
  output_type: "FormOutput",
  output_id: "form-1",
  run_id: "run-1",
  status: "partial",
  role: "dgr",
  layout_id: "dgr.bank",
  layout_version: "1",
  preserve_empty: true,
  run_metadata: {
    run_id: "run-1",
    evidence_version: "evidence-v3",
    registry_version: "registry-v2",
    produced_at: "2026-09-21T00:00:00Z",
  },
  gaps: [],
  rows: [
    {
      row_id: "dgr.bank:000",
      order: 0,
      kind: "dgr_row",
      enum: "REVENUE",
      label: "Выручка",
      value: 125,
      display_value: "125,0 млн ₽",
      status: "supported",
      evidence_refs: [{ evidence_id: "ev-1", source_ref: "page:7", document_version: "doc-v2" }],
    },
    {
      row_id: "dgr.bank:001",
      order: 1,
      kind: "dgr_row",
      enum: "RWA",
      label: "RWA",
      status: "missing",
      evidence_refs: [],
      missing_reason: "evidence_not_found",
    },
    {
      row_id: "dgr.bank:002",
      order: 2,
      kind: "dgr_row",
      enum: "CAPITAL",
      label: "Капитал",
      status: "conflicting",
      evidence_refs: [{ evidence_id: "ev-2" }, { evidence_id: "ev-3" }],
      missing_reason: "conflicting_evidence",
    },
  ],
};

describe("TypedResultView", () => {
  it("snapshots explicit missing and conflicting evidence states", () => {
    const html = renderToStaticMarkup(createElement(TypedResultView, { result }));

    expect(html).toContain("125,0 млн ₽");
    expect(html).toContain('data-row-status="missing"');
    expect(html).toContain("evidence_not_found");
    expect(html).toContain('data-row-status="conflicting"');
    expect(html).toContain("ev-2, ev-3");
    expect(html).toContain("ev-1 · page:7 · doc doc-v2");
    expect(html).toContain("evidence evidence-v3 · registry registry-v2");
    expect(html).toMatchSnapshot();
  });

  it("renders an extracted metric with its evidence and no client calculation", () => {
    const metric: ExtractedMetric = {
      schema_version: "1.0", output_type: "ExtractedMetric", status: "supported",
      metric_id: "metric-1", concept: "SALES_REVENUE", period_ref: "2025",
      definition_origin: "registry",
      scalar_fact: {
        evidence_id: "ev-revenue", source_ref: "page:12", document_version: "doc-v2",
        concept: "SALES_REVENUE", period_ref: "2025", basis: "IFRS",
        value: 125, unit: "RUB_MLN",
      },
    };
    const html = renderToStaticMarkup(createElement(TypedResultView, { result: metric }));
    expect(html).toContain("SALES_REVENUE");
    expect(html).toContain("125 RUB_MLN");
    expect(html).toContain("ev-revenue · page:12 · doc doc-v2 · 2025");
    expect(html).toMatchSnapshot();
  });

  it("formats an extracted metric without a binary floating-point tail", () => {
    const metric: ExtractedMetric = {
      schema_version: "1.0", output_type: "ExtractedMetric", status: "supported",
      metric_id: "metric-tail", concept: "SALES_REVENUE", period_ref: "2025",
      definition_origin: "registry",
      scalar_fact: {
        evidence_id: "ev-tail", source_ref: "page:12", concept: "SALES_REVENUE",
        period_ref: "2025", value: 42085.931000000004, unit: "RUB_MLN",
      },
    };

    const html = renderToStaticMarkup(createElement(TypedResultView, { result: metric }));

    expect(html).toContain("42 085,931 RUB_MLN");
    expect(html).not.toContain("42085.931000000004");
  });

  it("renders a calculated metric formula and exact evidence dependencies", () => {
    const metric: CalculatedMetric = {
      schema_version: "1.0", output_type: "CalculatedMetric", status: "supported",
      metric_id: "metric-2", concept: "NET_DEBT", period_ref: "2025",
      definition_origin: "ad_hoc",
      derived_fact: {
        evidence_id: "ev-net-debt", source_ref: "calc:1", concept: "NET_DEBT",
        period_ref: "2025", basis: "IFRS", value: 80, unit: "RUB_MLN",
        formula_id: "adhoc-1", formula_version: "1", expression: "DEBT - CASH",
        inputs: ["ev-debt", "ev-cash"],
      },
    };
    const html = renderToStaticMarkup(createElement(TypedResultView, { result: metric }));
    expect(html).toContain("DEBT - CASH");
    expect(html).toContain("ev-debt, ev-cash");
    expect(html).toMatchSnapshot();
  });

  it("renders monitor snapshots, changes, signals and source PlanRuns", () => {
    const monitor: MonitorResult = {
      schema_version: "1.0", output_type: "MonitorResult", status: "complete",
      result_id: "monitor-1",
      subject_key: { value: "issuer-1", entity_id: "entity-1", identifiers: { inn: "123" } },
      diff: {
        diff_id: "diff-1", previous_snapshot_id: "snap-1", current_snapshot_id: "snap-2",
        previous_plan_run_id: "run-1", current_plan_run_id: "run-2",
        comparability: "comparable", comparability_issues: [], new_facts: [], disappeared_facts: [],
        changed_structures: [], changed_thresholds: [],
        changed_facts: [{
          identity: "REVENUE|2025", old_evidence_id: "ev-1", new_evidence_id: "ev-2",
          old_value: 100, new_value: 110, absolute_delta: 10, relative_delta: 0.1, detail: "period change",
        }],
      },
      series: [],
      approaches: {
        report_id: "approach-1", policy_id: "default", policy_version: "1",
        signals: [{
          signal_id: "signal-1", rule_id: "rule-1", concept: "LEVERAGE", status: "approaching",
          direction: "toward", crossed: false, approaching: true, narrative: "Порог приблизился",
        }],
      },
      trace: {
        monitor_plan_run_id: "run-monitor", source_snapshot_ids: ["snap-1", "snap-2"],
        source_plan_run_ids: ["run-1", "run-2"],
      },
    };
    const html = renderToStaticMarkup(createElement(TypedResultView, { result: monitor }));
    expect(html).toContain("snap-1 → snap-2");
    expect(html).toContain("100 → 110");
    expect(html).toContain("Порог приблизился");
    expect(html).toContain("run-1, run-2");
    expect(html).toMatchSnapshot();
  });

  it("formats a calculated metric without a binary floating-point tail", () => {
    const metric: CalculatedMetric = {
      schema_version: "1.0", output_type: "CalculatedMetric", status: "supported",
      metric_id: "metric-calc-tail", concept: "NET_DEBT", period_ref: "2025",
      definition_origin: "ad_hoc",
      derived_fact: {
        evidence_id: "ev-calc-tail", source_ref: "calc:2", concept: "NET_DEBT",
        period_ref: "2025", basis: "IFRS", value: 42085.931000000004, unit: "RUB_MLN",
        formula_id: "adhoc-2", formula_version: "1", expression: "DEBT - CASH",
        inputs: ["ev-debt", "ev-cash"],
      },
    };

    const html = renderToStaticMarkup(createElement(TypedResultView, { result: metric }));

    expect(html).toContain("42 085,931 RUB_MLN");
    expect(html).not.toContain("42085.931000000004");
  });

  it("formats monitor change values without a binary floating-point tail", () => {
    const monitor: MonitorResult = {
      schema_version: "1.0", output_type: "MonitorResult", status: "complete",
      result_id: "monitor-tail",
      subject_key: { value: "issuer-1", entity_id: "entity-1", identifiers: {} },
      diff: {
        diff_id: "diff-tail", previous_snapshot_id: "snap-1", current_snapshot_id: "snap-2",
        previous_plan_run_id: "run-1", current_plan_run_id: "run-2",
        comparability: "comparable", comparability_issues: [], new_facts: [], disappeared_facts: [],
        changed_structures: [], changed_thresholds: [],
        changed_facts: [{
          identity: "REVENUE|2025", old_evidence_id: "ev-1", new_evidence_id: "ev-2",
          old_value: 42085.931000000004, new_value: 1453957.053, absolute_delta: 1, relative_delta: 0.1, detail: "period change",
        }],
      },
      series: [],
      approaches: { report_id: "approach-1", policy_id: "default", policy_version: "1", signals: [] },
      trace: { monitor_plan_run_id: "run-monitor", source_snapshot_ids: [], source_plan_run_ids: [] },
    };

    const html = renderToStaticMarkup(createElement(TypedResultView, { result: monitor }));

    expect(html).toContain("42 085,931 → 1 453 957,053");
    expect(html).not.toContain("42085.931000000004");
  });

  describe("PeriodChangeReport", () => {
    const refs = [{ evidence_id: "ev-cur", source_ref: "page:4" }, { evidence_id: "ev-prior" }];
    const computed = (over: Partial<PeriodChangeRow> = {}): PeriodChangeRow => ({
      row_id: "changes:SALES_REVENUE", order: 0, concept: "SALES_REVENUE", label: "Выручка",
      status: "computed", unit: "RUB_MLN", current_period_ref: "6M 2026", prior_period_ref: "6M 2025",
      current_value: 1500, prior_value: 1200, absolute_change: 300, relative_change_pct: 25,
      evidence_refs: refs, ...over,
    });
    const report = (over: Partial<PeriodChangeReport> = {}): PeriodChangeReport => ({
      schema_version: "1.0", output_type: "PeriodChangeReport", output_id: "changes-1", run_id: "run-1",
      status: "complete", role: "other", gaps: [], warnings: [],
      explanation_status: "ready",
      explanation: {
        schema_version: "1.0", output_type: "NarrativeOutput", output_id: "narrative-1", run_id: "run-1",
        status: "complete", text: "Выручка выросла за счёт объёмов.", cited_evidence_ids: ["ev-cur"],
      },
      rows: [
        computed(),
        computed({
          row_id: "changes:NET_INCOME", order: 1, concept: "NET_INCOME", label: "Чистая прибыль",
          current_value: 90, prior_value: 120, absolute_change: -30, relative_change_pct: -25,
        }),
        computed({
          row_id: "changes:EBITDA", order: 2, concept: "EBITDA", label: "EBITDA", status: "zero_base",
          prior_value: 0, current_value: 10, absolute_change: 10, relative_change_pct: null,
          reason: "предыдущее значение равно нулю — процент не определён",
        }),
        {
          row_id: "changes:COSTS", order: 3, concept: "COSTS", label: "Расходы", status: "no_prior_period",
          reason: "предыдущий период для сравнения не найден в отчёте", evidence_refs: [],
        },
      ],
      ...over,
    });

    it("shows the backend's changes, explicit statuses and the explanation", () => {
      const html = renderToStaticMarkup(createElement(TypedResultView, { result: report() }));

      expect(html).toContain('data-result-type="PeriodChangeReport"');
      expect(html).toContain("+25,0 %");
      expect(html).toContain("-25,0 %");
      expect(html).toContain('data-row-status="zero_base"');
      expect(html).toContain("предыдущее значение равно нулю — процент не определён");
      expect(html).toContain('data-row-status="no_prior_period"');
      expect(html).toContain("предыдущий период для сравнения не найден в отчёте");
      expect(html).toContain("6M 2025");
      expect(html).toContain("ev-cur · page:4, ev-prior");
      expect(html).toContain('data-result-type="NarrativeOutput"');
      expect(html).toContain("Выручка выросла за счёт объёмов.");
      expect(html).toMatchSnapshot();
    });

    it("keeps the table and names the reason when the explanation is unavailable", () => {
      const html = renderToStaticMarkup(createElement(TypedResultView, {
        result: report({
          status: "partial", explanation: null, explanation_status: "unavailable",
          explanation_note: "объяснение заблокировано проверкой опоры на доказательства",
        }),
      }));

      expect(html).toContain("+25,0 %");
      expect(html).toContain('data-explanation-status="unavailable"');
      expect(html).toContain("Объяснение недоступно: объяснение заблокировано проверкой опоры на доказательства");
      expect(html).not.toContain('data-result-type="NarrativeOutput"');
      expect(html).toMatchSnapshot();
    });

    it("prints the percentage supplied by the backend and never derives one on the client", () => {
      // Значения намеренно противоречат проценту: клиент обязан показать присланное 12,5 %,
      // а не пересчитать (300 − 100) / 100 = 200 %.
      const html = renderToStaticMarkup(createElement(TypedResultView, {
        result: report({ rows: [computed({ current_value: 300, prior_value: 100, absolute_change: 200, relative_change_pct: 12.5 })] }),
      }));

      expect(html).toContain("+12,5 %");
      expect(html).not.toContain("200,0 %");
    });

    it("formats values without floating-point tails and with digit grouping", () => {
      const html = renderToStaticMarkup(createElement(TypedResultView, {
        result: report({
          rows: [computed({
            prior_value: 20228.349000000002, current_value: 1384144.385, absolute_change: 1363916.036,
            relative_change_pct: 6742.5,
          })],
        }),
      }));

      expect(html).toContain("20 228,349");
      expect(html).not.toContain("20228.349000000002");
      expect(html).toContain("1 384 144,385");
    });

    it("lists the role concepts that are absent from the report as one gap", () => {
      const html = renderToStaticMarkup(createElement(TypedResultView, {
        result: report({ gaps: [{ code: "concepts_not_found", message: "В отчёте не найдено показателей роли: 60 из 78" }] }),
      }));

      expect(html).toContain("Нет данных: В отчёте не найдено показателей роли: 60 из 78");
    });
  });
});
