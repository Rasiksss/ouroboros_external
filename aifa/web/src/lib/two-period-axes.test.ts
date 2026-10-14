import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { comparativeHeader } from "@/components/workspace/panes/extracted-table";
import type { MetricRow } from "@/lib/projects";

/**
 * У промежуточного отчёта ДВЕ оси сравнения (#205, прогон РСХБ 19.08).
 *
 *     баланс   31.03.2026  против  31.12.2025   ← конец прошлого ГОДА
 *     ОПУ      Q1 2026     против  Q1 2025      ← тот же квартал год назад
 *
 * Колонка «прошлый период» одна, и подписать её одним периодом значит соврать
 * про две трети строк — причём правдоподобно: пользователь прочтёт балансовую
 * цифру как «первый квартал прошлого года». В эталоне разметки соотношение
 * ровно такое: 68 строк датированы, 30 подписаны интервалами.
 */
const row = (over: Partial<MetricRow>): MetricRow =>
  ({ enum: "X", name_ru: "Показатель", value_current: 1, value_prev: null, ...over }) as MetricRow;

describe("как называется колонка сравнительного периода", () => {
  it("подписей нет — колонка называется периодом прогона", () => {
    // Годовой отчёт: ось одна, движок сам заполнил обе клетки. Так и выглядел
    // живой прогон 19.08 — 110 строк, ноль подписей.
    const rows = [row({ value_prev: 2 }), row({ value_prev: 3 })];
    expect(comparativeHeader(rows, "FY 2018")).toBe("FY 2018");
  });

  it("строки спорят между собой — колонка называется общо", () => {
    const rows = [
      row({ value_prev: 2, period_prev_label: "31.12.2025" }),
      row({ value_prev: 3, period_prev_label: "Q1 2025" }),
    ];
    expect(comparativeHeader(rows, "Q1 2025")).toBe("");
  });

  it("🔴 одна подпись + строки без подписи = ДВЕ оси, называть нельзя", () => {
    // Ровно этот случай даёт промежуточный отчёт: балансовым строкам период
    // подобрали мы (31.12.2025), строкам ОПУ его нашёл сам движок и подписи у
    // них нет — их период равен периоду прогона (Q1 2025). Набор подписей
    // размера один, а осей две. Первая редакция правила называла тут колонку
    // балансовой датой и врала про весь ОПУ.
    const rows = [
      row({ value_prev: 908529, period_prev_label: "31.12.2025" }),
      row({ value_prev: 199063 }),
    ];
    expect(comparativeHeader(rows, "Q1 2025")).toBe("");
  });

  it("одна подпись, и она совпадает с периодом прогона — можно назвать", () => {
    const rows = [
      row({ value_prev: 2, period_prev_label: "Q1 2025" }),
      row({ value_prev: 3 }),
    ];
    expect(comparativeHeader(rows, "Q1 2025")).toBe("Q1 2025");
  });

  it("пустые клетки в счёт не идут", () => {
    // Строка без сравнительного значения ничего не говорит о периоде — по ней
    // нельзя ни назвать колонку, ни объявить оси разными.
    const rows = [row({ value_prev: null }), row({ value_prev: 2, period_prev_label: "31.12.2025" })];
    expect(comparativeHeader(rows, "Q1 2025")).toBe("31.12.2025");
  });
});

describe("точный период виден в самой строке", () => {
  const TABLE = readFileSync(
    fileURLToPath(new URL("../components/workspace/panes/extracted-table.tsx", import.meta.url)),
    "utf8",
  );

  it("в ячейке сказано, с каким периодом сравнение", () => {
    // Без этого «пусто во второй колонке» и «сравнили с другим периодом»
    // выглядят одинаково.
    expect(TABLE).toContain("Сравнение с ${prevAt}");
  });

  it("дельта тоже названа по своему периоду", () => {
    expect(TABLE).toContain('title: prevAt ? `к ${prevAt}` : undefined');
  });
});
