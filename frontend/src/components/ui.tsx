import { createContext, useCallback, useContext, useEffect, useRef, useState, ReactNode } from "react";
import { ChevronDown, Check } from "lucide-react";

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

/* ---- Select (themed dropdown; replaces native <select> app-wide) ----
   Always opens downward, matches the trigger width, and is styled like the rest
   of the UI (rounded, glass, teal accent). Supports flat options and groups. */
export type SelOpt = { value: string; label: string };
export type SelGroup = { label: string; options: SelOpt[] };
type SelItem = SelOpt | SelGroup;
const isGroup = (i: SelItem): i is SelGroup => Array.isArray((i as SelGroup).options);

export function Select({ value, onChange, options, placeholder = "—", className = "" }:
  { value: string; onChange: (v: string) => void; options: SelItem[]; placeholder?: string; className?: string }) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);

  const flat: SelOpt[] = [];
  options.forEach((i) => (isGroup(i) ? flat.push(...i.options) : flat.push(i)));
  const current = flat.find((o) => o.value === value);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);

  const Row = (o: SelOpt) => {
    const sel = o.value === value;
    return (
      <button key={o.value} type="button" onClick={() => { onChange(o.value); setOpen(false); }}
        className="w-full text-left px-2.5 py-2 text-[13.5px] rounded-lg transition flex items-center gap-2"
        style={{ background: sel ? "rgba(45,212,191,.16)" : "transparent", color: "var(--txt)" }}
        onMouseEnter={(e) => { if (!sel) e.currentTarget.style.background = "rgba(120,180,190,.10)"; }}
        onMouseLeave={(e) => { if (!sel) e.currentTarget.style.background = "transparent"; }}>
        <Check size={14} color="var(--accent)" style={{ flex: "0 0 auto", opacity: sel ? 1 : 0 }} />
        <span className="truncate">{o.label}</span>
      </button>
    );
  };

  return (
    <div ref={wrap} className={`relative ${className}`}>
      <button type="button" onClick={() => setOpen((v) => !v)}
        className="field flex items-center justify-between gap-2 text-left"
        style={{ cursor: "pointer", borderColor: open ? "var(--accent)" : undefined }}>
        <span className="truncate" style={current ? {} : { color: "var(--muted)" }}>
          {current ? current.label : placeholder}</span>
        <ChevronDown size={16} color="var(--muted)"
          style={{ flex: "0 0 auto", transition: ".18s", transform: open ? "rotate(180deg)" : "none" }} />
      </button>
      {open && (
        <div className="glass absolute top-full left-0 right-0 mt-1.5 p-1.5 z-50"
          style={{ maxHeight: 288, overflowY: "auto", borderRadius: 14 }}>
          {options.map((i, idx) => isGroup(i) ? (
            <div key={idx}>
              <div className="px-2.5 pt-2 pb-1 text-[11px] font-bold uppercase tracking-wide" style={{ color: "var(--muted)" }}>{i.label}</div>
              {i.options.map(Row)}
            </div>
          ) : Row(i))}
        </div>
      )}
    </div>
  );
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
