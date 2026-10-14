import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { calculationHint } from "./extracted-table";
import type { MetricRow } from "@/lib/projects";

/**
 * Подсказка расчётной строки показывает не только формулу, но и её числа (Б5).
 *
 * ЦЕНА. В подсказке стоял один текст формулы. Проверить по нему число нельзя:
 * у EBITDA есть фолбэк, считающий по ДРУГОМУ выражению, а отсутствующий
 * необязательный вход вычислитель достраивает нулём — формула при этом
 * выглядит полной. Пустая расчётная строка вообще ничего не объясняла.
 *
 * ЦЕНА ВТОРАЯ (20.08, состязательная проверка). Ту же подпись печатает клетка
 * «Источник» ролевой книги (`roles/formatter.py::_calculated_source`), и они
 * РАЗОШЛИСЬ: книга выводила сырые коды реестра и сырой float
 * (`OPERATING_PROFIT`, `28355.0`), экран — переведённое и отформатированное.
 * Аналитик, сверявший скачанную книгу с экраном, получал два разных ответа на
 * один вопрос «как это посчитано».
 *
 * Одного кода на Python и TypeScript не бывает — поэтому один ФАЙЛ СЛУЧАЕВ:
 * `tests/fixtures/calculation_signature.json`. Его читают оба сторожа, этот и
 * `tests/test_calculation_signature_is_one_rule.py`. Разметка правится в одном
 * месте, расхождение краснеет с обеих сторон.
 *
 * ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (проделана 19–20.08):
 *  1. вернуть `title: formulaRu(f, names)` в `sourceCell` → падает сторож
 *     раскладки (подсказка снова без чисел);
 *  2. убрать пометку «подставлен ноль» → падает случай нуля из разметки;
 *  3. убрать `inputs: r.inputs` из тела `/metrics/format-role` в
 *     `extracted-pane.tsx` → падает сторож белого списка;
 *  4. убрать обёртку `AVG(...)`/`abs(...)` у имени входа → падают случаи
 *     усреднения и снятого знака;
 *  5. убрать `входы за <период>` → падают все случаи с периодом.
 */
function row(patch: Partial<MetricRow>): MetricRow {
  return {
    enum: "EBITDA",
    name_ru: "EBITDA",
    value_current: 37850,
    value_prev: null,
    period_values: [],
    unit: "млн ₽",
    found: true,
    is_calculated: true,
    formula: "OPERATING_PROFIT + DEPRECIATION_AMORTIZATION",
    ...patch,
  } as MetricRow;
}

type SignatureCase = { name: string; row: Partial<MetricRow>; expect: string };
const FIXTURE = JSON.parse(
  readFileSync(
    fileURLToPath(
      new URL("../../../../../tests/fixtures/calculation_signature.json", import.meta.url),
    ),
    "utf8",
  ),
) as { names: Record<string, string>; cases: SignatureCase[] };

describe("подсказка расчётной строки печатается по общему правилу", () => {
  const names = new Map(Object.entries(FIXTURE.names));

  it("случаи в разметке есть — иначе сторож зеленеет ни на чём", () => {
    expect(FIXTURE.cases.length).toBeGreaterThanOrEqual(5);
  });

  for (const c of FIXTURE.cases) {
    it(c.name, () => {
      expect(calculationHint(row(c.row), names)).toBe(c.expect);
    });
  }
});

describe("входы доезжают до тех, кто их показывает", () => {
  function read(rel: string): string {
    return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
  }

  it("ячейка «Источник» берёт подсказку целиком, а не одну формулу", () => {
    // Комментарии вырезаны: объяснительная записка не имеет права озеленить
    // сторож вместо кода.
    const src = read("./extracted-table.tsx").replace(/\/\*[\s\S]*?\*\/|\/\/.*/g, "");
    const cell = src.slice(src.indexOf("function sourceCell"));
    expect(cell.slice(0, cell.indexOf("\n}"))).toContain("calculationHint(r, names)");
  });

  it("белый список полей роли несёт входы", () => {
    const pane = read("./extracted-pane.tsx").replace(/\/\*[\s\S]*?\*\/|\/\/.*/g, "");
    const body = pane.slice(pane.indexOf("void formatMetricsForRole("));
    expect(body.slice(0, body.indexOf("})),"))).toContain("inputs: r.inputs");
  });
});
