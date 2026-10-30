import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// jsdom не знает ResizeObserver (Ellipsis) и matchMedia (тема).
class NoopResizeObserver {
  observe() { /* нечего наблюдать */ }
  unobserve() { /* нечего наблюдать */ }
  disconnect() { /* нечего наблюдать */ }
}
(globalThis as any).ResizeObserver ??= NoopResizeObserver;
if (!window.matchMedia) {
  (window as any).matchMedia = (q: string) => ({
    matches: false, media: q, onchange: null,
    addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
    dispatchEvent() { return false; },
  });
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  localStorage.clear();
});
