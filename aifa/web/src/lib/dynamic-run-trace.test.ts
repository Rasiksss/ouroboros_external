import { describe, expect, it } from "vitest";

import {
  applyAuthorAttempt,
  applyDynamicMove,
  summarizeTrace,
  type AuthorAttemptEvent,
  type DynamicMoveEvent,
} from "@/lib/dynamic-run-trace";

function move(overrides: Partial<DynamicMoveEvent> = {}): DynamicMoveEvent {
  return {
    type: "dynamic_move",
    move: 1,
    status: "running",
    action: "execute",
    objective: "получить ряд Brent",
    reason: "нужен внешний ряд",
    scenarios: [],
    goals: [],
    new_atoms: [
      { name: "eph.brent_csv", kind: "download", purpose: "ряд Brent", output_type: "dyn:brent_csv", url: "https://x.org/a.csv" },
    ],
    steps: [{ atom: "eph.brent_csv", concept: null, period_ref: null }],
    outcome: "",
    details: "",
    ...overrides,
  };
}

function attempt(overrides: Partial<AuthorAttemptEvent> = {}): AuthorAttemptEvent {
  return {
    type: "author_attempt",
    move: 2,
    atom: "eph.calc",
    attempt: 1,
    status: "error",
    error: "ZeroDivisionError",
    code: "def run(i): return 1/0",
    ...overrides,
  };
}

describe("applyDynamicMove", () => {
  it("adds a move and updates the same card by move number when its outcome arrives", () => {
    const running = applyDynamicMove(undefined, move());
    const done = applyDynamicMove(running, move({ status: "done", outcome: "✓ 3 новых артефактов" }));

    expect(done.moves).toHaveLength(1);
    expect(done.moves[0]).toMatchObject({ move: 1, status: "done", outcome: "✓ 3 новых артефактов" });
    expect(done.moves[0].newAtoms[0]).toMatchObject({ name: "eph.brent_csv", kind: "download" });
  });

  it("is idempotent: a replayed event does not duplicate the card", () => {
    const once = applyDynamicMove(undefined, move());
    const twice = applyDynamicMove(once, move());

    expect(twice.moves).toHaveLength(1);
  });

  it("keeps moves ordered by number even if events arrive out of order", () => {
    let trace = applyDynamicMove(undefined, move({ move: 2 }));
    trace = applyDynamicMove(trace, move({ move: 1 }));

    expect(trace.moves.map((item) => item.move)).toEqual([1, 2]);
  });

  it("shows a rejected planner reply as its own card with the reason", () => {
    const trace = applyDynamicMove(undefined, move({
      status: "rejected", action: "rejected", objective: "", reason: "", new_atoms: [], steps: [],
      details: "new_atoms без steps",
    }));

    expect(trace.moves[0]).toMatchObject({ status: "rejected", action: "rejected", details: "new_atoms без steps" });
  });
});

describe("applyDynamicMove: части разобранной цели", () => {
  const goals = [{ goal_ref: "goal-1", description: "Рассчитать EBITDA (все периоды)" }];

  it("keeps the executed parts of the goal separately from the scenarios, and updates them with the move", () => {
    const running = applyDynamicMove(undefined, move({ goals, new_atoms: [], steps: [] }));
    const done = applyDynamicMove(running, move({ goals, status: "done", outcome: "✓ 1 новых артефактов", new_atoms: [], steps: [] }));

    expect(done.moves[0].goals).toEqual(goals);
    expect(done.moves[0].scenarios).toEqual([]);
  });

  it("reads a record written before part 18 (no goals field) as a move without parts", () => {
    const { goals: _omitted, ...legacy } = move({ goals });
    const trace = applyDynamicMove(undefined, legacy as DynamicMoveEvent);

    expect(trace.moves[0].goals).toEqual([]);
  });
});

describe("applyAuthorAttempt", () => {
  it("attaches attempts to their move in attempt order, and keeps them when the move updates", () => {
    let trace = applyDynamicMove(undefined, move({ move: 2, status: "running" }));
    trace = applyAuthorAttempt(trace, attempt({ attempt: 2, status: "ok", error: null, code: "def run(i): return 1" }));
    trace = applyAuthorAttempt(trace, attempt({ attempt: 1 }));
    trace = applyDynamicMove(trace, move({ move: 2, status: "done", outcome: "✓" }));

    expect(trace.moves[0].attempts.map((item) => [item.attempt, item.status])).toEqual([[1, "error"], [2, "ok"]]);
    expect(trace.moves[0].status).toBe("done");
  });

  it("is idempotent for a replayed attempt", () => {
    let trace = applyDynamicMove(undefined, move({ move: 2 }));
    trace = applyAuthorAttempt(trace, attempt());
    trace = applyAuthorAttempt(trace, attempt());

    expect(trace.moves[0].attempts).toHaveLength(1);
  });

  it("creates a running placeholder if the attempt arrives before its move card", () => {
    const trace = applyAuthorAttempt(undefined, attempt({ move: 3 }));

    expect(trace.moves[0]).toMatchObject({ move: 3, status: "running" });
    expect(trace.moves[0].attempts).toHaveLength(1);
  });
});

describe("summarizeTrace", () => {
  it("counts moves, rejected replies and author attempts, and tells whether it is still running", () => {
    let trace = applyDynamicMove(undefined, move({ move: 1, status: "rejected", action: "rejected" }));
    trace = applyDynamicMove(trace, move({ move: 2, status: "running" }));
    trace = applyAuthorAttempt(trace, attempt({ move: 2 }));

    expect(summarizeTrace(trace)).toEqual({ moves: 2, rejected: 1, authorAttempts: 1, state: "running" });
  });

  it("is finished once the last move is a terminal answer", () => {
    let trace = applyDynamicMove(undefined, move({ move: 1, status: "done" }));
    trace = applyDynamicMove(trace, move({ move: 2, status: "done", action: "finalize", new_atoms: [], steps: [] }));

    expect(summarizeTrace(trace).state).toBe("finished");
  });

  it("is empty without moves", () => {
    expect(summarizeTrace(undefined)).toEqual({ moves: 0, rejected: 0, authorAttempts: 0, state: "empty" });
  });
});
