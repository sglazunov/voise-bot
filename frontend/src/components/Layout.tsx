import { ReactNode, createContext, useContext, useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  LayoutGrid, CalendarClock, Link2, Cloud, Bot, ListChecks, Radio,
  Mic, Brain, BrainCircuit, User, Sun, Moon, RefreshCw, Power, LogOut,
  Menu, X, MoreHorizontal,
} from "lucide-react";
import { useTheme } from "../lib/theme";
import { api, logout } from "../lib/api";
import { confirmLeave } from "../lib/unsaved";
import { Switch } from "./ui";

const nav = [
  { to: "/", icon: LayoutGrid, label: "Обзор", end: true },
  { to: "/meetings", icon: CalendarClock, label: "Встречи" },
  { to: "/weeek", icon: Link2, label: "Weeek" },
  { to: "/cloud", icon: Cloud, label: "Облако" },
  { to: "/recorder", icon: Bot, label: "Бот-рекордер" },
  { to: "/scheduler", icon: ListChecks, label: "Планировщик" },
];
const tools = [
  { to: "/recognition", icon: Mic, label: "Распознавание" },
  // Страница «Нейросети» временно убрана из меню: подключение движков
  // перестраивается. Маршрут остаётся рабочим по прямой ссылке /providers,
  // чтобы ключи можно было править, пока идёт перестройка.
  // { to: "/providers", icon: BrainCircuit, label: "Нейросети" },
  { to: "/context", icon: Brain, label: "Контекст для ИИ" },
  { to: "/profile", icon: User, label: "Профиль" },
];
/* Bottom bar on phones — the 4 most-used screens + «Ещё» for the rest.
   Labels are deliberately short so they never wrap on a narrow screen. */
const tabs = [
  { to: "/", icon: LayoutGrid, label: "Обзор", end: true },
  { to: "/meetings", icon: CalendarClock, label: "Встречи" },
  { to: "/recognition", icon: Mic, label: "Распозн." },
  // Было «Нейросети» — страница временно убрана, пока перестраивается
  // подключение движков. На её место встал планировщик: это следующий по
  // частоте экран.
  { to: "/scheduler", icon: ListChecks, label: "План" },
];

/* Automation is a global control shown in three places (desktop topbar, mobile
   header, drawer) — keep ONE source of truth instead of three fetches. */
const AutoCtx = createContext<{ enabled: boolean | null; toggle: () => void }>({
  enabled: null, toggle: () => {},
});

function NavItem({ to, icon: Icon, label, end, onNavigate }: any) {
  return (
    <NavLink to={to} end={end}
      // Don't silently drop unsaved edits (e.g. the AI-context page).
      onClick={(e) => { if (!confirmLeave()) e.preventDefault(); else onNavigate?.(); }}
      className={({ isActive }) =>
        "flex items-center gap-3 rounded-xl px-3 py-2.5 text-[13.5px] font-semibold transition " +
        (isActive ? "text-[color:var(--accent-ink)]" : "text-[color:var(--muted)] hover:text-[color:var(--txt)]")}
      style={({ isActive }: any) => (isActive
        ? { background: "linear-gradient(90deg,var(--accent),var(--accent2))", boxShadow: "0 10px 24px -12px var(--accent)" }
        : {})}>
      <Icon size={18} className="flex-none" /> {label}
    </NavLink>
  );
}

function Brand() {
  return (
    <div className="flex items-center gap-3">
      <span className="grid place-items-center rounded-xl flex-none"
        style={{ width: 40, height: 40, background: "linear-gradient(135deg,var(--accent),var(--accent2))",
          boxShadow: "0 10px 22px -8px var(--accent)" }}>
        <Radio size={20} color="#04211d" />
      </span>
      <div className="min-w-0">
        <div className="font-extrabold text-[16px] leading-tight">MeetFlowAI</div>
        <div className="text-[11px] truncate" style={{ color: "var(--muted)" }}>Автозапись встреч</div>
      </div>
    </div>
  );
}

function AutoBadge() {
  return (
    <div className="glass p-3 text-[12px]" style={{ color: "var(--muted)" }}>
      <div className="flex items-center gap-2 font-semibold" style={{ color: "var(--txt)" }}>
        <span className="flex-none" style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--ok)",
          boxShadow: "0 0 0 4px rgba(52,211,153,.18)" }} /> Автоматика активна
      </div>
      <div className="mt-1.5">Сервер опрашивает Weeek и записывает встречи 24/7.</div>
    </div>
  );
}

export function Layout() {
  const [drawer, setDrawer] = useState(false);
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const { theme, toggle: toggleTheme } = useTheme();
  const loc = useLocation();

  useEffect(() => {
    api.get("/api/automation/scheduler/status").then((s) => setEnabled(!!s.enabled)).catch(() => setEnabled(false));
  }, []);
  useEffect(() => { setDrawer(false); }, [loc.pathname]);   // navigating closes the drawer
  useEffect(() => {   // don't let the page scroll behind an open drawer
    document.body.style.overflow = drawer ? "hidden" : "";
    return () => { document.body.style.overflow = ""; };
  }, [drawer]);

  async function toggleAuto() {
    const next = !enabled; setEnabled(next);
    try { await api.post("/api/automation/settings", { enabled: next }); } catch { setEnabled(!next); }
  }

  return (
    <AutoCtx.Provider value={{ enabled, toggle: toggleAuto }}>
      {/* overflow-x:clip is the backstop: a stray wide element must never scroll the
          shell sideways, which would slide the sticky header off the right edge.
          `clip` (unlike `hidden`) makes no scroll container, so sticky still works. */}
      <div className="flex min-h-screen w-full" style={{ overflowX: "clip" }}>
        {/* ---- Desktop sidebar ---- */}
        <aside className="hidden lg:flex w-[248px] flex-none self-start sticky top-0 h-screen overflow-y-auto flex-col gap-1 p-3.5"
          style={{ background: "var(--side)", borderRight: "1px solid var(--line)",
            backdropFilter: "blur(14px)", WebkitBackdropFilter: "blur(14px)" }}>
          <div className="glass px-3 py-3 mb-1"><Brand /></div>
          <nav className="flex flex-col gap-1 mt-1">{nav.map((n) => <NavItem key={n.to} {...n} />)}</nav>
          <div className="text-[10.5px] font-bold uppercase tracking-wide mt-3 mb-1 px-2" style={{ color: "var(--muted)" }}>Инструменты</div>
          <nav className="flex flex-col gap-1">{tools.map((n) => <NavItem key={n.to} {...n} />)}</nav>
          <div className="mt-auto"><AutoBadge /></div>
        </aside>

        {/* ---- Mobile drawer ---- */}
        {drawer && (
          <div className="lg:hidden" style={{ position: "fixed", inset: 0, zIndex: 1500 }}>
            <div onClick={() => setDrawer(false)}
              style={{ position: "absolute", inset: 0, background: "rgba(4,12,16,.55)" }} />
            <div className="flex flex-col gap-1 p-3.5 overflow-y-auto"
              style={{ position: "absolute", insetBlock: 0, left: 0, width: "min(84vw,300px)",
                background: "var(--side)", borderRight: "1px solid var(--line)",
                backdropFilter: "blur(14px)", WebkitBackdropFilter: "blur(14px)" }}>
              <div className="glass px-3 py-3 mb-1 flex items-center justify-between gap-2">
                <Brand />
                <button className="btn-ghost grid place-items-center flex-none"
                  style={{ width: 34, height: 34, borderRadius: 10 }}
                  onClick={() => setDrawer(false)} aria-label="Закрыть меню"><X size={16} /></button>
              </div>
              <nav className="flex flex-col gap-1 mt-1">
                {nav.map((n) => <NavItem key={n.to} {...n} onNavigate={() => setDrawer(false)} />)}</nav>
              <div className="text-[10.5px] font-bold uppercase tracking-wide mt-3 mb-1 px-2" style={{ color: "var(--muted)" }}>Инструменты</div>
              <nav className="flex flex-col gap-1">
                {tools.map((n) => <NavItem key={n.to} {...n} onNavigate={() => setDrawer(false)} />)}</nav>

              {/* global controls live here on phones */}
              <div className="glass flex items-center justify-between gap-3 px-3.5 py-2.5 mt-3">
                <span className="flex items-center gap-2 text-[13px] font-bold">
                  <Power size={16} color={enabled ? "var(--accent)" : "var(--muted)"} /> Автоматика</span>
                <Switch on={!!enabled} onChange={toggleAuto} />
              </div>
              <button className="btn btn-ghost mt-2 justify-start" onClick={toggleTheme}>
                {theme === "light" ? <Moon size={16} /> : <Sun size={16} />}
                Тема: {theme === "light" ? "светлая" : "тёмная"}</button>
              <button className="btn btn-danger mt-2 justify-start" onClick={logout}>
                <LogOut size={16} /> Выйти</button>
              <div className="mt-3"><AutoBadge /></div>
            </div>
          </div>
        )}

        <main className="flex-1 min-w-0 pb-[76px] lg:pb-0">
          {/* ---- Mobile header ---- */}
          <div className="lg:hidden flex items-center justify-between gap-2 px-3 py-2.5 sticky top-0 z-40"
            style={{ background: "var(--side)", borderBottom: "1px solid var(--line)",
              backdropFilter: "blur(14px)", WebkitBackdropFilter: "blur(14px)" }}>
            <button className="btn-ghost grid place-items-center flex-none"
              style={{ width: 40, height: 40, borderRadius: 12 }}
              onClick={() => setDrawer(true)} aria-label="Меню"><Menu size={18} /></button>
            <span className="grid place-items-center rounded-xl flex-none"
              style={{ width: 36, height: 36, background: "linear-gradient(135deg,var(--accent),var(--accent2))" }}>
              <Radio size={18} color="#04211d" /></span>
            <div className="glass flex items-center gap-2 px-2.5 py-1.5 flex-none">
              <Power size={15} color={enabled ? "var(--accent)" : "var(--muted)"} />
              <Switch size="sm" on={!!enabled} onChange={toggleAuto} />
            </div>
          </div>

          <Outlet />
        </main>

        {/* ---- Mobile bottom tabs ---- */}
        <nav className="lg:hidden flex items-stretch"
          style={{ position: "fixed", bottom: 0, left: 0, right: 0, zIndex: 40,
            background: "var(--side)", borderTop: "1px solid var(--line)",
            backdropFilter: "blur(14px)", WebkitBackdropFilter: "blur(14px)",
            paddingBottom: "env(safe-area-inset-bottom)" }}>
          {tabs.map((t) => (
            <NavLink key={t.to} to={t.to} end={t.end}
              onClick={(e) => { if (!confirmLeave()) e.preventDefault(); }}
              className="flex-1 flex flex-col items-center justify-center gap-1 py-2 text-[10.5px] font-semibold"
              style={({ isActive }: any) => ({ color: isActive ? "var(--accent)" : "var(--muted)" })}>
              <t.icon size={19} />
              <span className="truncate max-w-full px-0.5">{t.label}</span>
            </NavLink>
          ))}
          <button onClick={() => setDrawer(true)}
            className="flex-1 flex flex-col items-center justify-center gap-1 py-2 text-[10.5px] font-semibold"
            style={{ color: "var(--muted)", background: "none", border: 0 }}>
            <MoreHorizontal size={19} />
            <span>Ещё</span>
          </button>
        </nav>
      </div>
    </AutoCtx.Provider>
  );
}

/* Per-page shell: topbar (title + controls) + content.
   Phones: global controls live in the header/drawer, so the topbar keeps only the
   title and the page's own actions (stretched to full width, refresh beside them). */
export function Page({ title, subtitle, actions, onRefresh, children }:
  { title: string; subtitle?: string; actions?: ReactNode; onRefresh?: () => void; children: ReactNode }) {
  const { theme, toggle } = useTheme();
  const { enabled, toggle: toggleAuto } = useContext(AutoCtx);

  const refreshBtn = onRefresh ? (
    <button className="btn-ghost grid place-items-center flex-none" style={{ width: 42, height: 42, borderRadius: 12 }}
      title="Обновить" onClick={onRefresh}><RefreshCw size={16} /></button>
  ) : null;

  return (
    <div className="px-4 md:px-6 lg:px-8 py-4 lg:py-6">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-[21px] lg:text-[26px] font-bold m-0 leading-tight">{title}</h1>
          {subtitle && <div className="text-[12.5px] lg:text-[13.5px] mt-1" style={{ color: "var(--muted)" }}>{subtitle}</div>}
        </div>
        {/* desktop-only control cluster */}
        <div className="hidden lg:flex items-center gap-2.5 flex-none">
          {actions}
          <button className="btn-ghost grid place-items-center" style={{ width: 42, height: 42, borderRadius: 12 }}
            title="Тема" onClick={toggle}>{theme === "light" ? <Moon size={17} /> : <Sun size={17} />}</button>
          {refreshBtn}
          <div className="glass flex items-center gap-3 px-3.5 py-2">
            <Power size={16} color={enabled ? "var(--accent)" : "var(--muted)"} />
            <b className="text-[13px]">Автоматика</b>
            <Switch on={!!enabled} onChange={toggleAuto} />
          </div>
          <button className="btn-ghost grid place-items-center" style={{ width: 42, height: 42, borderRadius: 12 }}
            title="Выйти" onClick={logout}><LogOut size={16} /></button>
        </div>
      </div>

      {/* mobile actions row — page actions stretch, refresh stays a square */}
      {(actions || refreshBtn) && (
        <div className="flex lg:hidden items-center gap-2 mt-3">
          <div className="flex-1 flex gap-2 [&>*]:flex-1 min-w-0">{actions}</div>
          {refreshBtn}
        </div>
      )}

      <div className="mt-4 lg:mt-6">{children}</div>
    </div>
  );
}
