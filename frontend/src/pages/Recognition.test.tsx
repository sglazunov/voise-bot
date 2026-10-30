import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ToastProvider } from "../components/ui";
import { Call, deferred, mockFetch, reply } from "../test/fetch";
import Recognition from "./Recognition";

// Защиты опроса карточки встречи (ревью 30.10, P1 фронтенда), которые tsc не
// ловит: удали их — сборка зелёная, а живая расшифровка двоится и удалённая
// задача молотит сервер каждые 2 с.
const NOW = new Date("2026-10-30T12:00:00Z").getTime();
const JOB = { id: "j1", filename: "Планёрка.mp4", status: "running", progress: 0.1,
              created_at: NOW / 1000 };

async function tick(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

async function openJob(route: (url: string, call: Call) => Response | Promise<Response>) {
  vi.useFakeTimers({ now: NOW });
  const calls = mockFetch((url, call) => {
    if (url === "/api/jobs") return reply([JOB]);
    return route(url, call);
  });
  render(<MemoryRouter><ToastProvider><Recognition /></ToastProvider></MemoryRouter>);
  await tick(0);
  fireEvent.click(screen.getByText("Планёрка.mp4"));
  await tick(0);
  return calls;
}

const partials = (calls: Call[]) => calls.filter((c) => c.url.includes("/partial"));
const fulls = (calls: Call[]) => calls.filter((c) => c.url === "/api/jobs/j1");

describe("Recognition: опрос выбранной встречи", () => {
  it("пока /partial в пути, новые тики не уходят (in-flight)", async () => {
    const slow = deferred<Response>();
    let n = 0;
    const calls = await openJob((url) => {
      if (url === "/api/jobs/j1") return reply(JOB);
      if (url.includes("/partial")) return ++n === 1 ? slow.promise
        : reply({ status: "running", progress: 0.3, from: 1, segments: [{ text: "два" }] });
      return reply({});
    });
    await tick(7000);                                  // три тика ждут медленный ответ
    expect(partials(calls)).toHaveLength(1);
    await act(async () => {
      slow.resolve(reply({ status: "running", progress: 0.2, from: 0, segments: [{ text: "раз" }] }));
    });
    await tick(2000);
    expect(partials(calls)).toHaveLength(2);
    expect(partials(calls)[1].url).toBe("/api/jobs/j1/partial?since=1");
  });

  it("повторный ответ с тем же from не удваивает живой текст", async () => {
    const calls = await openJob((url) => {
      if (url === "/api/jobs/j1") return reply(JOB);
      if (url.includes("/partial"))
        return reply({ status: "running", progress: 0.2, from: 0, segments: [{ text: "раз" }] });
      return reply({});
    });
    fireEvent.click(screen.getByText("Расшифровка"));
    await tick(6000);
    expect(partials(calls).length).toBeGreaterThan(2);
    expect(screen.getByText(/распознаётся вживую · фрагментов: 1/)).toBeTruthy();
  });

  it("карточка не пришла — повтор с паузой 2, 4, 8 с и кнопка «Повторить сейчас»", async () => {
    let ok = false;
    const calls = await openJob((url) => {
      if (url === "/api/jobs/j1") return ok ? reply({ ...JOB, status: "done" }) : reply({ detail: "boom" }, 500);
      if (url.includes("/partial")) return reply({ status: "done", from: 0, segments: [] });
      return reply({});
    });
    expect(fulls(calls)).toHaveLength(1);              // t=0, отказ
    expect(screen.getByText("Не удалось загрузить встречу")).toBeTruthy();
    await tick(2000);
    expect(fulls(calls)).toHaveLength(2);              // t=2: пауза 2 с
    await tick(2000);
    expect(fulls(calls)).toHaveLength(2);              // t=4: ждём (пауза 4 с)
    await tick(2000);
    expect(fulls(calls)).toHaveLength(3);              // t=6
    await tick(6000);
    expect(fulls(calls)).toHaveLength(3);              // t=12: ждём (пауза 8 с)
    await tick(2000);
    expect(fulls(calls)).toHaveLength(4);              // t=14
    ok = true;
    fireEvent.click(screen.getByText("Повторить сейчас"));
    await tick(0);
    expect(fulls(calls)).toHaveLength(5);
    expect(screen.queryByText("Не удалось загрузить встречу")).toBeNull();
  });
});
