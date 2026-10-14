import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { assistantAnswerLayout } from "./assistant-answer-layout";
import { executionGraphsOf } from "./execution-graphs";
import { nodeDisplayStatus } from "./plan-graph-progress";
import { applyChatEvent, resetForReplay, type TextMessage } from "./projects";

const fixturePath = (name: string) => fileURLToPath(
  new URL(`../../../finengine/tests/dynamic_run/fixtures/sse/${name}`, import.meta.url),
);

const events = (name: string): unknown[] => JSON.parse(readFileSync(fixturePath(name), "utf8"));
const emptyMessage = (): TextMessage =>
  ({ id: "assistant-1", role: "assistant", content: "", created_at: 0 }) as TextMessage;

function replay(name: string, initial: TextMessage = emptyMessage()): TextMessage {
  return events(name).reduce<TextMessage>(
    (message, event) => applyChatEvent(message, event as never),
    initial,
  );
}

function failedNodeCount(message: TextMessage): number {
  return Object.values(message.nodeRuns ?? {}).filter((run) => run.status === "failed").length;
}

function visibleAnswer(message: TextMessage) {
  return {
    layout: assistantAnswerLayout(message, false),
    text: message.content,
    cards: message.typedResults ?? [],
    errors: message.typedErrors ?? [],
  };
}

describe("общий контракт ответа backend → reducer → отрисовка", () => {
  it("finalized показывает формулу над двумя карточками", () => {
    const answer = visibleAnswer(replay("finalized-with-cards-and-text.json"));

    expect(answer.layout.contentPlacement).toBe("before-results");
    expect(answer.layout.showTypedResults).toBe(true);
    expect(answer.text).toContain("CV = population_sd / mean × 100");
    expect(answer.text).toContain("CV ≈ 1,9428 %");
    expect(answer.cards).toHaveLength(2);
  });

  it("gave_up — один обычный ответ, не красная ошибка", () => {
    const answer = visibleAnswer(replay("gave-up-as-answer.json"));

    expect(answer.layout.contentPlacement).toBe("before-results");
    expect(answer.text).toContain("Внешний источник сейчас недоступен");
    expect(answer.errors).toHaveLength(0);
  });

  it("stopped показывает текст и частичные карточки", () => {
    const answer = visibleAnswer(replay("stopped-with-partials.json"));

    expect(answer.layout.contentPlacement).toBe("before-results");
    expect(answer.layout.showTypedResults).toBe(true);
    expect(answer.cards.length).toBeGreaterThan(0);
    expect(answer.errors).toHaveLength(0);
    expect(answer.text.toLowerCase()).not.toMatch(/цикл|планировщик|evidence_ids|dynamic_run_|extractedmetric|ответа подряд/);
    expect(answer.text).not.toMatch(/\d{4,}\.\d/);
  });

  it("детерминированный остаток показывает текст под карточкой", () => {
    const answer = visibleAnswer(replay("deterministic-not-performed.json"));

    expect(answer.layout.contentPlacement).toBe("after-results");
    expect(answer.text).toContain("Не выполнено: сделай презентацию");
    expect(answer.cards).toHaveLength(1);
  });

  it("analysisCard по-прежнему заменяет markdown того же отчёта", () => {
    const message = {
      ...emptyMessage(),
      content: "тот же отчёт в markdown",
      analysisCard: { title: "Отчёт" } as never,
    } as TextMessage;

    expect(assistantAnswerLayout(message, false).contentPlacement).toBeNull();
  });

  for (const name of [
    "finalized-with-cards-and-text.json",
    "gave-up-as-answer.json",
    "stopped-with-partials.json",
    "deterministic-not-performed.json",
  ]) {
    it(`реплей не теряет и не задваивает ответ: ${name}`, () => {
      const first = replay(name);
      const replayed = replay(name, resetForReplay(first));

      expect(replayed.content).toBe(first.content);
      expect(replayed.typedResults ?? []).toEqual(first.typedResults ?? []);
      expect(visibleAnswer(replayed).layout).toEqual(visibleAnswer(first).layout);
    });
  }

  for (const name of ["dev-case-reuse.json", "dev-external-source-give-up.json", "dev-compound-one-cycle.json", "dev-cycle-file-delivery.json"]) {
    it(`реплеит очищенный поток dev тем же reducer: ${name}`, () => {
      const answer = visibleAnswer(replay(name));

      expect(answer.errors).toHaveLength(0);
      expect(answer.text).not.toBe("");
    });
  }

  it("составная цель со стенда: один ответ, ход цикла в трассе, карточки и ни одной красной ошибки", () => {
    const message = replay("dev-compound-one-cycle.json");
    const answer = visibleAnswer(message);

    expect(answer.errors).toHaveLength(0);
    expect(answer.cards.length).toBeGreaterThan(0);
    expect(message.dynamicTrace?.moves.length).toBeGreaterThan(0);
    expect(failedNodeCount(message)).toBe(0);
  });

  it("файл из цикла со стенда: ссылка на скачивание и карточка файла в одном сообщении, без ошибок", () => {
    const message = replay("dev-cycle-file-delivery.json");
    const file = (message.files ?? [])[0];
    const card = (message.typedResults ?? []).find((result) => result.output_type === "FileArtifact");

    expect(message.files).toHaveLength(1);
    expect(file.filename).toMatch(/\.xlsx$/);
    expect(card).toBeDefined();
    expect(visibleAnswer(message).errors).toHaveLength(0);
    expect(message.content).not.toBe("");
  });

  it("«данных нет» со стенда: один отказ, цикла нет, узлы без ошибок", () => {
    const message = replay("dev-no-data-no-cycle.json");
    const answer = visibleAnswer(message);

    expect(answer.errors.map((error) => error.code)).toEqual(["metric_not_found"]);
    expect(message.dynamicTrace).toBeUndefined();
    expect(failedNodeCount(message)).toBe(0);
  });

  for (const name of ["gave-up-as-answer.json", "stopped-with-partials.json"]) {
    it(`done обычного ответа не красит недозакрытый узел ошибкой: ${name}`, () => {
      const stream = events(name);
      const doneIndex = stream.findIndex((event) => (event as { type?: string }).type === "done");
      const beforeDone = stream.slice(0, doneIndex).reduce<TextMessage>(
        (message, event) => applyChatEvent(message, event as never),
        emptyMessage(),
      );
      // Фикстура — журнал до конверта: в ней done без статуса и все узлы закрыты. Статус
      // `succeeded` backend отдаёт для обоих исходов (test_chat_completion_status.py),
      // а узел оставляем открытым, чтобы обработчику было что красить.
      const graphs = executionGraphsOf(beforeDone);
      const lastGraph = graphs.at(-1)!;
      const [lastKey, lastRun] = Object.entries(lastGraph.nodeRuns).at(-1)!;
      const unfinished = {
        ...beforeDone,
        planGraphs: graphs.map((graph) => graph.id === lastGraph.id ? {
          ...graph,
          nodeRuns: { ...graph.nodeRuns, [lastKey]: { ...lastRun, status: "running" as const } },
        } : graph),
      };

      const afterDone = applyChatEvent(unfinished, { type: "done", status: "succeeded" } as never);

      expect(failedNodeCount(afterDone)).toBe(failedNodeCount(unfinished));
      const closed = executionGraphsOf(afterDone).at(-1)!;
      expect(closed.nodeRuns[lastKey].status).toBe("running");
      expect(nodeDisplayStatus(closed.nodeRuns[lastKey], Boolean(closed.terminal))).toBe("interrupted");
    });
  }
});
