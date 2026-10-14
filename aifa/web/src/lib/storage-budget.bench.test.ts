import { describe, expect, it } from "vitest";
import { __fitToBudgetНаивно, __fitToBudgetБыстро, type Project } from "./projects";

/** Замер, а не утверждение: цена правки должна быть видна числом. */
describe("цена обрезки на профиле владельца", () => {
  it("линейная версия быстрее наивной и даёт тот же ответ", () => {
    const крупные = Array.from({ length: 104 }, (_, i) => ({
      id: `p${i}`, updated_at: 1_000_000 + i,
      messages: Array.from({ length: 12 }, (_, j) => ({
        id: `m${j}`, role: j % 2 ? "assistant" : "user",
        content: "ы".repeat(i % 7 === 0 ? 12_000 : 2_000),
      })),
    })) as unknown as Project[];
    const бюджет = 1_572_864;

    const t0 = performance.now();
    const наивно = __fitToBudgetНаивно(крупные, бюджет);
    const цена1 = performance.now() - t0;
    const t1 = performance.now();
    const быстро = __fitToBudgetБыстро(крупные, бюджет);
    const цена2 = performance.now() - t1;

    console.log(`  наивно ${цена1.toFixed(1)} мс → быстро ${цена2.toFixed(1)} мс (${(цена1 / цена2).toFixed(1)}x), оставлено ${быстро.length}`);
    expect(быстро.map((p) => p.id)).toEqual(наивно.map((p) => p.id));
    // Порог с большим запасом: меряем на CI разной мощности, а падать от
    // шума планировщика сторож не должен. Ловим возврат квадратичной цены.
    expect(цена2).toBeLessThan(цена1 / 3);
  });
});
