import { beforeEach, describe, expect, it } from "vitest";
import { DEFAULT_PREFS, loadPrefs, savePrefs } from "@/lib/prefs";

/**
 * «Переключение по умолчанию», которое не переключает никого.
 *
 * Правки одного лишь DEFAULT_PREFS НЕ ХВАТАЕТ: loadPrefs мержит сохранённое
 * ПОВЕРХ дефолта, а у того, кто уже пользовался стендом, в localStorage лежит
 * старое значение — умолчание до него не доходит вовсе. Это блокер Б8 плана
 * миграции, найденный состязательной проверкой: без миграции «неделя работы на
 * новом умолчании», которая служит единственным основанием для необратимого
 * сноса, была бы отработана на СТАРОМ пути, и никто бы этого не заметил —
 * проверка «работает по умолчанию» проходит на чистом профиле.
 *
 * Второе требование, не менее важное: перевод РАЗОВЫЙ. Иначе ручной выбор
 * «Finengine» в тумблере затирался бы при каждой перезагрузке, и остаться на
 * старом пути было бы невозможно даже нажав кнопку.
 */
// ⚠️ Ключ обязан совпадать с prefs.ts БУКВА В БУКВУ. Первая версия теста
// писала в «finengine:prefs» вместо «finengine:prefs:v1» — loadPrefs её просто
// не видел и всегда возвращал дефолт, поэтому два теста «прошли» по ложной
// причине, а два упали. Тест, пишущий не туда, проверяет пустоту.
const KEY = "finengine:prefs:v1";

// Среда тестов — node, браузерного хранилища в ней нет. Подменяем минимумом:
// проверяется ЛОГИКА перевода, а не сам браузер, и подмена её не искажает.
const store = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => void store.set(k, v),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
};
// savePrefs шлёт событие в окно — в node его нет, а падать тест не должен.
(globalThis as Record<string, unknown>).window ??= globalThis;
(globalThis as { dispatchEvent?: unknown }).dispatchEvent ??= () => true;

describe("умолчание пути хода", () => {
  beforeEach(() => localStorage.clear());

  it("для чистого профиля путь по умолчанию — миграционный", () => {
    expect(DEFAULT_PREFS.arch).toBe("aifa_gateway");
    expect(loadPrefs().arch).toBe("aifa_gateway");
  });

  it("сохранённый старый путь ПЕРЕВОДИТСЯ, а не игнорируется", () => {
    localStorage.setItem(KEY, JSON.stringify({ ...DEFAULT_PREFS, arch: "finengine_baseline" }));
    expect(loadPrefs().arch).toBe("aifa_gateway");
  });

  it("ручной возврат на старый путь ПЕРЕЖИВАЕТ перезагрузку", () => {
    // Первая загрузка переводит и ставит метку.
    localStorage.setItem(KEY, JSON.stringify({ ...DEFAULT_PREFS, arch: "finengine_baseline" }));
    expect(loadPrefs().arch).toBe("aifa_gateway");
    // Человек нажал «Finengine» в тумблере.
    savePrefs({ ...loadPrefs(), arch: "finengine_baseline" });
    // Перезагрузка не должна отнимать этот выбор.
    expect(loadPrefs().arch).toBe("finengine_baseline");
  });

  it("другие осознанно выбранные пути не трогаются", () => {
    // Взят ЖИВОЙ путь. Раньше здесь стоял "aifa_baseline" — он снят 21.08.2026
    // и теперь мигрирует, поэтому проверять им «не трогаются» больше нельзя:
    // тест утверждал бы обратное тому, что мы сделали нарочно.
    localStorage.setItem(KEY, JSON.stringify({ ...DEFAULT_PREFS, arch: "finengine_dev" }));
    expect(loadPrefs().arch).toBe("finengine_dev");
  });

  it.each(["aifa_baseline", "aifa", "aifa_dev"])(
    "снятый путь %s уводится с сохранённого выбора, а не упирается в 400",
    (снятый) => {
      // Без миграции сохранённый выбор пережил бы правку и упёрся бы в отказ
      // бэкенда при ПЕРВОМ ЖЕ извлечении: умолчание до localStorage не доходит.
      // Движки считали по пятой копии реестра и давали числа в сто раз меньше.
      localStorage.setItem(KEY, JSON.stringify({ ...DEFAULT_PREFS, arch: снятый }));
      // Именно на петлю ядра: человек выбирал «AIFA», ею сегодня и является
      // aifa_gateway. Увести его на Finengine значило бы сменить продукт.
      expect(loadPrefs().arch).toBe("aifa_gateway");
    },
  );
});
