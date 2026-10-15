import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { fetchLastRun } from "./api";
import { extractionFromStoredRun } from "./projects";

/**
 * Таблица прогона читается С СЕРВЕРА, а не только из localStorage (#276).
 *
 * ДЕФЕКТ. Исход прогона жил ровно в двух местах: в потоке SSE открытой вкладки
 * и в хранилище браузера. Вкладку закрыли раньше конца прогона — час работы
 * движка исчезал. 18.08 так пропали ДОМ.РФ и ГК «Самолёт»: в логе бэкенда
 * «прогресс станции некуда доставить … подписчика нет», на сервере таблицы нет
 * нигде.
 *
 * Бэкенд теперь кладёт СОБРАННЫЙ ЭКРАН в строку прогона — те же три тела
 * событий, из которых карточка складывается в живом потоке. Здесь проверяется
 * обратное разворачивание.
 */
afterEach(() => vi.unstubAllGlobals());

const SCREEN = {
  start: {
    doc_id: "doc-samolet",
    company_name: "ГК «Самолёт»",
    company_type: "non_financial",
    total_extractable: 110,
    period_current: "2024",
    period_prev: "2023",
    unit: "млн ₽",
  },
  rows: [
    { enum: "NET_PROFIT", name_ru: "Чистая прибыль", value_current: 12.5, value_prev: 9, found: true },
    { enum: "TOTAL_EQUITY", name_ru: "Итого капитал", value_current: 80, value_prev: 70, found: true },
  ],
  done: {
    metrics_found: 2,
    role: "dgr",
    role_label: "ДГР",
    role_format: "wide",
    table_output_enums: ["NET_PROFIT", "TOTAL_EQUITY"],
    formatted: { columns: [], rows: [] },
  },
};

const stored = (over: Partial<Record<string, unknown>> = {}) =>
  ({
    nonce: "4ce68a11deadbeef",
    doc_id: "doc-samolet",
    finished_at: 1_755_500_000,
    screen: SCREEN,
    ...over,
  }) as Parameters<typeof extractionFromStoredRun>[0];

describe("прогон, прочитанный с сервера", () => {
  it("разворачивается в ту же карточку, что рисует живой поток", () => {
    const card = extractionFromStoredRun(stored(), "ГК «Самолёт»");

    expect(card).not.toBeNull();
    expect(card!.doc_id).toBe("doc-samolet");
    expect(card!.status).toBe("done");
    expect(card!.metrics.map((m) => m.enum)).toEqual(["NET_PROFIT", "TOTAL_EQUITY"]);
    expect(card!.meta?.period_current).toBe("2024");
    expect(card!.meta?.company_name).toBe("ГК «Самолёт»");
    // Роль и её белый список обязаны доехать: без них таблица нарисуется
    // общей раскладкой под подписью «ДГР» — молча и правдоподобно.
    expect(card!.extraction_role).toBe("dgr");
    expect(card!.table_output_enums).toEqual(["NET_PROFIT", "TOTAL_EQUITY"]);
    expect(card!.formatted).toBeTruthy();
  });

  it("карточка одного прогона не размножается при повторном чтении", () => {
    // id выведен из нонса. Случайный id давал бы новую карточку на каждый
    // ответ сервера — рабочая область пересобирала бы таблицу на ровном месте.
    expect(extractionFromStoredRun(stored(), "x")!.id).toBe(
      extractionFromStoredRun(stored(), "x")!.id,
    );
  });

  it("оборванный прогон отдаёт строки, но НЕ называется завершённым", () => {
    // Так выглядит прогон по Банку «Санкт-Петербург» 19.08: 133 строки
    // приехали, терминала не было. Пометить его «готово» значило бы соврать
    // дважды — про полноту набора и про то, что раскладка роли просто пуста,
    // а не отсутствует.
    const card = extractionFromStoredRun(
      stored({
        status: "failed",
        error: "фоновый воркер затих, не отдав результат",
        screen: { start: SCREEN.start, rows: SCREEN.rows, done: null },
      }),
      "БАНК «САНКТ-ПЕТЕРБУРГ",
    );

    expect(card!.status).toBe("error");
    expect(card!.error).toContain("затих");
    expect(card!.metrics).toHaveLength(2);
    expect(card!.meta?.period_current).toBe("2024");
    expect(card!.formatted).toBeUndefined();
  });

  it("экран без строк — это «показывать нечего», а не пустая таблица", () => {
    // Пустая карточка означала бы «прогон прошёл и не нашёл ничего» — прямая
    // ложь про прогон, который просто старее этой правки.
    expect(extractionFromStoredRun(stored({ screen: null }), "x")).toBeNull();
    expect(extractionFromStoredRun(stored({ screen: { rows: [] } }), "x")).toBeNull();
  });
});

describe("ручка исходов", () => {
  it("404 — это «прогонов нет», а не ошибка", async () => {
    // На стенде без FINENGINE_RUNS_DATABASE_URL хранилище выключено и молчит
    // всегда. Ронять на этом рабочую область нельзя: она и без сервера рисует
    // то, что есть локально.
    vi.stubGlobal("fetch", async () => new Response("", { status: 404 }));
    await expect(fetchLastRun("doc-1")).resolves.toBeNull();
  });

  it("настоящий отказ доносится наверх, а не глотается", async () => {
    vi.stubGlobal("fetch", async () => new Response("boom", { status: 500 }));
    await expect(fetchLastRun("doc-1")).rejects.toThrow(/boom/);
  });

  it("спрашивается вид прогона — иначе исход анализа встанет вместо таблицы", async () => {
    const seen: string[] = [];
    vi.stubGlobal("fetch", async (url: string) => {
      seen.push(url);
      return new Response("", { status: 404 });
    });
    await fetchLastRun("doc-1");
    expect(seen[0]).toBe("/api/runs/by-doc/doc-1?kind=extract");
  });
});

describe("рабочая область действительно спрашивает сервер", () => {
  const PANE = readFileSync(
    fileURLToPath(new URL("../components/workspace/panes/extracted-pane.tsx", import.meta.url)),
    "utf8",
  );

  it("зона документа читает прогон с сервера, а не только из проекта", () => {
    // 🔴 СТОРОЖ ДОСТАВКИ, А НЕ ЛОГИКИ. Ручка `/runs/by-doc` и разворачивание
    // экрана могут быть безупречны и при этом не значить НИЧЕГО — ровно так
    // первая редакция хранилища исходов пролежала на стенде сутки, не сделав
    // ничего: `grep -rn "/runs/by-doc" web/src` не находил ни одного места.
    expect(PANE).toContain("fetchLastRun(activeDocId)");
    expect(PANE).toContain("extractionFromStoredRun(stored, docLabel)");
  });

  it("серверный прогон не подменяет ЖИВУЮ карточку", () => {
    // Иначе таблица замирала бы посреди прогона: строки доезжают потоком, а
    // сервер отдаёт исход только завершённого.
    expect(PANE).toContain("local?.status !== \"running\" || Boolean(project?.activeChatRun)");
  });
});

describe("живой поток и читатель собирают карточку ОДНОЙ фабрикой", () => {
  const LIVE = readFileSync(
    fileURLToPath(new URL("../components/workspace/use-chat-run.ts", import.meta.url)),
    "utf8",
  );

  it("в обработчике extract_start нет своей сборки карточки", () => {
    // 🔴 БЕЗ ЭТОГО СТОРОЖА ДВЕ КАРТОЧКИ РАЗЪЕДУТСЯ МОЛЧА. Здесь лежал литерал
    // на пятнадцать полей; появись шестнадцатое — читатель сохранённого
    // прогона о нём не узнает, и таблица после перезагрузки страницы окажется
    // другой. Обе при этом выглядят правдоподобно, различие видно только сверкой.
    expect(LIVE).toContain("extractionFromStart(ev, {");
    expect(LIVE).not.toContain('role: "extraction",');
  });
});
