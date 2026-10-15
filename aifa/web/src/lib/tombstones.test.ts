import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  adoptServerTombstones,
  applyServerDeletions,
  deleteProject,
  sweepOrphanRunKeys,
  loadProjects,
  loadTombstoneIds,
  saveProjects,
  type Project,
} from "@/lib/projects";

/**
 * Здесь нужен настоящий localStorage: проверяем не чистую функцию, а сцепку
 * «метка сервера → список удалённых в браузере → запись на диск». Ровно на
 * этой сцепке ломался инцидент 11.08 — фильтрация в слиянии сама по себе
 * бесполезна, потому что saveProjects возвращает «диск-only» проекты с диска.
 * Среда тестов node, jsdom нет — обходимся минимальным хранилищем в памяти.
 */
function installStorage(): void {
  const mem = new Map<string, string>();
  const storage = {
    getItem: (k: string) => mem.get(k) ?? null,
    setItem: (k: string, v: string) => void mem.set(k, v),
    removeItem: (k: string) => void mem.delete(k),
    clear: () => mem.clear(),
    key: (i: number) => Array.from(mem.keys())[i] ?? null,
    get length() {
      return mem.size;
    },
  };
  const g = globalThis as unknown as Record<string, unknown>;
  g.localStorage = storage;
  g.window = g.window ?? { addEventListener() {}, dispatchEvent() {} };
  // reportStorage читает location.pathname; без него он молча падает в свой
  // catch, и проверить след было бы нечем.
  g.location = g.location ?? { pathname: "/v2" };
}

/** Что легло в историю уведомлений (notices пишет её в тот же localStorage). */
function noticeTexts(): string {
  return (globalThis as unknown as { localStorage: Storage }).localStorage.getItem(
    "aifa:notices:v1",
  ) ?? "";
}

function project(id: string): Project {
  return {
    id,
    name: id,
    created_at: 1,
    updated_at: 1,
    doc_ids: [],
    messages: [],
  } as Project;
}

describe("предупреждение о заполнении", () => {
  beforeEach(() => {
    installStorage();
    vi.restoreAllMocks();
  });

  it("на обычном объёме молчит", () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(""));
    saveProjects("nate", [project("chat-1"), project("chat-2")]);
    expect(noticeTexts()).not.toContain("Хранилище браузера заполнено");
  });
  it("на 75% потолка предупреждает ДО того, как встанет загрузка", () => {
    // Потолок измерен: 2 621 440 символов (5 МиБ в UTF-16). Раньше первым
    // сигналом была невозможность приложить документ — предупреждать было нечем.
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(""));
    const fat = { ...project("chat-fat"), name: "x".repeat(2_100_000) } as Project;
    saveProjects("nate", [fat]);

    expect(noticeTexts()).toContain("Хранилище браузера заполнено");
  });

});

describe("надгробия сервера", () => {
  beforeEach(() => {
    installStorage();
  });

  it("после усыновления saveProjects не воскрешает чат с диска", () => {
    saveProjects("nate", [project("chat-old"), project("chat-keep")]);
    expect(loadProjects("nate")).toHaveLength(2);

    // Сервер сообщил: chat-old удалён (время в секундах, как в блобе).
    const added = adoptServerTombstones("nate", { "chat-old": Math.floor(Date.now() / 1000) });
    expect(added).toBe(1);

    // Писатель кладёт только оставшийся — правило «диск-only остаются жить»
    // вернуло бы chat-old, если бы метку не усыновили.
    saveProjects("nate", [project("chat-keep")]);
    expect(loadProjects("nate").map((p) => p.id)).toEqual(["chat-keep"]);
  });

  it("усыновлённая метка уезжает обратно на сервер в PUT", () => {
    adoptServerTombstones("nate", { "chat-old": Math.floor(Date.now() / 1000) });
    expect(loadTombstoneIds("nate")).toContain("chat-old");
  });

  it("просроченная метка (старше 30 дней) не принимается", () => {
    const old = Math.floor((Date.now() - 31 * 24 * 3600 * 1000) / 1000);
    expect(adoptServerTombstones("nate", { "chat-old": old })).toBe(0);
    expect(loadTombstoneIds("nate")).toHaveLength(0);
  });

  it("замок: место освобождается ДО записи меток, а не после", () => {
    // Живой инцидент 11.08. Хранилище переполнено, метки — НОВЫЙ ключ, его
    // запись бросает квоту. Если сокращать список после меток, применить
    // удаление нельзя вообще: чтобы освободить место, надо записать.
    // Здесь имитируем ровно это: setItem на новый ключ бросает, на
    // существующий проходит.
    saveProjects("nate", [project("chat-old"), project("chat-keep")]);
    const ls = (globalThis as unknown as { localStorage: Storage }).localStorage;
    const realSet = ls.setItem.bind(ls);
    let quotaHits = 0;
    ls.setItem = (k: string, v: string) => {
      if (ls.getItem(k) === null) {
        quotaHits += 1;
        const e = new Error("The quota has been exceeded.");
        e.name = "QuotaExceededError";
        throw e;
      }
      realSet(k, v);
    };

    const res = applyServerDeletions("nate", { "chat-old": Math.floor(Date.now() / 1000) });

    ls.setItem = realSet;
    expect(quotaHits).toBeGreaterThan(0); // метки лечь не смогли — как у владельца
    expect(res.removed).toBe(1); // но список УЖЕ сокращён, замок разорван
    expect(loadProjects("nate").map((p) => p.id)).toEqual(["chat-keep"]);
  });

  it("применение удалений сокращает список и принимает метки", () => {
    saveProjects("nate", [project("chat-old"), project("chat-keep")]);
    const res = applyServerDeletions("nate", { "chat-old": Math.floor(Date.now() / 1000) });
    expect(res).toEqual({ removed: 1, adopted: 1 });
    expect(loadProjects("nate").map((p) => p.id)).toEqual(["chat-keep"]);
    expect(loadTombstoneIds("nate")).toEqual(["chat-old"]);
  });

  it("если и замена меньшим падает — ключ снимается и пишется заново", () => {
    // Запасной ход на случай, если браузер отвергает ЛЮБОЙ setItem при
    // исчерпанной квоте. removeItem квотой не ограничен.
    saveProjects("nate", [project("chat-old"), project("chat-keep")]);
    const ls = (globalThis as unknown as { localStorage: Storage }).localStorage;
    const realSet = ls.setItem.bind(ls);
    const realRemove = ls.removeItem.bind(ls);
    let removedKey = false;
    ls.setItem = (k: string, v: string) => {
      if (!removedKey) {
        const e = new Error("The quota has been exceeded.");
        e.name = "QuotaExceededError";
        throw e;
      }
      realSet(k, v);
    };
    ls.removeItem = (k: string) => {
      removedKey = true;
      realRemove(k);
    };

    const res = applyServerDeletions("nate", { "chat-old": Math.floor(Date.now() / 1000) });

    ls.setItem = realSet;
    ls.removeItem = realRemove;
    expect(removedKey).toBe(true);
    expect(res.removed).toBe(1);
    expect(loadProjects("nate").map((p) => p.id)).toEqual(["chat-keep"]);
  });

  it("пустой ответ сервера ничего не удаляет", () => {
    saveProjects("nate", [project("chat-keep")]);
    expect(adoptServerTombstones("nate", null)).toBe(0);
    expect(adoptServerTombstones("nate", {})).toBe(0);
    expect(applyServerDeletions("nate", null)).toEqual({ removed: 0, adopted: 0 });
    expect(applyServerDeletions("nate", {})).toEqual({ removed: 0, adopted: 0 });
    expect(loadProjects("nate").map((p) => p.id)).toEqual(["chat-keep"]);
  });
});


describe("ключи прогонов не переживают свои чаты", () => {
  it("удаление чата снимает его ir_cal_run", () => {
    // Штатно ключ снимает тот, кто его завёл, — по завершении прогона. Но
    // брошенный прогон на УДАЛЁННОМ чате снять его уже некому: чата нет,
    // экрана нет, повода зайти нет. Запись жила вечно в тех же 5 МиБ, из-за
    // которых зеркало потом выбрасывает живые диалоги.
    const ls = (globalThis as unknown as { localStorage: Storage }).localStorage;
    ls.clear();
    saveProjects("nate", [project("chat-x"), project("chat-y")]);
    ls.setItem("ir_cal_run_chat-x", "{}");
    ls.setItem("ir_cal_run_chat-y", "{}");

    deleteProject("nate", "chat-x");

    expect(ls.getItem("ir_cal_run_chat-x")).toBeNull();
    // Соседа не трогаем: убрать за собой — не повод подмести чужое.
    expect(ls.getItem("ir_cal_run_chat-y")).toBe("{}");
  });

  it("метла снимает ключи чатов, которых уже нет", () => {
    // Уборка при удалении лечит будущее, но не прошлое: ключи, осиротевшие
    // раньше, снять некому — их владелец удалён.
    const ls = (globalThis as unknown as { localStorage: Storage }).localStorage;
    ls.clear();
    saveProjects("nate", [project("chat-live")]);
    ls.setItem("ir_cal_run_chat-live", "{}");
    ls.setItem("ir_cal_run_chat-gone-1", "{}");
    ls.setItem("ir_cal_run_chat-gone-2", "{}");
    ls.setItem("ir_cal_run_chat-gone-3", "{}");

    expect(sweepOrphanRunKeys("nate")).toBe(3);

    expect(ls.getItem("ir_cal_run_chat-live")).toBe("{}");
    expect(ls.getItem("ir_cal_run_chat-gone-1")).toBeNull();
    // ⚠️ Третий важнее первых двух: удаление ВНУТРИ обхода `localStorage.key(i)`
    // сдвигает живой индекс и пропускает соседей. Одного осиротевшего ключа
    // такой дефект не показал бы.
    expect(ls.getItem("ir_cal_run_chat-gone-3")).toBeNull();
  });

  it("метла НЕ трогает сценарные ключи — их id не про чаты", () => {
    // `aifa:v2:scenario-run:<id>` адресуется id СЦЕНАРИЯ (в v2/page.tsx это
    // отдельное состояние рядом с activeId). Сверить его со списком чатов
    // нельзя, а подмести «по маске» значит стереть указатель живого прогона.
    //
    // ⚠️ В ПЕРВОЙ РЕДАКЦИИ Я УДАЛЯЛ ЭТОТ КЛЮЧ ПО id ЧАТА, и тест это «доказал»
    // — потому что ключ в тесте создавал я сам. В реальности такого ключа не
    // бывает: строка была мёртвой, а зелёный тест выдавал её за работу.
    const ls = (globalThis as unknown as { localStorage: Storage }).localStorage;
    ls.clear();
    saveProjects("nate", [project("chat-live")]);
    ls.setItem("aifa:v2:scenario-run:scn-777", "run-42");

    sweepOrphanRunKeys("nate");
    deleteProject("nate", "chat-live");

    expect(ls.getItem("aifa:v2:scenario-run:scn-777")).toBe("run-42");
  });

  it("пустое хранилище — метла молчит и не падает", () => {
    const ls = (globalThis as unknown as { localStorage: Storage }).localStorage;
    ls.clear();
    saveProjects("nate", [project("chat-live")]);
    expect(sweepOrphanRunKeys("nate")).toBe(0);
  });
});
