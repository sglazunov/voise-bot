import { useEffect, useState } from "react";
import { Video, RefreshCw, Square, Clock, Loader2, Link2, NotebookPen } from "lucide-react";
import { Page } from "../components/Layout";
import { Switch, StatusBadge, Modal, useToast } from "../components/ui";
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

  const [polling, setPolling] = useState(false);
  async function pollNow() {
    if (polling) return;
    setPolling(true);
    try { const r = await api.post("/api/automation/scheduler/poll-now"); toast(r.detail || "Обновлено из Weeek"); await load(); }
    catch (e: any) { toast(e.message, true); }
    finally { setPolling(false); }
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

  // Д6: participant's live notes attach to the meeting's recognition job and
  // become the protocol's most trusted source.
  const [notesFor, setNotesFor] = useState<Meeting | null>(null);
  const [notesText, setNotesText] = useState("");
  const [notesSaving, setNotesSaving] = useState(false);
  async function openNotes(m: Meeting) {
    setNotesFor(m); setNotesText("");
    try {
      const j = await api.get(`/api/jobs/${m.job_id}`);
      setNotesText(j.user_notes || "");
    } catch { /* job may still be spinning up */ }
  }
  async function saveNotes(regen: boolean) {
    if (!notesFor?.job_id || notesSaving) return;
    setNotesSaving(true);
    try {
      await api.post(`/api/jobs/${notesFor.job_id}/notes`, { notes: notesText });
      if (regen) { await api.post(`/api/jobs/${notesFor.job_id}/reanalyze`, {}); toast("Заметки сохранены, протокол пересобирается"); }
      else toast("Заметки сохранены — учтутся при сборке протокола");
      setNotesFor(null);
    } catch (e: any) { toast(e.message, true); }
    finally { setNotesSaving(false); }
  }

  // Manual attach: when automation couldn't deliver (LLM was down, cloud
  // refused the video…), the user pastes the links and we write them into the
  // task's Weeek fields ourselves.
  const [linksFor, setLinksFor] = useState<Meeting | null>(null);
  const [videoUrl, setVideoUrl] = useState("");
  const [protoUrl, setProtoUrl] = useState("");
  const [sending, setSending] = useState(false);
  function openLinks(m: Meeting) {
    setLinksFor(m); setVideoUrl(m.cloud_url || ""); setProtoUrl("");
  }
  async function sendLinks() {
    if (!linksFor || sending) return;
    setSending(true);
    try {
      const r = await api.post("/api/automation/meetings/links", {
        task_id: String(linksFor.task_id), video_url: videoUrl.trim(), protocol_url: protoUrl.trim() });
      toast(r.detail || "Ссылки прикреплены"); setLinksFor(null);
    } catch (e: any) { toast(e.message, true); }
    finally { setSending(false); }
  }

  const all = s?.meetings ?? [];
  const list = all.filter((m) =>
    filter === "today" ? isToday(m.start) : filter === "work" ? WORK.includes(m.state) :
    filter === "done" ? m.state === "done" : true);

  return (
    <Page title="Встречи" subtitle="Ближайшие и прошедшие встречи с их статусами записи" onRefresh={load}
      actions={<button className="btn btn-ghost" onClick={pollNow} disabled={polling}>
        {polling ? <Loader2 size={15} className="animate-spin" /> : <RefreshCw size={15} />}
        {polling ? "Обновляю…" : "Обновить из Weeek"}</button>}>
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
            {/* flex-wrap + w-full on the action cluster: on a phone the buttons drop
                to their own row instead of squeezing the title down to nothing. */}
            <div className="flex items-center gap-3 md:gap-3.5 flex-wrap">
              <div className="grid place-items-center rounded-xl flex-none"
                style={{ width: 44, height: 44, background: rec ? "rgba(248,113,113,.16)" : "rgba(45,212,191,.13)" }}>
                <Video size={20} color={rec ? "#fca5a5" : "var(--accent)"} />
              </div>
              <div className="min-w-0 flex-1">
                <div className="font-bold text-[14px] truncate">{m.title}</div>
                <div className="flex items-center gap-2 text-[11.5px] mt-1 flex-wrap" style={{ color: "var(--muted)" }}>
                  <span className="flex items-center gap-1"><Clock size={12} /> {fmtDateTime(m.start)}</span>
                  {/* the detail line runs long ("…распознавание+протокол — job 6ca…"),
                      so a pill shape only looks right when it stays short */}
                  {m.detail && <span className="glass2 rounded-lg px-2 py-0.5 line-clamp-2 min-w-0">{m.detail}</span>}
                </div>
              </div>
              <div className="flex-none flex items-center gap-2">
                <StatusBadge state={m.state} />
                {m.job_id && (
                  <button className="btn-ghost grid place-items-center flex-none"
                    style={{ width: 30, height: 30, borderRadius: 9 }}
                    title="Заметки со встречи — станут скелетом протокола"
                    onClick={() => openNotes(m)}><NotebookPen size={14} /></button>
                )}
                {!rec && (
                  <button className="btn-ghost grid place-items-center flex-none"
                    style={{ width: 30, height: 30, borderRadius: 9 }}
                    title="Прикрепить ссылки на видео/протокол к задаче Weeek"
                    onClick={() => openLinks(m)}><Link2 size={14} /></button>
                )}
              </div>
              {rec ? (
                <div className="w-full lg:w-auto flex justify-end">
                  <button className="btn btn-danger" onClick={() => stopOne(m)}><Square size={13} /> Стоп</button>
                </div>
              ) : m.state === "scheduled" ? (
                <div className="w-full lg:w-auto flex items-center justify-end gap-2 text-[12.5px]" style={{ color: "var(--muted)" }}>
                  <button className="btn btn-ghost" onClick={() => runNow(m)}>Сейчас</button>
                  <span>Пишем</span><Switch size="sm" on={willRecord} onChange={() => setDecision(m, !willRecord)} />
                </div>
              ) : JOINABLE.includes(m.state) ? (
                <div className="w-full lg:w-auto flex justify-end">
                  <button className="btn btn-primary" onClick={() => runNow(m)}
                    title="Запустить бота на эту встречу вручную">
                    <Video size={14} /> Подключиться</button>
                </div>
              ) : null}
            </div>
            {rec && (
              <div className="mt-3" style={{ height: 6, borderRadius: 6, background: "rgba(120,140,150,.2)", overflow: "hidden" }}>
                <div style={{ height: "100%", width: "62%", borderRadius: 6, background: "linear-gradient(90deg,var(--accent),var(--accent2))" }} /></div>
            )}
          </div>
        );
      }) : <div className="glass p-8 text-center text-[13px]" style={{ color: "var(--muted)" }}>Встреч нет.</div>}

      <Modal open={!!notesFor} onClose={() => setNotesFor(null)}
        title={<span className="flex items-center gap-2"><NotebookPen size={16} color="var(--accent)" /> Заметки со встречи</span>}>
        <div className="text-[12.5px] mb-3" style={{ color: "var(--muted)" }}>
          {notesFor?.title || "Встреча"} · заметки участника — самый достоверный
          источник: протокол строится на них как на скелете
        </div>
        <textarea className="field" rows={6} value={notesText}
          onChange={(e) => setNotesText(e.target.value)}
          placeholder="что решили, кто что взял, ключевые цифры…" />
        <div className="flex gap-2 justify-end mt-3 flex-wrap">
          <button className="btn btn-ghost" onClick={() => setNotesFor(null)}>Отмена</button>
          <button className="btn btn-ghost" disabled={notesSaving} onClick={() => saveNotes(false)}>Сохранить</button>
          <button className="btn btn-primary" disabled={notesSaving} onClick={() => saveNotes(true)}>
            {notesSaving ? <Loader2 size={14} className="animate-spin" /> : <NotebookPen size={14} />}
            Сохранить и пересобрать</button>
        </div>
      </Modal>

      <Modal open={!!linksFor} onClose={() => setLinksFor(null)}
        title={<span className="flex items-center gap-2"><Link2 size={16} color="var(--accent)" /> Ссылки для задачи Weeek</span>}>
        <div className="text-[12.5px] mb-3" style={{ color: "var(--muted)" }}>
          {linksFor?.title || "Встреча"} · попадут в поля
          «Видео встречи» / «Протокол встречи» (или комментарием)
        </div>
        <label className="lbl">Ссылка на видео</label>
        <input className="field mb-3" placeholder="https://…" value={videoUrl}
          onChange={(e) => setVideoUrl(e.target.value)} />
        <label className="lbl">Ссылка на протокол</label>
        <input className="field mb-4" placeholder="https://…" value={protoUrl}
          onChange={(e) => setProtoUrl(e.target.value)} />
        <div className="flex gap-2 justify-end">
          <button className="btn btn-ghost" onClick={() => setLinksFor(null)}>Отмена</button>
          <button className="btn btn-primary" onClick={sendLinks}
            disabled={sending || !(videoUrl.trim() || protoUrl.trim())}>
            {sending ? <Loader2 size={14} className="animate-spin" /> : <Link2 size={14} />}
            Прикрепить
          </button>
        </div>
      </Modal>
    </Page>
  );
}
