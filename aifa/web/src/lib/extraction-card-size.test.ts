import { describe, expect, it } from "vitest";

import type { FormattedTable } from "@/lib/api";
import { applyExtractEvent, type ExtractionMessage } from "@/lib/projects";

/**
 * Вес карточки извлечения — не эстетика, а работоспособность загрузки.
 *
 * Замер 11.08.2026 по серверному блобу владельца: самая тяжёлая карточка —
 * 150 КБ, из них formatted 35 368 байт и table_formatted ТЕ ЖЕ 35 368 байт,
 * побайтово. При пределе localStorage в единицы мегабайт этот дубль съедал
 * место, из-за нехватки которого не сохранялся новый чат и не прикреплялся
 * документ.
 */
function table(rows: number): FormattedTable {
  return {
    role: "cib",
    role_label: "CIB",
    company_type: "nonfinancial",
    format: "cib_long",
    sheet_name: "Показатели",
    columns: ["Показатель", "FY2024", "FY2023"],
    rows: Array.from({ length: rows }, (_, i) => ({
      enum: `M_${i}`,
      name_ru: `Показатель ${i}`,
      values: ["1", "2"],
    })),
    periods: ["FY2024", "FY2023"],
  } as unknown as FormattedTable;
}

function card(): ExtractionMessage {
  return {
    id: "x1",
    role: "extraction",
    created_at: 1,
    doc_id: "doc-1",
    doc_label: "отчёт.pdf",
    status: "running",
    metrics: [],
  } as unknown as ExtractionMessage;
}

describe("вес карточки извлечения", () => {
  it("done не кладёт вторую копию таблицы в table_formatted", () => {
    const done = applyExtractEvent(card(), {
      type: "done",
      formatted: table(120),
    } as never);

    expect(done.formatted).toBeTruthy();
    expect(done.table_formatted).toBeUndefined();
  });

  it("раскладку, выбранную пользователем, done не затирает", () => {
    // table_formatted означает «пользователь сменил раскладку». Пользователь
    // мог сделать это ДО прихода done — его выбор сильнее.
    const mine = table(3);
    const done = applyExtractEvent({ ...card(), table_formatted: mine }, {
      type: "done",
      formatted: table(120),
    } as never);

    expect(done.table_formatted).toBe(mine);
  });

  it("карточка не весит вдвое больше своей таблицы", () => {
    const done = applyExtractEvent(card(), {
      type: "done",
      formatted: table(120),
    } as never);

    const whole = JSON.stringify(done).length;
    const one = JSON.stringify(done.formatted).length;
    // С дублем было бы ≥ 2×. Порог 1.5× ловит возврат дубля и оставляет запас
    // на остальные поля карточки.
    expect(whole).toBeLessThan(one * 1.5);
  });
});
