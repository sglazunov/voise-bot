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

// Второй раунд ревью 30.10, §3.
describe("useSettings: второй раунд — reload, catch и окно смены команды", () => {
  it("reload() из замыкания прежней команды грузит ТЕКУЩУЮ команду", async () => {
    const forB = deferred<Response>();
    const calls = mockFetch((url) => (url.includes("team=a")
      ? reply({ team: "a", rec_include: "A" }) : forB.promise));
    const { result, rerender } = renderHook(({ team }) => useSettings(team),
      { initialProps: { team: "a" } });
    await waitFor(() => expect(result.current.s.team).toBe("a"));
    const reloadA = result.current.reload;     // как в onSave Weeek после await save
    rerender({ team: "b" });
    await act(async () => { forB.resolve(reply({ team: "b", rec_include: "B" })); });
    await waitFor(() => expect(result.current.s.team).toBe("b"));
    await act(async () => { await reloadA(); });
    expect(result.current.s.team).toBe("b");
    expect(result.current.s.rec_include).toBe("B");
    expect(calls.filter((c) => c.url.includes("team=a"))).toHaveLength(1);
  });

  it("ответ прежней команды не ложится, даже если её reload ушёл последним", async () => {
    let n = 0;
    const lateA = deferred<Response>();
    mockFetch((url) => {
      if (url.includes("team=a")) return ++n === 1 ? reply({ team: "a" }) : lateA.promise;
      return new Promise<Response>(() => {});
    });
    const { result, rerender } = renderHook(({ team }) => useSettings(team),
      { initialProps: { team: "a" } });
    await waitFor(() => expect(result.current.s.team).toBe("a"));
    let p: Promise<unknown> = Promise.resolve();
    act(() => { p = result.current.reload(); });   // reload ушёл, ответа нет
    rerender({ team: "b" });
    await act(async () => { lateA.resolve(reply({ team: "a" })); await p; });
    expect(result.current.ready).toBe(false);
    expect(result.current.s.team).toBeUndefined();
  });

  it("отказ прежней команды после переключения — не ошибка и не сброс выбора", async () => {
    const forA = deferred<Response>();
    const gone = vi.fn();
    mockFetch((url) => (url.includes("team=a") ? forA.promise : reply({ team: "b" })));
    const { result, rerender } = renderHook(({ team }) => useSettings(team, gone),
      { initialProps: { team: "a" } });
    rerender({ team: "b" });
    await waitFor(() => expect(result.current.s.team).toBe("b"));
    await act(async () => { forA.resolve(reply({ detail: "нет" }, 403)); });
    expect(gone).not.toHaveBeenCalled();
    expect(result.current.failed).toBe(false);
  });

  it("сразу после смены команды форма пуста и «Сохранить» закрыто", async () => {
    const seen: { team: string; ready: boolean; shown: any }[] = [];
    const calls = mockFetch((url) => (url.includes("team=a")
      ? reply({ team: "a" }) : new Promise<Response>(() => {})));
    const { result, rerender } = renderHook(({ team }) => {
      const r = useSettings(team);
      seen.push({ team, ready: r.ready, shown: r.s.team });
      return r;
    }, { initialProps: { team: "a" } });
    await waitFor(() => expect(result.current.s.team).toBe("a"));
    rerender({ team: "b" });
    // Ни одного рендера, где выбрана b, а в форме — настройки a.
    expect(seen.filter((x) => x.team === "b" && (x.ready || x.shown === "a"))).toEqual([]);
    expect(result.current.ready).toBe(false);
    await expect(result.current.save({ rec_include: "x" })).rejects.toThrow();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("правка поля до загрузки не открывает сохранение", async () => {
    mockFetch(() => new Promise<Response>(() => {}));
    const { result } = renderHook(() => useSettings("b"));
    act(() => { result.current.set("weeek_project_id", "1"); });
    expect(result.current.ready).toBe(false);
  });

  it("признак основателя переживает смену команды (выбор не пропадает)", async () => {
    mockFetch((url) => (url.includes("team=b")
      ? new Promise<Response>(() => {}) : reply({ team: "own", can_manage_teams: true })));
    const { result, rerender } = renderHook(({ team }) => useSettings(team),
      { initialProps: { team: "" } });
    await waitFor(() => expect(result.current.canManage).toBe(true));
    rerender({ team: "b" });
    expect(result.current.ready).toBe(false);
    expect(result.current.canManage).toBe(true);
  });
});
