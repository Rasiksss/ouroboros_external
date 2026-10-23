import { describe, expect, it } from "vitest";

import { applyChatEvent, resetForReplay, type TextMessage } from "./projects";
import { rootPlanRevisions, type RootPlanEvent } from "./root-plan";
import { PANEL_TITLE } from "./workspace-layout";

function proposal(taskId: string, objectives: string[]): RootPlanEvent {
  return {
    type: "root_plan",
    task_id: taskId,
    requirements: [{ id: "R1", description: "Выручка", source: "«выручку»" }],
    assumptions: [],
    questions: [],
    steps: objectives.map((objective, i) => ({
      id: `S${i + 1}`,
      objective,
      depends_on: [],
      execution: "scenario: extract_registered_metric",
      outputs: ["ExtractedMetric"],
      criteria: [{ id: "C1", description: "значение со страницей", verification: "чтение" }],
    })),
  };
}

function message(): TextMessage {
  return resetForReplay({ id: "a1", role: "assistant", content: "", created_at: 1 } as TextMessage);
}

describe("root plan events", () => {
  it("keeps every proposal of the turn as a revision, the latest one is current", () => {
    let msg = message();
    msg = applyChatEvent(msg, proposal("t1", ["Извлечь"]) as never);
    msg = applyChatEvent(msg, proposal("t2", ["Извлечь", "Собрать Excel"]) as never);

    const revisions = rootPlanRevisions(msg);
    expect(revisions.map((r) => r.taskId)).toEqual(["t1", "t2"]);
    expect(revisions.map((r) => r.number)).toEqual([1, 2]);
    expect(revisions.map((r) => r.current)).toEqual([false, true]);
    expect(revisions[1].steps.map((s) => s.objective)).toEqual(["Извлечь", "Собрать Excel"]);
  });

  it("does not duplicate a proposal when the journal is replayed", () => {
    let msg = message();
    msg = applyChatEvent(msg, proposal("t1", ["Извлечь"]) as never);
    msg = applyChatEvent(msg, proposal("t1", ["Извлечь"]) as never);

    expect(rootPlanRevisions(msg)).toHaveLength(1);
  });

  it("starts empty after a replay reset", () => {
    let msg = message();
    msg = applyChatEvent(msg, proposal("t1", ["Извлечь"]) as never);

    expect(rootPlanRevisions(resetForReplay(msg))).toEqual([]);
  });
});

describe("plan column", () => {
  it("takes the place of the source document column", () => {
    expect(PANEL_TITLE.doc).toBe("План");
  });
});
