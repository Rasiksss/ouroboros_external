import { beforeEach, describe, expect, it } from "vitest";
import { DEFAULT_PREFS, loadPrefs, savePrefs } from "@/lib/prefs";

/**
 * Тумблер «Поиск в интернете» включён по умолчанию (решение владельца 08.10.2026).
 *
 * Одной правки DEFAULT_PREFS мало по той же причине, что и у пути хода
 * (prefs-arch-default.test.ts): сохранённое лежит ПОВЕРХ умолчания, а savePrefs
 * пишет настройки целиком — прежнее `webSearch: false` сохранено у каждого, кто
 * открывал приложение, и «не трогал» от «выключил сам» по нему не отличить.
 * Поэтому включение разовое, по метке; после него выбор человека соблюдается.
 */
const KEY = "finengine:prefs:v1";

const store = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => void store.set(k, v),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
};
(globalThis as Record<string, unknown>).window ??= globalThis;
(globalThis as { dispatchEvent?: unknown }).dispatchEvent ??= () => true;

describe("умолчание тумблера «Поиск в интернете»", () => {
  beforeEach(() => localStorage.clear());

  it("для чистого профиля поиск включён", () => {
    expect(DEFAULT_PREFS.webSearch).toBe(true);
    expect(loadPrefs().webSearch).toBe(true);
  });

  it("сохранённое прежнее умолчание «выключено» один раз включается", () => {
    localStorage.setItem(KEY, JSON.stringify({ ...DEFAULT_PREFS, webSearch: false, role: "risks" }));
    const prefs = loadPrefs();
    expect(prefs.webSearch).toBe(true);
    expect(prefs.role).toBe("risks"); // остальные настройки не сбрасываются
  });

  it("выключенное пользователем после этого соблюдается и переживает перезагрузку", () => {
    localStorage.setItem(KEY, JSON.stringify({ ...DEFAULT_PREFS, webSearch: false }));
    expect(loadPrefs().webSearch).toBe(true);
    savePrefs({ ...loadPrefs(), webSearch: false });
    expect(loadPrefs().webSearch).toBe(false);
    expect(loadPrefs().webSearch).toBe(false);
  });
});
