import type { ChatStreamEvent } from "@/lib/api";

/**
 * Обрыв потока хода: под ОДНИМ симптомом прячутся два противоположных случая.
 *
 * 05.08 пользователь дважды получил одинаковое «⚠️ Load failed» (это текст
 * Safari, а не наш): в client_events.jsonl обе записи совпали бит-в-бит. А на
 * сервере исходы были обратные — журнал 33f0cf28 кончался
 * `…→session_lost→done` (прогон убит перезапуском бэкенда, работы нет), журнал
 * 2ae08a6a — `…→usage→sources→done` (прогон дожил и завершился через шесть
 * минут ПОСЛЕ обрыва, результат лежал готовым). Различить их по тексту ошибки
 * нельзя в принципе: у отказа fetch нет сведений о судьбе прогона.
 *
 * Признак машинный и ровно один: ПЕРЕПОДПИСАТЬСЯ на тот же
 * GET /chat/runs/{id}/stream — подписка реплеит журнал целиком — и посмотреть,
 * что в нём лежит. Здесь эта логика живёт отдельно от React, чтобы её
 * исполнял и проверял один и тот же код.
 */

/** Событие журнала хода. `session_lost` дописывает бэкенд при подъёме — в
 *  контракте чата (ChatStreamEvent) его нет, он приходит только в реплее. */
export type RunEvent = ChatStreamEvent | { type: "session_lost"; message?: string };

/** Чем кончилась ОДНА подписка на журнал. */
export type RunTail =
  /** В журнале есть `session_lost` — прогон не пережил перезапуск сервера. */
  | { kind: "lost"; notice: string }
  /** Журнал дошёл до `done` без `session_lost` — прогон жив/завершён. */
  | { kind: "finished" }
  /** Журнал кончился на `error` — прогон упал или его отменили. */
  | { kind: "failed"; notice: string }
  /** Поток кончился, не дойдя до `done`, — судьба прогона неизвестна. */
  | { kind: "open" };

/** Итог переподписки после обрыва. */
export type RunVerdict =
  | { kind: "lost"; notice: string }
  | { kind: "finished" }
  /** Прогон кончился ошибкой сервера или отменой. Работы за ним нет —
   *  догонять нечего, надо повторять запрос. */
  | { kind: "failed"; notice: string }
  /** За отведённые попытки дозвониться не удалось. Это ТРЕТЬЕ состояние, а не
   *  «прогон погиб»: прогон, скорее всего, идёт и результат копится. */
  | { kind: "unreachable"; attempts: number }
  /** Подписку сняли намеренно (ушли из чата, «Стоп», размонтирование). */
  | { kind: "aborted" };

const LOST_NOTICE = "Прогон оборвался при перезапуске сервера. Запустите его заново.";

/**
 * Пробник хвоста: пропускает поток событий насквозь и запоминает приговор.
 *
 * Именно «насквозь», а не «вместо»: подписчик как рисовал события сам, так и
 * рисует, — здесь появляется только то, чего у него не было, машинный ответ на
 * вопрос «прогон пережил обрыв или нет».
 */
export function createRunProbe() {
  let lost = "";
  let done = false;
  // Хвостов на самом деле ТРИ, а не два. `close()` в журнал не пишет ничего
  // (run_orchestrator.py:109-118), а `_drive()` при исключении и при отмене
  // эмитит `{"type":"error"}` и закрывается (:190-196). Значит журнал упавшего
  // и отменённого прогона кончается на `error` БЕЗ `done`. Без этой ветки
  // такой хвост читался как «поток ещё открыт» — и мы семь раз перечитывали
  // журнал заведомо мёртвого прогона, а потом обещали пользователю
  // сохранённый результат, которого нет.
  //
  // ⚠️ Но `error` терминален НЕ ВСЕГДА: на чат-пути он эмитится ВНУТРИ цикла
  // (orchestrator/agent.py:1533) и ход продолжается. Поэтому решает не сам
  // факт ошибки, а то, что поток кончился ИМЕННО НА НЕЙ.
  let lastError = "";
  let lastWasError = false;
  return {
    async *wrap(stream: AsyncIterable<RunEvent>): AsyncGenerator<RunEvent> {
      for await (const ev of stream) {
        if (ev.type === "session_lost") lost = ev.message || LOST_NOTICE;
        if (ev.type === "done") done = true;
        if (ev.type === "error") {
          const e = ev as { message?: string; error?: string };
          lastError = e.message || e.error || "";
          lastWasError = true;
        } else {
          lastWasError = false;
        }
        yield ev;
      }
    },
    /** Чем кончился журнал. Спрашивать ПОСЛЕ того, как поток прокачан. */
    tail(): RunTail {
      if (lost) return { kind: "lost", notice: lost };
      if (done) return { kind: "finished" };
      if (lastWasError) return { kind: "failed", notice: lastError };
      return { kind: "open" };
    },
  };
}

/**
 * Паузы перед попытками переподписки, мс. Первая попытка — сразу.
 *
 * Прежние 1,5 + 3 + 4,5 ≈ 9 с не доживали до ответа: замеренное окно
 * недоступности бэкенда 05.08 — не меньше 27 с (обрыв 12:05:33, маркер
 * `session_lost` дописан в 12:06). Пользователь получал «связь не
 * восстановилась» ровно тогда, когда сервер ещё поднимался. Здесь ~59 с —
 * с запасом, но КОНЕЧНО: бесконечная петля переподключения молотила бы сеть
 * и никогда не признавалась, что не дозвонилась.
 */
export const RECONNECT_DELAYS_MS = [0, 2000, 4000, 8000, 15000, 15000, 15000];

function isAbort(e: unknown): boolean {
  return (e as { name?: string } | null)?.name === "AbortError";
}

/**
 * Ограниченная переподписка на прогон после обрыва: несколько попыток с
 * нарастающей паузой, потом честное «не дозвонился».
 */
export async function reconnectRun(o: {
  /** Одна попытка: подписаться и прокачать журнал до конца. Номер попытки —
   *  чтобы про один обрыв не докладывать пользователю семь раз подряд. */
  attempt: (n: number) => Promise<RunTail>;
  /** Видимое состояние «переподключаюсь»: попытка n из total. */
  onAttempt?: (n: number, total: number) => void;
  /** Подписку сняли — прекращаем немедленно, молча. */
  aborted?: () => boolean;
  delays?: number[];
  sleep?: (ms: number) => Promise<void>;
}): Promise<RunVerdict> {
  const delays = o.delays ?? RECONNECT_DELAYS_MS;
  const sleep = o.sleep ?? ((ms: number) => new Promise((r) => setTimeout(r, ms)));
  const total = delays.length;
  for (let n = 1; n <= total; n++) {
    if (o.aborted?.()) return { kind: "aborted" };
    o.onAttempt?.(n, total);
    if (delays[n - 1] > 0) await sleep(delays[n - 1]);
    if (o.aborted?.()) return { kind: "aborted" };
    try {
      const tail = await o.attempt(n);
      if (tail.kind === "lost") return { kind: "lost", notice: tail.notice };
      if (tail.kind === "finished") return { kind: "finished" };
      if (tail.kind === "failed") return { kind: "failed", notice: tail.notice };
      // "open": поток закрылся, не дойдя до done — пробуем ещё раз.
    } catch (e) {
      if (isAbort(e) || o.aborted?.()) return { kind: "aborted" };
    }
  }
  return { kind: "unreachable", attempts: total };
}

/** Состояние прерванного хода — ПОЛЕ сообщения, а не строка в его тексте. */
export type InterruptedState =
  | { state: "reconnecting"; attempt: number; total: number }
  | { state: "lost"; notice: string }
  | { state: "failed"; notice: string }
  | { state: "unreachable" };

/** Что показать и что предложить нажать. */
export type InterruptedNotice = {
  text: string;
  /** «resend» — гнать заново (работы на сервере нет);
   *  «reattach» — догнать ответ (работа есть, повторный запуск её осиротит);
   *  «none» — ждём, кнопка не нужна. */
  action: "none" | "resend" | "reattach";
  actionLabel: string;
};

/**
 * Текст и действие по состоянию. Действия РАЗНЫЕ не для красоты: в случае
 * живого прогона повторная отправка затирает маркер `activeChatRun` и
 * осиротит готовый результат — предлагать «повторить» там нельзя.
 */
export function interruptedNotice(i: InterruptedState): InterruptedNotice {
  if (i.state === "lost") {
    return {
      text: `${i.notice} Результата за ним нет — работа оборвалась на середине.`,
      action: "resend",
      actionLabel: "Отправить заново",
    };
  }
  if (i.state === "failed") {
    return {
      text: i.notice
        ? `Прогон завершился ошибкой: ${i.notice}. Результата за ним нет.`
        : "Прогон завершился ошибкой. Результата за ним нет.",
      action: "resend",
      actionLabel: "Отправить заново",
    };
  }
  if (i.state === "unreachable") {
    return {
      text:
        "Связь оборвалась, но прогон продолжается на сервере — результат сохраняется. " +
        "Догоните ответ или обновите страницу; повторять запрос не нужно.",
      action: "reattach",
      actionLabel: "Догнать ответ",
    };
  }
  return {
    text: `Связь оборвалась — переподключаюсь (попытка ${i.attempt} из ${i.total})…`,
    action: "none",
    actionLabel: "",
  };
}
