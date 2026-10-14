import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { activeStepOf } from "./active-step";
import type { PlanStep } from "./projects";

/**
 * Оформление хода работы — восемь замечаний владельца 19.08 (бэклог Б1).
 *
 * Здесь закрыты те, что проверяемы кодом:
 *  п.2 текст выходит за границы области;
 *  п.3 размышления не выровнены с шагами;
 *  п.4 «нижний текст крупным шрифтом» — пока ход идёт, проза агента должна
 *      идти регистром хроники (2–3 обновляющиеся строки), а не телом ответа;
 *  п.5 план замер на красном шаге: не видно ни текущего шага, ни того, как
 *      агент исправляется.
 *
 * П.1 (техническое имя инструмента посреди фразы) закрыт с другой стороны —
 * сторожем `tests/test_every_tool_has_a_human_name.py`: имя всплывало потому,
 * что подписи у тула не было вовсе.
 */
const PANE_RAW = readFileSync(
  fileURLToPath(new URL("../components/workspace/panes/chat-pane.tsx", import.meta.url)),
  "utf8",
);

/**
 * Комментарии из исходника ВЫРЕЗАЮТСЯ, и это не украшение проверки.
 * Первая редакция сторожа искала `break-words` во всём тексте функции — и
 * находила его в комментарии, которым правка объяснена. Мутация «убрать
 * перенос из класса» прошла зелёной: сторож сторожил мою же объяснительную
 * записку, а не код.
 */
function code(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/^\s*\/\/.*$/gm, " ");
}

const PANE = code(PANE_RAW);

const step = (title: string, status: PlanStep["status"]): PlanStep =>
  ({ title, status }) as PlanStep;

describe("где ход находится сейчас", () => {
  it("идущий шаг важнее очередного", () => {
    const got = activeStepOf([step("Классификация", "done"), step("Извлечение", "running"),
                              step("Расчёт", "pending")]);
    expect(got).toEqual({ index: 1, title: "Извлечение", status: "running" });
  });

  it("никто не идёт — показываем очередной", () => {
    const got = activeStepOf([step("Классификация", "done"), step("Извлечение", "pending")]);
    expect(got?.index).toBe(1);
  });

  it("упал последний шаг — строка не исчезает, а показывает падение", () => {
    // Ровно случай жалобы: план встал на красном `fe_calc`, и до правки
    // строка «где мы» пропадала совсем — экран замирал без объяснений.
    const got = activeStepOf([step("Извлечение", "done"), step("Расчёт", "error")]);
    expect(got).toEqual({ index: 1, title: "Расчёт", status: "error" });
  });

  it("споткнулся дважды — берём последнее место, а не первое", () => {
    const got = activeStepOf([step("Расчёт", "error"), step("Разбор", "error")]);
    expect(got?.index).toBe(1);
  });

  it("плана нет — и строки нет", () => {
    expect(activeStepOf(null)).toBeNull();
    expect(activeStepOf([])).toBeNull();
  });
});

describe("пока ход идёт, проза агента — это хроника, а не ответ", () => {
  it("живой ход рисует LiveProse вместо тела ответа", () => {
    // 31.08.2026: живой текст переехал ВНУТРЬ ленты хода (жалоба владельца:
    // «где-то есть черта слева, а где-то нет» — заметки шли за чертой, а
    // текущий текст отдельным блоком без неё). Предмет сторожа тот же: пока
    // ход идёт, проза рисуется LiveProse-ом, а тело ответа — только после.
    expect(PANE).toContain("liveTail={streaming && !message.analysisCard && !hasTypedResults ? message.content : undefined}");
    expect(PANE).toContain("{liveTail && <LiveProse text={liveTail} />}");
    expect(PANE).toContain("assistantAnswerLayout(message, Boolean(streaming))");
    expect(PANE).toContain('answerLayout.contentPlacement === "before-results"');
  });

  it("тело ответа стоит над карточками цикла и под карточками детерминированного пути (Р7)", () => {
    // Компонент не монтируется в тестах: порядок блоков закреплён по исходнику,
    // а какое из двух мест выбрано — решает чистая `assistantAnswerLayout`.
    const before = PANE.indexOf('answerLayout.contentPlacement === "before-results"');
    const cards = PANE.indexOf("message.typedResults?.map(");
    const after = PANE.indexOf('answerLayout.contentPlacement === "after-results"');
    expect(before).toBeGreaterThan(-1);
    expect(before).toBeLessThan(cards);
    expect(cards).toBeLessThan(after);
  });

  it("LiveProse режет поток на последние строки, а строки переносит, не обрезает", () => {
    // 31.08 владелец поймал `truncate` живьём: в узкой колонке от строки
    // оставалось три слова и многоточие — «сужение по ширине скрывает текст
    // логов». Контракт теперь двухчастный: поток по-прежнему режется на
    // последние строки (защита от простыни 17.08), но каждая строка ДОНОСИТ
    // текст переносом с потолком, а не отрезает его по ширине.
    const body = PANE.split("function LiveProse")[1].split("\n}")[0];
    expect(body).toContain("thinkingLines(text)");
    expect(body).toContain("break-words");
    expect(body).toContain("line-clamp");
    expect(body).not.toContain("truncate");
  });

  it("текущий шаг показывается, пока ход идёт, а не только при пустом тексте", () => {
    expect(PANE).toContain("{streaming && activeStep && (");
    expect(PANE).not.toContain("{!message.content && activeStep && (");
  });
});

describe("хроника не вылезает за область и стоит одной левой границей", () => {
  it("заметка переносит длинные слова и выровнена по шагам", () => {
    const note = PANE.split("function RunNote")[1].split("\n}")[0];
    expect(note).toContain("break-words");
    expect(note).toContain("pl-[14px]");
  });

  it("строка шага не растягивает ленту", () => {
    // Проверяем ИМЕННО ТУ строку, где стоит класс: `min-w-0` есть и у ленты,
    // и у строки шага, и общий поиск по функции не различал бы, у кого он
    // пропал (мутация «убрать min-w-0 у ленты» проходила зелёной).
    const ribbon = PANE.split("function RunRibbon")[1].split("\nfunction ")[0].split("\n");
    const box = ribbon.find((l) => l.includes("border-l-2")) ?? "";
    expect(box).toContain("min-w-0");
    // 31.08.2026, вторая редакция. Сначала `overflow-hidden` сменился на
    // `overflow-y-auto` + `max-h` (прокрутка внутри ленты при стриме). Затем
    // владелец поймал обратную сторону: у ЗАКОНЧЕННОГО хода внутренний скролл
    // прятал хвост логов в узкой колонке. Теперь ограничение УСЛОВНОЕ — только
    // пока ход жив (`live && "max-h-…"`), и класс живёт строкой ниже
    // контейнера. Предмет сторожа прежний: при живом стриме лента обязана
    // ограничивать разлив; горизонталь держат min-w-0 + break-words у строк.
    const liveClamp = ribbon.find((l) => l.includes("max-h-[")) ?? "";
    expect(liveClamp).toContain("live &&");
    expect(liveClamp).toMatch(/overflow-(hidden|y-auto|auto)/);
    const row = ribbon.find((l) => l.includes("items-start gap-2")) ?? "";
    expect(row).toContain("min-w-0");
    expect(row).toContain("break-words");
  });
});
