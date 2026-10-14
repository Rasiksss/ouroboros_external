/**
 * Точечный заказ живёт в чате — рабочая область его live не рисует.
 *
 * РЕШЕНИЕ ВЛАДЕЛЬЦА 31.08.2026 (прогон 0038f0a6): «таблицу в рабочей области
 * заполняем, если прогон полный/ролевой; если просто говорим "посчитай чистый
 * долг" в чате — таблица в чате». Признак структурный: `narrow_order` ставит
 * скрипт навыка из СУЖЕНИЯ СОСТАВА (requested / свои формулы), никаких
 * слов-детектов «извлеки» в коде нет — владелец прямо велел их не заводить.
 */
import { describe, expect, it } from "vitest";
import {
  applyExtractEvent,
  areaExtractionOf,
  extractionFromStart,
  type ExtractionMessage,
} from "./projects";
import type { ExtractStreamEvent } from "./api";

const карточка = (over: Partial<ExtractionMessage>): ExtractionMessage =>
  ({
    id: over.id ?? "m1",
    role: "extraction",
    doc_id: "d1",
    doc_label: "НОВАТЭК",
    company_type: "non_financial",
    created_at: 1,
    status: "running",
    metrics: [],
    event_count: 0,
    ...over,
  }) as ExtractionMessage;

describe("признак точечного заказа доезжает до карточки", () => {
  it("фабрика карточки берёт narrow_order из шапки extract_start", () => {
    const m = extractionFromStart(
      { narrow_order: true, company_type: "non_financial" },
      { id: "m1", doc_id: "d1", doc_label: "НОВАТЭК", at: 1 },
    );
    expect(m.narrow_order).toBe(true);
  });

  it("реплей-стрим (событие start) тоже несёт признак — после обрыва карточка не забывает, что она чатовая", () => {
    const m = applyExtractEvent(карточка({}), {
      type: "start",
      narrow_order: true,
    } as unknown as ExtractStreamEvent);
    expect(m.narrow_order).toBe(true);
  });
});

describe("выбор карточки для рабочей области", () => {
  it("живой точечный прогон область пропускает — показывает прошлый полный итог", () => {
    const узкий = карточка({ id: "narrow", narrow_order: true, status: "running" });
    const полный = карточка({ id: "full", status: "done" });
    expect(areaExtractionOf([узкий, полный])?.id).toBe("full");
  });

  it("завершённый точечный прогон область ПОКАЗЫВАЕТ: это сохранённый итог, а не стрим", () => {
    const готов = карточка({ id: "narrow-done", narrow_order: true, status: "done" });
    expect(areaExtractionOf([готов])?.id).toBe("narrow-done");
  });

  it("живой ПОЛНЫЙ прогон рисуется как раньше — сужение его не задевает", () => {
    const полный = карточка({ id: "full-live", status: "running" });
    expect(areaExtractionOf([полный])?.id).toBe("full-live");
  });
});
