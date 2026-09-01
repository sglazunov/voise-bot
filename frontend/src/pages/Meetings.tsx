import { useEffect, useState } from "react";
import { Video, RefreshCw, Square, Clock, Loader2, Link2, NotebookPen, Radio, ScrollText } from "lucide-react";
import { Page } from "../components/Layout";
import { Ellipsis, Switch, StatusBadge, Modal, useToast } from "../components/ui";
import { api } from "../lib/api";
import { Status, Meeting, isToday, fmtDateTime } from "../lib/format";

const FILTERS = [
  { id: "all", label: "Все" }, { id: "today", label: "Сегодня" },
  { id: "work", label: "В работе" }, { id: "done", label: "Готовые" },
];
const WORK = ["recording", "uploading", "transcribing", "analyzing"];
// States where the bot isn't running but the user can still launch it manually.
// Состояния, из которых бота ещё можно запустить руками. Для "error" это
// верно ТОЛЬКО пока записи нет: если запись уже лежит на диске, повторный заход
// открыл бы тот же файл на запись (имя детерминировано) и затёр её.
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

  // Д6+Д10: one modal per meeting — the LIVE transcript growing during the
  // recording on top, the participant's notes underneath. Notes live on the
  // meeting itself, so they work before the recognition job even exists.
  const [notesFor, setNotesFor] = useState<Meeting | null>(null);
  const [logFor, setLogFor] = useState<Meeting | null>(null);
  const [notesText, setNotesText] = useState("");
  const [notesSaving, setNotesSaving] = useState(false);
  const [liveText, setLiveText] = useState("");
  const [liveFinal, setLiveFinal] = useState(false);
  async function openNotes(m: Meeting) {
    setNotesFor(m); setNotesText(""); setLiveText(""); setLiveFinal(false);
    api.get(`/api/automation/meetings/${encodeURIComponent(String(m.task_id))}/notes`)
      .then((r) => setNotesText(r.notes || "")).catch(() => {});
  }
  // Poll the live transcript while the modal is open (every 6 s during recording).
  useEffect(() => {
    if (!notesFor) return;
    let stop = false;
    const tick = async () => {
      try {
        const r = await api.get(`/api/automation/meetings/${encodeURIComponent(String(notesFor.task_id))}/live`);
        if (!stop) { setLiveText(r.text || ""); setLiveFinal(!!r.final); }
      } catch { /* meeting may have no live text yet */ }
    };
    tick();
    const t = setInterval(tick, 6000);
    return () => { stop = true; clearInterval(t); };
  }, [notesFor?.task_id]);
  async function saveNotes(regen: boolean) {
    if (!notesFor || notesSaving) return;
    setNotesSaving(true);
    try {
      await api.post(`/api/automation/meetings/${encodeURIComponent(String(notesFor.task_id))}/notes`, { notes: notesText });
      if (regen && notesFor.job_id) {
        await api.post(`/api/jobs/${notesFor.job_id}/reanalyze`, {});
        toast("Заметки сохранены, протокол пересобирается");
      } else toast("Заметки сохранены — учтутся при сборке протокола");
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
    // Подставляем то, что приложение уже знает. Раньше поле протокола было
    // всегда пустым, и если записи в облаке тоже не оказалось, кнопка
    // «Прикрепить» оставалась заблокированной — со стороны это выглядело как
    // «нажал, и ничего не произошло».
    setLinksFor(m);
    setVideoUrl(m.cloud_url || "");
    setProtoUrl((m as any).protocol_url || "");
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
                <Ellipsis as="div" className="font-bold text-[14px]">{m.title}</Ellipsis>
                <div className="flex items-center gap-2 text-[11.5px] mt-1 flex-wrap" style={{ color: "var(--muted)" }}>
                  <span className="flex items-center gap-1"><Clock size={12} /> {fmtDateTime(m.start)}</span>
                  {/* the detail line runs long ("…распознавание+протокол — job 6ca…"),
                      so a pill shape only looks right when it stays short */}
                  {m.detail && <span className="glass2 rounded-lg px-2 py-0.5 line-clamp-2 min-w-0">{m.detail}</span>}
                </div>
              </div>
              <div className="flex-none flex items-center gap-2">
                <StatusBadge state={m.state} />
                {rec && (
                  <button className="btn btn-ghost !px-2.5 !py-1 text-[11.5px] flex-none"
                    style={{ color: "var(--accent)" }}
                    title="Живая расшифровка + заметки"
                    onClick={() => openNotes(m)}>
                    <Radio size={13} /> Live</button>
                )}
                <button className="btn-ghost grid place-items-center flex-none"
                  style={{ width: 30, height: 30, borderRadius: 9 }}
                  title="Live-расшифровка и заметки со встречи"
                  onClick={() => openNotes(m)}><NotebookPen size={14} /></button>
                {/* Лог бота. Раньше наружу шла только последняя строка, и любую
                    проблему записи (не сработало стоп-слово, не найдена кнопка
                    чата, писал пустую комнату) приходилось разбирать вслепую. */}
                {m.logs?.length ? (
                  <button className="btn-ghost grid place-items-center flex-none"
                    style={{ width: 30, height: 30, borderRadius: 9 }}
                    title="Лог бота: что он видел и делал на встрече"
                    onClick={() => setLogFor(logFor?.task_id === m.task_id ? null : m)}>
                    <ScrollText size={14} /></button>
                ) : null}
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
              ) : JOINABLE.includes(m.state) && !m.has_recording ? (
                <div className="w-full lg:w-auto flex justify-end">
                  <button className="btn btn-primary" onClick={() => runNow(m)}
                    title="Запустить бота на эту встречу вручную">
                    <Video size={14} /> Подключиться</button>
                </div>
              ) : m.state === "error" && m.has_recording ? (
                // Красная карточка при ЦЕЛОЙ записи означает, что упало
                // распознавание или сборка протокола. Предлагать здесь
                // «Подключиться» — значит звать перезаписать готовую встречу.
                <div className="w-full lg:w-auto flex justify-end text-[11.5px]"
                  style={{ color: "var(--muted)" }}>
                  Запись цела — нажмите «Пересобрать» на странице распознавания.
                </div>
              ) : null}
            </div>
            {rec && (
              <div className="mt-3" style={{ height: 6, borderRadius: 6, background: "rgba(120,140,150,.2)", overflow: "hidden" }}>
                <div style={{ height: "100%", width: "62%", borderRadius: 6, background: "linear-gradient(90deg,var(--accent),var(--accent2))" }} /></div>
            )}
            {logFor?.task_id === m.task_id && (
              <div className="mt-3">
                <div className="text-[11.5px] mb-1.5" style={{ color: "var(--muted)" }}>
                  Что бот видел и делал. Строки со словом «Чат» показывают, открыл
                  ли он панель чата и заметил ли стоп-слово.
                </div>
                <pre className="glass2 rounded-xl p-3 text-[11.5px] whitespace-pre-wrap"
                  style={{ maxHeight: 300, overflow: "auto" }}>{m.logs?.join("\n")}</pre>
              </div>
            )}
          </div>
        );
      }) : <div className="glass p-8 text-center text-[13px]" style={{ color: "var(--muted)" }}>Встреч нет.</div>}

      <Modal open={!!notesFor} onClose={() => setNotesFor(null)}
        title={<span className="flex items-center gap-2"><NotebookPen size={16} color="var(--accent)" /> {notesFor?.state === "recording" ? "Live-расшифровка и заметки" : "Заметки со встречи"}</span>}>
        <div className="text-[12.5px] mb-2" style={{ color: "var(--muted)" }}>
          {notesFor?.title || "Встреча"}
        </div>
        {liveText ? (
          <div className="glass2 rounded-xl p-2.5 mb-3">
            <div className="text-[10.5px] font-bold uppercase tracking-wide mb-1"
              style={{ color: liveFinal ? "var(--muted)" : "var(--accent)" }}>
              {liveFinal ? "Финальная расшифровка" :
                notesFor?.state === "recording" ? "🔴 Идёт встреча — текст пополняется" : "Live-текст записи"}
            </div>
            <pre className="text-[11.5px] whitespace-pre-wrap font-sans max-h-44 overflow-y-auto m-0"
              style={{ color: "var(--txt)" }}>{liveText}</pre>
          </div>
        ) : notesFor?.state === "recording" ? (
          <div className="glass2 rounded-xl p-2.5 mb-3 text-[11.5px]" style={{ color: "var(--muted)" }}>
            Расшифровка появится через несколько минут после начала записи…
          </div>
        ) : null}
        <label className="lbl">Ваши заметки (скелет протокола)</label>
        <textarea className="field" rows={5} value={notesText}
          onChange={(e) => setNotesText(e.target.value)}
          placeholder="что решили, кто что взял, ключевые цифры…" />
        <div className="flex gap-2 justify-end mt-3 flex-wrap">
          <button className="btn btn-ghost" onClick={() => setNotesFor(null)}>Закрыть</button>
          <button className="btn btn-ghost" disabled={notesSaving} onClick={() => saveNotes(false)}>Сохранить</button>
          {notesFor?.job_id && notesFor?.state === "done" && (
            <button className="btn btn-primary" disabled={notesSaving} onClick={() => saveNotes(true)}>
              {notesSaving ? <Loader2 size={14} className="animate-spin" /> : <NotebookPen size={14} />}
              Сохранить и пересобрать</button>
          )}
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
        {!(videoUrl.trim() || protoUrl.trim()) && (
          <div className="text-[12px] mb-3" style={{ color: "var(--warn)" }}>
            ⚠ Прикреплять пока нечего: ни запись, ни протокол не выгружены в облако.
            Вставьте ссылку вручную — или сначала выгрузите файлы, тогда поля
            заполнятся сами.
          </div>
        )}
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
