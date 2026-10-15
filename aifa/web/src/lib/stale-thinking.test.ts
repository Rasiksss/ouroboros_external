import { afterEach, describe, expect, it, vi } from "vitest";

import { loadProjects, type Project, type TextMessage } from "./projects";

/**
 * «Ответ прерван» не должен появляться под ЖИВЫМ ходом (#210).
 *
 * ЧТО БЫЛО. `loadProjects` зовётся не только при загрузке страницы, а перед
 * каждой записью в хранилище (см. updateProject). Уход в другой чат снимает
 * подписку — это штатно, ход продолжается на сервере, — но сообщение остаётся
 * с `thinking`, и первая же запись из соседнего чата сохраняла поверх него
 * «⚠️ Ответ был прерван». Человек возвращался и видел приговор ходу, который
 * в эту секунду работал; кнопка предлагала отправить заново — то есть
 * осиротить готовящийся результат.
 *
 * Живой ход опознаётся маркером `activeChatRun`, который ставит рантайм.
 */
const USER = "t";

function withStorage(projects: Project[]) {
  const store = new Map<string, string>([
    [`finengine:projects:${USER}:v1`, JSON.stringify(projects)],
  ]);
  vi.stubGlobal("window", {});
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
  });
}

afterEach(() => vi.unstubAllGlobals());

function project(over: Partial<Project>): Project {
  return {
    id: "p1",
    name: "ОТП",
    created_at: 1,
    updated_at: 1,
    messages: [],
    ...over,
  } as Project;
}

const thinkingMsg = (id: string, content = ""): TextMessage =>
  ({ id, role: "assistant", content, thinking: "Извлекаю показатели…", created_at: 2 }) as TextMessage;

describe("живой ход не объявляется прерванным", () => {
  it("сообщение активного прогона не трогают вовсе", () => {
    // 🔴 КУПЛЕНО ПРОГОНОМ 26.08.2026. Раньше здесь ждали, что живому ходу
    // ГАСЯТ спиннер и вешают `unreachable` («Догнать ответ»). Это оказалось
    // приговором работающему ходу: функция зовётся перед КАЖДОЙ записью, то
    // есть и посреди стрима. Владелец запустил извлечение и на 31-й секунде
    // получил баннер обрыва — при живом потоке. В журнале клиента за тот день
    // нет ни одного `link_lost_reconnecting`: сторож связи не срабатывал,
    // приговор вынесла загрузка хранилища.
    //
    // Про связь решает только тот, кто про неё знает: сторож молчания (60 с) и
    // переподписка. Здесь — не трогаем ничего.
    withStorage([
      project({
        messages: [thinkingMsg("a1")],
        activeChatRun: { run_id: "r1", assistant_id: "a1", started_at: 1 },
      }),
    ]);

    const [p] = loadProjects(USER);
    const m = p.messages[0] as TextMessage;

    expect(m.thinking).toBe("Извлекаю показатели…"); // спиннер живого хода — верен
    expect(m.interrupted).toBeUndefined();
    expect(m.content).not.toContain("Ответ был прерван");
  });

  it("уборка соседних сообщений не отменяется живым ходом", () => {
    // Отрицательный контроль на мой же огрех: первая правка сбрасывала флаг
    // `changed` внутри `map`, и живое сообщение отменяло уборку соседей —
    // мёртвый спиннер оставался бы висеть на СТАРОМ сообщении.
    withStorage([
      project({
        messages: [thinkingMsg("старое"), thinkingMsg("a1")],
        activeChatRun: { run_id: "r1", assistant_id: "a1", started_at: 1 },
      }),
    ]);

    const [p] = loadProjects(USER);
    const [старое, живое] = p.messages as TextMessage[];

    expect(старое.thinking).toBeUndefined();
    expect(старое.interrupted).toEqual({ state: "unreachable" });
    expect(живое.thinking).toBe("Извлекаю показатели…");
  });

  it("сообщение без активного прогона получает пометку «не дозвонились», а не приговор", () => {
    // 🔴 КЕЙС 9d2dad93 (31.08.2026): после F5 маркер живого хода не защитил
    // сообщение, и приговор «Отправьте запрос ещё раз» лёг ТЕКСТОМ — при том
    // что прогон жил на сервере и станция дописала ответ через две минуты.
    // Теперь: спиннер снят, content ПУСТ (иначе догон позднего ответа считает
    // приговор ответом), пометка unreachable даёт кнопку «Догнать ответ».
    withStorage([project({ messages: [thinkingMsg("a1")] })]);

    const m = loadProjects(USER)[0].messages[0] as TextMessage;

    expect(m.thinking).toBeUndefined();
    expect(m.content).toBe("");
    expect(m.interrupted).toEqual({ state: "unreachable" });
  });

  it("маркер от ДРУГОГО сообщения не выгораживает это", () => {
    // Иначе один живой ход прикрывал бы все брошенные сообщения чата.
    withStorage([
      project({
        messages: [thinkingMsg("a1")],
        activeChatRun: { run_id: "r1", assistant_id: "другое", started_at: 1 },
      }),
    ]);

    const m = loadProjects(USER)[0].messages[0] as TextMessage;
    expect(m.interrupted).toEqual({ state: "unreachable" });
    expect(m.content).toBe("");
  });

  it("уже написанный ответ не затирается", () => {
    withStorage([project({ messages: [thinkingMsg("a1", "Итого активы — 617 811.")] })]);

    const m = loadProjects(USER)[0].messages[0] as TextMessage;
    expect(m.content).toBe("Итого активы — 617 811.");
    expect(m.thinking).toBeUndefined();
  });
});
