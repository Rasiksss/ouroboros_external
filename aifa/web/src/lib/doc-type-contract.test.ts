import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { DOC_TYPES } from "./api";

/**
 * Виды документа в выпадашке = перечисление DocType бэкенда (#250).
 *
 * ЗАЧЕМ СТОРОЖ. Список видов написан на фронте руками, а принимает его ручка
 * PATCH /documents/{id}, сверяя с DocType. Разъедутся — пользователь выберет
 * вид, получит 422 и решит, что выпадашка не работает. Ровно этим кончился
 * #142: схема отбивала запрос, фронт молчал, неделя ушла на поиск.
 *
 * Проверяем ОБА направления: лишний вид на фронте = гарантированный отказ,
 * недостающий = вид, который выставить нечем.
 */
const PY = readFileSync(
  fileURLToPath(new URL("../../../finengine/src/finengine/models/document.py", import.meta.url)),
  "utf8",
);

/** Значения DocType: строки после `=` внутри класса, до следующего класса. */
function docTypeValues(): string[] {
  const from = PY.indexOf("class DocType");
  const body = PY.slice(from, PY.indexOf("class ", from + 1));
  return [...body.matchAll(/=\s*"([a-z_]+)"/g)].map((m) => m[1]);
}

describe("выпадашка вида документа знает ровно то, что примет бэкенд", () => {
  it("состав совпадает с DocType", () => {
    expect([...DOC_TYPES.map((t) => t.id)].sort()).toEqual([...docTypeValues()].sort());
  });

  it("перечисление вообще нашлось", () => {
    // Отрицательный контроль: разбор питона регуляркой хрупок, и пустой
    // список сделал бы проверку выше самодовольно зелёной.
    expect(docTypeValues().length).toBeGreaterThan(3);
  });

  it("у каждого вида есть человеческая подпись", () => {
    for (const t of DOC_TYPES) expect(t.label.trim()).not.toBe("");
  });
});

describe("сохранение вида не врёт экрану", () => {
  // Выпадашка вида переезжала дважды за день: из шапки рабочей области — к
  // выбору документа, а потом ВНУТРЬ вкладки «Документы» (владелец: «список
  // должен быть в самой кнопке»). Сторож ходит за логикой, а не за прежним
  // адресом: тест, который смотрит в пустое место, зелен и бесполезен.
  const PANE = readFileSync(
    fileURLToPath(new URL("../components/workspace/panes/doc-picker.tsx", import.meta.url)),
    "utf8",
  );

  it("экран меняется ПОСЛЕ ответа ручки, а отказ назван вслух", () => {
    // Оптимистичная отрисовка показала бы вид, который никуда не сохранился:
    // перезагрузил страницу — снова прежний, и опять «фронт не работает».
    const from = PANE.indexOf("const change");
    const fn = PANE.slice(from, PANE.indexOf("[activeDoc, docs, setDocs]", from));
    expect(fn).toContain("await patchDocumentType");
    expect(fn.indexOf("await patchDocumentType")).toBeLessThan(fn.indexOf("setDocs("));
    expect(fn).toContain("toast.error");
  });
});
