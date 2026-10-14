import { describe, expect, it } from "vitest";
import type { FormattedTable } from "./api";
import {
  canReshapeTableRole,
  dieSegmentsTable,
  dieValuesTable,
  filterFormattedTable,
  filterRowsByOutputEnums,
  fmtFormattedCell,
  formattedCellValue,
  formattedDataRowCount,
  isGenericTableRole,
  reshapeBlockedReason,
  roleLayoutMissingNote,
  visibleFormattedColumns,
} from "./formatted-table";
import { tableRoleOf } from "./prefs";

/**
 * Раскладка роли: правила, на которых таблица либо показывает числа, либо
 * молча показывает пустоту.
 *
 * ИСТОРИЯ. Рабочая область читала ячейку как «первая колонка — `label`,
 * остальные — `values[key]`». Бэкенд кладёт значения не так: `cib` и `baifp`
 * пишут их ПОЛЯМИ строки, а `values` оставляют пустым; у `risks` подпись
 * лежит в `label_en`. Итог на экране: у CIB и БАИФП все колонки кроме первой
 * пустые, имя показателя стоит под заголовком «№», у «Рисков» пустая и первая.
 * Ошибка немая — таблица не падает, она врёт пустотой.
 */

/** Строка `cib`, как её отдаёт бэкенд: значения ПОЛЯМИ, `values` пустой. */
const CIB_TABLE: FormattedTable = {
  role: "cib",
  role_label: "CIB",
  company_type: "non_financial",
  format: "cib",
  columns: [
    { key: "n", label: "№" },
    { key: "label", label: "Показатель" },
    { key: "period", label: "Период" },
    { key: "value", label: "Значение", kind: "value" },
    { key: "unit", label: "Ед." },
  ],
  rows: [{ n: 1, label: "Выручка", period: "FY 2024", value: 1234567.5, unit: "млн ₽", values: {} }],
};

/** Строка `risks`: подпись в `label_en`, служебные колонки — под срез. */
const RISKS_TABLE: FormattedTable = {
  role: "risks",
  role_label: "Риски",
  company_type: "bank",
  format: "risks",
  columns: [
    { key: "label_en", label: "Показатель" },
    { key: "label_ru", label: "RU" },
    { key: "blank1", label: "" },
    { key: "2024", label: "2024", kind: "value" },
  ],
  rows: [
    {
      label_en: "Достаточность капитала",
      label_ru: "Достаточность капитала",
      is_percent: true,
      "2024": 0.145,
      values: {},
    },
  ],
};

/** Строка `dgr`: значения действительно в `values` — второй способ бэкенда. */
const DGR_TABLE: FormattedTable = {
  role: "dgr",
  role_label: "ДГР",
  company_type: "bank",
  format: "dgr",
  columns: [
    { key: "label", label: "Показатель" },
    { key: "2024", label: "2024", kind: "value" },
  ],
  rows: [{ label: "Активы", values: { "2024": 42 } }],
};

function renderRow(table: FormattedTable, index = 0): string[] {
  const row = table.rows[index];
  return visibleFormattedColumns(table).map((c) => fmtFormattedCell(formattedCellValue(row, c), c, row));
}

describe("значение ячейки берётся и с верхнего уровня строки, и из values", () => {
  it("cib: заполнены ВСЕ колонки, а не одна первая", () => {
    const cells = renderRow(CIB_TABLE);
    expect(cells[0]).toBe("1");
    expect(cells[1]).toBe("Выручка");
    expect(cells[2]).toBe("FY 2024");
    expect(cells[4]).toBe("млн ₽");
    // Пустых клеток быть не должно ни одной — именно так выглядел дефект.
    expect(cells.filter((c) => c === "")).toHaveLength(0);
  });

  it("risks: подпись строки не теряется, хотя лежит в label_en", () => {
    expect(renderRow(RISKS_TABLE)[0]).toBe("Достаточность капитала");
  });

  it("dgr: второй способ бэкенда (values) продолжает работать", () => {
    expect(renderRow(DGR_TABLE)).toEqual(["Активы", "42"]);
  });

  it("🔴 порядок именно такой: поле строки важнее одноимённого ключа в values", () => {
    // Отрицательный контроль на перестановку веток: если сначала читать
    // `values`, cib снова опустеет — там `values` пустой объект.
    const row = { label: "Выручка", values: { label: "" } };
    expect(formattedCellValue(row, { key: "label", label: "Показатель" })).toBe("Выручка");
  });
});

describe("формат числа", () => {
  it("доля превращается в проценты, а не показывается как 0.145", () => {
    const cells = renderRow(RISKS_TABLE);
    expect(cells[cells.length - 1]).toMatch(/^14[.,]5\s?%$/);
  });

  it("большое число разряжается, а не печатается как есть", () => {
    const cells = renderRow(CIB_TABLE);
    expect(cells[3]).not.toBe("1234567.5");
    expect(cells[3].replace(/\s/g, "").replace(",", ".")).toContain("1234567.5");
  });

  it("двоичный хвост числа не попадает в таблицу", () => {
    const column = { key: "v", label: "V", kind: "value" };
    expect(fmtFormattedCell(42085.931000000004, column, {})).toBe("42 085,93");
  });

  it("пусто остаётся пустым — ноль и null различимы", () => {
    const column = { key: "v", label: "V", kind: "value" };
    expect(fmtFormattedCell(null, column, {})).toBe("");
    expect(fmtFormattedCell(undefined, column, {})).toBe("");
    expect(fmtFormattedCell(0, column, {})).toBe("0");
  });
});

describe("колонки: служебные не рисуются", () => {
  it("risks: label_ru и blank1 срезаны", () => {
    expect(visibleFormattedColumns(RISKS_TABLE).map((c) => c.key)).toEqual(["label_en", "2024"]);
  });

  it("хвостовые распорки excel-выгрузки не становятся столбцами", () => {
    const table: FormattedTable = {
      ...DGR_TABLE,
      columns: [
        { key: "label", label: "Показатель" },
        { key: "2024", label: "2024", kind: "value" },
        { key: "blank2", label: "" },
        { key: "spacer:1", label: "" },
      ],
    };
    expect(visibleFormattedColumns(table).map((c) => c.key)).toEqual(["label", "2024"]);
  });

  it("hidden уважается", () => {
    const table: FormattedTable = {
      ...DGR_TABLE,
      columns: [{ key: "label", label: "Показатель" }, { key: "tech", label: "Тех", hidden: true }],
    };
    expect(visibleFormattedColumns(table).map((c) => c.key)).toEqual(["label"]);
  });
});

describe("поиск по роль-таблице", () => {
  const table: FormattedTable = {
    ...DGR_TABLE,
    rows: [
      { kind: "section", label: "Баланс" },
      { label: "Активы", values: { "2024": 42 } },
      { kind: "spacer" },
      { label: "Обязательства", values: { "2024": 7 } },
    ],
  };

  it("пустой запрос ничего не трогает", () => {
    expect(filterFormattedTable(table, "   ")).toBe(table);
  });

  it("найденная строка приходит вместе со своим разделом", () => {
    const out = filterFormattedTable(table, "актив");
    expect(out.rows.map((r) => r.label)).toEqual(["Баланс", "Активы"]);
  });

  it("раздел без находок не остаётся сиротой", () => {
    expect(filterFormattedTable(table, "нетакого").rows).toHaveLength(0);
  });

  it("ищет и по значению ячейки, не только по подписи", () => {
    expect(filterFormattedTable(table, "42").rows.map((r) => r.label)).toEqual(["Баланс", "Активы"]);
  });

  it("счёт строк не считает заголовки и распорки", () => {
    expect(formattedDataRowCount(table)).toBe(2);
  });
});

describe("ДИЭ: значения и сегменты — разные вкладки", () => {
  const table: FormattedTable = {
    role: "die",
    role_label: "ДИЭ",
    company_type: "non_financial",
    format: "die_nf",
    columns: [{ key: "label", label: "Показатель" }],
    rows: [
      { label: "Выручка" },
      { kind: "spacer" },
      { die_kind: "segment_section", label: "Структура выручки" },
      { label: "Сегмент А" },
    ],
  };

  it("на «Показателях» сегментов нет", () => {
    expect(dieValuesTable(table).rows.map((r) => r.label)).toEqual(["Выручка"]);
  });

  it("на «Сегментах» — только они", () => {
    expect(dieSegmentsTable(table)?.rows.map((r) => r.label)).toEqual([
      "Структура выручки",
      "Сегмент А",
    ]);
  });

  it("не-ДИЭ таблицу не режем и второй вкладки ей не выдумываем", () => {
    expect(dieValuesTable(DGR_TABLE)).toBe(DGR_TABLE);
    expect(dieSegmentsTable(DGR_TABLE)).toBeNull();
  });
});

describe("#142: перестраивать можно только с общей роли", () => {
  it("общая и «все показатели» разрешают смену", () => {
    expect(canReshapeTableRole("other")).toBe(true);
    expect(canReshapeTableRole("all")).toBe(true);
  });

  it("🔴 узкий прогон смену запрещает", () => {
    // Отрицательный контроль на снятие гейта: вернётся `true` — вернётся и
    // почти пустая таблица CIB, построенная по набору ДИЭ.
    for (const role of ["die", "cib", "risks", "baifp", "dgr"]) {
      expect(canReshapeTableRole(role)).toBe(false);
    }
  });

  it("причина названа словами, а не серой кнопкой", () => {
    const reason = reshapeBlockedReason("ДИЭ");
    expect(reason).toContain("ДИЭ");
    expect(reason).toContain("общей роли");
  });
});

describe("роль нормализуется ОДНИМ списком", () => {
  it("«all» — это обычная таблица, а не запрос за несуществующей раскладкой", () => {
    // Раньше подпись кнопки считалась по ROLE_OPTIONS («Общая»), а запрос — по
    // EXTRACTION_ROLE_OPTIONS («all»): экран говорил одно, сеть делала другое.
    expect(tableRoleOf("all")).toBe("other");
    expect(isGenericTableRole(tableRoleOf("all"))).toBe(true);
  });

  it("легаси-псевдонимы доезжают до своей роли", () => {
    expect(tableRoleOf("мглоб")).toBe("dgr");
    expect(tableRoleOf("MGLOB")).toBe("dgr");
  });

  it("неизвестное значение не уходит на сервер", () => {
    expect(tableRoleOf("зам_директора")).toBe("other");
    expect(tableRoleOf(undefined)).toBe("other");
  });
});

describe("output_enums сужает набор строк", () => {
  const rows = [{ enum: "TOTAL_ASSETS" }, { enum: "NET_INCOME" }, { enum: "EQUITY" }];

  it("список роли оставляет только свои строки", () => {
    expect(filterRowsByOutputEnums(rows, "die", ["net_income", "equity"]).map((r) => r.enum)).toEqual([
      "NET_INCOME",
      "EQUITY",
    ]);
  });

  it("🔴 пустой список — это «фильтра нет», а не «показывать нечего»", () => {
    // Отрицательный контроль: без проверки длины таблица и счётчики
    // обнулялись бы на любой роли без раскладки — бэкенд шлёт именно [].
    expect(filterRowsByOutputEnums(rows, "dgr", [])).toHaveLength(3);
    expect(filterRowsByOutputEnums(rows, "dgr", null)).toHaveLength(3);
  });

  it("общая роль не фильтруется никогда", () => {
    expect(filterRowsByOutputEnums(rows, "other", ["EQUITY"])).toHaveLength(3);
  });
});

describe("пустой ответ ручки называется вслух", () => {
  it("нет раскладки под пару роль×отчётность — так и сказано", () => {
    const note = roleLayoutMissingNote("ДГР", "unsupported");
    expect(note).toContain("ДГР");
    expect(note).toContain("нет");
  });

  it("раскладка пришла, но строк в ней нет — это другой случай", () => {
    expect(roleLayoutMissingNote("CIB", "cib", true)).toContain("пустая");
  });
});
