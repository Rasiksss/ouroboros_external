import type { NodeRunContract, PlanGraphContract } from "@/lib/api";
import type { DynamicTrace } from "@/lib/dynamic-run-trace";
import { nodeToken, type StoredNodeRun } from "@/lib/plan-graph-progress";

// A planner is limited to 12 moves. Thirty-two graphs leave room for repeated
// scenarios while bounding the persisted chat mirror: 32 copies of the real
// seven-node fixture occupy 210,974 JSON characters, below its 1.5 MiB budget.
export const MAX_EXECUTION_GRAPHS_PER_MESSAGE = 32;

export const NODE_RUN_HANDLED_FIELDS = [
  "attempt", "error", "finished_at", "node_key", "produced_evidence_ids", "started_at", "status",
] as const;
export const NODE_RUN_IGNORED_FIELDS = [
  "model_version", "node_run_id", "prompt_version", "run_id", "sandbox_profile",
] as const;

export type GraphTerminalStatus = "succeeded" | "partial" | "failed" | "cancelled";

export type ExecutionGraph = {
  id: string;
  move: number | null;
  graph: PlanGraphContract;
  nodeRuns: Record<string, StoredNodeRun>;
  openedAtMs: number;
  terminal?: { status: GraphTerminalStatus; atMs: number };
};

type GraphMessage = {
  id?: string;
  role?: string;
  planGraphs?: ExecutionGraph[];
  planGraph?: PlanGraphContract;
  nodeRuns?: Record<string, StoredNodeRun>;
  dynamicTrace?: DynamicTrace;
  typedErrors?: unknown[];
  created_at?: number;
};

export function graphMessageForWorkspace<T extends GraphMessage>(
  messages: T[],
  activeMessageId: string | null,
): T | null {
  if (activeMessageId) {
    const active = messages.find((message) => message.id === activeMessageId);
    return active && executionGraphsOf(active).length ? active : null;
  }
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (executionGraphsOf(messages[index]).length) return messages[index];
  }
  return null;
}

/** Графы вкладки «Граф исполнения» принадлежат тому сообщению, чья лента планировщика на экране:
 * иначе под ходами нового сообщения показывался бы граф прошлого, с номерами ходов, не
 * относящимися к нему. */
export function graphOwnerMessage<T extends GraphMessage>(traceMessage: T | null, graphMessage: T | null): T | null {
  return traceMessage?.dynamicTrace ? traceMessage : graphMessage;
}

function activeMove(trace: DynamicTrace | undefined): number | null {
  const running = [...(trace?.moves ?? [])].reverse().find((move) => move.status === "running");
  return running?.move ?? null;
}

function lastExecutedMove(trace: DynamicTrace | undefined): number | null {
  const reversed = [...(trace?.moves ?? [])].reverse();
  const executed = reversed.find((move) =>
    move.action === "execute" && Boolean(move.steps.length || move.scenarios.length || move.newAtoms.length)
  ) ?? reversed.find((move) => move.action === "execute");
  return executed?.move ?? null;
}

function graphId(graphs: ExecutionGraph[], move: number | null, hash: string): string {
  const occurrence = graphs.filter((item) => item.move === move && item.graph.graph_hash === hash).length + 1;
  return `${move ?? "deterministic"}:${hash}:${occurrence}`;
}

function trimGraphs(graphs: ExecutionGraph[]): ExecutionGraph[] {
  return graphs.slice(-MAX_EXECUTION_GRAPHS_PER_MESSAGE);
}

function storedNodeRun(run: NodeRunContract): StoredNodeRun {
  return {
    node_key: run.node_key, status: run.status, attempt: run.attempt, error: run.error,
    started_at: run.started_at, finished_at: run.finished_at, produced_evidence_ids: run.produced_evidence_ids,
  };
}

export function executionGraphsOf(message: GraphMessage): ExecutionGraph[] {
  if (message.planGraphs?.length) {
    if (!message.typedErrors?.length) return message.planGraphs;
    return message.planGraphs.map((graph) => graph.terminal ? graph : {
      ...graph,
      terminal: { status: "failed", atMs: graphFinishedAt(graph.nodeRuns, message.created_at) },
    });
  }
  if (!message.planGraph) return [];
  return [{
    id: `legacy:${message.planGraph.graph_hash}`,
    move: lastExecutedMove(message.dynamicTrace),
    graph: message.planGraph,
    nodeRuns: message.nodeRuns ?? {},
    openedAtMs: message.created_at ?? 0,
    ...(message.typedErrors?.length ? {
      terminal: { status: "failed" as const, atMs: graphFinishedAt(message.nodeRuns, message.created_at) },
    } : {}),
  }];
}

function graphFinishedAt(runs: Record<string, StoredNodeRun> | undefined, fallback = 0): number {
  return Math.max(
    fallback,
    ...Object.values(runs ?? {}).map((run) => run.finished_at ? Date.parse(run.finished_at) : 0),
  );
}

export function addExecutionGraph(
  current: ExecutionGraph[] | undefined,
  graph: PlanGraphContract,
  trace: DynamicTrace | undefined,
  atMs: number,
): ExecutionGraph[] {
  const graphs = current ?? [];
  const move = activeMove(trace);
  const duplicate = graphs.findIndex((item) =>
    !item.terminal && item.move === move && item.graph.graph_hash === graph.graph_hash
  );
  if (duplicate >= 0) {
    return graphs.map((item, index) => index === duplicate ? { ...item, graph } : item);
  }
  return trimGraphs([...graphs, {
    id: graphId(graphs, move, graph.graph_hash), move, graph, nodeRuns: {}, openedAtMs: atMs,
  }]);
}

export function applyExecutionNodeRun(
  current: ExecutionGraph[] | undefined,
  planGraphHash: string,
  run: NodeRunContract,
): ExecutionGraph[] {
  const graphs = current ?? [];
  let target = -1;
  for (let index = graphs.length - 1; index >= 0; index -= 1) {
    if (graphs[index].graph.graph_hash === planGraphHash) {
      target = index;
      break;
    }
  }
  if (target < 0) return graphs;
  const key = nodeToken(run.node_key);
  return graphs.map((item, index) => index === target ? {
    ...item, nodeRuns: { ...item.nodeRuns, [key]: storedNodeRun(run) },
  } : item);
}

export function closeExecutionMove(
  current: ExecutionGraph[] | undefined,
  move: number,
  status: GraphTerminalStatus,
  atMs: number,
): ExecutionGraph[] {
  return (current ?? []).map((item) =>
    item.move === move && !item.terminal ? { ...item, terminal: { status, atMs } } : item
  );
}

export function closeAllExecutionGraphs(
  current: ExecutionGraph[] | undefined,
  status: GraphTerminalStatus,
  atMs: number,
): ExecutionGraph[] {
  return (current ?? []).map((item) => item.terminal ? item : { ...item, terminal: { status, atMs } });
}

export function graphsForMove(graphs: ExecutionGraph[], move: number): ExecutionGraph[] {
  return graphs.filter((item) => item.move === move);
}

export function latestExecutionGraph(graphs: ExecutionGraph[], move: number | null = null): ExecutionGraph | null {
  const candidates = move === null ? graphs : graphsForMove(graphs, move);
  return candidates.at(-1) ?? null;
}

export type GraphSelection = { messageId: string | undefined; move: number | null; graphId: string | null };

/** Какой граф показывать во вкладке: выбор пользователя действует только в сообщении, где он
 * сделан, иначе остаётся «последний». Без этого номер хода из прошлого сообщения подменял бы
 * график нового. */
export function resolveGraphView(
  graphs: ExecutionGraph[],
  messageId: string | undefined,
  selection: GraphSelection | null,
): { selected: ExecutionGraph | null; moveGraphs: ExecutionGraph[]; graphCounts: Record<number, number> } {
  const defaultGraph = latestExecutionGraph(graphs);
  const choice = selection && selection.messageId === messageId ? selection : null;
  const move = choice?.move !== null && choice?.move !== undefined && graphsForMove(graphs, choice.move).length
    ? choice.move
    : defaultGraph?.move ?? null;
  const moveGraphs = move === null ? (defaultGraph ? [defaultGraph] : []) : graphsForMove(graphs, move);
  const selected = moveGraphs.find((graph) => graph.id === choice?.graphId) ?? moveGraphs.at(-1) ?? null;
  const graphCounts = Object.fromEntries(
    [...new Set(graphs.flatMap((graph) => graph.move === null ? [] : [graph.move]))]
      .map((item) => [item, graphsForMove(graphs, item).length]),
  );
  return { selected, moveGraphs, graphCounts };
}
