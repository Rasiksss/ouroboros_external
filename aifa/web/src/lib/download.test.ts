import { describe, expect, it } from "vitest";
import { rowsToTSV, tableToTSV, type TableLike } from "@/lib/download";

/** Собрать подделку таблицы из матрицы строк — ровно тот минимум, который
 *  читает tableToTSV у настоящего <table>. Так копирование проверяется без
 *  браузера. */
function fakeTable(cells: string[][]): TableLike {
  return { rows: cells.map((r) => ({ cells: r.map((textContent) => ({ textContent })) })) };
}

describe("TSV для вставки в Excel", () => {
  it("строки разделены табуляцией, ячейки — переводом строки", () => {
    expect(rowsToTSV([["Показатель", "2024"], ["Выручка", "1 200"]])).toBe(
      "Показатель\t2024\nВыручка\t1 200",
    );
  });

  it("перевод строки и табуляция ВНУТРИ ячейки сдвинули бы всю сетку — схлопываем", () => {
    expect(rowsToTSV([[" Чистый\tдолг \n/ EBITDA ", "1,3"]])).toBe("Чистый долг / EBITDA\t1,3");
  });

  it("пустая ячейка остаётся пустой колонкой, а не пропадает", () => {
    expect(rowsToTSV([["Выручка", null, undefined, "10"]])).toBe("Выручка\t\t\t10");
  });

  it("таблица из ленты чата снимается целиком, вместе с шапкой", () => {
    const tsv = tableToTSV(
      fakeTable([
        ["Показатель", "2024", "2023"],
        ["Выручка", "1 200", "1 000"],
        ["EBITDA", "300", "250"],
      ]),
    );
    expect(tsv.split("\n")).toHaveLength(3);
    expect(tsv.split("\n")[2]).toBe("EBITDA\t300\t250");
  });
});
