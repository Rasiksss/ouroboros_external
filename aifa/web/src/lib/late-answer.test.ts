import { describe, expect, it } from "vitest";

import { missingAssistantTail } from "./late-answer";

const ход = (текст: string) => ({ role: "assistant", content: текст });
const вопрос = (текст: string) => ({ role: "user", content: текст });

describe("догоняющий ответ", () => {
  it("отдаёт ответ, дописанный после конца хода", () => {
    // Живой случай 23.08: агент дописал разбор, лента о нём не знает.
    const feed = [вопрос("посчитай пять коэффициентов"), ход("Извлекаю показатели…")];
    const history = [
      { role: "user", content: "посчитай пять коэффициентов" },
      { role: "assistant", content: "Извлекаю показатели…" },
      { role: "assistant", content: "Вот расчёты: ROCE 18,1%…" },
    ];
    expect(missingAssistantTail(feed, history)).toEqual(["Вот расчёты: ROCE 18,1%…"]);
  });

  it("обычный ход ничего не догоняет", () => {
    const feed = [вопрос("привет"), ход("Здравствуйте")];
    const history = [
      { role: "user", content: "привет" },
      { role: "assistant", content: "Здравствуйте" },
    ];
    expect(missingAssistantTail(feed, history)).toEqual([]);
  });

  it("сверяет счётом, а не текстом", () => {
    // Лента собирается из дельт и проходит глушилку пересказа: тот же ответ
    // отличается от серверного пробелами и обрезкой. Сравни тексты — и он
    // приедет вторым сообщением, удвоив ответ на экране.
    const feed = [ход("Вот расчёты: ROCE 18,1%")];
    const history = [{ role: "assistant", content: "Вот расчёты: ROCE 18,1%…  " }];
    expect(missingAssistantTail(feed, history)).toEqual([]);
  });

  it("пустой пузырь в ленте за ответ не считается", () => {
    // Лента заводит пустое сообщение ассистента ещё ДО первой дельты — это
    // место под ответ, а не ответ. Посчитай его — и настоящий ответ, пришедший
    // после конца хода, будет признан уже показанным и не приедет никогда.
    const feed = [ход("Извлекаю…"), ход("")];
    const history = [
      { role: "assistant", content: "Извлекаю…" },
      { role: "assistant", content: "Готово: 13 показателей." },
    ];
    expect(missingAssistantTail(feed, history)).toEqual(["Готово: 13 показателей."]);
  });

  it("служебные записи истории в ленту не превращаются", () => {
    const feed = [ход("Извлекаю…")];
    const history = [
      { role: "assistant", content: "Извлекаю…" },
      { role: "tool_call", name: "exec" },
      { role: "user", content: "ещё вопрос" },
    ];
    expect(missingAssistantTail(feed, history)).toEqual([]);
  });

  it("сервер отстал от ленты — не отматываем назад", () => {
    const feed = [ход("раз"), ход("два")];
    const history = [{ role: "assistant", content: "раз" }];
    expect(missingAssistantTail(feed, history)).toEqual([]);
  });
});
