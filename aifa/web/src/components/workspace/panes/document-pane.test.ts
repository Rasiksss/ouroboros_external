import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * #143, вторая половина «панель опустела при живых данных».
 *
 * Эффект со списком документов перезапускается на каждую смену идентичности
 * project — а она случается при ВОЗВРАТЕ НА ВКЛАДКУ: подтяжка с сервера
 * перечитывает хранилище, и loadProjects отдаёт свежеразобранный объект. Раньше
 * одна неудачная попытка listDocuments() обнуляла и список, и выбранный
 * документ, и зона «Документ» писала «Документ не выбран» поверх извлечённой
 * таблицы.
 *
 * Проверяется исходником, а не рендером: поведение целиком в ветке catch
 * эффекта, а поднимать ради одной ветки jsdom и мокать сеть — дороже, чем
 * стеречь саму ветку. Тот же приём уже используется в tests/ для скриптов
 * станции.
 */
const SRC = readFileSync(
  fileURLToPath(new URL("./document-pane.tsx", import.meta.url)),
  "utf-8",
);

describe("DocumentPane: сбой запроса списка документов", () => {
  it("в catch выбор документа не обнуляется", () => {
    const catchBody = SRC.slice(SRC.indexOf(".catch("), SRC.indexOf("return () => {"));
    expect(catchBody).not.toContain("setActiveDocId");
    expect(catchBody).not.toContain("setDocs");
  });

  it("сброс на первый документ остаётся — но только по УСПЕШНОМУ списку", () => {
    const thenBody = SRC.slice(SRC.indexOf(".then((all)"), SRC.indexOf(".catch("));
    expect(thenBody).toContain("setActiveDocId(mine[0]?.doc_id ?? \"\")");
  });
});
