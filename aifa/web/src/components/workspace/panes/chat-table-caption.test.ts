import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { headingText } from "./chat-pane";

/**
 * Подпись таблицы в чате (правка Nate 17.08).
 *
 * ЖАЛОБА. «Значения в таблице слишком далеко от их названий» и «таблицу нужно
 * как-то подписывать». Оба дефекта — в одном месте: markdown-таблица шла с
 * `w-max min-w-full`, то есть растягивалась на всю ширину панели, и лишнее
 * место доставалось колонке названий; а заголовок прогона («## МегаФон ·
 * Ячейка … · Период … · Единицы …») оставался в тексте выше и при длинном
 * ответе уезжал с экрана вместе с ответом на «чьи это числа».
 */
describe("подпись таблицы берётся из заголовка выше", () => {
  it("собирает текст заголовка из вложенной разметки", () => {
    // Заголовок прогона печатается с жирным: «## МегаФон **Ячейка**: …».
    const node = ["МегаФон ", { props: { children: "Ячейка" } }, ": ifrs_nf_ru"];
    expect(headingText(node as never)).toBe("МегаФон Ячейка: ifrs_nf_ru");
  });

  it("пустой заголовок не ломает разбор", () => {
    expect(headingText(null as never)).toBe("");
    expect(headingText(undefined as never)).toBe("");
  });
});

describe("раскладка таблицы в чате", () => {
  const src = readFileSync(
    fileURLToPath(new URL("./chat-pane.tsx", import.meta.url)),
    "utf8",
  );

  it("таблица обнимает содержимое, а не тянется на всю панель", () => {
    // Отрицательный контроль на прежнее поведение: вернётся min-w-full —
    // числа снова уедут к правому краю, за десяток сантиметров от названия.
    // Именно ТЕГ, а не блок целиком: в блоке есть комментарий, объясняющий,
    // почему min-w-full убран, и он бы сам себя и завалил.
    const block = src.slice(src.indexOf("table: ({ children })"));
    const tag = block.slice(block.indexOf("<table "), block.indexOf("</table>"));
    expect(tag).toContain('className="w-max border-collapse');
    expect(tag).not.toContain("min-w-full");
  });

  it("таблица получает подпись, а не родовое «Таблица»", () => {
    expect(src).toContain("<CopyableTable caption={lastHeading.current}>");
  });
});
