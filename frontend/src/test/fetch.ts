import { vi } from "vitest";

// Подмена fetch: весь настоящий код api.ts (разбор ответа, ошибки) работает.
export function reply(data: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300, status,
    json: async () => data,
    text: async () => (typeof data === "string" ? data : JSON.stringify(data)),
  } as unknown as Response;
}

export type Call = { url: string; method: string; body: any };

export function mockFetch(route: (url: string, call: Call) => Response | Promise<Response>) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: any, init?: RequestInit) => {
    const url = String(input);
    const call = {
      url, method: (init?.method || "GET").toUpperCase(),
      body: typeof init?.body === "string" ? JSON.parse(init.body) : init?.body,
    };
    calls.push(call);
    return route(url, call);
  }));
  return calls;
}

export function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}
