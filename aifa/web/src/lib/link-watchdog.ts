/**
 * Решения сторожа живого потока — отдельно от React, чтобы их можно было
 * ПРОВЕРИТЬ.
 *
 * Куплено прогоном 24.08.2026 (Северсталь). Сторож связи показывал «Связь с
 * сервером пропала — переподключаюсь» и делал `ac.abort()`. Дальше не
 * происходило ничего: обрыв ловил `catch` по `AbortError` и глотал молча, а
 * эффект переподписки висел на `[project.id, activeRunId, dropTick]` — из них
 * не менялась ни одна. `dropTick` двигала только кнопка «Догнать ответ».
 *
 * Ответ возвращала ТОЛЬКО перезагрузка страницы: она обнуляет `followedRuns`,
 * и эффект подхватывает прогон заново. Владелец делал это руками трижды за два
 * дня, каждый раз считая отдельным сбоем; в клиентском журнале это видно как
 * `late_answer_started` в ту же секунду, что и `dialogs_pull` — то есть догон
 * запускался НА ЗАГРУЗКЕ СТРАНИЦЫ, а не на обрыве.
 *
 * Обещание в тексте и поведение в коде разошлись — и текст был правдивее.
 */

/** Подряд идущих автодогонов на один ход. Дальше решает человек кнопкой. */
export const AUTO_RETRY_MAX = 10;
export const STREAM_IDLE_MS = 10 * 60 * 1000;
export const STREAM_LINK_MS = 120 * 1000;
export const STREAM_STALLED_MS = 30 * 60 * 1000;

export function createStreamWatchdog(callbacks: {
  onLinkLost: (sawPing: boolean) => void;
  onStalled: () => void;
  onActivity: () => void;
}) {
  let sawPing = false;
  let linkTimer: ReturnType<typeof setTimeout> | null = null;
  let stalledTimer: ReturnType<typeof setTimeout> | null = null;
  const armWatchdog = (kind?: "real" | "ping") => {
    if (kind === "ping") sawPing = true;
    if (kind !== undefined) callbacks.onActivity();
    if (linkTimer) clearTimeout(linkTimer);
    linkTimer = setTimeout(() => callbacks.onLinkLost(sawPing), sawPing ? STREAM_LINK_MS : STREAM_IDLE_MS);
    if (kind !== "ping") {
      if (stalledTimer) clearTimeout(stalledTimer);
      stalledTimer = setTimeout(callbacks.onStalled, STREAM_STALLED_MS);
    }
  };
  const disarm = () => {
    if (linkTimer) clearTimeout(linkTimer);
    if (stalledTimer) clearTimeout(stalledTimer);
  };
  return { armWatchdog, disarm };
}

export type ВердиктОбрыва = {
  /** Переподписываться самим? */
  догоняем: boolean;
  /** Что сказать человеку. Обещать догон, которого не будет, запрещено. */
  текст: string;
};

/**
 * Что делать, когда поток замолчал.
 *
 * `биениеБыло` — видели ли хоть один кадр `ping`. Пока не видели, молчание
 * означает «бэкенд старый и биения не шлёт», и судить по короткому окну нельзя.
 * `догоновСделано` — сколько раз подряд уже переподписывались без единого
 * события между попытками.
 */
export function вердиктОбрыва(биениеБыло: boolean, догоновСделано: number): ВердиктОбрыва {
  if (догоновСделано >= AUTO_RETRY_MAX) {
    return { догоняем: false, текст: "Связь не восстановилась. Нажмите «Догнать ответ»." };
  }
  return {
    догоняем: true,
    текст: биениеБыло
      ? "Связь с сервером пропала — переподключаюсь."
      : "Поток не присылал данных 10 минут — переподключаюсь к тому же прогону.",
  };
}

/**
 * Можно ли подписаться на прогон, если в карте `followedRuns` уже есть запись.
 *
 * Карта стоит ПРОТИВ ДВОЙНОЙ подписки (закрытый баг c9214e3: каждая дельта
 * рисовалась дважды). Но оборванная подписка — не подписка: её контроллер
 * прерван, событий из неё не придёт никогда. Проверять одно лишь НАЛИЧИЕ
 * записи значило запереть догон навсегда: карта чистится в `finally`
 * потребителя, а он завершается АСИНХРОННО — уже после того, как эффект
 * прочитал её синхронно.
 */
export function можноПодписаться(держит: { signal: { aborted: boolean } } | undefined): boolean {
  return !держит || держит.signal.aborted;
}
