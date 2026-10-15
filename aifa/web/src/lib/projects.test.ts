import { describe, expect, it } from "vitest";
import { collectMetrics, mergeServerProjects } from "@/lib/projects";
import type { ExtractionMessage, Project } from "@/lib/projects";
import type { MetricEvent } from "@/lib/api";

/**
 * #143. Класс ошибки: «панель опустела при живых данных».
 *
 * Зона «Документ» рисует строки из ХРАНИЛИЩА, а лента чата — из своей копии в
 * памяти хука. Поэтому обеднение хранилища видно только в панели, и в чате всё
 * выглядит целым — глазами дефект ловится уже у пользователя. Тест закрывает
 * ровно эту дыру: что бы ни принесла подтяжка с сервера, число строк, которое
 * увидит панель (collectMetrics), не имеет права уменьшиться.
 */

function metric(i: number): MetricEvent {
  return {
    type: "metric",
    enum: `M_${i}`,
    name_ru: `Показатель ${i}`,
    value_current: i,
    value_prev: null,
    unit: "млн. руб.",
    found: true,
    is_calculated: false,
    formula: "",
  };
}

function extraction(over: Partial<ExtractionMessage> = {}): ExtractionMessage {
  return {
    id: "extract-1",
    role: "extraction",
    doc_id: "doc-1",
    doc_label: "Отчёт",
    company_type: "non_financial",
    created_at: 1000,
    status: "done",
    metrics: [],
    ...over,
  };
}

function project(over: Partial<Project> = {}): Project {
  return {
    id: "chat-1",
    name: "Чат",
    created_at: 1000,
    updated_at: 1000,
    doc_ids: ["doc-1"],
    messages: [],
    ...over,
  };
}

describe("mergeServerProjects", () => {
  it("серверная карточка извлечения не обедняет локальную", () => {
    // Ровно живой сценарий: сервер заморозил карточку на моменте старта
    // (metrics: [], running) и при этом выглядит свежее — хвост хода бампает
    // updated_at уже после последней записи клиента.
    const rich = extraction({ metrics: Array.from({ length: 75 }, (_, i) => metric(i)) });
    const local = [project({ messages: [rich] })];
    const server = [
      project({ updated_at: 9000, messages: [extraction({ status: "running" })] }),
    ];

    const merged = mergeServerProjects(local, server);
    expect(merged).not.toBeNull();
    expect(collectMetrics(merged![0], "doc-1")).toHaveLength(75);
  });

  it("серверные скаляры (имя чата, время) всё же применяются", () => {
    const local = [project({ name: "Старое имя" })];
    const server = [project({ name: "Новое имя", updated_at: 9000 })];

    const merged = mergeServerProjects(local, server)!;
    expect(merged[0].name).toBe("Новое имя");
    expect(merged[0].updated_at).toBe(9000);
  });

  it("сообщение, которого локально нет, сервер добавляет", () => {
    const local = [project({ messages: [] })];
    const server = [
      project({ updated_at: 9000, messages: [extraction({ metrics: [metric(1)] })] }),
    ];

    const merged = mergeServerProjects(local, server)!;
    expect(collectMetrics(merged[0], "doc-1")).toHaveLength(1);
  });

  it("чат, стёртый локально, возвращается с сервера целиком", () => {
    const merged = mergeServerProjects([], [project({ id: "chat-2" })])!;
    expect(merged.map((p) => p.id)).toEqual(["chat-2"]);
  });

  it("сервер не принёс ничего нового — писать нечего", () => {
    const local = [project()];
    expect(mergeServerProjects(local, [project()])).toBeNull();
    expect(mergeServerProjects(local, [])).toBeNull();
  });

  it("помеченный удалённым чат уходит из браузера, соседний остаётся", () => {
    // Живой инцидент 11.08: localStorage переполнился на 168 чатах, загрузка
    // файлов встала. 138 старых пометили удалёнными на сервере — браузер
    // остался при своих 168, потому что слияние умело только добавлять.
    const local = [project({ id: "chat-old" }), project({ id: "chat-keep" })];
    const server = [project({ id: "chat-keep" })];

    const merged = mergeServerProjects(local, server, { "chat-old": 1_760_000_000 })!;
    expect(merged.map((p) => p.id)).toEqual(["chat-keep"]);
  });

  it("надгробие не даёт чату приехать обратно с сервера", () => {
    // Снимок сервера может отставать: чат ещё в projects, но уже под меткой.
    // Верить надо метке, иначе удалённое воскресает при каждой подтяжке.
    const merged = mergeServerProjects(
      [project({ id: "chat-keep" })],
      [project({ id: "chat-old" }), project({ id: "chat-keep" })],
      { "chat-old": 1_760_000_000 },
    );
    expect(merged?.map((p) => p.id) ?? ["chat-keep"]).toEqual(["chat-keep"]);
  });

  it("удаление — единственное убавление: без метки чат жив", () => {
    const local = [project({ id: "chat-old" }), project({ id: "chat-keep" })];
    const merged = mergeServerProjects(local, [project({ id: "chat-keep" })], {});
    expect(merged).toBeNull();
  });
});
