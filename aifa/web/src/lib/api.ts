import type { AuthorAttemptEvent, DynamicMoveEvent, DynamicPlanEvent } from "@/lib/dynamic-run-trace";
import type { RootPlanEvent } from "@/lib/root-plan";

export type DocumentSummary = {
  doc_id: string;
  filename: string;
  doc_type: string;
  company_name: string;
  period: string;
  language: string;
  pages: number;
  /** Which OCR backend produced the .txt sidecar: "pymupdf" (default) or
   *  "idp" (cloud OCR). Empty for legacy uploads. */
  ocr_engine?: string;
};

export type UploadResponse = {
  doc_id: string;
  filename: string;
  chunks: number;
  pages: number;
  doc_type: string;
  language: string;
  /** True when the .txt sidecar isn't yet ready — UI should poll
   * /documents/{id}/status until processing=false. */
  processing?: boolean;
};

export type DocumentStatus = {
  doc_id: string;
  processing: boolean;
  company_name: string;
};

export type TurnUsage = {
  ts: number;
  model: string;
  turns: number;
  tokens_in: number;
  tokens_out: number;
  tokens_cached: number;
  cost_usd: number;
};

export type ProjectUsage = {
  tokens_in: number;
  tokens_out: number;
  tokens_cached: number;
  cost_usd: number;
  turns_total: number;
  user_turns: number;
};

// A11 final-result boundary. The UI renders these fields verbatim; it never
// reconstructs a row/report from prose and never derives financial values.
export type EvidenceRef = {
  evidence_id: string;
  concept?: string | null;
  period_ref?: string | null;
  source_ref?: string | null;
  document_version?: string | null;
};

export type OutputRunMetadata = {
  run_id: string;
  goal_ir_id?: string | null;
  entity_id?: string | null;
  registry_version?: string | null;
  methodology_version?: string | null;
  model_version?: string | null;
  prompt_version?: string | null;
  sandbox_image_version?: string | null;
  plan_graph_hash?: string | null;
  evidence_version: string;
  produced_at: string;
};

export type FormOutputRow = {
  row_id: string;
  order: number;
  kind: string;
  enum?: string | null;
  label: string;
  value?: number | string | null;
  display_value?: string | null;
  status: "supported" | "missing" | "conflicting" | "not_applicable" | "error";
  evidence_refs: EvidenceRef[];
  missing_reason?: string | null;
};

type TypedResultBase = {
  schema_version: "1.0";
  output_id: string;
  run_id: string;
  status: "complete" | "partial" | "blocked";
  run_metadata?: OutputRunMetadata | null;
};

export type FormOutput = TypedResultBase & {
  output_type: "FormOutput";
  role: string;
  layout_id: string;
  layout_version: string;
  preserve_empty: boolean;
  rows: FormOutputRow[];
  gaps: Array<{ code: string; message: string; concept?: string | null }>;
};

export type ReportOutput = TypedResultBase & {
  output_type: "ReportOutput";
  sections: Array<{
    section_id: string;
    title: string;
    status: FormOutputRow["status"];
    theses: Array<{
      thesis_id: string;
      text: string;
      status: FormOutputRow["status"];
      evidence_refs: EvidenceRef[];
    }>;
  }>;
  gaps: Array<{ code: string; message: string; concept?: string | null }>;
  warnings: Array<{ code: string; message: string; severity: string }>;
  publication_checks: { grounding: string; citation: string };
};

export type NarrativeOutput = TypedResultBase & {
  output_type: "NarrativeOutput";
  text: string;
  cited_evidence_ids: string[];
};

/** Сравнение одного показателя между двумя периодами ОДНОГО отчёта (Срез 32).
 * Числа, проценты и статусы посчитаны бэкендом; клиент их только форматирует. */
export type PeriodChangeRow = {
  row_id: string;
  order: number;
  concept: string;
  label: string;
  status: "computed" | "zero_base" | "no_prior_period" | "not_comparable";
  unit?: string | null;
  current_period_ref?: string | null;
  prior_period_ref?: string | null;
  current_value?: number | null;
  prior_value?: number | null;
  absolute_change?: number | null;
  relative_change_pct?: number | null;
  reason?: string | null;
  evidence_refs: EvidenceRef[];
};

export type PeriodChangeReport = TypedResultBase & {
  output_type: "PeriodChangeReport";
  role: string;
  rows: PeriodChangeRow[];
  explanation?: NarrativeOutput | null;
  explanation_status: "ready" | "unavailable";
  explanation_note?: string | null;
  gaps: Array<{ code: string; message: string; concept?: string | null }>;
  warnings: Array<{ code: string; message: string; severity: string }>;
};

export type ScalarFact = {
  evidence_id: string;
  source_ref: string;
  document_version?: string | null;
  concept?: string | null;
  period_ref?: string | null;
  basis?: string | null;
  value: number;
  unit?: string | null;
};

export type DerivedFact = ScalarFact & {
  formula_id: string;
  formula_version: string;
  expression: string;
  inputs: string[];
};

export type ExtractedMetric = {
  schema_version: "1.0";
  output_type: "ExtractedMetric";
  status: "supported";
  metric_id: string;
  scalar_fact: ScalarFact;
  concept: string;
  period_ref: string;
  definition_origin: "registry" | "ad_hoc";
};

export type CalculatedMetric = {
  schema_version: "1.0";
  output_type: "CalculatedMetric";
  status: "supported";
  metric_id: string;
  derived_fact: DerivedFact;
  concept: string;
  period_ref: string;
  definition_origin: "registry" | "ad_hoc";
};

export type MonitorFactDelta = {
  identity: string;
  old_evidence_id?: string | null;
  new_evidence_id?: string | null;
  old_value?: number | null;
  new_value?: number | null;
  absolute_delta?: number | null;
  relative_delta?: number | null;
  detail: string;
};

export type MonitorResult = {
  schema_version: "1.0";
  output_type: "MonitorResult";
  status: "complete";
  result_id: string;
  subject_key: { value: string; entity_id: string; identifiers: Record<string, string> };
  diff: {
    diff_id: string;
    previous_snapshot_id: string;
    current_snapshot_id: string;
    previous_plan_run_id: string;
    current_plan_run_id: string;
    comparability: "comparable" | "partial" | "incompatible";
    comparability_issues: string[];
    new_facts: MonitorFactDelta[];
    changed_facts: MonitorFactDelta[];
    disappeared_facts: MonitorFactDelta[];
    changed_structures: MonitorFactDelta[];
    changed_thresholds: MonitorFactDelta[];
  };
  series: unknown[];
  approaches: {
    report_id: string;
    policy_id: string;
    policy_version: string;
    signals: Array<{
      signal_id: string;
      rule_id: string;
      concept: string;
      status: string;
      direction: string;
      crossed: boolean;
      approaching: boolean;
      narrative: string;
    }>;
  };
  trace: {
    monitor_plan_run_id: string;
    source_snapshot_ids: string[];
    source_plan_run_ids: string[];
  };
};

export type FileArtifact = {
  schema_version: "1.0";
  output_type: "FileArtifact";
  artifact_id: string;
  run_id: string;
  /** Расширение без точки: платформа не привязана к списку форматов (xlsx, pptx, docx, …). */
  file_kind: string;
  filename: string;
  object_store_location: string;
  content_hash: string;
  template_id: string;
  template_version: string;
  /** Краткое описание структуры файла (листы, слайды); у старых артефактов пусто или отсутствует. */
  summary?: string;
  source_result: {
    result_type: "ReportOutput" | "FormOutput" | "NarrativeOutput" | "DynamicArtifact";
    result_id: string;
    schema_version: string;
    run_id: string;
    evidence_version: string;
    result_hash: string;
  };
};

/** Рыночный дайджест (ADR-0098): готовый Markdown и окно новостей; снимки новостей и макро клиент не разбирает. */
export type DigestOutput = {
  schema_version: "1.0";
  output_type: "DigestOutput";
  output_id: string;
  run_id: string;
  window_start: string;
  window_end: string;
  collected_at: string;
  markdown: string;
  status: "complete" | "partial" | "empty";
  warnings: string[];
};

export type TypedResult =
  | FormOutput | ReportOutput | NarrativeOutput | FileArtifact | DigestOutput
  | ExtractedMetric | CalculatedMetric | MonitorResult | PeriodChangeReport;

export type NodeRunContract = {
  node_run_id: string;
  node_key: { atom_name: string; atom_version: string; args_fingerprint: string };
  run_id: string;
  status: "planned" | "ready" | "running" | "succeeded" | "failed" | "blocked" | "skipped";
  attempt: number;
  sandbox_profile?: string;
  produced_evidence_ids?: string[];
  model_version?: string | null;
  prompt_version?: string | null;
  error?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
};

export type NodeKeyContract = { atom_name: string; atom_version: string; args_fingerprint: string };

/** Срез 29, Этап 7 — построенная топология одного PlanRun. Эмитится один раз
 * (`result_events.plan_graph_event`), до первого `node_run`; сами статусы
 * узлов приходят отдельно, через уже существующие `node_run` по тому же
 * `node_key`. */
export type PlanGraphContract = {
  graph_hash: string;
  /** Срез 31: `dynamic-…` — план собран агентом-планировщиком, иначе детерминированный. */
  planner_version?: string;
  root: NodeKeyContract;
  scenarios?: {
    goal_ref: string;
    goal_type: string;
    root: NodeKeyContract;
    depends_on: string[];
  }[];
  nodes: {
    node_key: NodeKeyContract;
    produces: string[];
    requires: NodeKeyContract[];
    sandbox_profile: string;
    status: "pending";
    scenario_refs?: string[];
  }[];
};

export type TypedRunError = {
  code: string;
  message: string;
  retryable: boolean;
  node_key?: string | null;
  details: Record<string, unknown>;
};

export type ChatStreamEvent =
  | { type: "session_id"; session_id: string }
  | { type: "text_delta"; text: string }
  | { type: "thinking"; text: string }
  // `proxied` — вызов пришёл с прокси, мимо текстового потока: порядок
  // относительно text_delta не гарантирован, границей текста НЕ считается.
  | { type: "tool_call"; name: string; args_summary?: string; proxied?: boolean }
  // Кадр-биения потока (run_orchestrator.HEARTBEAT_SEC). НЕ работа агента:
  // держит соединение живым в штатной тишине 3-6 минут внутри одного тула.
  // Потребитель обязан глотать его ДО общей обработки — иначе он попадёт в
  // patch() и каждые 15 секунд будет перезаписывать хранилище целиком.
  | { type: "ping" }
  // `status`/`reason` приходят от тулов с типизированным конвертом (Ш3).
  // У остальных их нет — и это НЕ значит «успех», это значит «тул ещё не
  // переведён»; `ok` у таких по-прежнему приходит из старого поведения.
  | {
      type: "tool_output";
      name: string;
      ok: boolean;
      status?: "ok" | "partial" | "failed";
      reason?: string;
    }
  | { type: "file"; file_id: string; filename: string; kind?: string }
  | { type: "tool_progress"; name: string; text: string }
  // Конец текстовой порции модели: сказала → сходила в инструмент → говорит
  // дальше. Вызовы инструментов гейтвей не транслирует, эта граница —
  // единственный способ отделить рассуждения по дороге от финального ответа.
  | { type: "text_segment_end" }
  | { type: "plan"; name?: string; steps: string[] }
  | {
      type: "plan_step";
      name?: string;
      index: number;
      status: "running" | "done" | "error";
      note?: string;
    }
  | {
      type: "extract_start";
      name?: string;
      doc_id?: string;
      company_name?: string;
      company_type?: string;
      total_extractable?: number;
      period_current?: string;
      period_prev?: string;
      unit?: string;
      /** Точечный заказ («посчитай чистый долг и кэш»): таблица живёт в чате,
       *  рабочая область live-стрим не рисует (решение владельца 31.08.2026). */
      narrow_order?: boolean;
    }
  | { type: "metric_row"; name?: string; metric: Record<string, unknown> }
  | {
      type: "extract_done";
      name?: string;
      metrics_found?: number;
      role?: string;
      role_label?: string;
      role_format?: string;
      // См. коммент у ExtractStreamEvent.done — то же поле на чат-канале.
      table_output_enums?: string[] | null;
      // Роль выбрана, но раскладки для этой отчётности нет (#141). Раньше
      // извлечение молча отдавало полный набор ячейки, и пользователь не мог
      // понять, сработал ли его выбор роли.
      role_not_applied?: boolean;
      role_notice?: string;
      formatted?: FormattedTable | null;
      periods?: string[];
    }
  | { type: "extract_error"; name?: string; message?: string }
  | { type: "clarify"; question: string; options: string[] }
  | { type: "bg_runs"; runs: { run_id: string; nonce?: string }[] }
  | { type: "sources"; sources: Record<string, unknown>[] }
  | { type: "usage"; turn: TurnUsage; project_total: ProjectUsage }
  | { type: "analysis_card"; card: AnalysisCard }
  | { type: "typed_result"; result: TypedResult; status: "complete" | "partial" }
  | { type: "node_run"; plan_graph_hash: string; node_run: NodeRunContract }
  | { type: "plan_graph"; graph_hash: string; planner_version?: string; root: NodeKeyContract; nodes: PlanGraphContract["nodes"]; scenarios?: PlanGraphContract["scenarios"] }
  // Лента гибридного планировщика: решения planner и попытки автора кода (бэкенд отдаёт их
  // через legacy-конверт, как и `plan_graph`; слияние — lib/dynamic-run-trace.ts).
  | DynamicMoveEvent
  | DynamicPlanEvent
  | AuthorAttemptEvent
  // План от автора плана (ADR-0074): ревизии одного хода.
  | RootPlanEvent
  | { type: "typed_error"; error: TypedRunError; partial_results: TypedResult[] }
  // Ошибка приезжает В ДВУХ ФОРМАХ, и это не небрежность типа, а факт двух
  // источников: агент перекладывает её в `error` (orchestrator/agent.py:1533),
  // а сам оркестратор прогонов пишет в журнал `message`
  // (run_orchestrator.py:191-195 — падение и отмена). Реплей журнала отдаёт
  // обе, поэтому потребитель обязан читать обе. Тип это скрывал.
  | { type: "error"; error: string; message?: string }
  | { type: "error"; error?: string; message: string }
  | { type: "done"; status?: "succeeded" | "partial" | "failed" | "cancelled" };

/** Events streamed by a scenario run (/scenarios/{id}/runs/{run_id}/stream):
 * a superset of ChatStreamEvent — agent_prompt relays the agent's chat events,
 * plus run-level started/progress/artifact. */
export type ScenarioRunEvent =
  | ChatStreamEvent
  | { type: "started"; kind?: string; name?: string }
  | { type: "progress"; text?: string }
  | { type: "artifact"; run_id: string; kind: string };

export async function getProjectUsage(projectId: string): Promise<ProjectUsage> {
  const r = await fetch(`/api/projects/${encodeURIComponent(projectId)}/usage`, {
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`usage fetch ${r.status}`);
  return r.json();
}

/** A metric value can be numeric or a short text label (audit opinion, "н/д",
 * etc). Widened from `number | null` when the role-formatted extraction table
 * was ported from upstream — segment/text rows carry strings. */
export type MetricValue = number | string | null;

export type MetricEvent = {
  type: "metric";
  enum: string;
  name_ru: string;
  value_current: MetricValue;
  value_prev: MetricValue;
  /** Per-period breakdown emitted by the role-aware extraction path (interim
   * quarters, multiple annual columns). Each entry pins its own source. */
  period_values?: Array<{
    period: string;
    value: MetricValue;
    unit?: string;
    page?: number | null;
    source_form?: string;
    source_text?: string;
  }>;
  unit: string;
  /** С КАКИМ периодом сравнивается ИМЕННО ЭТА строка (#205).
   *
   * У промежуточного отчёта две оси: баланс сравнивается с концом прошлого
   * года (31.12.2025), ОПУ — с тем же кварталом год назад (Q1 2025). Одна
   * подпись колонки на всю таблицу соврала бы про две трети строк, поэтому
   * подпись приезжает построчно и показывается подсказкой в ячейке. */
  period_prev_label?: string;
  found: boolean;
  is_calculated: boolean;
  formula: string;
  /** ЧЕМ ПОСЧИТАНО (Б5): числа, реально подставленные в формулу. Одного текста
   * формулы не хватает — отсутствующий необязательный вход вычислитель
   * достраивает нулём, и формула на вид остаётся полной. */
  /** `transform` — что сделали со входом перед подстановкой: `abs` снял знак
   * отчёта, `sign` сменил его на обратный (расчётный отток печатается в знаке
   * отчёта, а в формулу входит модулем), `avg` усреднил остаток за период;
   * `period` — колонка, за которую входы сняты. Без них подпись не
   * воспроизводится по тому, что на экране. */
  inputs?: Array<{
    enum: string;
    value: number | null;
    substituted_zero?: boolean;
    transform?: string;
    period?: string;
  }>;
  /** ПОЧЕМУ ПУСТО: входы формулы, которых в прогоне не нашлось. */
  calc_missing?: string[];
  /** Business/geographic segment this row belongs to (segments tab). */
  segment?: string;
  /** Segment axis — "operating_segment" | "geography" | … */
  dimension?: string;
  segment_fallback?: string;
  group_relation?: string;
  group_registration?: string;
  group_activity?: string;
  /** 1-based page number in the report where the value was found. Optional —
   * legacy runs and free-form extracts won't have it. UI uses it for the
   * «Источник» column for extracted metrics. */
  page?: number | null;
  /** Report form the value came from (e.g. «ОФП», «ОПУ», note number). */
  source_form?: string;
};

/** One key-figure row of an analysis card. `flag` drives the status pill
 * colour; `why` is surfaced on hover. Every field is a pre-formatted string
 * from the backend — the UI renders it verbatim. */
export type AnalysisCardKpi = {
  name: string;
  value: string;
  prev: string;
  delta: string;
  /** Ступени статуса строки. «crit» и «good» добавлены к трём исходным (#157-C):
   * без «crit» верхней ступенью был «risk», и отчёт, где под вопросом само
   * существование компании, выглядел как отчёт с подросшим левериджем; без
   * «good» сильная сторона была неотличима от отсутствия проблемы. */
  flag: "good" | "ok" | "warn" | "risk" | "crit" | "na";
  why: string;
};

/** A fully laid-out financial-analysis card, streamed on the chat SSE channel
 * as `{ type: "analysis_card", card }` just before the markdown answer. The
 * markdown text stays as a fallback; when a card is present the UI renders it
 * instead. Any field may be empty — render tolerantly. */
export type AnalysisCard = {
  company: string;
  period: string;
  company_type: string;
  standard: string;
  language: string;
  /**
   * Отчёт целиком, как его написал аналитик. С 11.08.2026 — ЕДИНСТВЕННЫЙ
   * источник текста карточки.
   *
   * До этого код резал markdown по заголовкам и сопоставлял их с ролями из
   * методики, а карточка потом рисовала `<h3>{title}</h3> + markdown(body)` в
   * том же порядке — то есть разбор восстанавливал структуру, чтобы вывести
   * ровно тот же markdown. Он ничего не производил, только терял блоки, чей
   * заголовок не совпал со словарём: синоним от модели, переименованный
   * пользователем раздел, отсутствие заголовков вовсе (ФосАгро 11.08 —
   * карточка вышла пустой).
   */
  report_md?: string;
  /** ↓ Прежний контракт. Новые прогоны эти поля НЕ наполняют — они остаются
   *  только ради карточек из сохранённых диалогов, где report_md ещё нет. */
  verdict: string;
  kpi: AnalysisCardKpi[];
  sections: { title: string; body: string }[];
  strengths: string[];
  watch: string[];
  conclusions: string[];
  footnote: string;
  /** Документ, к которому относится карточка — нужен ссылкам «PDF стр. N»,
   * чтобы прыжок знал, в каком документе открывать страницу (#157). */
  doc_id?: string;
  /** Спецификации графиков (данные + масштаб + подпись), НЕ готовый SVG:
   * рисует фронт. Числа берутся из таблицы заземления, а не сочиняются. */
  charts?: AnalysisChart[];
  /** Уровень риска на обложке — уровень, а не фраза в тексте. */
  risk_profile?: { level?: string; label?: string; why?: string[] };
  /** Факты извне. Каждый ОБЯЗАН нести дату и ссылку, иначе домысел
   * неотличим от факта — без источника строка не показывается. */
  external_context?: { text: string; source: string; url?: string; date?: string }[];
};

/** Один график карточки. Фронт рисует SVG сам по этой спецификации.
 * `unit` обязателен: масштаб оси выводится ИЗ ДАННЫХ, а не задаётся
 * константой — иначе график лжёт, не показав ни одной неверной цифры. */
export type AnalysisChart = {
  kind: "waterfall" | "bars" | "stacked";
  title: string;
  unit: string;
  points: { label: string; value: number; kind?: "start" | "delta" | "total" }[];
  note?: string;
  source?: string;
};

/** A pre-laid-out table returned by POST /metrics/format-role. The backend
 * decides columns/rows/formatting per analyst role; the UI just renders it. */
export type FormattedTable = {
  role: string;
  role_label: string;
  company_type: string;
  format: string;
  sheet_name?: string;
  periods?: string[];
  columns: Array<{ key: string; label: string; kind?: string; hidden?: boolean }>;
  rows: Array<{
    kind?: string;
    die_kind?: string;
    enum?: string;
    label?: string;
    label_en?: string;
    label_ru?: string;
    unit?: string;
    source?: string;
    indent?: number;
    bold?: boolean;
    highlight?: boolean;
    is_percent?: boolean;
    value_kind?: string;
    values?: Record<string, unknown>;
    [key: string]: unknown;
  }>;
};

export type MetricsRoleFormatResponse = {
  role: string;
  role_label: string;
  company_type: string;
  role_format: string;
  output_enums?: string[] | null;
  formatted?: FormattedTable | null;
};

export type ExtractStreamEvent =
  | {
      type: "start";
      total_metrics: number;
      total_extractable: number;
      company_type: string;
      unit: string;
      period_current: string;
      period_prev: string;
      periods?: string[];
      company_name?: string;
      role?: string;
      role_label?: string;
      role_format?: string;
    }
  | { type: "progress"; enum: string; name_ru: string }
  | MetricEvent
  | {
      type: "done";
      period_current: string;
      period_prev: string;
      periods?: string[];
      unit: string;
      company_type: string;
      company_name: string;
      tokens_in?: number;
      tokens_out?: number;
      tokens_cached?: number;
      cost_usd?: number;
      role?: string;
      role_label?: string;
      role_format?: string;
      // Белый список раскладки роли (#141). Бэкенд слал его всегда, а тип не
      // объявлял — поле отсекалось ещё на разборе SSE, и фильтр таблицы по роли
      // на живом пути не включался НИКОГДА (заполнялся только ручной сменой
      // раскладки постфактум). null = роль общая, фильтра нет.
      table_output_enums?: string[] | null;
      // См. коммент у ChatStreamEvent.extract_done.
      role_not_applied?: boolean;
      role_notice?: string;
      formatted?: FormattedTable;
    }
  | { type: "error"; message: string };

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    let msg = text;
    try {
      const j = JSON.parse(text);
      if (typeof j?.detail === "string") msg = j.detail;
    } catch {
      // not JSON — keep raw
    }
    throw new Error(msg);
  }
  return res.json() as Promise<T>;
}

export async function listDocuments(): Promise<DocumentSummary[]> {
  return handle(await fetch("/api/documents", { cache: "no-store" }));
}

/**
 * Скачать таблицу прогона книгой Excel (правка Nate 18.08).
 *
 * Книгу собирает БЭКЕНД (`POST /metrics/export.xlsx`, openpyxl): там же
 * задаются ширины колонок, формат чисел и подписи — на старом экране «/» это
 * работает с июля. Клеить xlsx на клиенте значило бы завести второй вид одной
 * и той же книги.
 */
export async function downloadMetricsXlsx(body: {
  company?: string;
  period_current?: string;
  period_prev?: string;
  unit?: string;
  rows: unknown[];
  filename?: string;
}): Promise<void> {
  const res = await fetch("/api/metrics/export.xlsx", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${text.slice(0, 200)}`);
  }
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = (body.filename || "показатели").replace(/[^\p{L}\p{N}\-_ ]+/gu, "_") + ".xlsx";
  a.click();
  URL.revokeObjectURL(url);
}

/** Экран прогона, каким он уехал в браузер: три тела событий, из которых
 *  карточка извлечения и складывается. Их пишет бэкенд теми же сборщиками,
 *  что и живые события (`api/station_turn._remember_screen`). */
export type StoredRunScreen = {
  start?: Record<string, unknown>;
  rows?: Record<string, unknown>[];
  done?: Record<string, unknown>;
};

export type StoredRun = {
  nonce: string;
  kind: string;
  status: string;
  doc_id: string;
  payload: Record<string, unknown> | null;
  screen: StoredRunScreen | null;
  error: string | null;
  started_at: number;
  finished_at: number;
  rev: number;
};

/**
 * Последний ЗАВЕРШЁННЫЙ прогон по документу — с СЕРВЕРА, а не из localStorage
 * (#276).
 *
 * ЗАЧЕМ. До этой ручки исход прогона жил ровно в двух местах: в потоке SSE
 * открытой вкладки и в хранилище браузера. Закрыл вкладку до конца прогона —
 * час работы движка исчезал; 18.08 так пропали ДОМ.РФ и «Самолёт».
 *
 * `null` на 404 — ШТАТНЫЙ ответ, а не ошибка: прогонов по документу может не
 * быть вовсе, а на стенде без `FINENGINE_RUNS_DATABASE_URL` хранилище выключено
 * и молчит всегда. Ронять на этом рабочую область нельзя — она и без сервера
 * рисует то, что есть локально.
 */
export async function fetchLastRun(docId: string, kind = "extract"): Promise<StoredRun | null> {
  const res = await fetch(
    `/api/runs/by-doc/${encodeURIComponent(docId)}?kind=${encodeURIComponent(kind)}`,
    { cache: "no-store", credentials: "include" },
  );
  if (res.status === 404) return null;
  return handle<StoredRun>(res);
}

/** Виды документа — ровно перечисление DocType бэкенда, порядок для человека.
 *  Значения менять нельзя: ручка сверяет их с DocType и отбивает незнакомое. */
export const DOC_TYPES: { id: string; label: string }[] = [
  { id: "ifrs_report", label: "отчётность МСФО" },
  { id: "rsbu_report", label: "отчётность РСБУ" },
  { id: "press_release", label: "пресс-релиз" },
  { id: "transcript", label: "транскрипт" },
  { id: "presentation", label: "презентация" },
  { id: "excel", label: "Excel" },
  { id: "other", label: "прочее" },
];

/** Поправить вид документа руками (#250). Вид ставится при загрузке
 *  автоматически и промахивается — от него зависит выбор ячейки реестра. */
export async function patchDocumentType(docId: string, docType: string): Promise<void> {
  await handle(
    await fetch(`/api/documents/${encodeURIComponent(docId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ doc_type: docType }),
    }),
  );
}

export type TelegramPairResponse = {
  code: string;
  expires_in_sec: number;
  bot_username: string;
};

export type TelegramStatusResponse = {
  linked: boolean;
  expired: boolean;
  linked_user: string | null;
};

export async function telegramPair(): Promise<TelegramPairResponse> {
  return handle(
    await fetch("/api/integrations/telegram/pair", { method: "POST", cache: "no-store" }),
  );
}

export async function telegramStatus(code: string): Promise<TelegramStatusResponse> {
  return handle(
    await fetch(`/api/integrations/telegram/status?code=${encodeURIComponent(code)}`, {
      cache: "no-store",
    }),
  );
}

export async function telegramUnlink(code: string): Promise<void> {
  const res = await fetch(`/api/integrations/telegram/unlink?code=${encodeURIComponent(code)}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text().catch(() => res.statusText)}`);
}

export async function uploadDocument(
  file: File,
  meta: {
    company_name?: string;
    period?: string;
    doc_type?: string;
    company_type?: string;
    ocr_engine?: "pymupdf" | "idp";
    /** Не запускать разбор сразу — его попросит ход чата. */
    defer_parse?: boolean;
    /** Прогресс выгрузки в процентах (только с XHR-веткой). */
    onProgress?: (percent: number) => void;
  } = {},
  signal?: AbortSignal,
): Promise<UploadResponse> {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("company_name", meta.company_name ?? "");
  fd.append("period", meta.period ?? "");
  fd.append("doc_type", meta.doc_type ?? "");
  fd.append("company_type", meta.company_type ?? "unknown");
  fd.append("ocr_engine", meta.ocr_engine ?? "pymupdf");
  if (meta.defer_parse) fd.append("defer_parse", "1");

  // Прогресс отдаёт только XHR: у fetch нет событий выгрузки тела, а показать
  // проценты по каждому файлу — требование рабочего экрана. Без onProgress
  // остаёмся на fetch, чтобы не терять поддержку AbortSignal-семантики.
  if (!meta.onProgress) {
    return handle(await fetch("/api/documents/upload", { method: "POST", body: fd, signal }));
  }
  const onProgress = meta.onProgress;
  return new Promise<UploadResponse>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/documents/upload");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.min(99, Math.round((e.loaded / e.total) * 100)));
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(100);
        try {
          resolve(JSON.parse(xhr.responseText) as UploadResponse);
        } catch {
          reject(new Error("Сервер вернул неразборчивый ответ на загрузку"));
        }
      } else {
        let detail = `Ошибка загрузки (${xhr.status})`;
        try {
          detail = (JSON.parse(xhr.responseText) as { detail?: string }).detail || detail;
        } catch {
          /* тело не JSON — оставляем код статуса */
        }
        reject(new Error(detail));
      }
    };
    xhr.onerror = () => reject(new Error("Сеть недоступна — файл не загрузился"));
    xhr.onabort = () => reject(new DOMException("Aborted", "AbortError"));
    if (signal) signal.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(fd);
  });
}

/** Запустить отложенный разбор (OCR). Идемпотентно — см. ручку на бэке. */
export async function startDocumentParse(
  docId: string,
): Promise<{ doc_id: string; processing: boolean; ready: boolean }> {
  return handle(await fetch(`/api/documents/${docId}/parse`, { method: "POST" }));
}

export type DemoSummary = {
  slug: string;
  title: string;
  description: string;
  company_name: string;
};

export type DemoLoadResponse = {
  doc_id: string;
  filename: string;
  pages: number;
  company_name: string;
  period_current: string;
  period_prev: string;
  unit: string;
  metrics: MetricEvent[];
};

export async function listDemos(): Promise<DemoSummary[]> {
  return handle(await fetch("/api/demo/list", { cache: "no-store" }));
}

export async function loadDemo(
  slug: string,
  signal?: AbortSignal,
): Promise<DemoLoadResponse> {
  return handle(
    await fetch(`/api/demo/load?slug=${encodeURIComponent(slug)}`, {
      method: "POST",
      signal,
    }),
  );
}

export async function deleteDocument(docId: string): Promise<void> {
  const res = await fetch(`/api/documents/${docId}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text().catch(() => res.statusText)}`);
}

/**
 * Локальные копии файлов, загруженных В ЭТОЙ вкладке: doc_id → blob-ссылка.
 *
 * ⚠️ ЗАЧЕМ (замер 11.08.2026). Сразу после загрузки панель документа брала PDF
 * ПО СЕРВЕРНОЙ ССЫЛКЕ — то есть браузер скачивал обратно файл, который сам же
 * только что отправил и всё ещё держал в памяти. В журнале прогона владельца
 * такое скачивание случалось дважды подряд.
 *
 * Канал у владельца несимметричный: замерено 13 КБ/с вверх при 20% потерь и
 * 394 КБ/с вниз. Пятимегабайтный отчёт после семиминутной заливки скачивался
 * обратно ещё ~13 секунд, и так дважды. Это не главная причина медленной
 * загрузки — главная в канале, — но это работа, которой можно не делать вовсе.
 */
const _localFiles = new Map<string, string>();

/** Запомнить локальный файл под его doc_id — зовётся сразу после загрузки. */
export function rememberLocalFile(docId: string, file: Blob): void {
  if (typeof window === "undefined" || !docId) return;
  const prev = _localFiles.get(docId);
  if (prev) URL.revokeObjectURL(prev);
  _localFiles.set(docId, URL.createObjectURL(file));
}

export function documentFileUrl(docId: string): string {
  // Файл, загруженный в этой вкладке, показываем из памяти. Перезагрузили
  // страницу — карта пуста, и ссылка снова серверная: копия в памяти это
  // ускорение, а не хранилище.
  return _localFiles.get(docId) ?? `${DIRECT_API}/documents/${docId}/file`;
}

// ─── Personal sources (Daily News subscriptions) ──────────────────────
// Backend module: src/finengine/api/personal_sources.py
// All endpoints under /sources/* and /chats/<id>/kb/*, user-scoped via
// the same cookie-JWT we already use for chat/upload calls.

export type PersonalSource = {
  id: string;
  display_name: string;
  type: string;       // "official" | "tier1" | "wire" | "wire_tg"
  region: string;     // "RU" | "US" | "EU" | "CN" | "global"
  connector: string;  // "rss" | "telegram" | "edgar" | "edisclosure" | …
  url: string;
  weight: number;
  active: boolean;
  kind: "default" | "user";
};

export async function listSources(): Promise<PersonalSource[]> {
  const r = await fetch("/api/sources", { cache: "no-store" });
  const data = await handle<{ sources: PersonalSource[] }>(r);
  return data.sources;
}

export type AddSourceBody = {
  url: string;
  display_name?: string;
  region?: "RU" | "US" | "EU" | "CN" | "global";
  type?: "official" | "tier1" | "wire" | "wire_tg";
  connector?: "rss" | "telegram" | "edgar" | "html" | "edisclosure" | "playwright_rss";
  weight?: number;
};

export async function addSource(body: AddSourceBody): Promise<PersonalSource> {
  const res = await fetch("/api/sources", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await handle<{ source: PersonalSource }>(res);
  return data.source;
}

export async function deleteSource(id: string): Promise<void> {
  const r = await fetch(`/api/sources/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!r.ok) throw new Error(await r.text());
}

export async function enableSource(id: string): Promise<void> {
  const r = await fetch(`/api/sources/${encodeURIComponent(id)}/enable`, {
    method: "POST",
  });
  if (!r.ok) throw new Error(await r.text());
}


async function* streamSSE<T>(url: string, init: RequestInit, signal?: AbortSignal): AsyncGenerator<T> {
  const res = await fetch(url, {
    ...init,
    signal,
    cache: "no-store",
    headers: { Accept: "text/event-stream", ...(init.headers ?? {}) },
  });
  if (!res.ok || !res.body) {
    throw new Error(`${res.status}: ${await res.text().catch(() => res.statusText)}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const raw = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const lines = raw.split("\n").filter((l) => l.startsWith("data: "));
      if (!lines.length) continue;
      const payload = lines.map((l) => l.slice(6)).join("\n");
      try {
        yield JSON.parse(payload) as T;
      } catch {
        // skip malformed frame
      }
    }
  }
}

// Bypass the Next.js dev-server proxy for SSE — go direct to the FastAPI
// backend (CORS is open). Some dev-server rewrite pipelines buffer streaming
// bodies, which starves the UI of events until the whole response finishes.
const DIRECT_API =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export function chatStream(
  body: {
    session_id?: string;
    message: string;
    doc_ids?: string[];
    /** Arch the user has selected for this chat. Backend uses it to attach
     * the AIFA Baseline pilot disclaimer and to tag chat_runs.jsonl rows. */
    arch?: string;
    /** Project id this chat belongs to. Backend uses it for per-user
     * data isolation (Пакет B). Empty string = no project scope —
     * tools won't be able to touch project files. */
    project_id?: string;
    /** User-selected analyst role for metric extraction (other/risks/baifp/
     * die/cib/dgr). Backend scopes fe_extract_metrics to this role's layout. */
    role?: string;
    /** МОДЕЛЬ ВХОДОВ: shortcut buttons pin the first turn's decision — the
     * client tool the gateway MUST call (OpenResponses tool_choice). Empty =
     * free-form turn. */
    pinned_tool?: string;
    /** Deterministic pin: fixes the pinned tool's ARGS too (e.g.
     * {depth:"quick", doc_id}) → backend runs fe_analyze directly, skipping the
     * LLM turn. Only honoured for fe_analyze/fe_extract_metrics. */
    pinned_tool_args?: Record<string, unknown>;
    /** Тумблер «Поиск в интернете» (меню «+»). false → веб-тулы не отдаются
     * модели на этот ход. Действует на инлайн-петле. */
    web_search?: boolean;
    /** Документ, открытый в панели исходника: запрос без уточнения («проведи
     * извлечение») бьёт в него, а не в первый документ чата. */
    active_doc_id?: string;
  },
  signal?: AbortSignal,
): AsyncGenerator<ChatStreamEvent> {
  return streamSSE<ChatStreamEvent>(
    `${DIRECT_API}/query/stream`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: body.session_id ?? "",
        message: body.message,
        doc_ids: body.doc_ids ?? [],
        arch: body.arch ?? "",
        project_id: body.project_id ?? "",
        role: body.role ?? "",
        pinned_tool: body.pinned_tool ?? "",
        pinned_tool_args: body.pinned_tool_args ?? {},
      }),
    },
    signal,
  );
}

/** #84 C3-1: асинхронный контракт чата. Ход = фоновый прогон на сервере:
 * старт отдаёт run_id сразу, ответ дописывается независимо от браузера. */
export async function startChatTurn(
  body: Parameters<typeof chatStream>[0],
): Promise<{
  schema_version: "1.0";
  event_schema_version: "1.0";
  run_id: string;
  events_url: string;
}> {
  const r = await fetch(`${DIRECT_API}/chat/turns`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: body.session_id ?? "",
      message: body.message,
      doc_ids: body.doc_ids ?? [],
      arch: body.arch ?? "",
      project_id: body.project_id ?? "",
      role: body.role ?? "",
      pinned_tool: body.pinned_tool ?? "",
      pinned_tool_args: body.pinned_tool_args ?? {},
      web_search: body.web_search ?? true,
      active_doc_id: body.active_doc_id ?? "",
    }),
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text().catch(() => r.statusText)}`);
  return r.json();
}

/** Подписка на ход чата (реплей + живой хвост) — переживает перезагрузку
 * страницы: тот же паттерн, что resumeExtractRunStream. */
export function chatRunStream(
  runId: string,
  signal?: AbortSignal,
): AsyncGenerator<ChatStreamEvent | { type: "session_lost"; message: string }> {
  return unwrapRunEvents(runId, signal);
}

type RunEventEnvelope =
  | { schema_version: "1.0"; event_type: "heartbeat"; sequence: number }
  | { schema_version: "1.0"; event_type: "legacy"; sequence: number; legacy_event: Record<string, unknown> }
  | { schema_version: "1.0"; event_type: "result"; sequence: number; delivery_status: "complete" | "partial"; result: TypedResult }
  | { schema_version: "1.0"; event_type: "node_run"; sequence: number; plan_graph_hash: string; node_run: NodeRunContract }
  | { schema_version: "1.0"; event_type: "error"; sequence: number; error: TypedRunError; partial_results: TypedResult[] }
  | { schema_version: "1.0"; event_type: "completed"; sequence: number; status: "succeeded" | "partial" | "failed" | "cancelled" };

async function* unwrapRunEvents(
  runId: string,
  signal?: AbortSignal,
): AsyncGenerator<ChatStreamEvent | { type: "session_lost"; message: string }> {
  const stream = streamSSE<RunEventEnvelope>(
    `${DIRECT_API}/chat/runs/${encodeURIComponent(runId)}/events`,
    { method: "GET", credentials: "include" },
    signal,
  );
  for await (const event of stream) {
    switch (event.event_type) {
      case "heartbeat":
        yield { type: "ping" };
        break;
      case "legacy":
        // Compatibility is pass-through only. In particular, the client does
        // not parse legacy prose to synthesize a typed final result.
        yield event.legacy_event as ChatStreamEvent | { type: "session_lost"; message: string };
        break;
      case "result":
        yield { type: "typed_result", result: event.result, status: event.delivery_status };
        break;
      case "node_run":
        yield { type: "node_run", plan_graph_hash: event.plan_graph_hash, node_run: event.node_run };
        break;
      case "error":
        yield { type: "typed_error", error: event.error, partial_results: event.partial_results };
        break;
      case "completed":
        yield { type: "done", status: event.status };
        break;
    }
  }
}

/** #84: прервать ход чата (кнопка «Стоп») — отмена серверного прогона. */
export async function cancelChatTurn(runId: string): Promise<void> {
  await fetch(`${DIRECT_API}/chat/runs/${runId}/cancel`, {
    method: "POST",
    credentials: "include",
  }).catch(() => undefined);
}

/** Subscribe to a scenario run's live stream (progress + relayed chat events).
 * Used by the run-as-chat view; survives client disconnect (run_orchestrator). */
export function scenarioRunStream(
  scenarioId: string,
  runId: string,
  signal?: AbortSignal,
): AsyncGenerator<ScenarioRunEvent> {
  return streamSSE<ScenarioRunEvent>(
    `${DIRECT_API}/scenarios/${encodeURIComponent(scenarioId)}/runs/${encodeURIComponent(runId)}/stream`,
    { method: "GET", credentials: "include" },
    signal,
  );
}
export async function getDocumentStatus(docId: string): Promise<DocumentStatus> {
  const r = await fetch(`${DIRECT_API}/documents/${docId}/status`, {
    credentials: "include",
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`status ${r.status}`);
  return r.json();
}

/** Ask the backend to re-lay-out already-extracted rows under a different
 * analyst role. Returns a role-specific FormattedTable + the enum whitelist
 * for that role. Used by the extraction-table role switcher (extracted-pane).
 * Routed through the Next dev proxy (small JSON, no streaming). */
export async function formatMetricsForRole(body: {
  company_name?: string;
  company_type?: string;
  role: string;
  period_current?: string;
  period_prev?: string;
  unit?: string;
  rows: Array<{
    enum: string;
    name_ru: string;
    segment?: string;
    dimension?: string;
    value_current: MetricValue;
    value_prev: MetricValue;
    period_values?: Array<{
      period: string;
      value: MetricValue;
      unit?: string;
      page?: number | null;
      source_form?: string;
      source_text?: string;
    }>;
    unit?: string;
    page?: number | null;
    source_form?: string;
    source_text?: string;
    is_calculated?: boolean;
    formula?: string;
  }>;
}): Promise<MetricsRoleFormatResponse> {
  return handle(
    await fetch("/api/metrics/format-role", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(body),
    }),
  );
}

/** Reattach to an in-flight extract run after page reload / VPN drop.
 * Backend keeps the extract task alive independently of the request that
 * started it — replays buffered events first, then tails live ones. */
export function resumeExtractRunStream(
  runId: string,
  signal?: AbortSignal,
): AsyncGenerator<ExtractStreamEvent | { type: "run_id"; run_id: string } | { type: "session_lost"; message: string }> {
  return streamSSE<ExtractStreamEvent | { type: "run_id"; run_id: string } | { type: "session_lost"; message: string }>(
    `${DIRECT_API}/extract/runs/${runId}/stream`,
    { method: "GET" },
    signal,
  );
}

export type WebSearchHit = {
  title: string;
  url: string;
  content: string;
  score: number;
};

export type WebSearchResponse = {
  enabled: boolean;
  message?: string;
  results: WebSearchHit[];
};

export async function webSearch(query: string, max_results = 5): Promise<WebSearchResponse> {
  const res = await fetch(`${DIRECT_API}/web_search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ query, max_results }),
  });
  if (!res.ok) {
    throw new Error(`Web search ${res.status}`);
  }
  return res.json();
}

// ───────────────────────── IR agents ─────────────────────────

export type IRCalendarCompany = {
  region: string;
  company: string;
  sub_group: string;
  ir_url: string;
};

export type IRCalendarEvent = {
  date: string;
  event_type: string;
  company: string;
  region: string;
  sub_group: string;
  source_url: string;
  raw_excerpt?: string;
  when?: "upcoming" | "recent";
  sources?: string[]; // e.g. ["ir_site","finnhub"] — confidence = length
  /** #95: пользовательская группа: reporting | investor_day | other. */
  group?: string;
};

export type IRCalendarStreamEvent =
  | { type: "started"; companies: number; window_back: number; window_fwd: number }
  | { type: "source_done"; source: string; events: number }
  | { type: "events"; company: string; events: IRCalendarEvent[] }
  | {
      type: "done";
      total_events: number;
      total_companies: number;
      by_source?: Record<string, number>;
      multi_source?: number;
      fetch_failed?: number;
      failed_companies?: string[];
    }
  | { type: "error"; message: string };

export type IRNewsItem = {
  title: string;
  url: string;
  summary: string;
  category: "macro" | "corporate" | "regulatory" | "other" | string;
  matched_topic: string;
  score: number;
};

export async function getIRCalendarCompanies(): Promise<{
  count: number;
  companies: IRCalendarCompany[];
}> {
  const r = await fetch(`${DIRECT_API}/ir/calendar/companies`, {
    credentials: "include",
    cache: "no-store",
  });
  return handle(r);
}

export function irCalendarStream(
  body: { limit?: number | null } = {},
  signal?: AbortSignal,
): AsyncGenerator<IRCalendarStreamEvent> {
  return streamSSE<IRCalendarStreamEvent>(
    `${DIRECT_API}/ir/calendar/stream`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: body.limit ?? null }),
    },
    signal,
  );
}

export type NewsDigestResult = {
  run_id: string;
  markdown: string;
  items_kept?: number;
  items_raw?: number;
  cost_usd?: number;
  duration_s?: number;
  from_cache?: boolean;
  age_hours?: number;
  error?: string | null;
};

/** Run the scripted Daily News ETL pipeline (RSS / EDGAR / e-disclosure →
 * rule filter → single LLM classify → render). Returns a markdown digest.
 * `fresh=false` (default) reuses a <24h cached digest if one exists. */
export async function runNewsDigest(
  opts: { fresh?: boolean } = {},
): Promise<NewsDigestResult> {
  const qs = opts.fresh ? "?fresh=true" : "";
  const r = await fetch(`${DIRECT_API}/skills/news_digest/run${qs}`, {
    method: "POST",
    credentials: "include",
    cache: "no-store",
  });
  return handle(r);
}


// ───────────────────── Project Sources ─────────────────────

export type ProjectSourceMeta = {
  file_id: string;
  filename: string;
  content_type: string;
  size: number;
  uploaded_at: number;     // unix seconds (server time)
  text_status: string;     // "ok" | "failed: ..." | ""
};

export async function listProjectSources(projectId: string): Promise<ProjectSourceMeta[]> {
  const r = await fetch(`${DIRECT_API}/projects/${projectId}/sources`, {
    credentials: "include",
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text().catch(() => r.statusText)}`);
  const data: { sources: ProjectSourceMeta[] } = await r.json();
  return data.sources;
}

export function projectSourceDownloadUrl(projectId: string, fileId: string): string {
  return `${DIRECT_API}/projects/${projectId}/sources/${fileId}/file`;
}

export async function uploadProjectSource(
  projectId: string,
  file: File,
  signal?: AbortSignal,
): Promise<ProjectSourceMeta> {
  const form = new FormData();
  form.append("file", file, file.name);
  const r = await fetch(`${DIRECT_API}/projects/${projectId}/sources/upload`, {
    method: "POST",
    credentials: "include",
    body: form,
    signal,
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text().catch(() => r.statusText)}`);
  return r.json();
}

export async function deleteProjectSource(projectId: string, fileId: string): Promise<void> {
  const r = await fetch(`${DIRECT_API}/projects/${projectId}/sources/${fileId}`, {
    method: "DELETE",
    credentials: "include",
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text().catch(() => r.statusText)}`);
}

export function projectSourceFileUrl(projectId: string, fileId: string): string {
  return `${DIRECT_API}/projects/${projectId}/sources/${fileId}/file`;
}

export async function getProjectSourceText(projectId: string, fileId: string): Promise<string> {
  const r = await fetch(`${DIRECT_API}/projects/${projectId}/sources/${fileId}/text`, {
    credentials: "include",
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text().catch(() => r.statusText)}`);
  const data: { text: string } = await r.json();
  return data.text;
}

/** Значение ячейки. `null` — формула, чьё значение книга не закэшировала
 *  (xlsx, собранный openpyxl, значений не считает): показывать надо `f`. */
export type SourceSheetCellValue = string | number | boolean | null;

export type SourceSheetCell = {
  /** 1-based номер строки/колонки — как в самом Excel. */
  r: number;
  c: number;
  v: SourceSheetCellValue;
  /** Тип для выравнивания: s|n|d|b|e. */
  t?: string;
  /** Формула строкой («=SUM(C3:C9)»); поля нет — ячейка не формульная. */
  f?: string;
  /** Жирный — единственное оформление, которое переносим (шапка таблицы). */
  b?: boolean;
};

export type SourceSheet = {
  file_id: string;
  filename: string;
  /** Имена всех листов книги — вкладки. */
  sheets: string[];
  /** Имя отданного листа (при пустом запросе — первый). */
  sheet: string;
  /** Настоящий размер листа: `cells` может покрывать меньше из-за капа. */
  n_rows: number;
  n_cols: number;
  /** true — сетка обрезана капом; говорить об этом честно, а не молча. */
  truncated: boolean;
  /** Ширины колонок в символах Excel; поля нет — ширины не заданы. */
  col_widths?: number[];
  /** РАЗРЕЖЕННЫЙ список: пустых ячеек в ответе нет. */
  cells: SourceSheetCell[];
};

/** Один лист xlsx-источника сеткой. Только .xlsx/.xlsm — на .xls и прочем
 *  сервер отвечает 415, на слишком большом файле 413, на неизвестном
 *  file_id или имени листа 404. */
export async function getProjectSourceSheet(
  projectId: string,
  fileId: string,
  opts: { sheet?: string; maxRows?: number; maxCols?: number } = {},
): Promise<SourceSheet> {
  const qs = new URLSearchParams();
  if (opts.sheet) qs.set("sheet", opts.sheet);
  if (opts.maxRows) qs.set("max_rows", String(opts.maxRows));
  if (opts.maxCols) qs.set("max_cols", String(opts.maxCols));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const r = await fetch(
    `${DIRECT_API}/projects/${projectId}/sources/${fileId}/sheet${suffix}`,
    { credentials: "include", cache: "no-store" },
  );
  if (!r.ok) throw new Error(`${r.status}: ${await r.text().catch(() => r.statusText)}`);
  return r.json();
}

/** История серверной сессии чата — страховочная копия переписки (см. бэк). */
export type ServerHistoryEntry =
  | { role: "user" | "assistant"; content: string }
  | { role: "tool_call"; name: string };

export async function fetchChatHistory(
  sessionId: string,
): Promise<{ session_id: string; history: ServerHistoryEntry[] }> {
  return handle(await fetch(`/api/chat/history/${encodeURIComponent(sessionId)}`));
}
