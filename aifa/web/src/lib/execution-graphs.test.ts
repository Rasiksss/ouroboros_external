import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";

import type { ChatStreamEvent } from "@/lib/api";
import {
  MAX_EXECUTION_GRAPHS_PER_MESSAGE,
  NODE_RUN_HANDLED_FIELDS,
  NODE_RUN_IGNORED_FIELDS,
  executionGraphsForDisplay,
  executionGraphsOf,
  graphMessageForWorkspace,
  graphOwnerMessage,
  graphsForMove,
  resolveGraphView,
} from "@/lib/execution-graphs";
import {
  __fitToBudgetБыстро,
  applyChatEvent,
  resetForReplay,
  type Project,
  type TextMessage,
} from "@/lib/projects";
import { nodeDisplayStatus, nodeStatusLabel, nodeToken, summarizeProgress } from "@/lib/plan-graph-progress";

function fixture(name: string): ChatStreamEvent[] {
  return JSON.parse(readFileSync(resolve(process.cwd(), "..", "finengine", "tests", "dynamic_run", "fixtures", "sse", name), "utf8"));
}

function message(): TextMessage {
  return { id: "assistant-1", role: "assistant", content: "", created_at: 1 };
}

function replay(events: ChatStreamEvent[]): TextMessage {
  return events.reduce((msg, event) => applyChatEvent(msg, event), message());
}

describe("execution graphs by planner move", () => {
  it("keeps two graphs of one move and does not mix equal node keys", () => {
    vi.spyOn(Date, "now").mockReturnValue(10_000);
    const msg = replay(fixture("workspace-cycle.json"));
    const graphs = executionGraphsOf(msg);

    expect(graphsForMove(graphs, 2)).toHaveLength(2);
    expect(graphsForMove(graphs, 4)).toHaveLength(0);
    expect(graphsForMove(graphs, 2).map((item) => item.nodeRuns["read.value|1.0.0|same-key"].produced_evidence_ids)).toEqual([
      [], ["evidence-from-case"],
    ]);
  });

  it("binds full backend fixture graphs to their moves and leaves the graphless final move empty", () => {
    const msg = replay(fixture("empty-read-then-give-up.json"));
    const graphs = executionGraphsOf(msg);
    expect(graphs.map((item) => item.move)).toEqual([1, 2]);
    expect(graphsForMove(graphs, 3)).toEqual([]);
  });

  it("replays S1 event by event: empty root finishes without result or 100 percent", () => {
    const events = fixture("empty-read-then-give-up.json");
    let msg = message();
    let sawRunning = false;
    for (const event of events) {
      msg = applyChatEvent(msg, event);
      const first = graphsForMove(executionGraphsOf(msg), 1)[0];
      if (!first) continue;
      const summary = summarizeProgress(first.graph, first.nodeRuns, first.terminal && {
        ...first.terminal, startedAtMs: first.openedAtMs,
      });
      if (summary.state === "running") sawRunning = true;
    }
    const graph = graphsForMove(executionGraphsOf(msg), 1)[0];
    const summary = summarizeProgress(graph.graph, graph.nodeRuns, {
      ...graph.terminal!, startedAtMs: graph.openedAtMs,
    });
    expect(sawRunning).toBe(true);
    expect(summary.state).toBe("empty");
    expect(summary.percent).toBeLessThan(100);
    expect(summary.finishedAtMs).not.toBeNull();
    expect(Object.values(graph.nodeRuns).map((run) => nodeStatusLabel(run, true))).toContain("нет результата");
  });

  it("part 14: a first attempt that failed stays a failed finished graph while the cycle runs, with no error in chat", () => {
    // Фикстура без времён (нормализатор их убирает): настоящий backend ставит finished_at каждому терминальному узлу
    const events = fixture("execution-failure-then-cycle.json").map((event) =>
      event.type === "node_run" && ["succeeded", "failed", "blocked"].includes(event.node_run.status)
        ? { ...event, node_run: { ...event.node_run, finished_at: "2026-10-06T10:00:00Z" } }
        : event,
    );
    const firstMove = events.findIndex((event) => event.type === "dynamic_move");
    // К14.4: до первого хода цикла граф закрыт по фактам узлов, без терминального события и без typed_error
    const before = replay(events.slice(0, firstMove));
    const [attempt] = executionGraphsOf(before);
    const live = summarizeProgress(attempt.graph, attempt.nodeRuns, attempt.terminal);
    expect(attempt.move).toBeNull();
    expect(attempt.terminal).toBeUndefined();
    expect(live.state).toBe("failed");
    expect(live.running).toBe(0);
    expect(live.finishedAtMs).not.toBeNull(); // таймер остановлен
    expect(before.typedErrors ?? []).toEqual([]);

    // К14.5: на выходе — графы хода идут следом, ошибки первой попытки нет, ответ один
    const after = replay(events);
    const graphs = executionGraphsOf(after);
    expect(graphs.map((item) => item.move)).toEqual([null, 1]);
    const [first, second] = graphs.map((item) =>
      summarizeProgress(item.graph, item.nodeRuns, item.terminal && { ...item.terminal, startedAtMs: item.openedAtMs }),
    );
    expect(first.state).toBe("failed");
    expect(second.state).toBe("succeeded");
    expect(after.typedErrors ?? []).toEqual([]);
    expect(after.content).toBe("Показатели получены.");
  });

  it.each(["succeeded", "partial", "failed", "cancelled"] as const)(
    "replays S0 terminal %s with no node_run as a finite non-running graph",
    (status) => {
      vi.spyOn(Date, "now").mockReturnValueOnce(1_000).mockReturnValue(2_500);
      const source = fixture("workspace-cycle.json");
      const graphOnly = source.slice(0, 2);
      let msg = replay(graphOnly);
      msg = applyChatEvent(msg, { type: "done", status });
      const graph = executionGraphsOf(msg)[0];
      const summary = summarizeProgress(graph.graph, graph.nodeRuns, {
        ...graph.terminal!, startedAtMs: graph.openedAtMs,
      });
      expect(summary.state).not.toBe("running");
      expect(summary.finishedAtMs! - summary.startedAtMs!).toBe(1_500);
      expect(graph.graph.nodes.map(() => nodeStatusLabel(undefined, true))).toEqual([
        "не выполнялся", "не выполнялся", "не выполнялся",
      ]);
    },
  );

  it("closes an old persisted graph when the saved answer already has a typed terminal error", () => {
    const live = replay(fixture("workspace-cycle.json").slice(0, 2));
    const persisted = {
      ...live,
      typedErrors: [{ code: "metric_not_found", message: "Показатель не найден", retryable: false, details: {} }],
    };

    const graph = executionGraphsOf(persisted)[0];

    expect(graph.terminal?.status).toBe("failed");
  });

  it("closes an inactive persisted graph whose historical done event was not saved", () => {
    const source = fixture("workspace-cycle.json");
    const persisted = replay(source.slice(0, 2));
    const graph = executionGraphsOf(persisted)[0];
    const completed = graph.graph.nodes.find((node) => nodeToken(node.node_key) !== nodeToken(graph.graph.root))!;
    const key = nodeToken(completed.node_key);
    persisted.planGraphs = [{
      ...graph,
      nodeRuns: {
        [key]: {
          node_key: completed.node_key,
          status: "succeeded",
          attempt: 1,
          produced_evidence_ids: ["evidence-1"],
        },
      },
    }];

    expect(executionGraphsForDisplay(persisted, true)[0].terminal).toBeUndefined();

    const closed = executionGraphsForDisplay(persisted, false)[0];
    const summary = summarizeProgress(closed.graph, closed.nodeRuns, {
      ...closed.terminal!, startedAtMs: closed.openedAtMs,
    });
    expect(closed.terminal?.status).toBe("partial");
    expect(summary.state).toBe("partial");
    expect(summary.running).toBe(0);
  });

  it("uses the same recorded dev fixtures for target evidence and an empty node", () => {
    const interrupted = fixture("dev-restart-stopped.json");
    const reused = fixture("dev-case-reuse.json");
    const emptyRun = interrupted.find((event) =>
      event.type === "node_run"
      && event.node_run.node_key.atom_name === "read.value"
      && event.node_run.status === "succeeded"
    );
    const targetRun = reused.find((event) =>
      event.type === "node_run" && event.node_run.status === "succeeded"
    );

    expect(emptyRun?.type).toBe("node_run");
    expect(targetRun?.type).toBe("node_run");
    if (emptyRun?.type !== "node_run" || targetRun?.type !== "node_run") throw new Error("fixture contract");
    expect(nodeDisplayStatus(emptyRun.node_run)).toBe("empty");
    expect(nodeDisplayStatus(targetRun.node_run)).toBe("succeeded");
  });

  it.each([
    { type: "typed_error", error: { code: "terminal", message: "boom", retryable: false, details: {} }, partial_results: [] },
    { type: "error", error: "boom" },
  ] as ChatStreamEvent[])("closes an open graph on terminal $type", (terminalEvent) => {
    let msg = replay(fixture("workspace-cycle.json").slice(0, 2));
    msg = applyChatEvent(msg, terminalEvent);
    expect(executionGraphsOf(msg)[0].terminal?.status).toBe("failed");
  });

  it("keeps four moves, two graphs of move 2, and defaults to the latest available graph", () => {
    const graphs = executionGraphsOf(replay(fixture("workspace-cycle.json")));
    expect([...new Set(graphs.map((graph) => graph.move))]).toEqual([1, 2, 3]);
    expect(graphsForMove(graphs, 2)).toHaveLength(2);
    expect(graphs.at(-1)?.move).toBe(3);
  });

  it("does not copy the latest graph into the legacy planGraph/nodeRuns fields", () => {
    const msg = replay(fixture("empty-read-then-give-up.json"));

    expect(msg.planGraphs).toHaveLength(2);
    expect(msg.planGraph).toBeUndefined();
    expect(msg.nodeRuns ?? {}).toEqual({});
  });

  it("resetForReplay also drops a legacy single graph so a replay does not flash a stale one", () => {
    const modern = replay(fixture("workspace-cycle.json"));
    const last = modern.planGraphs!.at(-1)!;
    const legacy: TextMessage = { ...message(), planGraph: last.graph, nodeRuns: last.nodeRuns };

    expect(executionGraphsOf(legacy)).toHaveLength(1);
    expect(executionGraphsOf(resetForReplay(legacy))).toEqual([]);
  });

  it("resetForReplay restores moves, author attempts and graphs without duplication", () => {
    vi.spyOn(Date, "now").mockReturnValue(20_000);
    const events = fixture("workspace-cycle.json");
    const once = replay(events);
    const twice = events.reduce((msg, event) => applyChatEvent(msg, event), resetForReplay(once));
    expect(twice.dynamicTrace).toEqual(once.dynamicTrace);
    expect(twice.planGraphs).toEqual(once.planGraphs);
  });

  it("reads the historical single-planGraph/common-nodeRuns format", () => {
    const events = fixture("workspace-cycle.json");
    const modern = replay(events);
    const last = modern.planGraphs!.at(-1)!;
    const legacy: TextMessage = {
      ...message(), planGraph: last.graph, nodeRuns: last.nodeRuns, dynamicTrace: modern.dynamicTrace,
    };
    const restored = executionGraphsOf(legacy);
    expect(restored).toHaveLength(1);
    expect(restored[0].graph.graph_hash).toBe("graph-d");
    expect(restored[0].move).toBe(3);
  });

  it("accounts for every NodeRun field published by backend as handled or explicitly ignored", () => {
    const nodeRunEvent = fixture("workspace-cycle.json").find((event) => event.type === "node_run")!;
    const published = Object.keys(nodeRunEvent.node_run).sort();
    expect([...NODE_RUN_HANDLED_FIELDS, ...NODE_RUN_IGNORED_FIELDS].sort()).toEqual(published);
  });

  it("stores only the NodeRun fields the workspace reads, so a 627-node graph stays small", () => {
    const source = fixture("empty-read-then-give-up.json");
    const planGraph = source.find((event) => event.type === "plan_graph")!;
    const sample = source.find((event) => event.type === "node_run" && event.node_run.status === "succeeded") as Extract<ChatStreamEvent, { type: "node_run" }>;
    const nodes = Array.from({ length: 627 }, (_, index) => ({
      ...planGraph.nodes[3],
      node_key: {
        atom_name: "read.value", atom_version: "1.0.0",
        args_fingerprint: `c${index}|period=6M 2026|basis=x|entity=ent-abcdef`,
      },
    }));
    let msg = applyChatEvent(message(), {
      ...planGraph, graph_hash: "big", nodes, root: nodes[0].node_key,
    });
    let fullChars = 0;
    for (const node of nodes) {
      const nodeRun = { ...sample.node_run, node_key: node.node_key, produced_evidence_ids: [] };
      fullChars += JSON.stringify(nodeRun).length;
      msg = applyChatEvent(msg, { type: "node_run", plan_graph_hash: "big", node_run: nodeRun });
    }
    const stored = Object.values(msg.planGraphs![0].nodeRuns);

    expect(stored).toHaveLength(627);
    for (const run of stored) {
      for (const field of NODE_RUN_IGNORED_FIELDS) expect(run).not.toHaveProperty(field);
    }
    const storedChars = JSON.stringify(stored).length;
    expect(storedChars).toBeLessThan(fullChars * 0.8);
    // Было 489 КБ; остальное — топология графа (узлы и их `requires`), не NodeRun.
    expect(JSON.stringify(msg).length).toBeLessThan(450_000);
  });

  it("caps a saved message at a documented number of graphs", () => {
    const source = fixture("empty-read-then-give-up.json").filter((event) =>
      ["dynamic_move", "plan_graph", "node_run", "author_attempt"].includes(event.type)
    );
    let msg = message();
    for (let cycle = 0; cycle < 20; cycle += 1) {
      for (const event of source) {
        if (event.type === "dynamic_move") {
          msg = applyChatEvent(msg, { ...event, move: event.move + cycle * 3 });
        } else if (event.type === "plan_graph") {
          msg = applyChatEvent(msg, { ...event, graph_hash: `${event.graph_hash}-${cycle}` });
        } else if (event.type === "node_run") {
          msg = applyChatEvent(msg, { ...event, plan_graph_hash: `${event.plan_graph_hash}-${cycle}` });
        } else {
          msg = applyChatEvent(msg, event);
        }
      }
    }
    expect(msg.planGraphs).toHaveLength(MAX_EXECUTION_GRAPHS_PER_MESSAGE);
    const storedChars = JSON.stringify(msg).length;
    if (process.env.AIFA_REPORT_MEASUREMENTS === "1") console.info(`K10.7 ${MAX_EXECUTION_GRAPHS_PER_MESSAGE} graphs: ${storedChars} chars`);
    expect(storedChars).toBeLessThan(512_000);
    const project = { id: "with-graphs", updated_at: 1, messages: [msg] } as Project;
    expect(__fitToBudgetБыстро([project], 1_572_864).map((item) => item.id)).toEqual(["with-graphs"]);
  });

  it("applies a graph choice only to the message it was made in", () => {
    const graphs = executionGraphsOf(replay(fixture("workspace-cycle.json")));
    const chosen = { messageId: "assistant-1", move: 2, graphId: graphsForMove(graphs, 2)[0].id };

    const same = resolveGraphView(graphs, "assistant-1", chosen);
    expect(same.selected?.id).toBe(chosen.graphId);
    expect(same.moveGraphs).toHaveLength(2);

    // Нумерация ходов начинается заново в каждом сообщении: выбор «ход 2» из прошлого
    // сообщения не должен подменять у нового сообщения граф по умолчанию (последний).
    const other = resolveGraphView(graphs, "assistant-2", chosen);
    expect(other.selected?.id).toBe(graphs.at(-1)?.id);
    expect(other.selected?.move).toBe(3);
  });

  it("falls back to the latest graph when the chosen move has no graphs and counts graphs per move", () => {
    const graphs = executionGraphsOf(replay(fixture("workspace-cycle.json")));
    const view = resolveGraphView(graphs, "assistant-1", { messageId: "assistant-1", move: 4, graphId: null });
    expect(view.selected?.move).toBe(3);
    expect(view.graphCounts).toEqual({ 1: 1, 2: 2, 3: 1 });
    expect(resolveGraphView([], "assistant-1", null)).toEqual({ selected: null, moveGraphs: [], graphCounts: {} });
  });

  it("shows graphs only of the message whose planner trace is on screen", () => {
    const older = replay(fixture("workspace-cycle.json"));
    // Новое сообщение: ход планировщика есть, графа нет (отказ на первом ходу).
    const newer = { ...message(), id: "assistant-2", dynamicTrace: { moves: [], attempts: [] } } as unknown as TextMessage;
    const messages = [older, newer];
    const graphMessage = graphMessageForWorkspace(messages, null);
    const traceMessage = newer;

    expect(graphMessage?.id).toBe(older.id);
    const owner = graphOwnerMessage(traceMessage, graphMessage);
    expect(owner?.id).toBe(newer.id);
    expect(executionGraphsOf(owner!)).toEqual([]);
    expect(graphOwnerMessage(null, graphMessage)?.id).toBe(older.id);
  });

  it("does not present the previous message graph as the active message graph", () => {
    const previous = replay(fixture("workspace-cycle.json"));
    const active = { ...message(), id: "assistant-2", dynamicTrace: { moves: [] } };
    expect(graphMessageForWorkspace([previous, active], active.id)).toBeNull();
    expect(graphMessageForWorkspace([previous, active], null)?.id).toBe(previous.id);
  });
});
