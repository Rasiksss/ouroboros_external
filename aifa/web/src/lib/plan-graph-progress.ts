import type { NodeRunContract, PlanGraphContract } from "@/lib/api";

/**
 * Срез 31, П7 — чистые функции прогресса плана исполнения.
 *
 * Компонент `PlanGraphWorkspace` только рисует; всё, что можно посчитать, лежит
 * здесь и проверяется без DOM: сводка выполнения (сколько узлов готово, идёт ли
 * работа, есть ли ошибки), состояние ребра по статусам его концов, категория
 * атома для цветовой подсказки и признак «план собран агентом-планировщиком».
 * Источник данных один — уже существующие события `plan_graph` (топология) и
 * `node_run` (статусы); второго источника истины нет.
 */

/** NodeRun в том виде, в каком его читает и хранит рабочая область: без идентификаторов запуска,
 * профиля песочницы и версий модели/промпта — они ни на что не влияют и занимали около трети записи. */
export type StoredNodeRun = Omit<
  NodeRunContract,
  "node_run_id" | "run_id" | "sandbox_profile" | "model_version" | "prompt_version"
>;

export type NodeStatus = NodeRunContract["status"] | "pending" | "empty" | "not_run" | "interrupted";
export type OverallState = "idle" | "running" | "succeeded" | "empty" | "partial" | "failed" | "cancelled";
export type EdgeState = "done" | "active" | "failed" | "idle";
export type AtomCategory = "read" | "calc" | "verify" | "output" | "other";

export function nodeToken(key: { atom_name: string; atom_version: string; args_fingerprint: string }): string {
  return `${key.atom_name}|${key.atom_version}|${key.args_fingerprint}`;
}

export type ProgressSummary = {
  total: number;
  done: number;
  running: number;
  failed: number;
  blocked: number;
  empty: number;
  notRun: number;
  pending: number;
  /** Целое 0..100: доля узлов с результатом; 100, когда итоговый узел дал результат. */
  percent: number;
  state: OverallState;
  /** Самый ранний `started_at` среди узлов, мс; `null`, пока ничего не запускалось. */
  startedAtMs: number | null;
  /** Самый поздний `finished_at`, мс; `null`, пока работа не закончена. */
  finishedAtMs: number | null;
};

export type GraphTerminal = {
  status: "succeeded" | "partial" | "failed" | "cancelled";
  atMs: number;
  startedAtMs?: number;
};

export const NODE_STATUS_LABEL: Record<NodeStatus, string> = {
  pending: "в очереди",
  planned: "в очереди",
  ready: "готов к запуску",
  running: "выполняется",
  succeeded: "готово",
  empty: "нет результата",
  not_run: "не выполнялся",
  interrupted: "не завершён",
  failed: "ошибка",
  blocked: "заблокирован",
  skipped: "пропущен",
};

export function nodeDisplayStatus(run: StoredNodeRun | undefined, terminal = false): NodeStatus {
  if (!run) return terminal ? "not_run" : "pending";
  if (terminal && run.status === "running") return "interrupted";
  if (terminal && ["planned", "ready"].includes(run.status)) return "not_run";
  if (run.status === "succeeded" && run.produced_evidence_ids !== undefined) {
    return run.produced_evidence_ids.length ? "succeeded" : "empty";
  }
  return run.status;
}

export function nodeStatusLabel(run: StoredNodeRun | undefined, terminal = false): string {
  return NODE_STATUS_LABEL[nodeDisplayStatus(run, terminal)];
}

/** Статус шага в ленте чата (`PlanStep`) — та же проекция, что у узла в рабочей области.
 * «Нет результата» — нейтральный итог нормального чтения, а не ошибка. */
export function nodeStepStatus(
  run: StoredNodeRun,
  terminal = false,
): "pending" | "running" | "done" | "error" | "empty" {
  const status = nodeDisplayStatus(run, terminal);
  if (status === "succeeded") return "done";
  if (status === "empty") return "empty";
  if (status === "running") return "running";
  return ["failed", "blocked", "skipped", "not_run", "interrupted"].includes(status) ? "error" : "pending";
}

export function statusOf(
  runs: Record<string, StoredNodeRun> | undefined,
  token: string,
  terminal = false,
): NodeStatus {
  return nodeDisplayStatus(runs?.[token], terminal);
}

export function summarizeProgress(
  graph: PlanGraphContract,
  nodeRuns?: Record<string, StoredNodeRun>,
  terminal?: GraphTerminal,
): ProgressSummary {
  let done = 0;
  let running = 0;
  let failed = 0;
  let blocked = 0;
  let empty = 0;
  let notRun = 0;
  let started: number | null = null;
  let finished: number | null = null;
  for (const node of graph.nodes) {
    const run = nodeRuns?.[nodeToken(node.node_key)];
    switch (nodeDisplayStatus(run, Boolean(terminal))) {
      case "succeeded":
        done += 1;
        break;
      case "running":
        running += 1;
        break;
      case "failed":
        failed += 1;
        break;
      case "blocked":
      case "skipped":
        blocked += 1;
        break;
      case "empty":
        empty += 1;
        break;
      case "not_run":
        notRun += 1;
        break;
      case "interrupted":
        failed += 1;
        break;
      default:
        break;
    }
    const s = run?.started_at ? Date.parse(run.started_at) : NaN;
    const f = run?.finished_at ? Date.parse(run.finished_at) : NaN;
    if (!Number.isNaN(s)) started = started === null ? s : Math.min(started, s);
    if (!Number.isNaN(f)) finished = finished === null ? f : Math.max(finished, f);
  }
  const total = graph.nodes.length;
  const pending = total - done - running - failed - blocked - empty - notRun;
  // Пока хоть один узел выполняется — работа идёт (даже если соседняя ветка упала).
  // Ошибка без выполняющихся узлов — итог «ошибка»: остальное уже не запустится.
  let state: OverallState;
  const rootStatus = nodeDisplayStatus(nodeRuns?.[nodeToken(graph.root)], Boolean(terminal));
  if (terminal?.status === "cancelled") state = "cancelled";
  else if (terminal?.status === "failed") state = "failed";
  else if (terminal?.status === "partial") state = "partial";
  // Упавший узел блокирует зависимых, поэтому blocked > 0 почти всегда идёт вместе с failed:
  // причина («Ошибка») важнее следствия («Завершено не полностью»), как и у живого графа.
  else if (terminal && failed > 0) state = "failed";
  else if (terminal) state = blocked > 0 ? "partial" : rootStatus === "succeeded" ? "succeeded" : "empty";
  else if (running > 0) state = "running";
  else if (failed > 0) state = "failed";
  else if (blocked > 0) state = "partial";
  else if (total > 0 && done === total && rootStatus === "succeeded") state = "succeeded";
  else if (done > 0) state = "running"; // пауза между узлами
  else state = "idle";
  const percent = state === "succeeded"
    ? 100
    : total === 0 ? 0 : Math.round((done / total) * 100);
  return {
    total,
    done,
    running,
    failed,
    blocked,
    empty,
    notRun,
    pending,
    percent,
    state,
    startedAtMs: started ?? terminal?.startedAtMs ?? null,
    // Время конца берётся из самих NodeRun: `terminal.atMs` — часы браузера в момент прихода
    // события, и при реплее журнала после обрыва связи это «сейчас», а не момент завершения.
    finishedAtMs: terminal
      ? finished ?? terminal.atMs
      : (["succeeded", "failed"].includes(state) ? finished : null),
  };
}

export const OVERALL_LABEL: Record<OverallState, string> = {
  idle: "Ожидание",
  running: "Выполняется",
  succeeded: "Готово",
  empty: "Завершено без результата",
  partial: "Завершено не полностью",
  failed: "Ошибка",
  cancelled: "Отменено",
};

/** Заголовок шапки графа. У закрытого графа без полного результата — «n из N узлов с результатом»,
 * а не процент: «43 %» рядом с «Завершено без результата» читается как прогресс. */
export function graphHeadline(summary: ProgressSummary): string {
  const label = OVERALL_LABEL[summary.state];
  if (summary.state === "running") return `${label} · ${summary.done} из ${summary.total}`;
  if (summary.state === "failed") return `${label} · ${summary.failed} из ${summary.total}`;
  if (summary.state === "empty" || summary.state === "partial") {
    return `${label} · ${summary.done} из ${summary.total} узлов с результатом`;
  }
  return label;
}

/** Полоса процента нужна, пока граф идёт, и на «Готово»; у закрытого без результата она лжёт о прогрессе. */
export function showsProgressBar(state: OverallState): boolean {
  return state === "idle" || state === "running" || state === "succeeded";
}

/** Состояние ребра «источник → приёмник» по статусам его концов. */
export function edgeState(source: NodeStatus, target: NodeStatus): EdgeState {
  if (["failed", "blocked", "interrupted"].includes(source)) return "failed";
  // Пустое чтение — нормальный исход, «не выполнялся» — не событие: нейтральное ребро, не красное.
  if (source === "empty" || source === "not_run") return "idle";
  if (source === "succeeded") return target === "running" || target === "ready" ? "active" : "done";
  if (source === "running") return "active";
  return "idle";
}

const CATEGORY_BY_PREFIX: [string, AtomCategory][] = [
  ["read.", "read"],
  ["locate", "read"],
  ["doc.", "read"],
  ["ingest.", "read"],
  ["identify.", "read"],
  ["calc.", "calc"],
  ["ref.", "calc"],
  ["assess.", "calc"],
  ["explain.", "calc"],
  ["norm.", "verify"],
  ["verify.", "verify"],
  ["out.", "output"],
  ["analyze.", "output"],
  ["monitor.", "output"],
];

export function atomCategory(atomName: string): AtomCategory {
  for (const [prefix, category] of CATEGORY_BY_PREFIX) {
    if (atomName === prefix || atomName.startsWith(prefix)) return category;
  }
  return "other";
}

export const CATEGORY_LABEL: Record<AtomCategory, string> = {
  read: "Чтение",
  calc: "Расчёт",
  verify: "Проверка",
  output: "Итог",
  other: "Шаг",
};

/** План собран агентом-планировщиком (`PlanGraph.planner_version = "dynamic-…"`). */
export function isDynamicPlan(graph: Pick<PlanGraphContract, "planner_version">): boolean {
  return (graph.planner_version ?? "").startsWith("dynamic");
}

/** Секунды → «0,4 с» / «12 с» / «1 мин 05 с». */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 10) return `${seconds.toFixed(1).replace(".", ",")} с`;
  if (seconds < 60) return `${Math.round(seconds)} с`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return `${minutes} мин ${String(rest).padStart(2, "0")} с`;
}
