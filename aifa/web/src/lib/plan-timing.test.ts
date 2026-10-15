import { describe, expect, it } from "vitest";
import { applyChatEvent } from "@/lib/projects";
import type { ExtractionMessage, TextMessage } from "@/lib/projects";

/**
 * Время по шагам плана (#120, правка Nate 17.08).
 *
 * «Сколько времени было потрачено» — половина вопроса «на что агент тратит
 * время». Времени в событиях плана нет, поэтому метку ставит редьюсер на смене
 * статуса. Раньше это делал сам островок в `useRef`: метки умирали вместе с
 * компонентом, и у ЗАВЕРШЁННОГО хода счётчики были пустыми — ровно тогда,
 * когда вопрос и задают.
 */
function msgWithPlan(): TextMessage {
  return applyChatEvent(
    { id: "m1", role: "assistant", content: "", created_at: 0 } as TextMessage,
    { type: "plan", name: "ход", steps: ["Определяю тип", "Извлекаю"] } as never,
  ) as TextMessage;
}

const step = (m: TextMessage, i: number) => (m.plan ?? [])[i];

describe("метки времени шага", () => {
  it("начало ставится на переходе в running", () => {
    const m = applyChatEvent(msgWithPlan(), {
      type: "plan_step", name: "ход", index: 0, status: "running",
    } as never) as TextMessage;
    expect(step(m, 0).startedAt).toBeGreaterThan(0);
    expect(step(m, 0).endedAt).toBeUndefined();
  });

  it("🔴 повторное running НЕ обнуляет отсчёт", () => {
    // Догон потока после разрыва и переотметка шага — штатные случаи; сбросить
    // начало значило бы показать «0 с» на шаге, который идёт пять минут.
    let m = applyChatEvent(msgWithPlan(), {
      type: "plan_step", name: "ход", index: 0, status: "running",
    } as never) as TextMessage;
    const first = step(m, 0).startedAt;
    m = applyChatEvent(m, {
      type: "plan_step", name: "ход", index: 0, status: "running",
    } as never) as TextMessage;
    expect(step(m, 0).startedAt).toBe(first);
  });

  it("завершение фиксирует конец, и он больше не двигается", () => {
    let m = applyChatEvent(msgWithPlan(), {
      type: "plan_step", name: "ход", index: 0, status: "running",
    } as never) as TextMessage;
    m = applyChatEvent(m, {
      type: "plan_step", name: "ход", index: 0, status: "done",
    } as never) as TextMessage;
    const end = step(m, 0).endedAt;
    expect(end).toBeGreaterThan(0);
    expect(step(m, 0).startedAt).toBeLessThanOrEqual(end!);
  });

  it("нетронутый шаг остаётся без меток", () => {
    const m = applyChatEvent(msgWithPlan(), {
      type: "plan_step", name: "ход", index: 0, status: "running",
    } as never) as TextMessage;
    expect(step(m, 1).startedAt).toBeUndefined();
  });
});

// Тип импортируется ради проверки, что публичный контракт не разъехался.
export type _Unused = ExtractionMessage;
