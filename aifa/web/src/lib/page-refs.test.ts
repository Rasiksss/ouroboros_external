import { describe, expect, it } from "vitest";
import {
  PAGE_HREF_PREFIX,
  linkifyPageRefs,
  pageFromHref,
  splitPageRefs,
} from "@/lib/page-refs";

/**
 * Ссылка на страницу исходника (#157) — фронтовая половина инварианта
 * «страницу нашёл ДВИЖОК, а не модель».
 *
 * Бэкенд (`reasoner.runner.scrub_page_refs`) оставляет запись «PDF стр. N»
 * ТОЛЬКО у страниц, которые движок реально показал модели в таблице
 * заземления; выдуманный номер он переписывает в «стр. N (страница не
 * подтверждена движком)». Здесь стережётся вторая половина: фронт делает
 * кликабельной ровно первую запись и НЕ узнаёт вторую. Иначе снятие пометки
 * на бэкенде стало бы бессмысленным — ссылка появилась бы всё равно.
 */

// Ровно та строка, которую бэкенд оставляет после сверки.
const VERIFIED = "Выручка выросла на 12% (PDF стр. 42).";
// И ровно та, в которую он переписывает непроверенный номер.
const DEMOTED = "Прибыль снизилась (стр. 7 (страница не подтверждена движком)).";

describe("splitPageRefs — проза карточки", () => {
  it("подтверждённая страница становится ссылкой", () => {
    const parts = splitPageRefs(VERIFIED);
    const links = parts.filter((p) => p.page > 0);
    expect(links).toEqual([{ text: "PDF стр. 42", page: 42 }]);
    // Текст не теряется: склейка кусков = исходная строка.
    expect(parts.map((p) => p.text).join("")).toBe(VERIFIED);
  });

  it("разжалованная бэкендом страница ссылкой НЕ становится", () => {
    expect(splitPageRefs(DEMOTED).filter((p) => p.page > 0)).toEqual([]);
  });

  it("голое «стр. N» без маркера PDF не кликается", () => {
    expect(splitPageRefs("см. стр. 15 отчёта").filter((p) => p.page > 0)).toEqual([]);
  });

  it("несколько ссылок в одной строке разбираются все", () => {
    const pages = splitPageRefs("PDF стр. 3 и PDF стр. 118")
      .filter((p) => p.page > 0)
      .map((p) => p.page);
    expect(pages).toEqual([3, 118]);
  });
});

describe("linkifyPageRefs — markdown-разделы", () => {
  it("подтверждённая страница превращается в markdown-ссылку", () => {
    expect(linkifyPageRefs(VERIFIED)).toContain(`[PDF стр. 42](${PAGE_HREF_PREFIX}42)`);
  });

  it("разжалованная страница остаётся текстом", () => {
    expect(linkifyPageRefs(DEMOTED)).toBe(DEMOTED);
  });
});

describe("pageFromHref — что перехватывает обработчик карточки", () => {
  it("узнаёт свою ссылку", () => {
    expect(pageFromHref(`${PAGE_HREF_PREFIX}42`)).toBe(42);
  });

  it("чужие ссылки не трогает", () => {
    expect(pageFromHref("https://example.com/#pdf-page-42")).toBe(0);
    expect(pageFromHref("#section-2")).toBe(0);
    expect(pageFromHref(null)).toBe(0);
  });
});
