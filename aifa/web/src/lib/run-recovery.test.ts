import { describe, expect, it } from "vitest";
import {
  createRunProbe,
  interruptedNotice,
  reconnectRun,
  RECONNECT_DELAYS_MS,
  type RunEvent,
  type RunTail,
} from "@/lib/run-recovery";

/**
 * Класс ошибки: «одно сообщение на два противоположных исхода».
 *
 * 05.08 пользователь дважды увидел одинаковое «⚠️ Load failed» (текст Safari,
 * не наш). Записи в client_events.jsonl совпали бит-в-бит:
 *   12:05:33 chat_stream_drop … run 33f0cf28 … detail "Load failed"
 *   12:17:25 chat_stream_drop … run 2ae08a6a … detail "Load failed"
 * А хвосты журналов на сервере оказались обратными: 33f0cf28 кончался
 * `…→session_lost→done` (прогон убит перезапуском, работы нет), 2ae08a6a —
 * `…→sources→done` (прогон дожил и завершился спустя шесть минут ПОСЛЕ обрыва).
 *
 * Поэтому тест проверяет не наличие строки, а РАЗЛИЧЕНИЕ: из одного и того же
 * клиентского симптома обязаны получаться разные вердикты и разные действия.
 */

/** Журнал прогона 33f0cf28: бэкенд перезапустился посреди fe_analyze. */
const JOURNAL_DEAD: RunEvent[] = [
  { type: "tool_progress", name: "fe_analyze", text: "читаю отчёт…" },
  {
    type: "session_lost",
    message: "Прогон оборвался при перезапуске сервера. Запустите его заново.",
  },
  { type: "done" },
];

/** Журнал прогона 2ae08a6a: поток у клиента порвался, работа дошла до конца. */
const JOURNAL_ALIVE: RunEvent[] = [
  { type: "text_delta", text: "Выручка выросла на 12%." },
  { type: "sources", sources: [] },
  { type: "done" },
];

async function* toStream(events: RunEvent[]): AsyncGenerator<RunEvent> {
  for (const ev of events) yield ev;
}

/** Потребитель ровно как в consumeRun (use-chat-run.ts): рисует событие,
 *  пропускает `session_lost` и обрывается на `done`. */
async function consume(journal: RunEvent[]): Promise<{ tail: RunTail; drawn: string[] }> {
  const probe = createRunProbe();
  const drawn: string[] = [];
  for await (const ev of probe.wrap(toStream(journal))) {
    if (ev.type === "session_lost") continue;
    if (ev.type === "done") break;
    drawn.push(ev.type);
  }
  return { tail: probe.tail(), drawn };
}

/** Обрыв у клиента: сначала тот самый отказ Safari, потом переподписка. */
async function afterDrop(journal: RunEvent[]) {
  let n = 0;
  return reconnectRun({
    attempt: async () => {
      n += 1;
      if (n === 1) throw new TypeError("Load failed");
      return (await consume(journal)).tail;
    },
    delays: [0, 0, 0],
    sleep: async () => {},
  });
}

describe("хвост журнала различает два исхода обрыва", () => {
  it("погибший и доживший прогон дают РАЗНЫЙ вердикт", async () => {
    const dead = await consume(JOURNAL_DEAD);
    const alive = await consume(JOURNAL_ALIVE);

    expect(dead.tail.kind).not.toBe(alive.tail.kind);
    expect(dead.tail).toEqual({
      kind: "lost",
      notice: "Прогон оборвался при перезапуске сервера. Запустите его заново.",
    });
    expect(alive.tail).toEqual({ kind: "finished" });
  });

  it("пробник ничего не съедает — ответ рисуется реплеем как обычно", async () => {
    const alive = await consume(JOURNAL_ALIVE);
    expect(alive.drawn).toEqual(["text_delta", "sources"]);
  });

  it("журнал без «done» — судьба неизвестна, а не «завершено»", async () => {
    const { tail } = await consume([{ type: "text_delta", text: "…" }]);
    expect(tail).toEqual({ kind: "open" });
  });

  it("ОДНА И ТА ЖЕ ошибка браузера ведёт к разным вердиктам после переподписки", async () => {
    const dead = await afterDrop(JOURNAL_DEAD);
    const alive = await afterDrop(JOURNAL_ALIVE);

    expect(dead.kind).not.toBe(alive.kind);
    expect(dead.kind).toBe("lost");
    expect(alive.kind).toBe("finished");
  });
});

describe("переподписка ограничена и говорит правду, когда не дозвонилась", () => {
  it("«не дозвонился» — ТРЕТЬЕ состояние, не «прогон погиб»", async () => {
    const seen: number[] = [];
    const verdict = await reconnectRun({
      attempt: async () => {
        throw new TypeError("Load failed");
      },
      onAttempt: (n) => seen.push(n),
      delays: [0, 0, 0],
      sleep: async () => {},
    });

    expect(verdict).toEqual({ kind: "unreachable", attempts: 3 });
    // Попыток ровно столько, сколько пауз: бесконечной петли нет.
    expect(seen).toEqual([1, 2, 3]);
    const lost = await afterDrop(JOURNAL_DEAD);
    expect(verdict.kind).not.toBe(lost.kind);
  });

  it("окно переподключения переживает перезапуск бэкенда (замер ≥27 с)", async () => {
    const slept: number[] = [];
    await reconnectRun({
      attempt: async () => {
        throw new TypeError("Load failed");
      },
      sleep: async (ms) => {
        slept.push(ms);
      },
    });
    const total = slept.reduce((a, b) => a + b, 0);
    expect(total).toBeGreaterThanOrEqual(27_000);
    expect(RECONNECT_DELAYS_MS[0]).toBe(0); // первая попытка — сразу
  });

  it("снятая подписка гасит переподписку молча", async () => {
    let calls = 0;
    const verdict = await reconnectRun({
      attempt: async () => {
        calls += 1;
        throw new TypeError("Load failed");
      },
      aborted: () => true,
      delays: [0, 0],
      sleep: async () => {},
    });
    expect(verdict).toEqual({ kind: "aborted" });
    expect(calls).toBe(0);
  });
});

describe("каждому исходу — своё действие", () => {
  it("погибшему прогону предлагают повторить, живому — догнать", () => {
    const lost = interruptedNotice({ state: "lost", notice: "Прогон оборвался." });
    const unreachable = interruptedNotice({ state: "unreachable" });

    expect(lost.action).not.toBe(unreachable.action);
    expect(lost.action).toBe("resend");
    expect(unreachable.action).toBe("reattach");
    // Тупика («ошибка есть, делать нечего») не остаётся ни в одном исходе.
    expect(lost.actionLabel).not.toBe("");
    expect(unreachable.actionLabel).not.toBe("");
  });

  it("пока переподключаемся — состояние видно, а кнопка не нужна", () => {
    const n = interruptedNotice({ state: "reconnecting", attempt: 2, total: 7 });
    expect(n.action).toBe("none");
    expect(n.text).toContain("2");
    expect(n.text).toContain("7");
  });
});

/**
 * ТРЕТИЙ ХВОСТ, найденный состязательной проверкой 05.08.
 *
 * Разобрано было два: «погиб» (`session_lost`) и «дожил» (`done`). А бэкенд
 * пишет ТРИ. `RunState.close()` в журнал не дописывает ничего
 * (run_orchestrator.py:109-118), а `_drive()` при исключении И при отмене
 * («Стоп») эмитит `{"type":"error"}` и закрывается (:190-196). Значит журнал
 * упавшего прогона кончается на `error` БЕЗ `done`.
 *
 * Без этой ветки такой хвост читался как «поток ещё открыт»: семь
 * переподключений за 59 секунд к заведомо мёртвому прогону, а в конце —
 * обещание сохранённого результата, которого не существует. Хуже исходного
 * «Load failed»: то хотя бы не врало.
 */
const JOURNAL_FAILED: RunEvent[] = [
  { type: "tool_call", name: "fe_extract_metrics", args_summary: "" },
  { type: "error", message: "RuntimeError: движок не ответил" },
];

/**
 * ⚠️ А здесь `error` НЕ терминален: на чат-пути он эмитится ВНУТРИ цикла
 * (orchestrator/agent.py:1533), и ход продолжается. Наивная правка «увидел
 * error → считай прогон упавшим» оборвала бы догон на первой промежуточной
 * ошибке и соврала бы по-новому. Решает не факт ошибки, а то, что поток
 * кончился ИМЕННО НА НЕЙ.
 */
const JOURNAL_ERROR_MIDSTREAM: RunEvent[] = [
  { type: "error", message: "фоновый прогон затих (таймаут)" },
  { type: "text_delta", text: "продолжаю без фоновых данных" },
  { type: "done" },
];

async function tailOf(journal: RunEvent[]): Promise<RunTail> {
  const probe = createRunProbe();
  async function* src() {
    for (const ev of journal) yield ev;
  }
  for await (const _ of probe.wrap(src())) void _;
  return probe.tail();
}

describe("третий хвост: прогон упал или отменён", () => {
  it("журнал, кончившийся на error без done, — это «упал», а не «поток открыт»", async () => {
    const tail = await tailOf(JOURNAL_FAILED);
    expect(tail.kind).toBe("failed");
    expect(tail.kind === "failed" && tail.notice).toContain("движок не ответил");
  });

  it("промежуточная ошибка НЕ считается падением — ход продолжился и дошёл до done", async () => {
    expect((await tailOf(JOURNAL_ERROR_MIDSTREAM)).kind).toBe("finished");
  });

  it("ошибка в середине + ОБРЫВ без done — судьба неизвестна, а не «упал»", async () => {
    // Именно этот случай отличает правильную реализацию от наивной «увидел
    // error → прогон упал». Сценарий не выдуманный: 05.08 ход эмитил
    // «фоновый прогон затих (таймаут)», продолжал работу — и связь оборвалась
    // на середине. Сказать здесь «упал, повторите» значит осиротить живой
    // прогон вместе с готовым результатом.
    const tail = await tailOf([
      { type: "error", message: "фоновый прогон затих (таймаут)" },
      { type: "text_delta", text: "продолжаю без фоновых данных" },
      { type: "tool_call", name: "fe_analyze", args_summary: "" },
    ] as RunEvent[]);
    expect(tail.kind).toBe("open");
  });

  it("упавший прогон НЕ переподключают семь раз — вердикт с первой попытки", async () => {
    let attempts = 0;
    const verdict = await reconnectRun({
      attempt: async () => {
        attempts += 1;
        return tailOf(JOURNAL_FAILED);
      },
      sleep: async () => undefined,
    });
    expect(attempts).toBe(1);
    expect(verdict.kind).toBe("failed");
  });

  it("упавшему прогону предлагают ПОВТОРИТЬ, а не «догнать» несуществующий результат", () => {
    const n = interruptedNotice({ state: "failed", notice: "движок не ответил" });
    expect(n.action).toBe("resend");
    expect(n.text).toContain("Результата за ним нет");
    // Ровно та ложь, которую чиним: обещание сохранённого результата.
    expect(n.text).not.toContain("сохран");
  });
});

/**
 * Кадр-биения не должен доходить до потребителя как работа: попав в общую
 * ветку, он штампует updated_at и вызывает полную перезапись хранилища плюс
 * PUT /dialogs — каждые 15 секунд, ровно в ту паузу, которую биение лечит.
 */
describe("кадр-биения — это связь, а не работа", () => {
  it("пробник пропускает ping насквозь и не считает его исходом", async () => {
    const tail = await tailOf([
      { type: "ping" },
      { type: "ping" },
      { type: "text_delta", text: "и вот ответ" },
      { type: "done" },
    ] as RunEvent[]);
    expect(tail.kind).toBe("finished");
  });

  it("поток из одних биений — судьба неизвестна, а не «завершено»", async () => {
    const tail = await tailOf([{ type: "ping" }, { type: "ping" }] as RunEvent[]);
    expect(tail.kind).toBe("open");
  });
});
