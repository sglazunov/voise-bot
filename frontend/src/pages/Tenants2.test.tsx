import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ToastProvider } from "../components/ui";
import { TeamPicker } from "../components/TeamPicker";
import { deferred, mockFetch, reply } from "../test/fetch";
import Weeek from "./Weeek";
import Recognition from "./Recognition";

// Второй раунд ревью 30.10, §3: поздние ответы страницы Weeek, сброс её
// списков при смене команды, плашка замка и опрос списка встреч.
const BOB = {
  team: "bob", can_manage_teams: true, locked_keys: [], weeek_token: true,
  weeek_project_id: "10", managed_by_server: true,
};
const OWN = { ...BOB, team: "own", weeek_project_id: "77", managed_by_server: false };

function show(page: JSX.Element) {
  return render(<MemoryRouter><ToastProvider>{page}</ToastProvider></MemoryRouter>);
}

function base(url: string) {
  if (url.startsWith("/api/automation/settings")) return reply(url.includes("team=bob") ? BOB : OWN);
  if (url.startsWith("/api/automation/teams")) return reply({ teams: [{ team: "bob" }, { team: "own", own: true }] });
  return null;
}

async function openBob(route: (url: string) => Response | Promise<Response>) {
  sessionStorage.setItem("vtx.manage-team", "bob");
  const calls = mockFetch((url) => base(url) || route(url));
  show(<Weeek />);
  await waitFor(() => expect(screen.getByLabelText("Чьи настройки открыть")).toBeTruthy());
  await waitFor(() => expect((screen.getByLabelText("ID проекта") as HTMLInputElement).value).toBe("10"));
  return calls;
}

function switchToOwn() {
  fireEvent.change(screen.getByLabelText("Чьи настройки открыть"), { target: { value: "" } });
}

describe("Weeek: поздний ответ прежней команды не показывается", () => {
  it("доски встреч команды A не появляются у B", async () => {
    const boards = deferred<Response>();
    await openBob((url) => (url.includes("/weeek/boards") ? boards.promise : reply({})));
    fireEvent.click(screen.getByLabelText("Загрузить доски проекта"));
    switchToOwn();
    await waitFor(() => expect((screen.getByLabelText("ID проекта") as HTMLInputElement).value).toBe("77"));
    await act(async () => { boards.resolve(reply({ boards: [{ id: 1, name: "Доска Боба" }] })); });
    expect(screen.queryByLabelText("Выбрать доску для встреч")).toBeNull();
    expect(screen.queryByText(/Доска Боба/)).toBeNull();
  });

  it("проекты и участники команды A не появляются у B", async () => {
    const projects = deferred<Response>();
    const members = deferred<Response>();
    await openBob((url) => {
      if (url.includes("/weeek/projects")) return projects.promise;
      if (url.includes("/weeek/members")) return members.promise;
      return reply({});
    });
    fireEvent.click(screen.getByRole("button", { name: /Мои проекты/ }));
    fireEvent.click(screen.getByText(/Участники Weeek/));
    switchToOwn();
    await waitFor(() => expect((screen.getByLabelText("ID проекта") as HTMLInputElement).value).toBe("77"));
    await act(async () => {
      projects.resolve(reply({ projects: [{ id: 5, name: "Проект Боба" }] }));
      members.resolve(reply({ members: [{ id: "u1", name: "Боб" }, { id: "u2", name: "Ева" }] }));
    });
    expect(screen.queryByText("Проект Боба")).toBeNull();
    expect(screen.queryByText(/Участники Weeek \(2\)/)).toBeNull();
  });

  it("проба задачи команды A не появляется у B", async () => {
    const probe = deferred<Response>();
    await openBob((url) => (url.includes("/weeek/probe") ? probe.promise : reply({})));
    fireEvent.change(screen.getByPlaceholderText("ID задачи в Weeek"), { target: { value: "101" } });
    fireEvent.click(screen.getByText("Показать"));
    switchToOwn();
    await waitFor(() => expect((screen.getByLabelText("ID проекта") as HTMLInputElement).value).toBe("77"));
    await act(async () => { probe.resolve(reply({ task: { id: 101, title: "Секрет Боба" } })); });
    expect(screen.queryByText(/Секрет Боба/)).toBeNull();
  });
});

describe("Weeek: смена команды сбрасывает уже загруженное", () => {
  it("участники и проба прежней команды пропадают", async () => {
    await openBob((url) => {
      if (url.includes("/weeek/members")) return reply({ members: [{ id: "u1", name: "Боб" }, { id: "u2", name: "Ева" }] });
      if (url.includes("/weeek/probe")) return reply({ task: { id: 101, title: "Секрет Боба" } });
      return reply({});
    });
    fireEvent.click(screen.getByText(/Участники Weeek/));
    await waitFor(() => expect(screen.getByText(/Участники Weeek \(2\)/)).toBeTruthy());
    fireEvent.change(screen.getByPlaceholderText("ID задачи в Weeek"), { target: { value: "101" } });
    fireEvent.click(screen.getByText("Показать"));
    await waitFor(() => expect(screen.getByText(/Секрет Боба/)).toBeTruthy());
    switchToOwn();
    await waitFor(() => expect((screen.getByLabelText("ID проекта") as HTMLInputElement).value).toBe("77"));
    expect(screen.queryByText(/Участники Weeek \(2\)/)).toBeNull();
    expect(screen.queryByText(/Секрет Боба/)).toBeNull();
  });

  it("выбор команды не пропадает, пока грузятся настройки новой", async () => {
    sessionStorage.setItem("vtx.manage-team", "bob");
    mockFetch((url) => (url === "/api/automation/settings" ? new Promise<Response>(() => {}) : base(url) || reply({})));
    show(<Weeek />);
    await waitFor(() => expect((screen.getByLabelText("ID проекта") as HTMLInputElement).value).toBe("10"));
    switchToOwn();
    expect(screen.getByLabelText("Чьи настройки открыть")).toBeTruthy();
    expect((screen.getByLabelText("ID проекта") as HTMLInputElement).value).toBe("");
    expect(screen.getAllByText("Сохранить")[0].closest("button")!.disabled).toBe(true);
  });
});

describe("TeamPicker: плашка замка только у команды под замком", () => {
  const note = () => screen.queryByRole("note");
  const pick = (s: Record<string, any>) => render(
    <TeamPicker s={s} team="" setTeam={() => {}} onManaged={async () => {}} />);

  it("без замка серверные ключи плашку не включают", () => {
    pick({ managed_by_server: false, locked_keys: ["managed_by_server", "local_dir"] });
    expect(note()).toBeNull();
  });
  it("под замком у основателя (ключей нет) — плашки нет", () => {
    pick({ managed_by_server: true, locked_keys: [] });
    expect(note()).toBeNull();
  });
  it("под замком у клиента — плашка есть", () => {
    pick({ managed_by_server: true, locked_keys: ["weeek_token"] });
    expect(note()).not.toBeNull();
  });
});

describe("Recognition: опрос списка встреч", () => {
  it("пока /api/jobs в пути, новый запрос списка не уходит", async () => {
    const NOW = new Date("2026-10-30T12:00:00Z").getTime();
    vi.useFakeTimers({ now: NOW });
    const first = deferred<Response>();
    let n = 0;
    const calls = mockFetch((url) => {
      if (url === "/api/jobs") return ++n === 1 ? first.promise : reply([]);
      return reply({});
    });
    show(<Recognition />);
    // Список пуст (ответа нет) — значит, опрос раз в 30 с; к 65-й секунде
    // без защиты ушли бы ещё два запроса.
    await act(async () => { await vi.advanceTimersByTimeAsync(65000); });
    expect(calls.filter((c) => c.url === "/api/jobs")).toHaveLength(1);
    await act(async () => { first.resolve(reply([])); });
    await act(async () => { await vi.advanceTimersByTimeAsync(35000); });
    expect(calls.filter((c) => c.url === "/api/jobs").length).toBeGreaterThan(1);
  });
});
