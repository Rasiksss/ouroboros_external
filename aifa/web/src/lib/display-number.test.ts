import { describe, expect, it } from "vitest";

import { formatDisplayNumber } from "./display-number";

// toLocaleString("ru-RU") ставит неразрывный пробел между разрядами.
const nbsp = (text: string) => text.replace(/ /g, " ");

describe("formatDisplayNumber", () => {
  it("убирает двоичный хвост и группирует разряды", () => {
    expect(formatDisplayNumber(42085.931000000004)).toBe(nbsp("42 085,931"));
    expect(formatDisplayNumber(0.1 + 0.2)).toBe("0,3");
  });

  it("не трогает целые, отрицательные и очень большие значения", () => {
    expect(formatDisplayNumber(100)).toBe("100");
    expect(formatDisplayNumber(-1234.5678)).toBe(nbsp("-1 234,568"));
    expect(formatDisplayNumber(1e21)).toBe(nbsp("1 000 000 000 000 000 000 000"));
  });

  it("не превращает малое ненулевое значение в ноль", () => {
    expect(formatDisplayNumber(1.5e-7)).toBe("0,00000015");
    expect(formatDisplayNumber(-0.0004)).toBe("-0,0004");
  });

  it("отрицательный ноль — просто ноль", () => {
    expect(formatDisplayNumber(-0)).toBe("0");
  });
});
