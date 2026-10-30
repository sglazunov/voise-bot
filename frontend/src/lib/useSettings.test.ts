import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { deferred, mockFetch, reply } from "../test/fetch";
import { useManagedTeam, useSettings } from "./useSettings";
import { logout } from "./api";

// Ревью 30.10, раздел 3: основатель в окне выбора команды писал настройки
// клиента A в клиента B — поздний ответ для A перезаписывал форму после
// переключения на B, и «Сохранить» уходило в B.
describe("useSettings: смена команды", () => {
  it("поздний ответ прежней команды не попадает в форму", async () => {
    const forA = deferred<Response>();
    const forB = deferred<Response>();
    mockFetch((url) => (url.includes("team=a") ? forA.promise : forB.promise));
    const { result, rerender } = renderHook(({ team }) => useSettings(team),
      { initialProps: { team: "a" } });
    rerender({ team: "b" });
    await act(async () => { forB.resolve(reply({ team: "b", rec_include: "B" })); });
    await waitFor(() => expect(result.current.s.team).toBe("b"));
    await act(async () => { forA.resolve(reply({ team: "a", rec_include: "A" })); });
    expect(result.current.s.team).toBe("b");
    expect(result.current.s.rec_include).toBe("B");
  });

  it("пока ответа для новой команды нет, сохранение закрыто", async () => {
    const forA = deferred<Response>();
    const calls = mockFetch((url) => (url.includes("team=a") ? forA.promise : new Promise<Response>(() => {})));
    const { result, rerender } = renderHook(({ team }) => useSettings(team),
      { initialProps: { team: "a" } });
    rerender({ team: "b" });
    await act(async () => { forA.resolve(reply({ team: "a" })); });
    expect(result.current.ready).toBe(false);
    await expect(result.current.save({ rec_include: "x" })).rejects.toThrow();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("сохранение уходит в выбранную команду", async () => {
    const calls = mockFetch((url) => reply({ team: url.includes("team=b") ? "b" : "own" }));
    const { result } = renderHook(() => useSettings("b"));
    await waitFor(() => expect(result.current.ready).toBe(true));
    await act(async () => { await result.current.save({ rec_include: "x" }); });
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.url).toBe("/api/automation/settings?team=b");
  });
});

describe("выбранная команда не переживает выход и отказ", () => {
  it("403 на чужую команду — выбор сбрасывается", async () => {
    sessionStorage.setItem("vtx.manage-team", "bob");
    mockFetch((url) => (url.includes("team=bob")
      ? reply({ detail: "Настройки другой команды меняет только основатель сервера." }, 403)
      : reply({ team: "own" })));
    const { result } = renderHook(() => {
      const [team, setTeam] = useManagedTeam();
      return { team, ...useSettings(team, () => setTeam("")) };
    });
    expect(result.current.team).toBe("bob");
    await waitFor(() => expect(result.current.team).toBe(""));
    await waitFor(() => expect(result.current.s.team).toBe("own"));
    expect(sessionStorage.getItem("vtx.manage-team")).toBeNull();
  });

  it("404 (команду удалили) — тоже сброс", async () => {
    const gone = vi.fn();
    mockFetch(() => reply({ detail: "Команда не найдена." }, 404));
    renderHook(() => useSettings("ghost", gone));
    await waitFor(() => expect(gone).toHaveBeenCalled());
  });

  it("сетевой сбой своей команды выбор не трогает", async () => {
    const gone = vi.fn();
    mockFetch(() => reply({ detail: "boom" }, 500));
    const { result } = renderHook(() => useSettings("bob", gone));
    await waitFor(() => expect(result.current.failed).toBe(true));
    expect(gone).not.toHaveBeenCalled();
  });

  it("выход стирает выбранную команду", async () => {
    sessionStorage.setItem("vtx.manage-team", "bob");
    const calls = mockFetch(() => reply({}));
    vi.spyOn(console, "error").mockImplementation(() => {});   // jsdom: «navigation not implemented»
    await logout();
    expect(calls[0].url).toBe("/api/auth/logout");
    expect(sessionStorage.getItem("vtx.manage-team")).toBeNull();
  });
});
