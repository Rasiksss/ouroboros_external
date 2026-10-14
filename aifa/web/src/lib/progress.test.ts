import { describe, expect, it } from "vitest";
import {
  applyChatEvent,
  applyExtractEvent,
  extractionMessageFromDemo,
  extractionProgress,
  turnProgress,
} from "@/lib/projects";
import type {
  ChatMessage,
  ExtractionMessage,
  Project,
  TextMessage,
} from "@/lib/projects";
import type { ChatStreamEvent, ExtractStreamEvent, MetricEvent } from "@/lib/api";

/**
 * #156. «Прогресс врёт» — три дефекта одного класса, найденные владельцем на
 * живом прогоне.
 *
 * События сюда подаются ТЕ ЖЕ, что приходят по SSE, и прогоняются через тот же
 * `applyExtractEvent` / `applyChatEvent`, что и в бою. Проверять счётчик на
 * руками собранном объекте сообщения бессмысленно: дефект жил ровно в том, КАК
 * лента событий превращается в число.
 */

function metric(over: Partial<MetricEvent> & { enum: string }): MetricEvent {
  return {
    type: "metric",
    name_ru: over.enum,
    value_current: 1,
    value_prev: null,
    unit: "млн. руб.",
    found: true,
    is_calculated: false,
    formula: "",
    ...over,
  };
}

function emptyExtraction(): ExtractionMessage {
  return {
    id: "ext-1",
    role: "extraction",
    doc_id: "d1",
    doc_label: "Отчёт.pdf",
    company_type: "non_financial",
    created_at: 100,
    status: "running",
    metrics: [],
  };
}

/** Прокрутить ленту событий извлечения боевым редуктором. */
function runExtraction(events: ExtractStreamEvent[]): ExtractionMessage {
  return events.reduce(applyExtractEvent, emptyExtraction());
}

function assistant(id: string, created_at: number): TextMessage {
  return { id, role: "assistant", content: "", created_at };
}

function runChat(msg: TextMessage, events: ChatStreamEvent[]): TextMessage {
  return events.reduce(applyChatEvent, msg);
}

function project(messages: ChatMessage[]): Project {
  return {
    id: "p1",
    name: "Чат",
    created_at: 0,
    updated_at: 0,
    doc_ids: ["d1"],
    messages,
  };
}

const START: ExtractStreamEvent = {
  type: "start",
  total_metrics: 5,
  total_extractable: 3,
  company_type: "non_financial",
  unit: "млн. руб.",
  period_current: "2024",
  period_prev: "2023",
};

describe("дробь извлечения считает одно множество с обеих сторон", () => {
  it("коэффициенты, сегменты и состав группы в числитель не идут", () => {
    // Ровно та лента, что дала владельцу «156/77»: движок эмитит `metric` из
    // шести мест, а знаменатель считает только извлекаемые.
    const ext = runExtraction([
      START,
      metric({ enum: "SALES_REVENUE" }),
      metric({ enum: "NET_INCOME" }),
      metric({ enum: "TOTAL_ASSETS" }),
      metric({ enum: "EBITDA_MARGIN", is_calculated: true, formula: "a/b" }),
      metric({ enum: "RATIO_NET_DEBT_EBITDA", is_calculated: true, formula: "a/b" }),
      metric({ enum: "SALES_REVENUE", segment: "Металлургия", dimension: "operating_segment" }),
      metric({ enum: "SALES_REVENUE", segment: "Россия", dimension: "geography" }),
      metric({ enum: "SALES_REVENUE", dimension: "consolidated_group" }),
    ]);

    expect(ext.metrics).toHaveLength(8); // лента цела — считаем иначе, а не режем
    expect(extractionProgress(ext)).toEqual({ done: 3, total: 3 });
  });

  it("добор пропущенных не засчитывает показатель дважды", () => {
    // Движок добирает не пришедшие в стриме enum'ы вторым проходом по буферу.
    const ext = runExtraction([
      START,
      metric({ enum: "SALES_REVENUE" }),
      metric({ enum: "SALES_REVENUE" }),
      metric({ enum: "sales_revenue" }),
    ]);
    expect(extractionProgress(ext).done).toBe(1);
  });

  it("числитель не перерастает знаменатель на полной ленте", () => {
    const ext = runExtraction([
      START,
      metric({ enum: "SALES_REVENUE" }),
      metric({ enum: "NET_INCOME" }),
      metric({ enum: "TOTAL_ASSETS" }),
      ...Array.from({ length: 40 }, (_, i) =>
        metric({ enum: `CALC_${i}`, is_calculated: true, formula: "a/b" }),
      ),
      ...Array.from({ length: 30 }, (_, i) =>
        metric({ enum: "SALES_REVENUE", segment: `S${i}`, dimension: "operating_segment" }),
      ),
    ]);
    const { done, total } = extractionProgress(ext);
    expect(done).toBeLessThanOrEqual(total);
  });

  it("демо-снимок объявляет знаменатель по тому же правилу", () => {
    // Демо кладёт готовую ленту без события `start`, и знаменатель считает
    // сам. Возьмёт длину ленты — карточка демо покажет «1 из 3».
    const demo = extractionMessageFromDemo({
      doc_id: "d1",
      doc_label: "Демо.pdf",
      period_current: "2024",
      period_prev: "2023",
      unit: "млн. руб.",
      metrics: [
        metric({ enum: "SALES_REVENUE" }),
        metric({ enum: "EBITDA_MARGIN", is_calculated: true, formula: "a/b" }),
        metric({ enum: "SALES_REVENUE", segment: "Металл", dimension: "operating_segment" }),
      ],
    });
    const { done, total } = extractionProgress(demo);
    expect(done).toBe(total);
  });

  it("карточка воркера показывает ту же дробь", () => {
    const ext = runExtraction([
      START,
      metric({ enum: "SALES_REVENUE" }),
      metric({ enum: "EBITDA_MARGIN", is_calculated: true, formula: "a/b" }),
      metric({ enum: "SALES_REVENUE", segment: "Металл", dimension: "operating_segment" }),
    ]);
    const { workers } = turnProgress(
      project([{ id: "u1", role: "user", content: "извлеки", created_at: 1 }, ext]),
    );
    const card = workers.find((w) => w.id === ext.id);
    expect(card).toBeDefined();
    expect([card!.found, card!.total]).toEqual([1, 3]);
  });
});

describe("план и карточки исполнителей — один источник состояния", () => {
  it("прогон закончился — ни один шаг не остаётся «выполняется»", () => {
    // Владелец видел «воркер: ГОТОВО» рядом с «шаг: ВЫПОЛНЯЕТСЯ».
    const msg = runChat(assistant("a1", 2), [
      { type: "plan", name: "fe_extract_metrics", steps: ["Классификация", "Извлечение", "Таблица"] },
      { type: "plan_step", name: "fe_extract_metrics", index: 0, status: "done" },
      { type: "plan_step", name: "fe_extract_metrics", index: 1, status: "running" },
    ]);
    const ext: ExtractionMessage = { ...runExtraction([START]), status: "done", created_at: 3 };

    const { lanes } = turnProgress(
      project([{ id: "u1", role: "user", content: "извлеки", created_at: 1 }, msg, ext]),
    );
    const lane = lanes.find((l) => l.name === "fe_extract_metrics");
    expect(lane?.steps.map((s) => s.status)).toEqual(["done", "done", "done"]);
  });

  it("исполнитель работает — план не пишет «в очереди»", () => {
    // Владелец видел «Финансовый анализ: РАБОТАЕТ» рядом с «шаг: В ОЧЕРЕДИ»:
    // объявление плана приходит раньше первой отметки шага.
    const msg = runChat(assistant("a1", 2), [
      { type: "tool_call", name: "fe_analyze" },
      { type: "plan", name: "fe_analyze", steps: ["Определить тип отчёта", "Прочитать отчёт", "Написать отчёт"] },
    ]);
    const { lanes, workers } = turnProgress(
      project([{ id: "u1", role: "user", content: "проанализируй", created_at: 1 }, msg]),
    );
    const lane = lanes.find((l) => l.name === "fe_analyze");
    expect(workers.some((w) => w.label.startsWith("Финансовый анализ") && w.status === "running")).toBe(true);
    expect(lane?.steps.map((s) => s.status)).toEqual(["running", "pending", "pending"]);
  });

  it("отметки шагов от станции остаются главными внутри идущей линии", () => {
    const msg = runChat(assistant("a1", 2), [
      { type: "tool_call", name: "fe_analyze" },
      { type: "plan", name: "fe_analyze", steps: ["Тип отчёта", "Чтение", "Отчёт"] },
      { type: "plan_step", name: "fe_analyze", index: 0, status: "done" },
      { type: "plan_step", name: "fe_analyze", index: 1, status: "running", note: "собрано 40 из 77 показателей…" },
    ]);
    const { lanes } = turnProgress(
      project([{ id: "u1", role: "user", content: "проанализируй", created_at: 1 }, msg]),
    );
    const lane = lanes.find((l) => l.name === "fe_analyze");
    expect(lane?.steps.map((s) => s.status)).toEqual(["done", "running", "pending"]);
    expect(lane?.steps[1].note).toBe("собрано 40 из 77 показателей…");
  });

  it("прогон прерван — идущий шаг закрывается, невзятые остаются в очереди", () => {
    const msg = runChat(assistant("a1", 2), [
      { type: "plan", name: "fe_extract_metrics", steps: ["Классификация", "Извлечение", "Таблица"] },
      { type: "plan_step", name: "fe_extract_metrics", index: 0, status: "done" },
      { type: "plan_step", name: "fe_extract_metrics", index: 1, status: "running" },
    ]);
    const ext: ExtractionMessage = { ...runExtraction([START]), status: "aborted", created_at: 3 };

    const { lanes } = turnProgress(
      project([{ id: "u1", role: "user", content: "извлеки", created_at: 1 }, msg, ext]),
    );
    const lane = lanes.find((l) => l.name === "fe_extract_metrics");
    expect(lane?.steps.map((s) => s.status)).toEqual(["done", "error", "pending"]);
  });

  it("завершённый инструмент не «доделывает» чужую линию", () => {
    const msg = runChat(assistant("a1", 2), [
      { type: "tool_call", name: "fe_analyze" },
      { type: "plan", name: "fe_analyze", steps: ["Тип отчёта", "Чтение"] },
      { type: "plan_step", name: "fe_analyze", index: 0, status: "done" },
      { type: "plan_step", name: "fe_analyze", index: 1, status: "running" },
      { type: "tool_call", name: "fe_idp_ocr" },
      { type: "tool_output", name: "fe_idp_ocr", ok: true },
    ]);
    const { lanes } = turnProgress(
      project([{ id: "u1", role: "user", content: "проанализируй", created_at: 1 }, msg]),
    );
    expect(lanes.find((l) => l.name === "fe_analyze")?.steps.map((s) => s.status)).toEqual([
      "done",
      "running",
    ]);
  });
});
