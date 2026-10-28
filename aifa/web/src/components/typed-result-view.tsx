import type { EvidenceRef, FormOutputRow, OutputRunMetadata, TypedResult, TypedRunError } from "@/lib/api";
import { formatDisplayNumber } from "@/lib/display-number";

const STATUS_LABEL: Record<FormOutputRow["status"], string> = {
  supported: "Подтверждено",
  missing: "Нет данных",
  conflicting: "Конфликт источников",
  not_applicable: "Не применяется",
  error: "Ошибка",
};

/** Форматирование числа бэкенда без арифметики: один знак после запятой и явный «+». */
function formatPct(value: number): string {
  return `${value > 0 ? "+" : ""}${formatDisplayNumber(value, 1, 1)} %`;
}

/** Значение для показа: без хвостов плавающей запятой (20228.349000000002) и с разделителями разрядов. */
function formatValue(value: number | null | undefined): string {
  return value == null ? "—" : formatDisplayNumber(value);
}

function Evidence({ refs }: { refs: EvidenceRef[] }) {
  if (!refs.length) return null;
  const labels = refs.map((ref) => [
    ref.evidence_id,
    ref.source_ref,
    ref.document_version ? `doc ${ref.document_version}` : null,
    ref.period_ref,
  ].filter(Boolean).join(" · "));
  return <small className="block text-[11px] text-[var(--color-neutral-500)]">Источники: {labels.join(", ")}</small>;
}

function RunMetadata({ metadata }: { metadata?: OutputRunMetadata | null }) {
  if (!metadata) return null;
  const versions = [
    `evidence ${metadata.evidence_version}`,
    metadata.registry_version ? `registry ${metadata.registry_version}` : null,
    metadata.methodology_version ? `methodology ${metadata.methodology_version}` : null,
  ].filter(Boolean).join(" · ");
  return <small className="block text-[11px] text-[var(--color-neutral-500)]">Run {metadata.run_id} · {versions}</small>;
}

/** Pure typed-result renderer: no formulas, totals, signs, ratios or periods. */
export function TypedResultView({ result }: { result: TypedResult }) {
  if (result.output_type === "ExtractedMetric") {
    const fact = result.scalar_fact;
    return (
      <section className="mt-2 text-[12.5px]" data-result-type="ExtractedMetric" data-schema-version={result.schema_version}>
        <h3 className="font-medium">{result.concept}</h3>
        <p className="tabular-nums">{formatValue(fact.value)} {fact.unit ?? ""}</p>
        <p className="text-[11px] text-[var(--color-neutral-500)]">
          {result.period_ref} · {fact.basis ?? "базис не указан"} · {result.definition_origin} · {result.status}
        </p>
        <Evidence refs={[{
          evidence_id: fact.evidence_id,
          source_ref: fact.source_ref,
          document_version: fact.document_version,
          period_ref: fact.period_ref,
        }]} />
      </section>
    );
  }

  if (result.output_type === "CalculatedMetric") {
    const fact = result.derived_fact;
    return (
      <section className="mt-2 text-[12.5px]" data-result-type="CalculatedMetric" data-schema-version={result.schema_version}>
        <h3 className="font-medium">{result.concept}</h3>
        <p className="tabular-nums">{formatValue(fact.value)} {fact.unit ?? ""}</p>
        <p className="font-mono text-[11px]">{fact.expression}</p>
        <p className="text-[11px] text-[var(--color-neutral-500)]">
          {result.period_ref} · {fact.basis ?? "базис не указан"} · {result.definition_origin} · {result.status}
        </p>
        <Evidence refs={[
          { evidence_id: fact.evidence_id, source_ref: fact.source_ref, document_version: fact.document_version },
          ...fact.inputs.map((evidence_id) => ({ evidence_id })),
        ]} />
      </section>
    );
  }

  if (result.output_type === "MonitorResult") {
    const deltas = [
      ...result.diff.new_facts.map((delta) => ({ ...delta, kind: "Новый факт" })),
      ...result.diff.changed_facts.map((delta) => ({ ...delta, kind: "Изменение" })),
      ...result.diff.disappeared_facts.map((delta) => ({ ...delta, kind: "Исчез" })),
    ];
    return (
      <section className="mt-2 space-y-2 text-[12.5px]" data-result-type="MonitorResult" data-schema-version={result.schema_version}>
        <h3 className="font-medium">Изменения: {result.subject_key.value}</h3>
        <p className="text-[11px] text-[var(--color-neutral-500)]">
          {result.diff.previous_snapshot_id} → {result.diff.current_snapshot_id} · {result.diff.comparability}
        </p>
        {result.diff.comparability_issues.map((issue) => <p key={issue}>Несопоставимость: {issue}</p>)}
        {deltas.map((delta) => (
          <p key={`${delta.kind}:${delta.identity}:${delta.new_evidence_id ?? delta.old_evidence_id ?? ""}`}>
            {delta.kind}: {delta.identity} · {formatValue(delta.old_value)} → {formatValue(delta.new_value)}
            {delta.detail ? ` · ${delta.detail}` : ""}
          </p>
        ))}
        {result.approaches.signals.map((signal) => (
          <p key={signal.signal_id}>{signal.concept}: {signal.narrative}</p>
        ))}
        <p className="text-[11px] text-[var(--color-neutral-500)]">
          Source PlanRun: {result.trace.source_plan_run_ids.join(", ")} · monitor {result.trace.monitor_plan_run_id}
        </p>
      </section>
    );
  }

  if (result.output_type === "PeriodChangeReport") {
    return (
      <section className="mt-2 space-y-3" data-result-type="PeriodChangeReport" data-schema-version={result.schema_version}>
        <div className="overflow-x-auto">
          <p className="mb-2 text-[12px] text-[var(--color-neutral-500)]">
            Изменения между периодами отчёта · {result.rows.length} показателей
            <RunMetadata metadata={result.run_metadata} />
          </p>
          <table className="w-full border-collapse text-left text-[12.5px]">
            <thead>
              <tr className="border-b border-[var(--color-neutral-200)] text-[11px] text-[var(--color-neutral-500)]">
                <th className="py-1 pr-3 font-normal">Показатель</th>
                <th className="py-1 pr-3 font-normal">Было</th>
                <th className="py-1 pr-3 font-normal">Стало</th>
                <th className="py-1 pr-3 font-normal">Δ</th>
                <th className="py-1 pr-3 font-normal">Δ %</th>
                <th className="py-1 font-normal">Примечание</th>
              </tr>
            </thead>
            <tbody>
              {result.rows.map((row) => (
                <tr key={row.row_id} data-row-status={row.status} className="border-b border-[var(--color-neutral-200)] align-top">
                  <td className="py-1.5 pr-3">{row.label}</td>
                  <td className="py-1.5 pr-3 tabular-nums">
                    {formatValue(row.prior_value)}
                    {row.prior_period_ref ? <small className="block text-[11px] text-[var(--color-neutral-500)]">{row.prior_period_ref}</small> : null}
                  </td>
                  <td className="py-1.5 pr-3 tabular-nums">
                    {formatValue(row.current_value)}
                    {row.current_period_ref ? <small className="block text-[11px] text-[var(--color-neutral-500)]">{row.current_period_ref}</small> : null}
                  </td>
                  <td className="py-1.5 pr-3 tabular-nums">{formatValue(row.absolute_change)}</td>
                  <td className="py-1.5 pr-3 tabular-nums">
                    {row.relative_change_pct == null ? "—" : formatPct(row.relative_change_pct)}
                  </td>
                  <td className="py-1.5 text-[11px] text-[var(--color-neutral-500)]">
                    {row.reason}
                    <Evidence refs={row.evidence_refs} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {result.gaps.map((gap) => <p key={`${gap.code}:${gap.concept ?? ""}`} className="text-[12px]">Нет данных: {gap.message}</p>)}
        {result.explanation_status === "ready" && result.explanation
          ? <TypedResultView result={result.explanation} />
          : (
            <p className="text-[12px]" data-explanation-status="unavailable">
              Объяснение недоступно{result.explanation_note ? `: ${result.explanation_note}` : ""}
            </p>
          )}
      </section>
    );
  }

  if (result.output_type === "FormOutput") {
    return (
      <section className="mt-2 overflow-x-auto" data-result-type="FormOutput" data-schema-version={result.schema_version}>
        <p className="mb-2 text-[12px] text-[var(--color-neutral-500)]">
          Макет {result.layout_id} · версия {result.layout_version} · {result.rows.length} строк
          <RunMetadata metadata={result.run_metadata} />
        </p>
        <table className="w-full border-collapse text-left text-[12.5px]">
          <tbody>
            {result.rows.map((row) => (
              <tr key={row.row_id} data-row-status={row.status} className="border-b border-[var(--color-neutral-200)] align-top">
                <td className="py-1.5 pr-3">{row.label}</td>
                <td className="py-1.5 pr-3 tabular-nums">{row.display_value ?? "—"}</td>
                <td className="py-1.5 text-[11px] text-[var(--color-neutral-500)]">
                  {STATUS_LABEL[row.status]}
                  {row.missing_reason ? ` · ${row.missing_reason}` : ""}
                  <Evidence refs={row.evidence_refs} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    );
  }

  if (result.output_type === "ReportOutput") {
    return (
      <article className="mt-2 space-y-4" data-result-type="ReportOutput" data-schema-version={result.schema_version}>
        <RunMetadata metadata={result.run_metadata} />
        {result.sections.map((section) => (
          <section key={section.section_id} data-section-status={section.status}>
            <h3 className="font-medium">{section.title}</h3>
            {section.theses.map((thesis) => (
              <div key={thesis.thesis_id} data-thesis-status={thesis.status} className="mt-1">
                <p>{thesis.text}</p>
                <Evidence refs={thesis.evidence_refs} />
              </div>
            ))}
          </section>
        ))}
        {result.gaps.map((gap) => <p key={`${gap.code}:${gap.concept ?? ""}`} className="text-[12px]">Нет данных: {gap.message}</p>)}
        {result.warnings.map((warning) => <p key={warning.code} className="text-[12px]">Предупреждение: {warning.message}</p>)}
      </article>
    );
  }

  if (result.output_type === "NarrativeOutput") {
    return (
      <article className="mt-2 whitespace-pre-wrap" data-result-type="NarrativeOutput" data-schema-version={result.schema_version}>
        {result.text}
        <RunMetadata metadata={result.run_metadata} />
        <Evidence refs={result.cited_evidence_ids.map((evidence_id) => ({ evidence_id }))} />
      </article>
    );
  }

  if (result.output_type === "DigestOutput") {
    return (
      <article className="mt-2 whitespace-pre-wrap" data-result-type="DigestOutput" data-schema-version={result.schema_version}>
        {result.markdown}
        <small className="block text-[11px] text-[var(--color-neutral-500)]">
          Дайджест {result.output_id} · статус {result.status} · окно {result.window_start} — {result.window_end}
        </small>
        {result.warnings.map((warning) => <p key={warning} className="text-[12px]">Предупреждение: {warning}</p>)}
      </article>
    );
  }

  return (
    <section className="mt-2 text-[12.5px]" data-result-type="FileArtifact" data-schema-version={result.schema_version}>
      <p>{result.filename}</p>
      <p className="text-[11px] text-[var(--color-neutral-500)]">
        Шаблон {result.template_id}@{result.template_version} · SHA-256 {result.content_hash}
      </p>
      <p className="text-[11px] text-[var(--color-neutral-500)]">
        Источник: {result.source_result.result_type} {result.source_result.result_id}
      </p>
    </section>
  );
}

export function TypedErrors({ errors }: { errors: TypedRunError[] }) {
  if (!errors.length) return null;
  return (
    <div className="mt-2 space-y-1" role="alert">
      {errors.map((error, index) => (
        <p key={`${error.code}:${index}`} className="text-[12px] text-[var(--color-accent-700)]">
          {error.code}: {error.message}
        </p>
      ))}
    </div>
  );
}
