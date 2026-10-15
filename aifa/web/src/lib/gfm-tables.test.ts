import { describe, expect, it } from "vitest";
import { stripGfmTables } from "./gfm-tables";

/**
 * Вырезание markdown-таблицы из ответа (правка Nate 17.08).
 *
 * Числа в чате теперь рисует та же сетка, что и в рабочей области, поэтому
 * markdown-таблицу из текста надо убрать — иначе одни и те же значения встанут
 * дважды подряд. Резать при этом можно ТОЛЬКО таблицу: пояснения, оговорки и
 * ссылки на страницы — это ответ пользователю, и терять их нельзя.
 */
describe("stripGfmTables", () => {
  it("вырезает таблицу и сохраняет текст вокруг", () => {
    const src = [
      "## МегаФон",
      "",
      "| Показатель | 2024 | 2025 |",
      "|---|---:|---:|",
      "| Выручка | 10 | 12 |",
      "| Прибыль | 1 | 2 |",
      "",
      "Расхождение по амортизации — см. примечание 7.",
    ].join("\n");
    const got = stripGfmTables(src);
    expect(got.had).toBe(true);
    expect(got.text).toContain("## МегаФон");
    expect(got.text).toContain("Расхождение по амортизации");
    expect(got.text).not.toContain("Выручка");
    expect(got.text).not.toContain("|---");
  });

  it("текст без таблицы не трогается", () => {
    const src = "Извлёк 77 показателей. Отчёт годовой, единицы — млн ₽.";
    const got = stripGfmTables(src);
    expect(got.had).toBe(false);
    expect(got.text).toBe(src);
  });

  it("одинокая строка с чертами — не таблица", () => {
    // Так пишут обычный текст, и вырезать его было бы потерей ответа.
    const src = "Формула: | выручка | минус | расходы |";
    const got = stripGfmTables(src);
    expect(got.had).toBe(false);
    expect(got.text).toBe(src);
  });

  it("вырезает несколько таблиц подряд", () => {
    const src = [
      "Показатели:",
      "| a | b |",
      "|---|---|",
      "| 1 | 2 |",
      "Сегменты:",
      "| c | d |",
      "|---|---|",
      "| 3 | 4 |",
      "Конец.",
    ].join("\n");
    const got = stripGfmTables(src);
    expect(got.had).toBe(true);
    expect(got.text).toBe("Показатели:\nСегменты:\nКонец.");
  });
});
