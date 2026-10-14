import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import type { ChatStreamEvent } from "@/lib/api";
import { applyChatEvent, turnProgress, type Project, type TextMessage } from "@/lib/projects";
import { executionGraphsOf } from "@/lib/execution-graphs";
import { nodeDisplayStatus, nodeStatusLabel } from "@/lib/plan-graph-progress";

function assistant(): TextMessage {
  return { id: "a1", role: "assistant", content: "", created_at: 2 };
}

describe("typed result integration", () => {
  it("derives progress from NodeRun and ignores model plan once graph state exists", () => {
    let message = applyChatEvent(assistant(), { type: "plan", steps: ["model prose step"] });
    message = applyChatEvent(message, {
      type: "node_run",
      plan_graph_hash: "sha256:graph",
      node_run: {
        node_run_id: "nr-1",
        node_key: { atom_name: "out.form", atom_version: "1.0.0", args_fingerprint: "abc" },
        run_id: "run-1",
        status: "running",
        attempt: 1,
      },
    });
    const project = {
      id: "p1",
      name: "test",
      created_at: 1,
      updated_at: 2,
      project_type: "generic",
      doc_ids: [],
      artifacts: [],
      messages: [{ id: "u1", role: "user", content: "go", created_at: 1 }, message],
    } as Project;

    const progress = turnProgress(project);
    expect(progress.lanes).toHaveLength(1);
    expect(progress.lanes[0].label).toBe("Граф выполнения");
    expect(progress.lanes[0].steps).toEqual(expect.arrayContaining([
      expect.objectContaining({ title: "out.form", status: "running" }),
    ]));
    expect(progress.lanes[0].steps.some((step) => step.title === "model prose step")).toBe(false);
  });

  it("keeps typed partial errors separate from prose", () => {
    const message = applyChatEvent(assistant(), {
      type: "typed_error",
      error: { code: "grounding_failed", message: "Claim has no evidence", retryable: false, details: {} },
      partial_results: [],
    });
    expect(message.typedErrors?.[0].code).toBe("grounding_failed");
    expect(message.content).toBe("");
  });

  it("uses the same node label in workspace projection and chat progress", () => {
    const key = { atom_name: "read.value", atom_version: "1.0.0", args_fingerprint: "empty" };
    let message = applyChatEvent(assistant(), {
      type: "plan_graph", graph_hash: "empty", root: key,
      nodes: [{ node_key: key, produces: ["ExtractedMetric"], requires: [], sandbox_profile: "read", status: "pending" }],
    });
    message = applyChatEvent(message, {
      type: "node_run", plan_graph_hash: "empty",
      node_run: {
        node_run_id: "empty-node", node_key: key, run_id: "empty-run",
        status: "succeeded", attempt: 1, produced_evidence_ids: [],
      },
    });
    const project = {
      id: "p", name: "test", created_at: 1, updated_at: 2, project_type: "generic",
      doc_ids: [], artifacts: [], messages: [message],
    } as Project;
    expect(turnProgress(project).lanes[0].steps[0].note).toBe(nodeStatusLabel(
      executionGraphsOf(message)[0].nodeRuns["read.value|1.0.0|empty"],
    ));
    expect(turnProgress(project).lanes[0].steps[0].note).toBe("нет результата");
  });

  it("does not paint normal empty reads red in the chat plan island", () => {
    const events = JSON.parse(fs.readFileSync(
      path.resolve(process.cwd(), "..", "tests", "dynamic_run", "fixtures", "sse", "empty-read-then-give-up.json"),
      "utf8",
    )) as ChatStreamEvent[];
    const message = events.slice(0, 23).reduce((current, event) => applyChatEvent(current, event), assistant());
    const project = {
      id: "p", name: "test", created_at: 1, updated_at: 2, project_type: "generic",
      doc_ids: [], artifacts: [], messages: [message],
    } as Project;

    const steps = turnProgress(project).lanes[0].steps;

    expect(steps.map((step) => [step.title, step.status])).toEqual([
      ["ingest.document", "done"], ["identify.classify", "done"], ["locate", "done"],
      ["read.value", "empty"], ["norm.unit", "empty"], ["norm.time", "empty"], ["verify.bounds", "empty"],
    ]);
  });

  it("closes unfinished node rows when the authoritative run terminal arrives", () => {
    const readKey = { atom_name: "read.value", atom_version: "1.0.0", args_fingerprint: "a" };
    const outKey = { atom_name: "out.form", atom_version: "1.0.0", args_fingerprint: "b" };
    let message = applyChatEvent(assistant(), {
      type: "plan_graph",
      graph_hash: "sha256:graph",
      root: outKey,
      nodes: [
        { node_key: readKey, produces: ["ExtractedMetric"], requires: [], sandbox_profile: "read", status: "pending" },
        { node_key: outKey, produces: ["FormOutput"], requires: [readKey], sandbox_profile: "compute", status: "pending" },
      ],
    });
    message = applyChatEvent(message, {
      type: "node_run",
      plan_graph_hash: "sha256:graph",
      node_run: {
        node_run_id: "nr-1",
        node_key: readKey,
        run_id: "run-1",
        status: "running",
        attempt: 1,
      },
    });
    message = applyChatEvent(message, {
      type: "node_run",
      plan_graph_hash: "sha256:graph",
      node_run: {
        node_run_id: "nr-2",
        node_key: outKey,
        run_id: "run-1",
        status: "planned",
        attempt: 1,
      },
    });

    message = applyChatEvent(message, { type: "done", status: "failed" });

    const graph = executionGraphsOf(message)[0];
    const runs = Object.values(graph.nodeRuns);
    expect(runs.map((run) => run.status)).toEqual(["running", "planned"]);
    expect(runs.map((run) => nodeDisplayStatus(run, Boolean(graph.terminal)))).toEqual([
      "interrupted", "not_run",
    ]);
    expect(graph.terminal?.status).toBe("failed");
  });

  it("contains no client-side financial calculation vocabulary", () => {
    const component = fs.readFileSync(
      path.resolve(process.cwd(), "src/components/typed-result-view.tsx"),
      "utf8",
    );
    const executable = component.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");
    expect(executable).toContain("row.display_value");
    expect(executable).not.toMatch(/EBITDA|ratio|subtotal|grandTotal|Math\.|NumberFormat|parseFloat/i);
    expect(executable).not.toMatch(/row\.value\s*[+\-*/]/);
  });
});
