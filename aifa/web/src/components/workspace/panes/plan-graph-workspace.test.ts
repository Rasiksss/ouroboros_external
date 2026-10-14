import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { PlanGraphWorkspace, layoutGraph } from "@/components/workspace/panes/plan-graph-workspace";
import type { NodeKeyContract, NodeRunContract, PlanGraphContract } from "@/lib/api";
import type { ChatStreamEvent } from "@/lib/api";
import { executionGraphsOf, graphsForMove } from "@/lib/execution-graphs";
import { applyChatEvent, type TextMessage } from "@/lib/projects";

/** Срез 29a, §4.6 — критерии приёмки полноценного DAG в рабочей области.
 * `renderToStaticMarkup` (тот же приём, что `typed-result-view.test.ts`):
 * tooltip здесь CSS-управляемый (виден по `:hover`/`:focus-within`), поэтому
 * его текст присутствует в статической разметке всегда — без симуляции
 * наведения/фокуса можно проверить, что hover и keyboard focus покажут
 * ОДНО И ТО ЖЕ описание (один и тот же DOM-узел, а не два разных рендера).
 */

function key(atom_name: string, args_fingerprint: string): NodeKeyContract {
  return { atom_name, atom_version: "1.0.0", args_fingerprint };
}

const LOCATE = key("locate", "concept=SALES_REVENUE");
const READ_VALUE = key("read.value", "concept=SALES_REVENUE");
const NORM_TIME = key("norm.time", "concept=SALES_REVENUE");
const OUT_FORM = key("out.form", "role=dgr");

const LINEAR_GRAPH: PlanGraphContract = {
  graph_hash: "hash-linear",
  root: READ_VALUE,
  nodes: [
    { node_key: LOCATE, produces: ["CandidateLocator:SALES_REVENUE"], requires: [], sandbox_profile: "read", status: "pending" },
    { node_key: READ_VALUE, produces: ["ExtractedMetric:SALES_REVENUE"], requires: [LOCATE], sandbox_profile: "model", status: "pending" },
  ],
};

const BRANCHING_GRAPH: PlanGraphContract = {
  graph_hash: "hash-branching",
  root: OUT_FORM,
  nodes: [
    { node_key: LOCATE, produces: ["CandidateLocator:SALES_REVENUE"], requires: [], sandbox_profile: "read", status: "pending" },
    { node_key: READ_VALUE, produces: ["ExtractedMetric:SALES_REVENUE"], requires: [LOCATE], sandbox_profile: "model", status: "pending" },
    { node_key: NORM_TIME, produces: ["Verified:time"], requires: [READ_VALUE], sandbox_profile: "compute", status: "pending" },
    { node_key: OUT_FORM, produces: ["FormOutput"], requires: [READ_VALUE, NORM_TIME], sandbox_profile: "compute", status: "pending" },
  ],
};

function run(k: NodeKeyContract, status: NodeRunContract["status"], extra: Partial<NodeRunContract> = {}): NodeRunContract {
  return { node_run_id: `${k.atom_name}-run`, node_key: k, run_id: "run-1", status, attempt: 1, ...extra };
}

function eventFixture(name: string): ChatStreamEvent[] {
  return JSON.parse(readFileSync(resolve(process.cwd(), "..", "tests", "dynamic_run", "fixtures", "sse", name), "utf8"));
}

function replayFixture(events: ChatStreamEvent[]): TextMessage {
  return events.reduce(
    (message, event) => applyChatEvent(message, event),
    { id: "fixture", role: "assistant", content: "", created_at: 1 } as TextMessage,
  );
}

function renderExecutionGraph(item: ReturnType<typeof executionGraphsOf>[number]): string {
  return renderToStaticMarkup(createElement(PlanGraphWorkspace, {
    graph: item.graph,
    nodeRuns: item.nodeRuns,
    terminal: item.terminal ? { ...item.terminal, startedAtMs: item.openedAtMs } : undefined,
  }));
}

describe("PlanGraphWorkspace", () => {
  it("shows every node and edge before any node_run arrives", () => {
    const html = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: LINEAR_GRAPH }));
    expect(html).toContain('data-node-status="pending"');
    // Обе pending-записи присутствуют — весь topology виден сразу.
    expect((html.match(/ data-node-status="pending"/g) ?? []).length).toBe(2);
    // Одно ребро locate -> read.value.
    expect((html.match(/ data-edge-state="/g) ?? []).length).toBe(1);
    expect(html).toContain('data-node-root="true"');
  });

  it("distinguishes a linear chain from a branching DAG by edge count", () => {
    const linear = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: LINEAR_GRAPH }));
    const branching = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: BRANCHING_GRAPH }));
    const edges = (html: string) => (html.match(/ data-edge-state="/g) ?? []).length;
    expect(edges(linear)).toBe(1);
    // locate->read.value, read.value->norm.time, read.value->out.form,
    // norm.time->out.form: 4 ребра — больше, чем у линейной цепочки.
    expect(edges(branching)).toBe(4);
    expect(edges(branching)).toBeGreaterThan(edges(linear));
  });

  it("labels compound scenarios and shows their dependency direction", () => {
    const compound: PlanGraphContract = {
      ...BRANCHING_GRAPH,
      scenarios: [
        { goal_ref: "profit", goal_type: "extract_registered_metric", root: READ_VALUE, depends_on: [] },
        { goal_ref: "margin", goal_type: "calculate_registered_metric", root: OUT_FORM, depends_on: ["profit"] },
      ],
    };
    const html = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: compound }));
    expect(html).toContain("data-compound-plan");
    expect(html).toContain("profit: extract_registered_metric");
    expect(html).toContain("margin: calculate_registered_metric ← profit");
  });

  it("renders running grey, succeeded emerald and failed maroon without relying on color alone", () => {
    const nodeRuns: Record<string, NodeRunContract> = {
      "locate|1.0.0|concept=SALES_REVENUE": run(LOCATE, "succeeded"),
      "read.value|1.0.0|concept=SALES_REVENUE": run(READ_VALUE, "running"),
    };
    const html = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: LINEAR_GRAPH, nodeRuns }));
    expect(html).toContain('data-node-status="succeeded"');
    expect(html).toContain('data-node-status="running"');
    // Текстовый статус — не только цвет.
    expect(html).toContain("готово");
    expect(html).toContain("выполняется");
    expect(html).toContain("--pg-success");

    const failedRuns: Record<string, NodeRunContract> = {
      "read.value|1.0.0|concept=SALES_REVENUE": run(READ_VALUE, "failed", { error: "источник не найден" }),
    };
    const failedHtml = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: LINEAR_GRAPH, nodeRuns: failedRuns }));
    expect(failedHtml).toContain('data-node-status="failed"');
    expect(failedHtml).toContain("ошибка");
    expect(failedHtml).toContain("источник не найден");
  });

  it("keeps topology identical across a running -> succeeded status transition", () => {
    const running = renderToStaticMarkup(createElement(PlanGraphWorkspace, {
      graph: LINEAR_GRAPH,
      nodeRuns: { "read.value|1.0.0|concept=SALES_REVENUE": run(READ_VALUE, "running") },
    }));
    const succeeded = renderToStaticMarkup(createElement(PlanGraphWorkspace, {
      graph: LINEAR_GRAPH,
      nodeRuns: { "read.value|1.0.0|concept=SALES_REVENUE": run(READ_VALUE, "succeeded") },
    }));
    // Позиции узлов (left/top в style) не сдвигаются между статусами.
    const positions = (html: string) => [...html.matchAll(/left:\s*(\d+)px;top:\s*(\d+)px/g)].map((m) => `${m[1]},${m[2]}`);
    expect(positions(running)).toEqual(positions(succeeded));
    expect(positions(running).length).toBe(2);
  });

  it("shows the same hover/focus description for a node — one DOM node, not two renderers", () => {
    const html = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: LINEAR_GRAPH }));
    expect(html).toContain("Поиск источника");
    expect(html).toContain("Находит в документе кандидаты источников нужного показателя.");
    expect(html).toContain('role="tooltip"');
  });

  it("falls back to the technical atom_name for an unknown atom without breaking the graph", () => {
    const unknownKey = key("future.atom", "x=1");
    const graph: PlanGraphContract = {
      graph_hash: "hash-unknown",
      root: unknownKey,
      nodes: [{ node_key: unknownKey, produces: ["Unknown"], requires: [], sandbox_profile: "read", status: "pending" }],
    };
    const html = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph }));
    expect(html).toContain("future.atom");
  });

  // ---------------------------------------------------------------------------
  // Срез 31, П7: живой прогресс и оформление.
  it("shows progress in the header: n of N, percent, a progressbar and the running state", () => {
    const nodeRuns: Record<string, NodeRunContract> = {
      "locate|1.0.0|concept=SALES_REVENUE": run(LOCATE, "succeeded"),
      "read.value|1.0.0|concept=SALES_REVENUE": run(READ_VALUE, "running"),
    };
    const html = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: BRANCHING_GRAPH, nodeRuns }));
    expect(html).toContain('data-plan-state="running"');
    expect(html).toContain("Выполняется · 1 из 4");
    expect(html).toContain('role="progressbar"');
    expect(html).toContain('aria-valuenow="25"');
    expect(html).toContain("width:25%");
    expect(html).toContain("всего узлов: 4");
  });

  it("marks a finished plan as done at 100% and a failed one with the error headline", () => {
    const allDone: Record<string, NodeRunContract> = {};
    for (const n of LINEAR_GRAPH.nodes) {
      allDone[`${n.node_key.atom_name}|1.0.0|${n.node_key.args_fingerprint}`] = run(n.node_key, "succeeded");
    }
    const done = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: LINEAR_GRAPH, nodeRuns: allDone }));
    expect(done).toContain('data-plan-state="succeeded"');
    expect(done).toContain("Готово");
    expect(done).toContain("width:100%");

    const failed = renderToStaticMarkup(createElement(PlanGraphWorkspace, {
      graph: LINEAR_GRAPH,
      nodeRuns: { "read.value|1.0.0|concept=SALES_REVENUE": run(READ_VALUE, "failed", { error: "нет источника" }) },
    }));
    expect(failed).toContain('data-plan-state="failed"');
    expect(failed).toContain("Ошибка · 1 из 2");
  });

  it("renders an empty succeeded root as «нет результата» and not 100 percent", () => {
    const nodeRuns: Record<string, NodeRunContract> = {
      "locate|1.0.0|concept=SALES_REVENUE": run(LOCATE, "succeeded", { produced_evidence_ids: ["source"] }),
      "read.value|1.0.0|concept=SALES_REVENUE": run(READ_VALUE, "succeeded", { produced_evidence_ids: [] }),
    };
    const html = renderToStaticMarkup(createElement(PlanGraphWorkspace, {
      graph: LINEAR_GRAPH, nodeRuns, terminal: { status: "succeeded", atMs: 10_000, startedAtMs: 9_000 },
    }));
    expect(html).toContain("Завершено без результата · 1 из 2 узлов с результатом");
    expect(html).toContain("нет результата");
    expect(html).not.toContain("width:100%");
    expect(html).not.toContain('role="progressbar"');
  });

  it("renders terminal nodes without node_run as «не выполнялся» and stops the timer", () => {
    const html = renderToStaticMarkup(createElement(PlanGraphWorkspace, {
      graph: LINEAR_GRAPH, terminal: { status: "failed", atMs: 10_000, startedAtMs: 7_500 },
    }));
    expect(html).toContain("Ошибка");
    expect((html.match(/не выполнялся/g) ?? []).length).toBeGreaterThanOrEqual(2);
    expect(html).toContain("2,5 с");
    expect(html).not.toContain("Выполняется");
  });

  it("renders the S1 backend fixture as completed without result", () => {
    const message = replayFixture(eventFixture("empty-read-then-give-up.json"));
    const html = renderExecutionGraph(graphsForMove(executionGraphsOf(message), 1)[0]);
    expect(html).toContain("Завершено без результата");
    expect(html).toContain("нет результата");
    expect(html).not.toContain("width:100%");
  });

  it("renders the S0 terminal fixture without a running timer or queued nodes", () => {
    let message = replayFixture(eventFixture("workspace-cycle.json").slice(0, 2));
    message = applyChatEvent(message, { type: "done", status: "failed" });
    const html = renderExecutionGraph(executionGraphsOf(message)[0]);
    expect(html).toContain("Ошибка");
    expect(html).toContain("не выполнялся");
    expect(html).not.toContain("Выполняется");
    expect(html).not.toMatch(/<div[^>]+data-node-status="pending"/);
  });

  it("lights an edge up once its source is done and makes it flow while the target runs", () => {
    const html = renderToStaticMarkup(createElement(PlanGraphWorkspace, {
      graph: LINEAR_GRAPH,
      nodeRuns: {
        "locate|1.0.0|concept=SALES_REVENUE": run(LOCATE, "succeeded"),
        "read.value|1.0.0|concept=SALES_REVENUE": run(READ_VALUE, "running"),
      },
    }));
    expect(html).toContain('data-edge-state="active"');
    const doneHtml = renderToStaticMarkup(createElement(PlanGraphWorkspace, {
      graph: LINEAR_GRAPH,
      nodeRuns: {
        "locate|1.0.0|concept=SALES_REVENUE": run(LOCATE, "succeeded"),
        "read.value|1.0.0|concept=SALES_REVENUE": run(READ_VALUE, "succeeded"),
      },
    }));
    expect(doneHtml).toContain('data-edge-state="done"');
    const idle = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: LINEAR_GRAPH }));
    expect(idle).toContain('data-edge-state="idle"');
  });

  it("gives every status its own icon so state is not carried by colour alone", () => {
    const icons = (status: NodeRunContract["status"]) => renderToStaticMarkup(createElement(PlanGraphWorkspace, {
      graph: LINEAR_GRAPH,
      nodeRuns: { "read.value|1.0.0|concept=SALES_REVENUE": run(READ_VALUE, status) },
    }));
    expect(icons("succeeded")).toContain('data-icon="check"');
    expect(icons("failed")).toContain('data-icon="cross"');
    expect(icons("running")).toContain('data-icon="spinner"');
    expect(icons("skipped")).toContain('data-icon="skip"');
    expect(icons("ready")).toContain('data-icon="ready"');
  });

  it("badges a plan composed by the planner agent and a deterministic one differently", () => {
    const dynamicGraph: PlanGraphContract = { ...LINEAR_GRAPH, planner_version: "dynamic-1.0.0" };
    const dynamicHtml = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: dynamicGraph }));
    expect(dynamicHtml).toContain('data-plan-kind="dynamic"');
    expect(dynamicHtml).toContain("собран агентом");
    const autoHtml = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: LINEAR_GRAPH }));
    expect(autoHtml).toContain('data-plan-kind="deterministic"');
    expect(autoHtml).toContain("автоплан");
  });

  it("switches to compact cards for very large graphs and keeps every node visible", () => {
    const big: PlanGraphContract = {
      graph_hash: "hash-big",
      root: key("out.report", "root"),
      nodes: Array.from({ length: 60 }, (_, i) => ({
        node_key: key("read.value", `concept=C${i}`),
        produces: [`ExtractedMetric:C${i}`],
        requires: [],
        sandbox_profile: "model",
        status: "pending" as const,
      })),
    };
    const html = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: big }));
    expect((html.match(/ data-node-status="/g) ?? []).length).toBe(60);
    expect(html).toContain("width:156px");
    expect(html).toContain("всего узлов: 60");
  });

  // Заполнение формы: документ → N × (locate → read.value) → итоговая форма. N = 190 даёт 382 узла
  // и ~570 рёбер; в работе таких графов 500–600 узлов, и интерфейс на них лагал.
  const formFill = (fields: number): PlanGraphContract => {
    const doc = key("ingest.document", "doc");
    const locates = Array.from({ length: fields }, (_, i) => key("locate", `concept=F${i}`));
    const reads = Array.from({ length: fields }, (_, i) => key("read.value", `concept=F${i}`));
    const root = key("out.form", "role=dgr");
    return {
      graph_hash: `hash-form-${fields}`,
      root,
      nodes: [
        { node_key: doc, produces: ["Doc"], requires: [], sandbox_profile: "read", status: "pending" as const },
        ...locates.map((k, i) => ({
          node_key: k, produces: [`L${i}`], requires: [doc], sandbox_profile: "read", status: "pending" as const,
        })),
        ...reads.map((k, i) => ({
          node_key: k, produces: [`V${i}`], requires: [locates[i]], sandbox_profile: "model", status: "pending" as const,
        })),
        { node_key: root, produces: ["Form"], requires: reads, sandbox_profile: "compute", status: "pending" as const },
      ],
    };
  };

  describe("large graphs (form filling, hundreds of nodes)", () => {
    const html = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: formFill(190) }));

    it("keeps every node and every edge visible", () => {
      expect((html.match(/ data-node-status="/g) ?? []).length).toBe(382);
      expect((html.match(/class="aifa-pg-edge"/g) ?? []).length).toBe(190 * 2 + 190);
      expect(html).toContain("всего узлов: 382");
    });

    it("is marked as a large plan, a small one is not", () => {
      expect(html).toContain('data-plan-scale="large"');
      expect(renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: BRANCHING_GRAPH }))).toContain(
        'data-plan-scale="normal"',
      );
    });

    it("does not keep a hidden tooltip subtree inside every card", () => {
      expect(html).not.toContain('role="tooltip"');
    });

    it("moves the node description into the native title so nothing is lost", () => {
      expect(html).toMatch(/<div[^>]*title="[^"]*"[^>]*data-node-status="pending"/);
    });

    it("draws edges without per-edge arrow markers", () => {
      expect(html).not.toContain("marker-end");
      expect(html).not.toContain("<marker");
    });

    it("keeps arrows and tooltips on a normal graph", () => {
      const normal = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: BRANCHING_GRAPH }));
      expect(normal).toContain("marker-end");
      expect(normal).toContain('role="tooltip"');
    });

    it("switches off continuous animations in a large plan", () => {
      expect(html).toMatch(/\.aifa-pg\[data-plan-scale="large"\] \.aifa-pg-edge\[data-edge-state="active"\][^}]*animation: none/);
      expect(html).toMatch(/\.aifa-pg\[data-plan-scale="large"\] \.aifa-pg-node[^}]*transition: none/);
    });
  });

  // ---------------------------------------------------------------------------
  // Живая проверка на dev (24.09.2026): правая панель ~500 px, цепочка из 7 узлов
  // «слева направо» — ~1960 px, виден только первый узел. Узкая длинная цепочка
  // теперь ложится сверху вниз; широкий граф остаётся горизонтальным.
  const chain = (length: number): PlanGraphContract => {
    const keys = Array.from({ length }, (_, i) => key("read.value", `concept=STEP${i}`));
    return {
      graph_hash: `hash-chain-${length}`,
      root: keys[length - 1],
      nodes: keys.map((k, i) => ({
        node_key: k, produces: [`X${i}`], requires: i === 0 ? [] : [keys[i - 1]],
        sandbox_profile: "model", status: "pending" as const,
      })),
    };
  };

  it("lays a long chain out top-to-bottom so the whole plan fits a narrow pane", () => {
    const layout = layoutGraph(chain(7));
    expect(layout.orientation).toBe("vertical");
    expect(new Set(layout.nodes.map((n) => n.x)).size).toBe(1);
    const tops = layout.nodes.map((n) => n.y);
    expect(tops).toEqual([...tops].sort((a, b) => a - b));
    expect(new Set(tops).size).toBe(7);
    expect(layout.width).toBeLessThan(300);
    expect(renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: chain(7) }))).toContain(
      'data-plan-orientation="vertical"',
    );
  });

  it("draws vertical edges from the bottom of a node to the top of the next one", () => {
    const layout = layoutGraph(chain(2));
    const [a, b] = layout.nodes;
    const html = renderToStaticMarkup(createElement(PlanGraphWorkspace, { graph: chain(2) }));
    const cx = a.x + layout.size.w / 2;
    expect(html).toContain(`M ${cx} ${a.y + layout.size.h} C ${cx} `);
    expect(html).toContain(`${b.x + layout.size.w / 2} ${b.y}"`);
  });

  it("keeps a wide shallow graph horizontal (more columns than rows would only make it wider)", () => {
    const root = key("out.report", "root");
    const leaves = Array.from({ length: 6 }, (_, i) => key("read.value", `concept=C${i}`));
    const wide: PlanGraphContract = {
      graph_hash: "hash-wide",
      root,
      nodes: [
        ...leaves.map((k, i) => ({
          node_key: k, produces: [`X${i}`], requires: [], sandbox_profile: "model", status: "pending" as const,
        })),
        { node_key: root, produces: ["Report"], requires: leaves, sandbox_profile: "compute", status: "pending" as const },
      ],
    };
    expect(layoutGraph(wide).orientation).toBe("horizontal");
  });

  it("centers a narrower layer under the widest one in the vertical layout", () => {
    const a = key("ingest.document", "doc");
    const b1 = key("locate", "concept=A");
    const b2 = key("locate", "concept=B");
    const c = key("calc.formula", "concept=F");
    const graph: PlanGraphContract = {
      graph_hash: "hash-diamond",
      root: c,
      nodes: [
        { node_key: a, produces: ["Doc"], requires: [], sandbox_profile: "read", status: "pending" },
        { node_key: b1, produces: ["A"], requires: [a], sandbox_profile: "read", status: "pending" },
        { node_key: b2, produces: ["B"], requires: [a], sandbox_profile: "read", status: "pending" },
        { node_key: c, produces: ["F"], requires: [b1, b2], sandbox_profile: "compute", status: "pending" },
      ],
    };
    const layout = layoutGraph(graph);
    expect(layout.orientation).toBe("vertical");
    const xs = Object.fromEntries(layout.nodes.map((n) => [n.node.node_key.args_fingerprint, n.x]));
    expect(xs["doc"]).toBe(xs["concept=F"]);
    expect(xs["doc"]).toBeGreaterThan(xs["concept=A"]);
    expect(xs["doc"]).toBeLessThan(xs["concept=B"]);
  });
});
