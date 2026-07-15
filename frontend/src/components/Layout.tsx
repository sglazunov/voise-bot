import { ReactNode, useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import {
  LayoutGrid, CalendarClock, Link2, Cloud, Bot, ListChecks, Radio,
  Mic, Brain, BrainCircuit, User, Sun, Moon, RefreshCw, Power, LogOut,
} from "lucide-react";
import { useTheme } from "../lib/theme";
import { api, logout } from "../lib/api";
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
  { to: "/providers", icon: BrainCircuit, label: "Нейросети" },
  { to: "/context", icon: Brain, label: "Контекст для ИИ" },
  { to: "/profile", icon: User, label: "Профиль" },
];

function NavItem({ to, icon: Icon, label, end }: any) {
  return (
    <NavLink to={to} end={end}
      className={({ isActive }) =>
        "flex items-center gap-3 rounded-xl px-3 py-2.5 text-[13.5px] font-semibold transition " +
        (isActive ? "text-[color:var(--accent-ink)]" : "text-[color:var(--muted)] hover:text-[color:var(--txt)]")}
      style={({ isActive }: any) => (isActive
        ? { background: "linear-gradient(90deg,var(--accent),var(--accent2))", boxShadow: "0 10px 24px -12px var(--accent)" }
        : {})}>
      <Icon size={18} /> {label}
    </NavLink>
  );
}

export function Layout() {
  return (
    <div className="flex min-h-screen">
      <aside className="w-[248px] flex-none self-start sticky top-0 h-screen overflow-y-auto flex flex-col gap-1 p-3.5"
        style={{ background: "var(--side)", borderRight: "1px solid var(--line)",
          backdropFilter: "blur(14px)", WebkitBackdropFilter: "blur(14px)" }}>
        <div className="glass flex items-center gap-3 px-3 py-3 mb-1">
          <span className="grid place-items-center rounded-xl"
            style={{ width: 40, height: 40, background: "linear-gradient(135deg,var(--accent),var(--accent2))",
              boxShadow: "0 10px 22px -8px var(--accent)" }}>
            <Radio size={20} color="#04211d" />
          </span>
          <div>
            <div className="font-extrabold text-[16px] leading-tight">MeetFlowAI</div>
            <div className="text-[11px]" style={{ color: "var(--muted)" }}>Автозапись встреч</div>
          </div>
        </div>
        <nav className="flex flex-col gap-1 mt-1">{nav.map((n) => <NavItem key={n.to} {...n} />)}</nav>
        <div className="text-[10.5px] font-bold uppercase tracking-wide mt-3 mb-1 px-2" style={{ color: "var(--muted)" }}>Инструменты</div>
        <nav className="flex flex-col gap-1">{tools.map((n) => <NavItem key={n.to} {...n} />)}</nav>
        <div className="mt-auto glass p-3 text-[12px]" style={{ color: "var(--muted)" }}>
          <div className="flex items-center gap-2 font-semibold" style={{ color: "var(--txt)" }}>
            <span style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--ok)",
              boxShadow: "0 0 0 4px rgba(52,211,153,.18)" }} /> Автоматика активна
          </div>
          <div className="mt-1.5">Сервер опрашивает Weeek и записывает встречи 24/7.</div>
        </div>
      </aside>
      <main className="flex-1 min-w-0"><Outlet /></main>
    </div>
  );
}

/* Per-page shell: topbar (title + global controls) + content. */
export function Page({ title, subtitle, actions, onRefresh, children }:
  { title: string; subtitle?: string; actions?: ReactNode; onRefresh?: () => void; children: ReactNode }) {
  const { theme, toggle } = useTheme();
  const [enabled, setEnabled] = useState<boolean | null>(null);

  useEffect(() => {
    api.get("/api/automation/scheduler/status").then((s) => setEnabled(!!s.enabled)).catch(() => setEnabled(false));
  }, []);

  async function toggleAuto() {
    const next = !enabled; setEnabled(next);
    try { await api.post("/api/automation/settings", { enabled: next }); } catch { setEnabled(!next); }
  }

  return (
    <div className="px-6 md:px-8 py-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-[26px] font-bold m-0">{title}</h1>
          {subtitle && <div className="text-[13.5px] mt-1" style={{ color: "var(--muted)" }}>{subtitle}</div>}
        </div>
        <div className="flex items-center gap-2.5">
          {actions}
          <button className="btn-ghost grid place-items-center" style={{ width: 42, height: 42, borderRadius: 12 }}
            title="Тема" onClick={toggle}>{theme === "light" ? <Moon size={17} /> : <Sun size={17} />}</button>
          {onRefresh && (
            <button className="btn-ghost grid place-items-center" style={{ width: 42, height: 42, borderRadius: 12 }}
              title="Обновить" onClick={onRefresh}><RefreshCw size={16} /></button>
          )}
          <div className="glass flex items-center gap-3 px-3.5 py-2">
            <Power size={16} color={enabled ? "var(--accent)" : "var(--muted)"} />
            <b className="text-[13px]">Автоматика</b>
            <Switch on={!!enabled} onChange={toggleAuto} />
          </div>
          <button className="btn-ghost grid place-items-center" style={{ width: 42, height: 42, borderRadius: 12 }}
            title="Выйти" onClick={logout}><LogOut size={16} /></button>
        </div>
      </div>
      <div className="mt-6">{children}</div>
    </div>
  );
}
