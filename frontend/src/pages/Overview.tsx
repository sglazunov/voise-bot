import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  CalendarClock, Clock3, ListChecks, Timer, Users2, Gavel, FileText,
  Radio, ShieldCheck, ArrowRight, Info,
} from "lucide-react";
import { Page } from "../components/Layout";
import { Card, StatusBadge } from "../components/ui";
import { api } from "../lib/api";
import { Status, isToday, fmtDateTime } from "../lib/format";

const PERIODS = [{ d: 7, l: "7 дней" }, { d: 30, l: "30 дней" }, { d: 90, l: "90 дней" }];
const plural = (n: number, a: string, b: string, c: string) => {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return a;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return b;
  return c;
};

/* Hero stat tile — for a single headline number a figure IS the right form, not a chart. */
function Stat({ icon: Icon, n, label, hint }: { icon: any; n: React.ReactNode; label: string; hint?: string }) {
  return (
    <Card className="relative overflow-hidden">
      <div className="grid place-items-center rounded-xl absolute right-3 top-3"
        style={{ width: 32, height: 32, background: "rgba(45,212,191,.14)" }}><Icon size={16} color="var(--accent)" /></div>
      <div className="text-[26px] font-extrabold leading-tight"><span style={{
        background: "linear-gradient(90deg,var(--accent),var(--accent2))", WebkitBackgroundClip: "text",
        backgroundClip: "text", color: "transparent" }}>{n}</span></div>
      <div className="text-[11.5px] mt-1 uppercase tracking-wide" style={{ color: "var(--muted)" }}>{label}</div>
      {hint && <div className="text-[11px] mt-1.5 leading-snug" style={{ color: "var(--muted)", opacity: .85 }}>{hint}</div>}
    </Card>
  );
}

/* Secondary figure — no plot, just the number. */
function Mini({ icon: Icon, n, label }: { icon: any; n: React.ReactNode; label: string }) {
  return (
    <div className="glass2 rounded-2xl px-3.5 py-3 flex items-center gap-3">
      <Icon size={16} color="var(--accent)" className="flex-none" />
      <div className="min-w-0">
        <div className="font-bold text-[15px] leading-tight">{n}</div>
        <div className="text-[11px] truncate" style={{ color: "var(--muted)" }}>{label}</div>
      </div>
    </div>
  );
}

/* Change over time, ONE series -> bars. Single hue (the app accent; its contrast
   vs both surfaces is validated), so no legend is needed — the title names it.
   Marks: 4px rounded ends anchored to the baseline, 2px gap, recessive empties. */
function TrendBars({ data }: { data: { date: string; count: number }[] }) {
  const max = Math.max(1, ...data.map((d) => d.count));
  const day = (iso: string) => { const [, m, d] = iso.split("-"); return `${d}.${m}`; };
  return (
    <>
      <div className="flex items-end gap-[2px]" style={{ height: 116 }}>
        {data.map((d) => (
          <div key={d.date} className="flex-1 flex items-end" style={{ height: "100%" }}
            title={`${day(d.date)} — ${d.count} ${plural(d.count, "встреча", "встречи", "встреч")}`}>
            <div style={{
              width: "100%", height: `${(d.count / max) * 100}%`, minHeight: d.count ? 3 : 2,
              background: d.count ? "var(--accent)" : "rgba(120,140,150,.16)",
              borderRadius: "4px 4px 0 0",
            }} />
          </div>
        ))}
      </div>
      <div className="flex justify-between text-[11px] mt-1.5" style={{ color: "var(--muted)" }}>
        <span>{data.length ? day(data[0].date) : ""}</span>
        <span>максимум за день: {max}</span>
        <span>{data.length ? day(data[data.length - 1].date) : ""}</span>
      </div>
    </>
  );
}

export default function Overview() {
  const [s, setS] = useState<Status | null>(null);
  const [st, setSt] = useState<any>(null);
  const [days, setDays] = useState(30);

  const load = () => api.get("/api/automation/scheduler/status").then(setS).catch(() => {});
  const loadStats = (d: number) => api.get(`/api/stats?days=${d}`).then(setSt).catch(() => {});
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, []);
  useEffect(() => { loadStats(days); }, [days]);

  const all = s?.meetings ?? [];
  const today = all.filter((m) => isToday(m.start));
  const live = all.find((m) => m.state === "recording");
  const recent = all.slice(0, 5);
  const topMax = Math.max(1, ...((st?.top || []).map((t: any) => t.hours)));

  return (
    <Page title="Обзор" subtitle="Что автоматизация дала бизнесу и что происходит прямо сейчас"
      onRefresh={() => { load(); loadStats(days); }}>

      {/* Filters live in one row above the metrics */}
      <div className="glass p-2.5 flex items-center gap-2 mb-3.5 flex-wrap">
        <span className="text-[12.5px] mr-1" style={{ color: "var(--muted)" }}>Период:</span>
        {PERIODS.map((p) => (
          <button key={p.d} onClick={() => setDays(p.d)}
            className="rounded-full px-4 py-1.5 text-[13px] font-semibold transition"
            style={days === p.d
              ? { background: "linear-gradient(90deg,var(--accent),var(--accent2))", color: "var(--accent-ink)" }
              : { background: "rgba(120,180,190,.08)", color: "var(--muted)" }}>{p.l}</button>
        ))}
      </div>

      {/* Primary business outcomes */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Stat icon={CalendarClock} n={st?.meetings ?? "—"} label="Встреч обработано" />
        <Stat icon={Clock3} n={st ? `${st.hours} ч` : "—"} label="Часов записано" />
        <Stat icon={ListChecks} n={st?.tasks ?? "—"} label="Поручений зафиксировано" />
        <Stat icon={Timer} n={st ? `≈ ${st.hours_saved} ч` : "—"} label="Сэкономлено на протоколах"
          hint={st ? `оценка: ручное протоколирование ≈${Math.round((st.saved_coeff || 0) * 100)}% длительности встречи` : undefined} />
      </div>

      {/* Trend + where the time actually goes */}
      <div className="grid lg:grid-cols-[1.15fr_.85fr] gap-3.5 mt-3.5">
        <Card>
          <div className="font-bold text-[14px]">Встреч по дням</div>
          <div className="text-[12px] mb-3" style={{ color: "var(--muted)" }}>Загрузка за последние {st?.days ?? days} дн.</div>
          {st?.by_day?.length ? <TrendBars data={st.by_day} />
            : <div className="text-[13px] py-8 text-center" style={{ color: "var(--muted)" }}>Пока нет данных.</div>}
        </Card>

        <Card>
          <div className="font-bold text-[14px]">Что съедает время</div>
          <div className="text-[12px] mb-3" style={{ color: "var(--muted)" }}>Регулярные встречи по суммарным часам</div>
          {st?.top?.length ? (
            <div className="space-y-2.5">
              {st.top.map((t: any) => (
                <div key={t.title}>
                  <div className="flex justify-between gap-2 text-[12.5px] mb-1">
                    <span className="truncate">{t.title}</span>
                    <span className="flex-none" style={{ color: "var(--muted)" }}>{t.hours.toFixed(1)} ч · {t.count}×</span>
                  </div>
                  <div style={{ height: 8, borderRadius: 6, background: "rgba(120,140,150,.16)", overflow: "hidden" }}>
                    <div style={{ width: `${(t.hours / topMax) * 100}%`, height: "100%",
                      background: "var(--accent)", borderRadius: 6 }} />
                  </div>
                </div>
              ))}
            </div>
          ) : <div className="text-[13px] py-6 text-center" style={{ color: "var(--muted)" }}>Пока нет данных.</div>}
        </Card>
      </div>

      {/* Secondary figures */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-2.5 mt-3.5">
        <Mini icon={Users2} n={st ? `${st.person_hours} ч` : "—"} label="Человеко-часов встреч" />
        <Mini icon={Gavel} n={st?.decisions ?? "—"} label="Решений зафиксировано" />
        <Mini icon={FileText} n={st?.protocols ?? "—"} label="Протоколов готово" />
        <Mini icon={Clock3} n={st ? `${st.avg_minutes} мин` : "—"} label="Средняя встреча" />
        <Mini icon={ShieldCheck} n={st ? `${st.reliability}%` : "—"}
          label={st?.failed ? `Успешно (сбоев: ${st.failed})` : "Успешных обработок"} />
      </div>

      {/* Operational: what's happening now */}
      <div className="grid lg:grid-cols-[1.15fr_.85fr] gap-3.5 mt-3.5">
        <Card>
          <div className="flex items-center justify-between mb-3">
            <div className="font-bold text-[14px]">Последние встречи</div>
            <Link to="/meetings" className="text-[13px] flex items-center gap-1">Все встречи <ArrowRight size={14} /></Link>
          </div>
          {recent.length ? recent.map((m) => (
            <div key={String(m.task_id) + m.start} className="glass2 rounded-2xl px-4 py-3 mb-2 flex items-center justify-between gap-3">
              <div className="min-w-0"><div className="font-semibold text-[13.5px] truncate">{m.title}</div>
                <div className="text-[11.5px] mt-0.5" style={{ color: "var(--muted)" }}>{fmtDateTime(m.start)}{m.detail ? " · " + m.detail : ""}</div></div>
              <StatusBadge state={m.state} />
            </div>
          )) : <div className="text-[13px] py-2" style={{ color: "var(--muted)" }}>Встреч пока нет.</div>}
        </Card>

        <Card>
          <div className="flex items-center justify-between mb-3">
            <div className="font-bold text-[14px]">Прямо сейчас</div>
            {live && <span className="chip" style={{ color: "#fca5a5" }}>
              <span style={{ width: 7, height: 7, borderRadius: "50%", background: "#f87171" }} /> REC</span>}
          </div>
          {live ? (
            <>
              <div className="font-bold text-[15px]">{live.title}</div>
              <div className="text-[12.5px] mt-1" style={{ color: "var(--muted)" }}>{live.detail || fmtDateTime(live.start)}</div>
            </>
          ) : <div className="text-[13px]" style={{ color: "var(--muted)" }}>Сейчас записей нет.</div>}
          <div className="glass2 rounded-2xl px-4 py-3 mt-3 flex items-center gap-3">
            <Radio size={16} color={s?.running ? "var(--accent)" : "var(--muted)"} className="flex-none" />
            <div className="text-[12.5px]">
              <b>{s?.running ? "Автоматика работает" : "Автоматика остановлена"}</b>
              <div style={{ color: "var(--muted)" }}>
                Сегодня встреч: {today.length} · записей: {s?.active ?? 0}/{s?.max_parallel ?? 1}</div>
            </div>
          </div>
        </Card>
      </div>

      <div className="flex items-start gap-2 mt-3.5 text-[11.5px] px-1" style={{ color: "var(--muted)" }}>
        <Info size={13} className="flex-none mt-0.5" />
        <span>Все цифры, кроме «сэкономлено», измерены по фактически обработанным встречам.
          Экономия — оценка по указанному коэффициенту, а не замер.</span>
      </div>
    </Page>
  );
}
