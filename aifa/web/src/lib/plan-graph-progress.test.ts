import { describe, expect, it } from "vitest";

import type { NodeKeyContract, NodeRunContract, PlanGraphContract } from "@/lib/api";
import {
  atomCategory,
  edgeState,
  formatDuration,
  graphHeadline,
  isDynamicPlan,
  nodeDisplayStatus,
  nodeStatusLabel,
  nodeToken,
  showsProgressBar,
  summarizeProgress,
} from "@/lib/plan-graph-progress";

function key(atom_name: string, fp = "x"): NodeKeyContract {
  return { atom_name, atom_version: "1.0.0", args_fingerprint: fp };
}

const A = key("locate");
const B = key("read.value");
const C = key("norm.time");

const GRAPH: PlanGraphContract = {
  graph_hash: "h",
  root: C,
  nodes: [
    { node_key: A, produces: [], requires: [], sandbox_profile: "p", status: "pending" },
    { node_key: B, produces: [], requires: [A], sandbox_profile: "p", status: "pending" },
    { node_key: C, produces: [], requires: [B], sandbox_profile: "p", status: "pending" },
  ],
};

function run(k: NodeKeyContract, status: NodeRunContract["status"], extra: Partial<NodeRunContract> = {}): NodeRunContract {
  return {
    node_run_id: "r", node_key: k, run_id: "run", status, attempt: 1,
    produced_evidence_ids: status === "succeeded" ? ["evidence-1"] : [],
    ...extra,
  };
}

describe("summarizeProgress", () => {
  it("is idle with 0% before any node_run arrives", () => {
    const s = summarizeProgress(GRAPH);
    expect(s).toMatchObject({ total: 3, done: 0, running: 0, pending: 3, percent: 0, state: "idle" });
    expect(s.startedAtMs).toBeNull();
  });

  it("counts succeeded, running and pending nodes live", () => {
    const runs = { [nodeToken(A)]: run(A, "succeeded"), [nodeToken(B)]: run(B, "running") };
    const s = summarizeProgress(GRAPH, runs);
    expect(s).toMatchObject({ done: 1, running: 1, pending: 1, percent: 33, state: "running" });
    expect(s.finishedAtMs).toBeNull();
  });

  it("is running between two nodes: some done, none currently running", () => {
    const runs = { [nodeToken(A)]: run(A, "succeeded") };
    expect(summarizeProgress(GRAPH, runs).state).toBe("running");
  });

  it("is succeeded at 100% once every node succeeded, with the elapsed window", () => {
    const runs = {
      [nodeToken(A)]: run(A, "succeeded", { started_at: "2026-09-24T10:00:00Z", finished_at: "2026-09-24T10:00:02Z" }),
      [nodeToken(B)]: run(B, "succeeded", { started_at: "2026-09-24T10:00:02Z", finished_at: "2026-09-24T10:00:05Z" }),
      [nodeToken(C)]: run(C, "succeeded", { started_at: "2026-09-24T10:00:05Z", finished_at: "2026-09-24T10:00:06Z" }),
    };
    const s = summarizeProgress(GRAPH, runs);
    expect(s).toMatchObject({ done: 3, percent: 100, state: "succeeded" });
    expect((s.finishedAtMs! - s.startedAtMs!) / 1000).toBe(6);
  });

  it("is failed as soon as a node failed and nothing is running", () => {
    const runs = { [nodeToken(A)]: run(A, "succeeded"), [nodeToken(B)]: run(B, "failed", { error: "boom" }) };
    const s = summarizeProgress(GRAPH, runs);
    expect(s).toMatchObject({ done: 1, failed: 1, state: "failed" });
  });

  it("stays running while a failed branch coexists with a running node", () => {
    const runs = { [nodeToken(A)]: run(A, "failed"), [nodeToken(B)]: run(B, "running") };
    expect(summarizeProgress(GRAPH, runs).state).toBe("running");
  });

  it("is safe for an empty graph", () => {
    expect(summarizeProgress({ ...GRAPH, nodes: [] })).toMatchObject({ total: 0, percent: 0, state: "idle" });
  });

  it("projects a succeeded node without evidence as «нет результата», not «готово»", () => {
    const runs = {
      [nodeToken(A)]: run(A, "succeeded", { produced_evidence_ids: [] }),
      [nodeToken(B)]: run(B, "succeeded", { produced_evidence_ids: ["case-evidence"] }),
    };
    expect(nodeDisplayStatus(runs[nodeToken(A)])).toBe("empty");
    expect(nodeStatusLabel(runs[nodeToken(A)])).toBe("нет результата");
    expect(nodeDisplayStatus(runs[nodeToken(B)])).toBe("succeeded");
    expect(nodeStatusLabel(runs[nodeToken(B)])).toBe("готово");
  });

  it("finishes without a result when the root produced no evidence", () => {
    const runs = Object.fromEntries(GRAPH.nodes.map((node) => [
      nodeToken(node.node_key),
      run(node.node_key, "succeeded", {
        produced_evidence_ids: node.node_key === C ? [] : [node.node_key.atom_name],
      }),
    ]));
    expect(summarizeProgress(GRAPH, runs, { status: "succeeded", atMs: 10_000 })).toMatchObject({
      state: "empty", percent: 67, finishedAtMs: 10_000,
    });
  });

  it("does not call a graph with blocked nodes ready", () => {
    const runs = {
      [nodeToken(A)]: run(A, "succeeded"),
      [nodeToken(B)]: run(B, "blocked"),
      [nodeToken(C)]: run(C, "blocked"),
    };
    expect(summarizeProgress(GRAPH, runs, { status: "partial", atMs: 10_000 }).state).toBe("partial");
  });

  it("takes the finish time of a closed graph from its node runs, not from the replay clock", () => {
    const started = Date.parse("2026-10-01T10:00:00Z");
    const runs = Object.fromEntries(GRAPH.nodes.map((node, index) => [
      nodeToken(node.node_key),
      run(node.node_key, "succeeded", {
        started_at: new Date(started + index * 1000).toISOString(),
        finished_at: new Date(started + index * 1000 + 800).toISOString(),
      }),
    ]));
    // Реплей журнала после обрыва связи ставит atMs по часам браузера «сейчас».
    const replayedAt = started + 3 * 24 * 3600 * 1000;
    const summary = summarizeProgress(GRAPH, runs, { status: "succeeded", atMs: replayedAt });

    expect(summary.startedAtMs).toBe(started);
    expect(summary.finishedAtMs).toBe(started + 2800);
  });

  it("writes «n из N узлов с результатом» instead of a percent for a closed graph without a result", () => {
    const runs = {
      [nodeToken(A)]: run(A, "succeeded"),
      [nodeToken(B)]: run(B, "succeeded", { produced_evidence_ids: [] }),
      [nodeToken(C)]: run(C, "succeeded", { produced_evidence_ids: [] }),
    };
    const closed = summarizeProgress(GRAPH, runs, { status: "succeeded", atMs: 10_000 });
    expect(graphHeadline(closed)).toBe("Завершено без результата · 1 из 3 узлов с результатом");
    expect(showsProgressBar(closed.state)).toBe(false);

    const live = summarizeProgress(GRAPH, { [nodeToken(A)]: run(A, "succeeded"), [nodeToken(B)]: run(B, "running") });
    expect(graphHeadline(live)).toBe("Выполняется · 1 из 3");
    expect(showsProgressBar(live.state)).toBe(true);
    expect(graphHeadline(summarizeProgress(GRAPH, undefined, { status: "cancelled", atMs: 1 }))).toBe("Отменено");
  });

  it("keeps «Ошибка» for a closed graph whose failed node blocked its dependents", () => {
    const runs = {
      [nodeToken(A)]: run(A, "succeeded"),
      [nodeToken(B)]: run(B, "failed", { error: "boom" }),
      [nodeToken(C)]: run(C, "blocked"),
    };
    const live = summarizeProgress(GRAPH, runs);
    const closed = summarizeProgress(GRAPH, runs, { status: "succeeded", atMs: 10_000 });

    expect(live.state).toBe("failed");
    expect(closed.state).toBe("failed");
  });

  it.each(["succeeded", "partial", "failed", "cancelled"] as const)(
    "closes a graph with no node_run on terminal %s",
    (status) => {
      const summary = summarizeProgress(GRAPH, undefined, { status, atMs: 10_000 });
      expect(summary.state).not.toBe("running");
      expect(summary.finishedAtMs).toBe(10_000);
      expect(GRAPH.nodes.map(() => nodeStatusLabel(undefined, true))).toEqual([
        "не выполнялся", "не выполнялся", "не выполнялся",
      ]);
    },
  );

  it.each([
    ["succeeded", "all", "succeeded"], ["succeeded", "some", "failed"], ["succeeded", "none", "empty"],
    ["partial", "all", "partial"], ["partial", "some", "partial"], ["partial", "none", "partial"],
    ["failed", "all", "failed"], ["failed", "some", "failed"], ["failed", "none", "failed"],
    ["cancelled", "all", "cancelled"], ["cancelled", "some", "cancelled"], ["cancelled", "none", "cancelled"],
  ] as const)("projects terminal %s with %s node runs as %s", (status, coverage, expected) => {
    const allRuns = Object.fromEntries(GRAPH.nodes.map((node) => [
      nodeToken(node.node_key), run(node.node_key, "succeeded"),
    ]));
    const someRuns = {
      [nodeToken(A)]: run(A, "succeeded"),
      [nodeToken(B)]: run(B, "running"),
    };
    const runs = coverage === "all" ? allRuns : coverage === "some" ? someRuns : undefined;
    const summary = summarizeProgress(GRAPH, runs, { status, atMs: 10_000 });

    expect(summary.state).toBe(expected);
    expect(summary.running).toBe(0);
    expect(summary.pending).toBe(0);
    expect(summary.finishedAtMs).toBe(10_000);
  });

  it("handles a 627-node full analysis with normal empty reads and a produced root", () => {
    const nodes = Array.from({ length: 627 }, (_, index) => {
      const nodeKey = key(index === 626 ? "out.report" : "read.value", `node=${index}`);
      return {
        node_key: nodeKey,
        produces: [index === 626 ? "ReportOutput" : `ExtractedMetric:M${index}`],
        requires: index === 0 ? [] : [key(index === 626 ? "read.value" : "read.value", `node=${index - 1}`)],
        sandbox_profile: "test",
        status: "pending" as const,
      };
    });
    const graph = { graph_hash: "large", root: nodes.at(-1)!.node_key, nodes };
    const runs = Object.fromEntries(nodes.map((node, index) => [
      nodeToken(node.node_key),
      run(node.node_key, "succeeded", { produced_evidence_ids: index === 626 ? ["report"] : [] }),
    ]));
    const started = performance.now();
    const summary = summarizeProgress(graph, runs, { status: "succeeded", atMs: 10_000 });
    const elapsedMs = performance.now() - started;
    if (process.env.AIFA_REPORT_MEASUREMENTS === "1") console.info(`P10.3 627 nodes: ${elapsedMs.toFixed(3)} ms`);
    expect(summary).toMatchObject({ state: "succeeded", done: 1, empty: 626, failed: 0, percent: 100 });
    expect(elapsedMs).toBeLessThan(100);
  });
});

describe("edgeState", () => {
  it("lights up green once the source is done and flows while the target runs", () => {
    expect(edgeState("succeeded", "pending")).toBe("done");
    expect(edgeState("succeeded", "running")).toBe("active");
    expect(edgeState("running", "pending")).toBe("active");
    expect(edgeState("pending", "pending")).toBe("idle");
    expect(edgeState("failed", "pending")).toBe("failed");
    expect(edgeState("blocked", "pending")).toBe("failed");
  });

  it("does not draw an alarming edge after a normal empty read or a node that never ran", () => {
    // В полном анализе пустых чтений сотни — красные рёбра из каждого мешали бы читать граф.
    expect(edgeState("empty", "succeeded")).toBe("idle");
    expect(edgeState("not_run", "not_run")).toBe("idle");
    expect(edgeState("interrupted", "not_run")).toBe("failed");
  });
});

describe("atomCategory and plan kind", () => {
  it("classifies atoms for the colour hint", () => {
    expect(atomCategory("locate")).toBe("read");
    expect(atomCategory("read.value")).toBe("read");
    expect(atomCategory("calc.formula")).toBe("calc");
    expect(atomCategory("norm.unit")).toBe("verify");
    expect(atomCategory("verify.bounds")).toBe("verify");
    expect(atomCategory("out.report")).toBe("output");
    expect(atomCategory("future.atom")).toBe("other");
  });

  it("recognises a plan composed by the planner agent", () => {
    expect(isDynamicPlan({ planner_version: "dynamic-1.0.0" })).toBe(true);
    expect(isDynamicPlan({ planner_version: "1.0.0" })).toBe(false);
    expect(isDynamicPlan({})).toBe(false);
  });

  it("formats durations for a human", () => {
    expect(formatDuration(0.42)).toBe("0,4 с");
    expect(formatDuration(12.4)).toBe("12 с");
    expect(formatDuration(65)).toBe("1 мин 05 с");
    expect(formatDuration(-1)).toBe("—");
  });
});
