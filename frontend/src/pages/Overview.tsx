import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { CalendarClock, CheckCircle2, Radio, Timer, Link2, Bot, Cloud, FileText, ArrowRight } from "lucide-react";
import { Page } from "../components/Layout";
import { Card, StatusBadge } from "../components/ui";
import { api } from "../lib/api";
import { Status, isToday, fmtDateTime } from "../lib/format";

const pipeline = [
  { icon: Link2, title: "Weeek", sub: "Источник встреч" },
  { icon: Bot, title: "Бот-рекордер", sub: "Запись Телемоста" },
  { icon: Cloud, title: "Облако", sub: "Выгрузка mp4" },
  { icon: FileText, title: "Протокол", sub: "Распознавание · Word" },
];

function Kpi({ icon: Icon, n, label }: { icon: any; n: React.ReactNode; label: string }) {
  return (
    <Card className="relative overflow-hidden">
      <div className="grid place-items-center rounded-xl absolute right-3 top-3"
        style={{ width: 32, height: 32, background: "rgba(45,212,191,.14)" }}><Icon size={16} color="var(--accent)" /></div>
      <div className="text-[26px] font-extrabold"><span style={{
        background: "linear-gradient(90deg,var(--accent),var(--accent2))", WebkitBackgroundClip: "text",
        backgroundClip: "text", color: "transparent" }}>{n}</span></div>
      <div className="text-[11.5px] mt-1 uppercase tracking-wide" style={{ color: "var(--muted)" }}>{label}</div>
    </Card>
  );
}

export default function Overview() {
  const [s, setS] = useState<Status | null>(null);
  const load = () => api.get("/api/automation/scheduler/status").then(setS).catch(() => {});
  useEffect(() => { load(); const t = setInterval(load, 4000); return () => clearInterval(t); }, []);

  const all = s?.meetings ?? [];
  const today = all.filter((m) => isToday(m.start));
  const done = all.filter((m) => m.state === "done").length;
  const live = all.find((m) => m.state === "recording");
  const recent = all.slice(0, 5);

  return (
    <Page title="Обзор" subtitle="Состояние автоматизации записи встреч в реальном времени" onRefresh={load}>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Kpi icon={CalendarClock} n={today.length} label="Встреч сегодня" />
        <Kpi icon={CheckCircle2} n={done} label="Готово сегодня" />
        <Kpi icon={Radio} n={`${s?.active ?? 0} / ${s?.max_parallel ?? 1}`} label="Идёт запись" />
        <Kpi icon={Timer} n={s?.running ? "24/7" : "стоп"} label="Поток" />
      </div>

      <div className="grid lg:grid-cols-[1.15fr_.85fr] gap-3.5 mt-3.5">
        <Card>
          <div className="flex items-center gap-2 mb-3">
            <div><div className="font-bold text-[14px]">Конвейер автоматизации</div>
              <div className="text-[12px]" style={{ color: "var(--muted)" }}>Встреча из Weeek проходит все этапы автоматически</div></div>
            <span className="chip ml-auto" style={{ color: "var(--accent-ink)",
              background: "linear-gradient(90deg,var(--accent),var(--accent2))" }}>{s?.running ? "работает" : "стоит"}</span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
            {pipeline.map((p) => (
              <div key={p.title} className="glass2 rounded-2xl p-3 text-center">
                <div className="grid place-items-center rounded-xl mx-auto mb-2"
                  style={{ width: 42, height: 42, background: "rgba(45,212,191,.13)" }}><p.icon size={19} color="var(--accent)" /></div>
                <div className="font-bold text-[13px]">{p.title}</div>
                <div className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>{p.sub}</div>
                <ArrowRight size={14} className="mx-auto mt-2" color="var(--muted)" />
              </div>
            ))}
          </div>
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
              <div className="mt-6">
                <div className="flex justify-between text-[12px] mb-1.5" style={{ color: "var(--muted)" }}>
                  <span>Записывается…</span></div>
                <div style={{ height: 8, borderRadius: 6, background: "rgba(120,140,150,.2)", overflow: "hidden" }}>
                  <div style={{ height: "100%", width: "60%", borderRadius: 6,
                    background: "linear-gradient(90deg,var(--accent),var(--accent2))" }} /></div>
              </div>
            </>
          ) : <div className="text-[13px] py-6" style={{ color: "var(--muted)" }}>Сейчас записей нет.</div>}
        </Card>
      </div>

      <Card className="mt-3.5">
        <div className="flex items-center justify-between mb-3">
          <div className="font-bold text-[14px]">Последние встречи</div>
          <Link to="/meetings" className="text-[13px] flex items-center gap-1">Все встречи <ArrowRight size={14} /></Link>
        </div>
        {recent.length ? recent.map((m) => (
          <div key={String(m.task_id) + m.start} className="glass2 rounded-2xl px-4 py-3 mb-2 flex items-center justify-between gap-3">
            <div><div className="font-semibold text-[13.5px]">{m.title}</div>
              <div className="text-[11.5px] mt-0.5" style={{ color: "var(--muted)" }}>{fmtDateTime(m.start)}{m.detail ? " · " + m.detail : ""}</div></div>
            <StatusBadge state={m.state} />
          </div>
        )) : <div className="text-[13px] py-2" style={{ color: "var(--muted)" }}>Встреч пока нет.</div>}
      </Card>
    </Page>
  );
}
