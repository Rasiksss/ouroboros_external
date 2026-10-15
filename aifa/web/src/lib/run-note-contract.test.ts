import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { stripGfmTables } from "./gfm-tables";

/**
 * Заметка агента в «ходе работы» не должна быть простынёй (#207).
 *
 * ЧТО БЫЛО. Всё, что агент напечатал до очередного инструмента, уходит
 * заметкой в хронику. Печатал он туда и таблицу на сотню строк — она вставала
 * в ленту целиком. Владелец: «результат извлечения вываливается простынёй в
 * ход работы — и это НЕ следствие отказа», то есть так выглядел удачный прогон.
 *
 * Правки две, и они разной природы: таблицу УБИРАЕМ (её те же числа уже
 * показаны сеткой — это второй экземпляр), длинный текст СВОРАЧИВАЕМ с
 * раскрытием (написанное агентом выбрасывать нельзя).
 */
const PANE = readFileSync(
  fileURLToPath(new URL("../components/workspace/panes/chat-pane.tsx", import.meta.url)),
  "utf8",
);

describe("таблица из заметки уходит, текст вокруг остаётся", () => {
  const note = [
    "Извлёк показатели по ОТП Банку.",
    "",
    "| Показатель | 2024 | 2023 |",
    "|---|---:|---:|",
    "| Итого активы | 617 811 | 360 868 |",
    "| Чистая прибыль | 78 568 | 43 117 |",
    "",
    "Дальше считаю коэффициенты.",
  ].join("\n");

  it("числа вырезаны, пояснения целы", () => {
    const got = stripGfmTables(note);
    expect(got.had).toBe(true);
    expect(got.text).toContain("Извлёк показатели по ОТП Банку.");
    expect(got.text).toContain("Дальше считаю коэффициенты.");
    expect(got.text).not.toContain("617 811");
  });

  it("заметка без таблицы не трогается", () => {
    // Отрицательный контроль: иначе «уборка» съедала бы обычные реплики.
    const plain = "Документ распознан, 214 страниц. Определяю ячейку реестра.";
    expect(stripGfmTables(plain)).toEqual({ text: plain, had: false });
  });
});

describe("хроника и сетка договорились между собой", () => {
  it("сетка показывается, даже если таблица уехала в заметку", () => {
    // Без этого выходило худшее: в хронике простыня, а сетки нет вовсе —
    // showGrid смотрел ТОЛЬКО на итоговый текст ответа.
    expect(PANE).toContain("notesHadTable");
    expect(PANE).toContain("stripped.had || notesHadTable");
  });

  it("таблицу из заметки убираем ТОЛЬКО когда есть сетка", () => {
    // Иначе числа исчезли бы совсем: удалили из заметки, а показать негде.
    expect(PANE).toContain("stripTable={hasGrid}");
  });

  it("длинную заметку сворачиваем, а не режем", () => {
    const from = PANE.indexOf("function RunNote");
    const fn = PANE.slice(from, PANE.indexOf("function ThinkingLine", from));
    expect(fn).toContain("line-clamp-3");
    expect(fn).toContain("показать целиком");
    expect(fn).not.toMatch(/body\.slice\(/);
  });
});
