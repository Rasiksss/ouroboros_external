import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { chatRunStream, type ChatStreamEvent } from "@/lib/api";
import { executionGraphsOf } from "@/lib/execution-graphs";
import { summarizeProgress } from "@/lib/plan-graph-progress";
import { applyChatEvent, resetForReplay, type TextMessage } from "@/lib/projects";

// Стык «журнал прогона → конверт A11 (`subscribe_versioned`) → `unwrapRunEvents` → редьюсер».
// Кадры — настоящий выход backend (`test_workspace_event_contract.py`), с настоящими временами
// в `node_run`: так идёт каждая перезагрузка страницы во время прогона и каждый обрыв связи.
const FRAMES = readFileSync(
  resolve(process.cwd(), "..", "finengine", "tests", "dynamic_run", "fixtures", "sse", "workspace-wire.json"), "utf8",
);
const RUN_HOOK = readFileSync("src/components/workspace/use-chat-run.ts", "utf8");

async function unwrap(): Promise<ChatStreamEvent[]> {
  const body = (JSON.parse(FRAMES) as unknown[]).map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join("");
  vi.stubGlobal("fetch", async () => new Response(body, { status: 200 }));
  const events: ChatStreamEvent[] = [];
  for await (const event of chatRunStream("wire-run")) events.push(event as ChatStreamEvent);
  return events;
}

function replay(events: ChatStreamEvent[]): TextMessage {
  const fresh: TextMessage = { id: "assistant-1", role: "assistant", content: "", created_at: 1 };
  return events.reduce((message, event) => applyChatEvent(message, event), resetForReplay(fresh));
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("replay of the run journal through the A11 envelope", () => {
  it("passes done through the message reducer before ending the live stream", () => {
    const start = RUN_HOOK.indexOf('} else if (ev.type === "done") {');
    const end = RUN_HOOK.indexOf("} else {", start + 1);
    expect(start).toBeGreaterThan(-1);
    expect(RUN_HOOK.slice(start, end)).toContain("applyChatEvent");
  });

  it("unwraps every frame kind the workspace depends on", async () => {
    const events = await unwrap();

    expect(events.filter((event) => event.type === "dynamic_move")).toHaveLength(5);
    expect(events.filter((event) => event.type === "plan_graph")).toHaveLength(3);
    expect(events.filter((event) => event.type === "node_run")).toHaveLength(6);
    expect(events.find((event) => event.type === "typed_error")).toMatchObject({
      error: { code: "dynamic_run_failed" },
    });
    expect(events.at(-1)).toEqual({ type: "done", status: "failed" });
  });

  it("restores graphs, moves and states with the finish time from NodeRun, not the replay clock", async () => {
    const events = await unwrap();
    // Часы реплея — через трое суток после прогона.
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-10-04T10:00:00Z"));
    const message = replay(events);
    const graphs = executionGraphsOf(message);
    const summaries = graphs.map((item) => summarizeProgress(
      item.graph, item.nodeRuns, { ...item.terminal!, startedAtMs: item.openedAtMs },
    ));

    expect(graphs.map((item) => item.move)).toEqual([1, 2, 3]);
    expect(graphs.map((item) => item.terminal?.status)).toEqual(["succeeded", "succeeded", "failed"]);
    expect(summaries.map((item) => item.state)).toEqual(["succeeded", "failed", "failed"]);
    // Первый граф: старт 10:00:00, последний узел закончен в 10:00:05,5.
    expect(summaries[0].finishedAtMs! - summaries[0].startedAtMs!).toBe(5_500);
    expect(message.typedErrors).toHaveLength(1);
  });

  it("does not double graphs or moves when the journal is replayed twice", async () => {
    const events = await unwrap();
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-10-04T10:00:00Z"));
    const once = replay(events);
    const twice = events.reduce((message, event) => applyChatEvent(message, event), resetForReplay(once));

    expect(twice.planGraphs).toEqual(once.planGraphs);
    expect(twice.dynamicTrace).toEqual(once.dynamicTrace);
  });
});
