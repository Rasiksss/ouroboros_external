import { describe, expect, it } from "vitest";

import { scopeRowsToSlice } from "./table-slice";

const ПРОЧИТАНЫ = ["SALES_REVENUE", "OPERATING_PROFIT", "TOTAL_ASSETS"];
const ФОРМУЛЫ = ["EBITDA", "ROE", "NET_DEBT", "FREE_CASH_FLOW"];
const rows = [...ПРОЧИТАНЫ, ...ФОРМУЛЫ].map((enumId) => ({ enum: enumId }));

describe("срез показа таблицы", () => {
  it("оставляет ровно то, что просил человек", () => {
    // Живой случай 22.08: девять прочитанных против шестидесяти на экране.
    const shown = scopeRowsToSlice(rows, ПРОЧИТАНЫ);
    expect(shown.map((r) => r.enum)).toEqual(ПРОЧИТАНЫ);
  });

  it("срез применяется и без роли — «Общая» не исключение", () => {
    // Ровно этот случай и был сломан: у роли other фильтр не применялся вовсе,
    // чем бы срез ни был, и человек видел весь реестр ячейки.
    expect(scopeRowsToSlice(rows, ["SALES_REVENUE"]).map((r) => r.enum)).toEqual([
      "SALES_REVENUE",
    ]);
  });

  it("пустой срез — это «фильтра нет», а не «показывать нечего»", () => {
    // role_format=unsupported шлёт пустой массив; спутать значит обнулить
    // рабочую область на полностью удавшемся прогоне.
    expect(scopeRowsToSlice(rows, [])).toHaveLength(rows.length);
    expect(scopeRowsToSlice(rows, null)).toHaveLength(rows.length);
    expect(scopeRowsToSlice(rows, undefined)).toHaveLength(rows.length);
  });

  it("срез из одних пробелов таблицу не обнуляет", () => {
    expect(scopeRowsToSlice(rows, ["", "   "])).toHaveLength(rows.length);
  });

  it("регистр и пробелы в именах не теряют строк", () => {
    expect(scopeRowsToSlice(rows, [" sales_revenue "]).map((r) => r.enum)).toEqual([
      "SALES_REVENUE",
    ]);
  });
});
