/**
 * Размышления агента по дороге ≠ финальный ответ.
 *
 * 🔴 ТРИ ЖАЛОБЫ ВЛАДЕЛЬЦА, и правило дважды переезжало. История нужна тут
 * целиком — каждая следующая починка ломала предыдущую:
 *
 * 1. «Стриминг размышлений выглядит как финальный ответ» — порции модели
 *    склеивались в одно тело. Первая починка: демоция по `tool_progress` и
 *    прокси-`tool_call`.
 * 2. «Текст режется посреди слова, ответа в чате нет» — те события идут
 *    ДРУГИМ каналом и падают посреди предложения. Вторая починка: границы
 *    ставит только `text_segment_end`.
 * 3. «Опять беда с визуализацией» (прогон f0494f77, 31.08.2026): модель
 *    закрыла message-item ПОСРЕДИ фразы — «…с округлением до» /
 *    segment_end / «копеек).» — и начало ответа уехало в заметку. А в
 *    прогоне 9d2dad93 segment_end не пришёл НИ РАЗУ за 117 событий: на
 *    прокси-тулах модель item не закрывает, и без демоции по тулу вся
 *    проза оставалась бы в ответе.
 *
 * КОНТРАКТ ИТОГОВЫЙ: text_segment_end текст НЕ режет (ненадёжен с обеих
 * сторон). Прозу от ответа отделяет ФАКТ РАБОТЫ: любой `tool_call` (прокси
 * и инлайновый — одинаково) демоцирует накопленный текст заметкой в ленту.
 * `tool_progress` — только строка состояния. Текст после последнего
 * инструмента — ответ.
 */
import { describe, expect, it } from "vitest";
import { applyChatEvent, demoteProseToNote, resetForReplay, type TextMessage } from "./projects";

const пустое = (): TextMessage =>
  ({ id: "m1", role: "assistant", content: "", created_at: 0 }) as TextMessage;

describe("конец message-item текст не режет (жалоба №3)", () => {
  it("живой прогон f0494f77: segment_end посреди фразы — ответ целый", () => {
    let m = пустое();
    for (const кусок of ["451 621 × 1,15 = ", "**519 364,15** ", "(с округлением до"]) {
      m = applyChatEvent(m, { type: "text_delta", text: кусок } as never);
    }
    m = applyChatEvent(m, { type: "text_segment_end" } as never);
    m = applyChatEvent(m, { type: "text_delta", text: " копеек)." } as never);
    expect(m.content).toBe("451 621 × 1,15 = **519 364,15** (с округлением до копеек).");
    expect((m.beats ?? []).filter((b) => b.kind === "note")).toHaveLength(0);
  });

  it("финальный ответ не опустошается своим же segment_end", () => {
    let m = пустое();
    m = applyChatEvent(m, { type: "text_delta", text: "Чистый долг — 80 961." } as never);
    m = applyChatEvent(m, { type: "text_segment_end" } as never);
    expect(m.content).toBe("Чистый долг — 80 961.");
    expect((m.beats ?? []).length).toBe(0);
  });
});

describe("прозу от ответа отделяет факт работы", () => {
  it("прокси-tool_call демоцирует прозу заметкой — прогон 9d2dad93 шёл вовсе без segment_end", () => {
    let m = пустое();
    m = applyChatEvent(m, { type: "text_delta", text: "Смотрю, есть ли ROE в реестре." } as never);
    m = applyChatEvent(m, { type: "tool_call", name: "read_source", proxied: true } as never);
    m = applyChatEvent(m, { type: "text_delta", text: "ROE есть — запускаю расчёт." } as never);
    m = applyChatEvent(m, { type: "tool_call", name: "read_source", proxied: true } as never);
    m = applyChatEvent(m, { type: "text_delta", text: "Готово: 25,4 %." } as never);

    const заметки = (m.beats ?? []).filter((b) => b.kind === "note").map((b) => b.text);
    expect(заметки).toEqual(["Смотрю, есть ли ROE в реестре.", "ROE есть — запускаю расчёт."]);
    expect(m.content).toBe("Готово: 25,4 %.");
  });

  it("инлайновый tool_call демоцирует так же", () => {
    let m = пустое();
    m = applyChatEvent(m, { type: "text_delta", text: "Проверю данные." } as never);
    m = applyChatEvent(m, { type: "tool_call", name: "fe_news_lookup" } as never);
    expect(m.content).toBe("");
    expect(m.beats?.some((b) => b.kind === "note" && b.text === "Проверю данные.")).toBe(true);
  });

  it("tool_progress обновляет строку состояния и не трогает текст", () => {
    let m = пустое();
    m = applyChatEvent(m, { type: "text_delta", text: "Это займёт несколько " } as never);
    m = applyChatEvent(m, { type: "tool_progress", name: "fe", text: "Читаю отчёт" } as never);
    expect(m.thinking).toBe("Читаю отчёт");
    m = applyChatEvent(m, { type: "text_delta", text: "минут." } as never);
    expect(m.content).toBe("Это займёт несколько минут.");
    expect((m.beats ?? []).filter((b) => b.kind === "note")).toHaveLength(0);
  });

  it("demoteProseToNote — то же правило для extract_start (зовёт use-chat-run)", () => {
    let m = пустое();
    m = applyChatEvent(m, { type: "text_delta", text: "Создам файл и запущу расчёт" } as never);
    m = demoteProseToNote(m);
    expect(m.content).toBe("");
    expect(m.beats?.at(-1)).toEqual({ kind: "note", text: "Создам файл и запущу расчёт" });
    // пустой контент — не заметка
    expect((demoteProseToNote(m).beats ?? []).length).toBe(1);
  });
});

describe("реплей после обрыва строит сообщение с нуля", () => {
  /** 🔴 Воспроизведение прогона владельца 31.08: «Список источников,
   *  Классификация отчёта» стояли в ленте ДВАЖДЫ — по разу на подписку.
   *  Сброс перед реплеем забывал `beats`, и журнал дописывался к старому. */
  const события = [
    { type: "text_delta", text: "Смотрю источники." },
    { type: "tool_call", name: "list_sources", proxied: true },
    { type: "tool_call", name: "fe_classify_report", proxied: true },
    { type: "text_segment_end" },
    { type: "text_delta", text: "Готово: 77,8 %." },
  ] as const;

  const прогнать = (нач: TextMessage) =>
    события.reduce((m, ev) => applyChatEvent(m, ev as never), нач);

  it("повторный реплей не задваивает шаги и заметки", () => {
    const первая = прогнать(пустое());
    const вторая = прогнать(resetForReplay(первая));
    expect(вторая.beats).toEqual(первая.beats);
    expect((вторая.beats ?? []).filter((b) => b.kind === "tool")).toHaveLength(2);
    expect(вторая.content).toBe(первая.content);
  });

  it("сброс обнуляет ленту и контент", () => {
    let m = пустое();
    m = applyChatEvent(m, { type: "text_delta", text: "Хвост." } as never);
    m = applyChatEvent(m, { type: "text_segment_end" } as never);
    const после = resetForReplay(m);
    expect(после.beats).toEqual([]);
    expect(после.content).toBe("");
  });
});

describe("хвост оборванной фразы не становится началом ответа", () => {
  /** 🔴 Живые прогоны 9d2dad93 и f5a8cd62: модель рвёт фразу вокруг
   *  инструмента и шлёт хвост СКЛЕЕННЫМ с началом новой мысли в одной дельте
   *  («…формат входа для расчёта по» + тул + «среднимОба коэффициента…»).
   *  На экране это давало «среднимОба …» первым абзацем ответа. */
  it("«среднимОба» из прогона f5a8cd62: хвост уходит в заметку, ответ начинается с настоящего начала", () => {
    let m = пустое();
    m = applyChatEvent(m, { type: "text_delta", text: "смотрю формат входа для расчёта по" } as never);
    m = applyChatEvent(m, { type: "tool_call", name: "read_source", proxied: true } as never);
    m = applyChatEvent(m, { type: "text_delta", text: "среднимОба коэффициента уже посчитаны." } as never);

    const notes = (m.beats ?? []).filter((b) => b.kind === "note").map((b) => b.text);
    expect(notes).toEqual(["смотрю формат входа для расчёта по средним"]);
    expect(m.content).toBe("Оба коэффициента уже посчитаны.");
  });

  it("«извлеВижу» из прогона 9d2dad93 — та же склейка, латиница в хвосте не путает", () => {
    let m = пустое();
    m = applyChatEvent(m, { type: "text_delta", text: "проверяю, какие числа уже" } as never);
    m = applyChatEvent(m, { type: "tool_call", name: "read_source", proxied: true } as never);
    m = applyChatEvent(m, { type: "text_delta", text: "расчёт.TOTAL_EQUITY не извлечён. Дальше сам ответ." } as never);
    const notes = (m.beats ?? []).filter((b) => b.kind === "note").map((b) => b.text);
    expect(notes).toEqual(["проверяю, какие числа уже расчёт.TOTAL_EQUITY не извлечён."]);
    expect(m.content).toBe("Дальше сам ответ.");
  });

  it("ответ с заглавной после тула не трогается", () => {
    let m = пустое();
    m = applyChatEvent(m, { type: "text_delta", text: "запускаю расчёт" } as never);
    m = applyChatEvent(m, { type: "tool_call", name: "read_source", proxied: true } as never);
    m = applyChatEvent(m, { type: "text_delta", text: "Готово: 25,4 %." } as never);
    expect(m.content).toBe("Готово: 25,4 %.");
    expect((m.beats ?? []).filter((b) => b.kind === "note").map((b) => b.text)).toEqual([
      "запускаю расчёт",
    ]);
  });
});
