import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config";

// Тесты фронтенда (ревью 30.10: «у фронтенда нет ни одного теста» — tsc не
// ловит удаление защиты от гонок и повторов). Отдельный файл, чтобы сборка
// vite не тянула vitest.
export default mergeConfig(viteConfig, defineConfig({
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
    restoreMocks: true,
  },
}));
