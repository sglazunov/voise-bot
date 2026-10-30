import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ToastProvider } from "../components/ui";
import { mockFetch, reply } from "../test/fetch";
import Weeek from "./Weeek";
import Cloud from "./Cloud";

// Ревью 30.10, раздел 3: `?team=` молча терялся на «Диагностике задачи»,
// «Участниках Weeek» и «Тесте загрузки» — основатель, открывший клиента,
// проверял токен и облако СВОЕЙ команды.
const SETTINGS = {
  team: "bob", can_manage_teams: true, locked_keys: [], weeek_token: true,
  managed_by_server: true, cloud: "yandex_disk", yandex_disk: { folder: "disk:/x" },
};

function route(url: string) {
  if (url.startsWith("/api/automation/settings")) return reply(SETTINGS);
  if (url.startsWith("/api/automation/teams")) return reply({ teams: [{ team: "bob" }] });
  if (url.includes("/weeek/members")) return reply({ members: [] });
  if (url.includes("/clouds/test")) return reply({ ok: true, url: "https://x" });
  return reply({});
}

function show(page: JSX.Element) {
  return render(<MemoryRouter><ToastProvider>{page}</ToastProvider></MemoryRouter>);
}

describe("страницы основателя передают выбранную команду", () => {
  it("Weeek: диагностика задачи и участники — с ?team=", async () => {
    sessionStorage.setItem("vtx.manage-team", "bob");
    const calls = mockFetch(route);
    show(<Weeek />);
    await waitFor(() => expect(calls.some((c) => c.url === "/api/automation/settings?team=bob")).toBe(true));
    fireEvent.change(screen.getByPlaceholderText("ID задачи в Weeek"), { target: { value: "101" } });
    fireEvent.click(screen.getByText("Показать"));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/weeek/probe"))).toBe(true));
    expect(calls.find((c) => c.url.includes("/weeek/probe"))!.url)
      .toBe("/api/automation/weeek/probe?task_id=101&team=bob");
    fireEvent.click(screen.getByText(/Участники Weeek/));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/weeek/members"))).toBe(true));
    expect(calls.find((c) => c.url.includes("/weeek/members"))!.url)
      .toBe("/api/automation/weeek/members?refresh=1&team=bob");
  });

  it("Облако: тест загрузки — облаком выбранной команды", async () => {
    sessionStorage.setItem("vtx.manage-team", "bob");
    const calls = mockFetch(route);
    show(<Cloud />);
    await waitFor(() => expect(screen.getAllByText(/Тест загрузки/).length).toBeGreaterThan(0));
    const btn = screen.getAllByText(/Тест загрузки/)[0].closest("button")!;
    expect(btn.disabled).toBe(false);
    fireEvent.click(btn);
    await waitFor(() => expect(calls.some((c) => c.url.includes("/clouds/test"))).toBe(true));
    expect(calls.find((c) => c.url.includes("/clouds/test"))!.url)
      .toBe("/api/automation/clouds/test?team=bob");
  });

  it("Облако: токен, введённый для клиента, не уходит в свою команду", async () => {
    sessionStorage.setItem("vtx.manage-team", "bob");
    const calls = mockFetch((url) => (url.startsWith("/api/automation/teams")
      ? reply({ teams: [{ team: "bob" }] })
      : reply({ ...SETTINGS, managed_by_server: false })));
    show(<Cloud />);
    await waitFor(() => expect(screen.getByPlaceholderText("вставьте токен")).toBeTruthy());
    fireEvent.change(screen.getByPlaceholderText("вставьте токен"), { target: { value: "токен-боба" } });
    fireEvent.change(screen.getByLabelText("Чьи настройки открыть"), { target: { value: "" } });
    await waitFor(() => expect(calls.some((c) => c.url === "/api/automation/settings")).toBe(true));
    await waitFor(() => expect(screen.getAllByText("Сохранить")[0].closest("button")!.disabled).toBe(false));
    fireEvent.click(screen.getAllByText("Сохранить")[0]);
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.url).toBe("/api/automation/settings");
    expect(post.body.yandex_disk.token).toBeUndefined();
  });

  it("Облако: 403 на выбранную команду — откат на свою", async () => {
    sessionStorage.setItem("vtx.manage-team", "bob");
    const calls = mockFetch((url) => (url.includes("team=bob")
      ? reply({ detail: "Только основатель сервера." }, 403)
      : reply({ ...SETTINGS, team: "own", can_manage_teams: false })));
    show(<Cloud />);
    await waitFor(() => expect(calls.some((c) => c.url === "/api/automation/settings")).toBe(true));
    expect(sessionStorage.getItem("vtx.manage-team")).toBeNull();
  });
});
