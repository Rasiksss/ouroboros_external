import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/**
 * Приложенный документ виден в отправленном сообщении, а чип в поле ввода гаснет.
 *
 * ЖАЛОБА ВЛАДЕЛЬЦА 20.08 (Северсталь). Приложил PDF, ушёл на другие вкладки,
 * вернулся, отправил вопрос — иконки файла в отправленном сообщении НЕТ, а чип
 * остался висеть в поле ввода. «Документ ушёл вообще или нет?»
 *
 * ДОКУМЕНТ УХОДИЛ. Он в `docIdsForTurn`, и ход отдаёт его бэкенду. Врал экран.
 *
 * МЕХАНИКА, И ОНА КРУГОВАЯ:
 *  1. `use-chat-run` считал «приложено этим ходом» как «не было в `doc_ids`
 *     проекта до отправки». Приложение файла пополняет `doc_ids` СРАЗУ;
 *  2. уход на другую вкладку и возврат перечитывают проект из хранилища —
 *     документ в копии уже есть;
 *  3. значит `attachedNow` пуст, и у сообщения нет `files`;
 *  4. а чип в поле ввода (`chat-pane.attachedUnsent`) гаснет по правилу «есть
 *     в `doc_ids` и НАЗВАН каким-нибудь сообщением». Сообщение не назвало —
 *     чип не гаснет никогда.
 *
 * Два места считали «приложено» ПО-РАЗНОМУ, и расхождение замыкалось в круг.
 * Теперь оба смотрят на одно: назван ли документ хоть одним сообщением.
 *
 * ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть `new Set(p.doc_ids ?? [])` в
 * `use-chat-run.ts` — тест обязан покраснеть.
 */
const RUN = readFileSync(
  fileURLToPath(new URL("../components/workspace/use-chat-run.ts", import.meta.url)),
  "utf8",
);
const PANE = readFileSync(
  fileURLToPath(new URL("../components/workspace/panes/chat-pane.tsx", import.meta.url)),
  "utf8",
);

/** Без комментариев: сторож читает код, а не объяснение к нему. */
const code = (s: string) =>
  s.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/^\s*\/\/.*$/gm, " ");

describe("приложенный документ и чип поля ввода считаются одинаково", () => {
  it("ход считает приложенным то, чего не называло ни одно сообщение", () => {
    const body = code(RUN);
    expect(body).toMatch(/const priorDocs = new Set\(\s*p\.messages/);
    expect(body).not.toMatch(/const priorDocs = new Set\(p\.doc_ids/);
  });

  it("правило берётся из files сообщений, а не из doc_ids проекта", () => {
    const at = code(RUN).indexOf("const priorDocs");
    const block = code(RUN).slice(at, at + 260);
    expect(block).toContain("files");
    expect(block).toContain("file_id");
  });

  it("чип поля ввода гаснет по тому же признаку", () => {
    const body = code(PANE);
    const at = body.indexOf("const attachedUnsent");
    expect(at).toBeGreaterThan(0);
    const block = body.slice(at, at + 700);
    expect(block).toContain("files");
    expect(block).toContain("inMessages");
  });

  it("сторож не пустой: оба места найдены", () => {
    expect(code(RUN)).toContain("attachedNow");
    expect(code(PANE)).toContain("attachedUnsent");
  });
});
