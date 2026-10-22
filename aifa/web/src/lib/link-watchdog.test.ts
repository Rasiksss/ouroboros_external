import { afterEach, describe, expect, it, vi } from "vitest";
import { AUTO_RETRY_MAX, createStreamWatchdog, вердиктОбрыва, можноПодписаться } from "./link-watchdog";

afterEach(() => vi.useRealTimers());

describe("долгая работа автора и связь имеют разные сроки", () => {
  it("первичное взведение не сбрасывает счётчик неудачных переподписок", () => {
    vi.useFakeTimers();
    let retries = 0;
    for (let index = 0; index <= AUTO_RETRY_MAX; index++) {
      const watchdog = createStreamWatchdog({
        onActivity: () => { retries = 0; },
        onStalled: vi.fn(),
        onLinkLost: (sawPing) => {
          if (вердиктОбрыва(sawPing, retries).догоняем) retries++;
        },
      });
      watchdog.armWatchdog();
      vi.advanceTimersByTime(10 * 60_000);
      watchdog.disarm();
    }
    expect(retries).toBe(AUTO_RETRY_MAX);
    expect(вердиктОбрыва(false, retries).догоняем).toBe(false);
  });

  it("ждёт автора20 минут с heartbeat, но срабатывает после30 без прогресса", () => {
    vi.useFakeTimers();
    const lost = vi.fn();
    const stalled = vi.fn();
    const watchdog = createStreamWatchdog({ onLinkLost: lost, onStalled: stalled, onActivity: vi.fn() });
    watchdog.armWatchdog();
    for (let minute = 1; minute <= 20; minute++) {
      vi.advanceTimersByTime(60_000);
      watchdog.armWatchdog("ping");
    }
    expect(lost).not.toHaveBeenCalled();
    expect(stalled).not.toHaveBeenCalled();
    for (let minute = 21; minute < 30; minute++) {
      vi.advanceTimersByTime(60_000);
      watchdog.armWatchdog("ping");
    }
    vi.advanceTimersByTime(60_000);
    expect(stalled).toHaveBeenCalledOnce();
    watchdog.disarm();
  });

  it("до первого heartbeat ждёт10 минут и выбирает догон того же прогона", () => {
    vi.useFakeTimers();
    const lost = vi.fn();
    const watchdog = createStreamWatchdog({ onLinkLost: lost, onStalled: vi.fn(), onActivity: vi.fn() });
    watchdog.armWatchdog();
    vi.advanceTimersByTime(5 * 60_000);
    expect(lost).not.toHaveBeenCalled();
    vi.advanceTimersByTime(5 * 60_000);
    expect(lost).toHaveBeenCalledWith(false);
    expect(вердиктОбрыва(false, 0).догоняем).toBe(true);
    watchdog.disarm();
  });

  it("disarm снимает оба таймера и не вызывает догон после ухода", () => {
    vi.useFakeTimers();
    const lost = vi.fn();
    const stalled = vi.fn();
    const watchdog = createStreamWatchdog({ onLinkLost: lost, onStalled: stalled, onActivity: vi.fn() });
    watchdog.armWatchdog("real");
    watchdog.armWatchdog("ping");
    watchdog.disarm();
    vi.advanceTimersByTime(60 * 60_000);
    expect(lost).not.toHaveBeenCalled();
    expect(stalled).not.toHaveBeenCalled();
  });
});

describe("сторож связи обязан делать то, что обещает", () => {
  it("обещав переподключение — переподключается", () => {
    // Ровно тот случай, что сорвал прогон 24.08: биение видели, догонов ещё
    // не было. Раньше здесь говорилось «переподключаюсь» и не делалось ничего.
    const v = вердиктОбрыва(true, 0);
    expect(v.догоняем).toBe(true);
    expect(v.текст).toContain("переподключаюсь");
  });

  it("не обещает переподключения, когда его не будет", () => {
    // Потолок исчерпан. Текст обязан смениться ВМЕСТЕ с поведением: обещание
    // догона, которого не случится, — это ровно тот дефект, что чиним.
    const v = вердиктОбрыва(true, AUTO_RETRY_MAX);
    expect(v.догоняем).toBe(false);
    expect(v.текст).not.toContain("переподключаюсь");
    expect(v.текст).toContain("Догнать ответ");
  });

  it("до первого биения восстанавливает подписку вместо повторного запроса", () => {
    // Бэкенд может быть старым и биения не слать. Объявлять обрыв связи по
    // короткому окну значило бы рвать штатную паузу в окне выкатки.
    const v = вердиктОбрыва(false, 0);
    expect(v.догоняем).toBe(true);
    expect(v.текст).toContain("переподключаюсь");
    expect(v.текст).not.toContain("попробуйте ещё раз");
  });

  it("догоны считаются ПОДРЯД, потолок не липнет к ходу целиком", () => {
    // Девятый догон ещё законен: иначе долгий прогон с редкими обрывами
    // упирался бы в потолок из-за давних, уже вылеченных.
    expect(вердиктОбрыва(true, AUTO_RETRY_MAX - 1).догоняем).toBe(true);
  });
});

describe("оборванная подписка не притворяется живой", () => {
  const живая = { signal: { aborted: false } };
  const мёртвая = { signal: { aborted: true } };

  it("живая подписка запрещает вторую — это защита от двойной отрисовки", () => {
    // Закрытый баг c9214e3: подписались дважды, каждая дельта рисовалась два раза.
    expect(можноПодписаться(живая)).toBe(false);
  });

  it("оборванная — НЕ запрещает: событий из неё не придёт никогда", () => {
    // Здесь и запирался догон навсегда. Карта чистится в `finally` потребителя,
    // а он завершается асинхронно — эффект читает карту раньше и видел запись.
    expect(можноПодписаться(мёртвая)).toBe(true);
  });

  it("пустая карта — подписываемся", () => {
    expect(можноПодписаться(undefined)).toBe(true);
  });
});
