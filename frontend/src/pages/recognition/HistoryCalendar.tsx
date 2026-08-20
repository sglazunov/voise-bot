// Календарь истории встреч. Вынесен из Recognition.tsx: страница разрослась до
// тысячи строк, и календарь с его названиями месяцев к самому распознаванию
// отношения не имеет.
import { useState } from "react";
import { ChevronLeft, ChevronRight, CalendarDays } from "lucide-react";

export const MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
  "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
export const dayKey = (ms: number) => {
  const d = new Date(ms);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
};
/** «2026-6-29» → «29 июля» — чтобы на кнопке было видно, что фильтр включён. */
export const MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
  "июля", "августа", "сентября", "октября", "ноября", "декабря"];
export const fmtDayLabel = (key: string) => {
  const [, m, d] = key.split("-").map(Number);
  return `${d} ${MONTHS_GEN[m] ?? ""}`.trim();
};

/** Календарь истории: месяц, год и день. Дни без встреч не кликаются. */

export function HistoryCalendar({ jobs, value, onPick }:
  { jobs: any[]; value: string | null; onPick: (key: string | null) => void }) {
  const first = value ? value.split("-").map(Number) : null;
  const [view, setView] = useState(() =>
    first ? new Date(first[0], first[1], 1) : new Date());

  // В какие дни вообще были встречи — по ним и подсвечиваем календарь.
  const have = new Set(jobs.map((j) => dayKey((j.created_at || 0) * 1000)));
  const y = view.getFullYear(), m = view.getMonth();
  const daysInMonth = new Date(y, m + 1, 0).getDate();
  // Понедельник — первый день недели (getDay(): воскресенье = 0).
  const shift = (new Date(y, m, 1).getDay() + 6) % 7;

  return (
    <div className="glass2 rounded-2xl p-3 mb-2.5">
      <div className="flex items-center justify-between mb-2">
        <button className="btn-ghost grid place-items-center" style={{ width: 28, height: 28, borderRadius: 8 }}
          onClick={() => setView(new Date(y, m - 1, 1))}><ChevronLeft size={14} /></button>
        <div className="text-[13px] font-semibold">{MONTHS[m]} {y}</div>
        <button className="btn-ghost grid place-items-center" style={{ width: 28, height: 28, borderRadius: 8 }}
          onClick={() => setView(new Date(y, m + 1, 1))}><ChevronRight size={14} /></button>
      </div>
      <div className="grid grid-cols-7 gap-1 text-center text-[10px] mb-1" style={{ color: "var(--muted)" }}>
        {["пн", "вт", "ср", "чт", "пт", "сб", "вс"].map((d) => <div key={d}>{d}</div>)}
      </div>
      <div className="grid grid-cols-7 gap-1">
        {Array.from({ length: shift }).map((_, i) => <div key={`e${i}`} />)}
        {Array.from({ length: daysInMonth }).map((_, i) => {
          const day = i + 1;
          const key = `${y}-${m}-${day}`;
          const has = have.has(key);
          const active = value === key;
          return (
            <button key={key} disabled={!has}
              onClick={() => onPick(active ? null : key)}
              className="text-[12px] rounded-lg py-1 transition"
              style={{
                background: active ? "var(--accent)" : has ? "rgba(45,212,191,.13)" : "transparent",
                color: active ? "#04212f" : has ? "var(--text)" : "var(--muted)",
                fontWeight: has ? 600 : 400,
                opacity: has ? 1 : 0.35,
                cursor: has ? "pointer" : "default",
              }}>{day}</button>
          );
        })}
      </div>
      {value && (
        <button className="btn-ghost w-full mt-2 text-[12px]" onClick={() => onPick(null)}>
          Показать все встречи</button>
      )}
    </div>
  );
}

