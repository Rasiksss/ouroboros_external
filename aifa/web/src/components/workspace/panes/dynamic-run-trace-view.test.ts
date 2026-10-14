import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { ChatStreamEvent } from "@/lib/api";

import { DynamicRunTraceView } from "@/components/workspace/panes/dynamic-run-trace-view";
import {
  applyAuthorAttempt,
  applyDynamicMove,
  applyDynamicPlan,
  type DynamicMoveEvent,
  summarizeTrace,
  type DynamicTrace,
} from "@/lib/dynamic-run-trace";

function move(overrides: Partial<DynamicMoveEvent>): DynamicMoveEvent {
  return {
    type: "dynamic_move", move: 1, status: "done", action: "execute", objective: "", reason: "",
    scenarios: [], goals: [], new_atoms: [], steps: [], outcome: "", details: "", ...overrides,
  };
}

function render(trace: DynamicTrace, live = true): string {
  return renderToStaticMarkup(createElement(DynamicRunTraceView, { trace, live }));
}

function sampleTrace(): DynamicTrace {
  let trace = applyDynamicMove(undefined, move({
    move: 1, objective: "получить ряд Brent", reason: "нужен внешний ряд",
    new_atoms: [{ name: "eph.brent_csv", kind: "download", purpose: "ряд Brent", output_type: "dyn:brent_csv", url: "https://x.org/a.csv" }],
    steps: [{ atom: "eph.brent_csv", concept: null, period_ref: null }],
    outcome: "✓ 1 новых артефактов",
  }));
  trace = applyDynamicMove(trace, move({
    move: 2, status: "running", objective: "сопоставить ряды", reason: "считаем динамику",
    new_atoms: [{ name: "eph.compare", kind: "code", purpose: "процентные изменения", output_type: "dyn:cmp", url: null }],
    steps: [{ atom: "eph.compare", concept: null, period_ref: null }],
  }));
  trace = applyAuthorAttempt(trace, {
    type: "author_attempt", move: 2, atom: "eph.compare", attempt: 1, status: "error",
    error: "ZeroDivisionError: division by zero", code: "def run(i):\n    return {'x': 1/0}",
  });
  return applyAuthorAttempt(trace, {
    type: "author_attempt", move: 2, atom: "eph.compare", attempt: 2, status: "ok",
    error: null, code: "def run(i):\n    return {'x': 1.0}",
  });
}

describe("DynamicRunTraceView", () => {
  it("shows the planner decision of every move: objective, reason, new atom and step", () => {
    const html = render(sampleTrace());

    expect(html).toContain("получить ряд Brent");
    expect(html).toContain("нужен внешний ряд");
    expect(html).toContain("eph.brent_csv");
    expect(html).toContain("загрузка");
    expect(html).toContain("✓ 1 новых артефактов");
  });

  it("shows the executed part of the goal apart from the scenarios, with its reference and a readable description", () => {
    const trace = applyDynamicMove(undefined, move({
      move: 1, status: "running", objective: "получить EBITDA",
      goals: [{ goal_ref: "goal-1", description: "Рассчитать EBITDA (все периоды)" }],
      scenarios: ["Извлеки выручку за все периоды"],
    }));

    const html = render(trace);

    expect(html).toMatch(/data-move-goal="goal-1"/);
    expect(html).toContain("часть цели goal-1: Рассчитать EBITDA (все периоды)");
    expect(html).toContain("сценарий AIFA: Извлеки выручку за все периоды");
  });

  it("renders a move recorded before part 18 (no goals in the event) without parts", () => {
    const { goals: _omitted, ...legacy } = move({ move: 1, objective: "получить ряд Brent" });
    const html = render(applyDynamicMove(undefined, legacy as DynamicMoveEvent));

    expect(html).toContain("получить ряд Brent");
    expect(html).not.toContain("data-move-goal");
  });

  it("shows the query of a web.search step and the reason when the search is switched off", () => {
    // Форма шага — та, что шлёт backend (tests/dynamic_run/test_web_search_review.py,
    // test_the_move_event_of_a_search_step_carries_the_query): запрос едет в concept.
    const trace = applyDynamicMove(undefined, move({
      move: 1, objective: "найти решения по ключевой ставке",
      steps: [{ atom: "web.search", concept: "ключевая ставка Банка России 2026", period_ref: null }],
      outcome: "ход завершён с ошибками",
      details: "✗ web.search  → нет результата (0): поиск в интернете выключен пользователем",
    }));

    const html = render(trace);

    expect(html).toContain("web.search · ключевая ставка Банка России 2026");
    expect(html).toContain("поиск в интернете выключен пользователем");
  });

  it("shows the author as a separate actor with each attempt, its error and its code", () => {
    const html = render(sampleTrace());

    expect(html).toContain("Автор");
    expect(html).toContain("Попытка 1");
    expect(html).toContain("ZeroDivisionError: division by zero");
    expect(html).toContain("Попытка 2");
    expect(html).toContain("return {&#x27;x&#x27;: 1.0}");
    expect(html).toMatch(/data-attempt-status="error"/);
    expect(html).toMatch(/data-attempt-status="ok"/);
  });

  it("marks the card status and the action so the move can be styled and found", () => {
    const html = render(sampleTrace());

    expect(html).toMatch(/data-move="1"[^>]*data-move-status="done"/);
    expect(html).toMatch(/data-move="2"[^>]*data-move-status="running"/);
  });

  it("shows a rejected planner reply with its reason, not as a normal move", () => {
    const trace = applyDynamicMove(undefined, move({
      move: 1, status: "rejected", action: "rejected", details: "атомы ['eph.x'] объявлены в new_atoms, но не вызваны",
    }));

    const html = render(trace);

    expect(html).toContain("ответ не принят");
    expect(html).toContain("не вызваны");
  });

  it("shows the final answer of a finalize move", () => {
    const trace = applyDynamicMove(undefined, move({ move: 3, action: "finalize", details: "Выручка выросла на 4%." }));

    expect(render(trace)).toContain("Выручка выросла на 4%.");
  });

  it("shows the whole plan above the moves, and already before the first move", () => {
    const planned = applyDynamicPlan(undefined, {
      type: "dynamic_plan", plan: ["получить показатели", "собрать Excel"], files: ["xlsx"],
    });

    const before = render(planned);
    const after = render(applyDynamicMove(planned, move({ move: 1, objective: "получить показатели формой" })));

    expect(before).toContain('data-trace-plan="2"');
    expect(before).toContain("собрать Excel");
    expect(before).toContain("Файл в ответе: xlsx");
    expect(after.indexOf("собрать Excel")).toBeLessThan(after.indexOf("получить показатели формой"));
  });

  it("replaces the plan when the planner rewrites it and keeps the moves", () => {
    let trace = applyDynamicPlan(undefined, { type: "dynamic_plan", plan: ["старый путь"], files: [] });
    trace = applyDynamicMove(trace, move({ move: 1 }));
    trace = applyDynamicPlan(trace, { type: "dynamic_plan", plan: ["другой путь"], files: [] });

    expect(trace.plan).toEqual(["другой путь"]);
    expect(trace.moves).toHaveLength(1);
  });

  it("renders nothing for an empty trace", () => {
    expect(render({ moves: [] })).toBe("");
  });

  it("does not pretend a stopped run is still running once the run is no longer live", () => {
    const html = render(sampleTrace(), false);

    expect(html).toContain("прогон завершён");
    expect(html).not.toContain("идёт");
  });

  it("offers the graphs attached to a move from the trace", () => {
    const html = renderToStaticMarkup(createElement(DynamicRunTraceView, {
      trace: sampleTrace(), live: false, graphCounts: { 1: 2 }, onSelectGraphMove: () => undefined,
    }));
    expect(html).toContain("Показать графы: 2");
    expect(html).toContain('data-move-graphs="2"');
  });

  it("part 14: the entry card of the shared backend stream names why the planner took over, and is not a move", () => {
    const events: ChatStreamEvent[] = JSON.parse(readFileSync(
      resolve(process.cwd(), "..", "tests", "dynamic_run", "fixtures", "sse", "execution-failure-then-cycle.json"), "utf8",
    ));
    const trace = events.reduce<DynamicTrace | undefined>(
      (current, event) => (event.type === "dynamic_move" ? applyDynamicMove(current, event) : current), undefined,
    ) as DynamicTrace;

    const html = render(trace, false);

    expect(trace.moves[0]).toMatchObject({ move: 0, action: "entry", status: "done" });
    expect(html).toContain("Вход в цикл");
    expect(html).toContain("Стандартный граф");
    expect(html).toContain("пакетный вызов &#x27;read.value&#x27; не вернул &#x27;NET_INCOME&#x27;");
    expect(html).not.toContain("Ход 0");
    expect(summarizeTrace(trace).moves).toBe(trace.moves.length - 1);
  });

  it("an entry card alone is a running trace, not a finished one", () => {
    const trace = applyDynamicMove(undefined, move({ move: 0, action: "entry", reason: "✗ read.value" }));

    expect(summarizeTrace(trace)).toMatchObject({ moves: 0, state: "running" });
  });
});
