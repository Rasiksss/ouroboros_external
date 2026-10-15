import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

/**
 * Тесты фронта. Первые появились из-за #143: «панель опустела при живых
 * данных» — класс ошибок, который глазами ловится только постфактум, у
 * пользователя. Здесь проверяется ЧИСТАЯ логика (слияние снимков, сборка
 * строк, сериализация в TSV) — без браузера и без jsdom: всё, что нужно было
 * поймать, выражается функциями над данными.
 */
export default defineConfig({
  resolve: {
    // Тот же алиас, что в tsconfig.json — иначе импорты «@/lib/...» не найдутся.
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    include: ["src/**/*.test.ts"],
    environment: "node",
  },
});
