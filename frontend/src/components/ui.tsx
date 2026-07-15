import { createContext, useCallback, useContext, useState, ReactNode } from "react";

/* ---- Switch (rounded pill toggle) ---- */
export function Switch({ on, onChange, size = "md" }: { on: boolean; onChange: () => void; size?: "sm" | "md" }) {
  const w = size === "sm" ? 40 : 46, h = size === "sm" ? 22 : 26, k = h - 6;
  return (
    <button type="button" onClick={onChange} aria-pressed={on}
      style={{ width: w, height: h, borderRadius: 999, position: "relative", flex: "0 0 auto",
        background: on ? "linear-gradient(90deg,var(--accent),var(--accent2))" : "rgba(120,140,150,.3)",
        transition: ".18s", cursor: "pointer", border: 0 }}>
      <span style={{ position: "absolute", top: 3, left: on ? w - k - 3 : 3, width: k, height: k, borderRadius: "50%",
        background: "#fff", transition: ".18s", boxShadow: "0 2px 6px rgba(0,0,0,.3)" }} />
    </button>
  );
}

/* ---- Card ---- */
export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`glass p-4 md:p-[18px] ${className}`}>{children}</div>;
}

/* ---- Toast ---- */
type Toast = { id: number; msg: string; bad?: boolean };
const ToastCtx = createContext<(msg: string, bad?: boolean) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((msg: string, bad?: boolean) => {
    const id = Date.now() + Math.random();
    setItems((s) => [...s, { id, msg, bad }]);
    setTimeout(() => setItems((s) => s.filter((t) => t.id !== id)), 3500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div style={{ position: "fixed", right: 16, bottom: 16, zIndex: 100, display: "flex", flexDirection: "column", gap: 8 }}>
        {items.map((t) => (
          <div key={t.id} className="glass" style={{ padding: "11px 14px", fontSize: 13,
            color: t.bad ? "#fca5a5" : "var(--txt)", maxWidth: 360 }}>{t.msg}</div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

/* ---- Status badge for meeting states ---- */
const BADGE: Record<string, { t: string; c: string; bg: string }> = {
  scheduled: { t: "Запланирована", c: "var(--muted)", bg: "rgba(120,140,150,.18)" },
  recording: { t: "Идёт запись", c: "#fca5a5", bg: "rgba(248,113,113,.16)" },
  uploading: { t: "Загрузка в облако", c: "#c4b5fd", bg: "rgba(139,92,246,.18)" },
  transcribing: { t: "Распознавание", c: "var(--accent)", bg: "rgba(45,212,191,.14)" },
  analyzing: { t: "Генерация протокола", c: "var(--accent)", bg: "rgba(45,212,191,.14)" },
  done: { t: "Готово", c: "#5eead4", bg: "rgba(52,211,153,.16)" },
  error: { t: "Ошибка", c: "#fca5a5", bg: "rgba(248,113,113,.16)" },
  missed: { t: "Пропущена", c: "#94a3b8", bg: "rgba(148,163,184,.16)" },
  skipped: { t: "Не записываем", c: "var(--muted)", bg: "rgba(120,140,150,.18)" },
  no_time: { t: "Без времени", c: "var(--warn)", bg: "rgba(251,191,36,.16)" },
};
export function StatusBadge({ state, dot = true }: { state: string; dot?: boolean }) {
  const b = BADGE[state] || BADGE.scheduled;
  return (
    <span className="chip" style={{ color: b.c, background: b.bg }}>
      {dot && <span style={{ width: 7, height: 7, borderRadius: "50%", background: b.c }} />} {b.t}
    </span>
  );
}
