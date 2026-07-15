import { useEffect, useState } from "react";
import { Video, RefreshCw, Square, Clock, Tag } from "lucide-react";
import { Page } from "../components/Layout";
import { Switch, StatusBadge, useToast } from "../components/ui";
import { api } from "../lib/api";
import { Status, Meeting, isToday, fmtDateTime } from "../lib/format";

const FILTERS = [
  { id: "all", label: "Все" }, { id: "today", label: "Сегодня" },
  { id: "work", label: "В работе" }, { id: "done", label: "Готовые" },
];
const WORK = ["recording", "uploading", "transcribing", "analyzing"];
// States where the bot isn't running but the user can still launch it manually.
const JOINABLE = ["missed", "skipped", "no_time", "error"];

export default function Meetings() {
  const [s, setS] = useState<Status | null>(null);
  const [filter, setFilter] = useState("today");
  const toast = useToast();
  const load = () => api.get("/api/automation/scheduler/status").then(setS).catch(() => {});
  useEffect(() => { load(); const t = setInterval(load, 4000); return () => clearInterval(t); }, []);

  async function pollNow() {
    try { const r = await api.post("/api/automation/scheduler/poll-now"); toast(r.detail || "Обновлено из Weeek"); load(); }
    catch (e: any) { toast(e.message, true); }
  }
  async function setDecision(m: Meeting, record: boolean) {
    try { await api.post(`/api/automation/meetings/${encodeURIComponent(String(m.task_id))}/decision`, { record }); load(); }
    catch (e: any) { toast(e.message, true); }
  }
  async function runNow(m: Meeting) {
    try { await api.post(`/api/automation/scheduler/run-now?task_id=${encodeURIComponent(String(m.task_id))}`); toast("Запись запущена"); load(); }
    catch (e: any) { toast(e.message, true); }
  }
  async function stopOne(m: Meeting) {
    try { const r = await api.post(`/api/automation/scheduler/stop-recording?task_id=${encodeURIComponent(String(m.task_id))}`); toast(r.detail || "Останавливаю…"); load(); }
    catch (e: any) { toast(e.message, true); }
  }

  const all = s?.meetings ?? [];
  const list = all.filter((m) =>
    filter === "today" ? isToday(m.start) : filter === "work" ? WORK.includes(m.state) :
    filter === "done" ? m.state === "done" : true);

  return (
    <Page title="Встречи" subtitle="Ближайшие и прошедшие встречи с их статусами записи" onRefresh={load}
      actions={<button className="btn btn-ghost" onClick={pollNow}><RefreshCw size={15} /> Обновить из Weeek</button>}>
      <div className="glass p-2.5 flex items-center gap-2 mb-3.5 flex-wrap">
        {FILTERS.map((f) => (
          <button key={f.id} onClick={() => setFilter(f.id)}
            className="rounded-full px-4 py-1.5 text-[13px] font-semibold transition"
            style={filter === f.id
              ? { background: "linear-gradient(90deg,var(--accent),var(--accent2))", color: "var(--accent-ink)" }
              : { background: "rgba(120,180,190,.08)", color: "var(--muted)" }}>{f.label}</button>
        ))}
      </div>

      {list.length ? list.map((m) => {
        const rec = m.state === "recording";
        const willRecord = m.record_flag !== false;
        return (
          <div key={String(m.task_id) + m.start} className="glass p-4 mb-2.5">
            <div className="flex items-center gap-3.5">
              <div className="grid place-items-center rounded-xl flex-none"
                style={{ width: 44, height: 44, background: rec ? "rgba(248,113,113,.16)" : "rgba(45,212,191,.13)" }}>
                <Video size={20} color={rec ? "#fca5a5" : "var(--accent)"} />
              </div>
              <div className="min-w-0 flex-1">
                <div className="font-bold text-[14px] truncate">{m.title}</div>
                <div className="flex items-center gap-2 text-[11.5px] mt-1 flex-wrap" style={{ color: "var(--muted)" }}>
                  <span className="flex items-center gap-1"><Clock size={12} /> {fmtDateTime(m.start)}</span>
                  {m.detail && <span className="glass2 rounded-full px-2 py-0.5">{m.detail}</span>}
                </div>
              </div>
              <div className="flex items-center gap-2.5 flex-none">
                <StatusBadge state={m.state} />
                {rec ? (
                  <button className="btn btn-danger" onClick={() => stopOne(m)}><Square size={13} /> Стоп</button>
                ) : m.state === "scheduled" ? (
                  <div className="flex items-center gap-2 text-[12.5px]" style={{ color: "var(--muted)" }}>
                    <button className="btn btn-ghost" onClick={() => runNow(m)}>Сейчас</button>
                    <span>Пишем</span><Switch size="sm" on={willRecord} onChange={() => setDecision(m, !willRecord)} />
                  </div>
                ) : JOINABLE.includes(m.state) ? (
                  <button className="btn btn-primary" onClick={() => runNow(m)}
                    title="Запустить бота на эту встречу вручную">
                    <Video size={14} /> Подключиться</button>
                ) : null}
              </div>
            </div>
            {rec && (
              <div className="mt-3" style={{ height: 6, borderRadius: 6, background: "rgba(120,140,150,.2)", overflow: "hidden" }}>
                <div style={{ height: "100%", width: "62%", borderRadius: 6, background: "linear-gradient(90deg,var(--accent),var(--accent2))" }} /></div>
            )}
          </div>
        );
      }) : <div className="glass p-8 text-center text-[13px]" style={{ color: "var(--muted)" }}>Встреч нет.</div>}
    </Page>
  );
}
