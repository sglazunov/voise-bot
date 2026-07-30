import { useEffect, useRef, useState } from "react";
import {
  Mic, UploadCloud, FileAudio, Sparkles, Users, Monitor, Loader2, RotateCcw,
  Download, FileText, X, ChevronRight, ChevronLeft, CalendarDays,
} from "lucide-react";
import { Page } from "../components/Layout";
import { Card, Select, useToast } from "../components/ui";
import { api } from "../lib/api";
import { fmtDateTime } from "../lib/format";

const MODELS = [
  { v: "", l: "По умолчанию" }, { v: "small", l: "small — быстро" },
  { v: "medium", l: "medium — баланс" }, { v: "large-v3-turbo", l: "large-v3-turbo" },
  { v: "large-v3", l: "large-v3 — точнее всего" },
];
// Transcript download formats (backend: /api/jobs/{id}/result?format=…)
const TRANSCRIPT_FORMATS = [
  { value: "txt", label: "TXT" },
  { value: "srt", label: "SRT" },
  { value: "json", label: "JSON" },
];
const RU_STATUS: Record<string, string> = {
  queued: "в очереди", running: "распознаётся", paused: "пауза", analyzing: "формируется протокол",
  done: "готово", error: "ошибка", cancelled: "отменено",
};
const isBusy = (st: string) => ["queued", "running", "paused", "analyzing"].includes(st);

function List({ title, items, verify }: { title: string; items?: any[]; verify?: any[] }) {
  if (!items?.length) return null;
  return (
    <div className="mb-4">
      <div className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent)" }}>{title}</div>
      <ul className="space-y-1.5">
        {items.map((it, i) => {
          const v = verify?.[i];                       // Д5: grounding info (optional)
          const unverified = v && !v.ok;
          return (
            <li key={i} className="text-[13px] leading-relaxed flex gap-2"
              style={unverified ? { color: "var(--muted)", opacity: 0.85 } : undefined}>
              <ChevronRight size={14} className="flex-none mt-0.5" color="var(--muted)" />
              <span className="min-w-0">
                {typeof it === "string" ? it : `${it.task}${it.owner && it.owner !== "—" ? ` — ${it.owner}` : ""}`}
                {unverified && (
                  <span className="ml-1.5 chip whitespace-nowrap text-[10.5px]"
                    style={{ color: "var(--warn)", background: "rgba(251,191,36,.12)" }}
                    title="В расшифровке не нашлось дословного подтверждения — проверьте пункт">
                    ⚠ проверьте</span>
                )}
                {v?.ok && v.quote && (
                  <details className="mt-0.5">
                    <summary className="text-[11px] cursor-pointer" style={{ color: "var(--muted)" }}>
                      основание{v.t ? ` · ${v.t}` : ""}{v.source === "notes" ? " · из заметок" : ""}</summary>
                    <div className="text-[11.5px] italic mt-0.5 pl-2" style={{ color: "var(--muted)", borderLeft: "2px solid var(--line)" }}>
                      «{v.quote}»</div>
                  </details>
                )}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// Д12: render the transcript from JSON segments, greying-out and underlining
// the spots Whisper itself decoded with low confidence — that's exactly where
// names and numbers need a human glance.
const LOWCONF = -0.7;
function fmtTs(sec: number): string {
  const s = Math.floor(sec || 0), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`
           : `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
const MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
  "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
const dayKey = (ms: number) => {
  const d = new Date(ms);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
};
/** «2026-6-29» → «29 июля» — чтобы на кнопке было видно, что фильтр включён. */
const MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
  "июля", "августа", "сентября", "октября", "ноября", "декабря"];
const fmtDayLabel = (key: string) => {
  const [, m, d] = key.split("-").map(Number);
  return `${d} ${MONTHS_GEN[m] ?? ""}`.trim();
};

/** Календарь истории: месяц, год и день. Дни без встреч не кликаются. */
function HistoryCalendar({ jobs, value, onPick }:
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

function LiveTranscript({ segments }: { segments: any[] }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  // Липкая автопрокрутка: пока человек внизу — доматываем к свежему тексту, но
  // если он отлистал назад читать, не дёргаем его обратно каждые две секунды.
  useEffect(() => {
    const el = boxRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [segments.length]);

  return (
    <div>
      <div className="flex items-center gap-2 text-[11.5px] mb-2" style={{ color: "var(--muted)" }}>
        <Loader2 size={12} className="animate-spin" color="var(--accent)" />
        распознаётся вживую · фрагментов: {segments.length}
      </div>
      <div ref={boxRef} className="max-h-[420px] overflow-y-auto pr-1"
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}>
        {/* Живые фрагменты приходят как {start, end, text} — имён говорящих в
            них ещё нет (спикеры определяются после распознавания), поэтому
            показываем сплошной текст. */}
        <div className="text-[12.5px] leading-relaxed">
          {segments.map((s: any, i: number) => String(s.text || "").trim()).join(" ")}
        </div>
      </div>
    </div>
  );
}

function SegmentView({ segments }: { segments: any[] }) {
  const anyConf = segments.some((s) => typeof s.avg_logprob === "number");
  let lastSpeaker: string | null | undefined = undefined;
  return (
    <div className="text-[12.5px] leading-relaxed">
      {anyConf && (
        <div className="text-[11px] mb-2" style={{ color: "var(--muted)" }}>
          <span style={{ borderBottom: "1px dotted var(--warn)" }}>Подчёркнутое</span> — низкая
          уверенность распознавания: проверьте имена и цифры.
        </div>
      )}
      {segments.map((s, i) => {
        const low = typeof s.avg_logprob === "number" && s.avg_logprob < LOWCONF;
        const speakerChanged = s.speaker !== lastSpeaker;
        lastSpeaker = s.speaker;
        return (
          <div key={i} className={speakerChanged && s.speaker ? "mt-2" : ""}>
            {speakerChanged && s.speaker && (
              <div className="font-semibold text-[12px]" style={{ color: "var(--accent)" }}>
                [{fmtTs(s.start)}] {s.speaker}:</div>
            )}
            <span
              title={low ? `Whisper не уверен в этом фрагменте (logprob ${s.avg_logprob?.toFixed(2)})` : undefined}
              style={low ? { color: "var(--muted)", borderBottom: "1px dotted var(--warn)" } : undefined}>
              {!s.speaker && <span style={{ color: "var(--muted)" }}>[{fmtTs(s.start)}] </span>}
              {s.text}{" "}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// Д13: инлайн-редактор протокола. Списки редактируются построчно; задачи —
// «текст — ответственный». Правки человека доверенные: verification снимается.
const taskLine = (t: any) => `${t.task}${t.owner ? ` — ${t.owner}` : ""}`;
const parseTasks = (s: string) => s.split("\n").map((l) => l.trim()).filter(Boolean)
  .map((l) => { const i = l.lastIndexOf(" — "); return i > 0
    ? { task: l.slice(0, i).trim(), owner: l.slice(i + 3).trim() }
    : { task: l, owner: "" }; });
const parseLines = (s: string) => s.split("\n").map((l) => l.trim()).filter(Boolean);

function ProtocolEditor({ a, jobId, onSaved, onCancel, toast }:
  { a: any; jobId: string; onSaved: (a: any) => void; onCancel: () => void; toast: any }) {
  const [d, setD] = useState<any>(() => ({
    summary: a.summary || "",
    detailed: (a.detailed || []).map((t: any) => ({ topic: t.topic || "", details: t.details || "" })),
    decisions: (a.decisions || []).join("\n"),
    tasks: (a.tasks || []).map(taskLine).join("\n"),
    minor_tasks: (a.minor_tasks || []).map(taskLine).join("\n"),
    done_tasks: (a.done_tasks || []).map(taskLine).join("\n"),
  }));
  const [busy, setBusy] = useState(false);
  const [regen, setRegen] = useState<number | null>(null);
  async function save() {
    setBusy(true);
    try {
      const r = await api.patch(`/api/jobs/${jobId}/analysis`, { analysis: {
        summary: d.summary, detailed: d.detailed,
        decisions: parseLines(d.decisions),
        tasks: parseTasks(d.tasks), minor_tasks: parseTasks(d.minor_tasks),
        done_tasks: parseTasks(d.done_tasks) } });
      toast("Правки сохранены, Word-протокол пересобран");
      onSaved(r.analysis);
    } catch (e: any) { toast(e.message, true); }
    finally { setBusy(false); }
  }
  async function regenTopic(i: number) {
    setRegen(i);
    try {
      const r = await api.post(`/api/jobs/${jobId}/regen-topic`, { index: i });
      setD({ ...d, detailed: (r.analysis.detailed || []).map((t: any) => ({ topic: t.topic || "", details: t.details || "" })) });
      toast("Раздел перегенерирован");
    } catch (e: any) { toast(e.message, true); }
    finally { setRegen(null); }
  }
  const lbl = (s: string) => <label className="lbl mt-3">{s}</label>;
  return (
    <div>
      {lbl("Кратко")}
      <textarea className="field" rows={3} value={d.summary} onChange={(e) => setD({ ...d, summary: e.target.value })} />
      {lbl("Темы")}
      {d.detailed.map((t: any, i: number) => (
        <div key={i} className="glass2 rounded-xl p-2.5 mb-2">
          <div className="flex gap-2 items-center mb-1.5">
            <input className="field" value={t.topic}
              onChange={(e) => { const dd = [...d.detailed]; dd[i] = { ...t, topic: e.target.value }; setD({ ...d, detailed: dd }); }} />
            <button className="btn btn-ghost flex-none" disabled={regen !== null}
              title="Перегенерировать этот раздел нейросетью (остальное не трогается)"
              onClick={() => regenTopic(i)}>
              {regen === i ? <Loader2 size={14} className="animate-spin" /> : <RotateCcw size={14} />}</button>
          </div>
          <textarea className="field" rows={4} value={t.details}
            onChange={(e) => { const dd = [...d.detailed]; dd[i] = { ...t, details: e.target.value }; setD({ ...d, detailed: dd }); }} />
        </div>
      ))}
      {lbl("Решения (по строке)")}
      <textarea className="field" rows={3} value={d.decisions} onChange={(e) => setD({ ...d, decisions: e.target.value })} />
      {lbl("Задачи (строка: «задача — ответственный»)")}
      <textarea className="field" rows={4} value={d.tasks} onChange={(e) => setD({ ...d, tasks: e.target.value })} />
      {lbl("Мелкие задачи")}
      <textarea className="field" rows={3} value={d.minor_tasks} onChange={(e) => setD({ ...d, minor_tasks: e.target.value })} />
      {lbl("Уже сделано")}
      <textarea className="field" rows={2} value={d.done_tasks} onChange={(e) => setD({ ...d, done_tasks: e.target.value })} />
      <div className="flex gap-2 justify-end mt-3">
        <button className="btn btn-ghost" onClick={onCancel}>Отмена</button>
        <button className="btn btn-primary" disabled={busy} onClick={save}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : <FileText size={14} />} Сохранить правки</button>
      </div>
    </div>
  );
}

// Д13: чат по встрече — вопрос → ответ с таймкодами из расшифровки.
function AskBlock({ jobId }: { jobId: string }) {
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  async function ask() {
    if (q.trim().length < 3 || busy) return;
    setBusy(true); setAnswer("");
    try { const r = await api.post(`/api/jobs/${jobId}/ask`, { question: q.trim() }); setAnswer(r.answer || ""); }
    catch (e: any) { toast(e.message, true); }
    finally { setBusy(false); }
  }
  return (
    <div className="glass2 rounded-2xl p-3 mt-4">
      <div className="text-[12.5px] font-semibold mb-2">💬 Спросить по встрече</div>
      <div className="flex gap-2">
        <input className="field" placeholder="например: что решили по тегам?"
          value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") ask(); }} />
        <button className="btn btn-primary flex-none" disabled={busy} onClick={ask}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : "Спросить"}</button>
      </div>
      {answer && (
        <div className="text-[12.5px] leading-relaxed mt-2.5 whitespace-pre-wrap">{answer}</div>
      )}
    </div>
  );
}

function Protocol({ a }: { a: any }) {
  if (!a) return null;
  return (
    <div>
      {a._warning && (
        <div className="glass2 rounded-2xl p-3 mb-4 text-[12.5px]" style={{ color: "var(--warn)" }}>
          ⚠ {a._warning}
        </div>
      )}
      {a.participants?.length ? (
        <div className="mb-4">
          <div className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent)" }}>Участники</div>
          <div className="flex flex-wrap gap-1.5">
            {a.participants.map((p: any, i: number) => (
              <span key={i} className="chip" style={{ color: "var(--txt)" }}>{p.name}{p.role ? ` · ${p.role}` : ""}</span>
            ))}
          </div>
        </div>
      ) : null}
      {a.summary && (<div className="mb-4"><div className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent)" }}>Кратко</div>
        <div className="text-[13px] leading-relaxed">{a.summary}</div></div>)}
      {a.detailed?.length ? (
        <div className="mb-4"><div className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent)" }}>По темам</div>
          {a.detailed.map((t: any, i: number) => (
            <div key={i} className="glass2 rounded-xl p-3 mb-2">
              <div className="font-semibold text-[13px] mb-1">{t.topic}</div>
              <div className="text-[12.5px] leading-relaxed" style={{ color: "var(--muted)" }}>{t.details}</div>
            </div>
          ))}
        </div>
      ) : null}
      <List title="Ключевые мысли" items={a.key_thoughts} />
      <List title="Выводы" items={a.conclusions} />
      <List title="Решения" items={a.decisions} verify={a.verification?.decisions} />
      <List title="Уже сделано" items={a.done_tasks} verify={a.verification?.done_tasks} />
      <List title="Задачи" items={a.tasks} verify={a.verification?.tasks} />
      <List title="Мелкие задачи" items={a.minor_tasks} verify={a.verification?.minor_tasks} />
    </div>
  );
}

export default function Recognition() {
  const [jobs, setJobs] = useState<any[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [live, setLive] = useState<any>(null);   // live stage from /partial
  const [transcript, setTranscript] = useState<string>("");
  // Д12: segments with per-segment Whisper confidence (from format=json) —
  // low-confidence spots get highlighted so the editor knows where to check.
  const [segments, setSegments] = useState<any[] | null>(null);
  const [tab, setTab] = useState<"protocol" | "transcript">("protocol");
  const [editing, setEditing] = useState(false);          // Д13: protocol editor
  useEffect(() => { setEditing(false); }, [sel]);
  const [fmt, setFmt] = useState("txt");   // transcript download format
  const [busy, setBusy] = useState(false);
  const [prog, setProg] = useState(0);
  const [engines, setEngines] = useState<{ value: string; label: string }[]>([]);
  // Настройки формы переживают обновление страницы (localStorage). Заметки и
  // контекст не сохраняем — они у каждой встречи свои.
  const OPTS_DEFAULTS = { language: "ru", model: "", analyze: true, diarize: false, capture_screen: false, identify_speakers: false, provider: "auto", context_hint: "", preset: "universal" };
  const [opts, setOpts] = useState<any>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("vtx-recognition-opts") || "{}");
      return { ...OPTS_DEFAULTS, ...saved, context_hint: "", user_notes: "" };
    } catch { return { ...OPTS_DEFAULTS }; }
  });
  useEffect(() => {
    const { context_hint, user_notes, ...persist } = opts;
    try { localStorage.setItem("vtx-recognition-opts", JSON.stringify(persist)); } catch { /* quota */ }
  }, [opts]);
  const [presets, setPresets] = useState<{ value: string; label: string }[]>([]);
  useEffect(() => { api.get("/api/presets").then((d) => setPresets(d.presets || [])).catch(() => {}); }, []);
  const [recommend, setRecommend] = useState("");
  useEffect(() => { api.get("/api/system/recommend").then((d) => setRecommend(d.detail || "")).catch(() => {}); }, []);
  // Д14: поиск по всем встречам (debounce 350 мс)
  const [searchQ, setSearchQ] = useState("");
  const [searchRes, setSearchRes] = useState<any[]>([]);
  // Календарь истории: выбранный день («2026-6-29») и раскрыт ли сам календарь.
  const [dayFilter, setDayFilter] = useState<string | null>(null);
  const [showCal, setShowCal] = useState(false);
  // Связь с сервером потеряна — показываем это честно, а не замиранием.
  const [offline, setOffline] = useState(false);
  useEffect(() => {
    const q = searchQ.trim();
    if (q.length < 2) { setSearchRes([]); return; }
    const t = setTimeout(() => {
      api.get(`/api/search?q=${encodeURIComponent(q)}`)
        .then((d) => setSearchRes(d.results || [])).catch(() => setSearchRes([]));
    }, 350);
    return () => clearTimeout(t);
  }, [searchQ]);
  const fileRef = useRef<HTMLInputElement>(null);
  const toast = useToast();

  // Опрос списка задач, устойчивый к обрыву связи.
  //
  // Раньше он слал запрос каждые 4 секунды безусловно. Когда у клиента пропадала
  // сеть (сон ноутбука, переключение Wi-Fi или VPN), запросы копились по
  // 30 секунд в «Pending», консоль заполнялась ERR_NAME_NOT_RESOLVED, а человек
  // видел лишь замерший интерфейс и не понимал, что связи нет.
  const inFlight = useRef(false);
  const loadJobs = async () => {
    if (inFlight.current) return;          // предыдущий ответ ещё не пришёл
    if (typeof navigator !== "undefined" && navigator.onLine === false) {
      setOffline(true);
      return;                              // сети нет — не плодим мёртвые запросы
    }
    inFlight.current = true;
    try {
      setJobs(await api.get("/api/jobs"));
      setOffline(false);
    } catch {
      setOffline(true);
    } finally {
      inFlight.current = false;
    }
  };
  // Список для показа: либо все встречи, либо только выбранный в календаре день.
  const shownJobs = dayFilter
    ? jobs.filter((j: any) => dayKey((j.created_at || 0) * 1000) === dayFilter)
    : jobs;
  useEffect(() => { loadJobs(); const t = setInterval(loadJobs, 4000); return () => clearInterval(t); }, []);
  useEffect(() => { api.get("/api/providers").then((d) => setEngines(d.engines || [])).catch(() => {}); }, []);

  // Load selected job detail + transcript, poll while busy.
  // While it's working we also pull /partial — the LIVE stage of recognition and
  // protocol building (stage + streamed characters), so the user sees progress.
  useEffect(() => {
    if (!sel) { setDetail(null); setTranscript(""); setSegments(null); setLive(null); return; }
    let alive = true;
    const tick = async () => {
      try {
        const j = await api.get(`/api/jobs/${sel}`);
        if (!alive) return;
        setDetail(j);
        if (isBusy(j.status)) {
          try { const p = await api.get(`/api/jobs/${sel}/partial`); if (alive) setLive(p); } catch { /* ignore */ }
        } else {
          setLive(null);
        }
        // The transcript is FINISHED as soon as the protocol stage starts —
        // show it during "analyzing" too, not only when the whole job is done.
        if (j.status === "done" || j.status === "cancelled" || j.status === "analyzing") {
          api.get(`/api/jobs/${sel}/result?format=json`)
            .then((d) => { if (alive && d?.segments?.length) setSegments(d.segments); })
            .catch(() => {});
          api.text(`/api/jobs/${sel}/result?format=txt`).then((t) => alive && setTranscript(t)).catch(() => {});
        }
      } catch { /* gone */ }
    };
    tick();
    const t = setInterval(() => { if (detail && isBusy(detail.status)) tick(); }, 2000);
    return () => { alive = false; clearInterval(t); };
  }, [sel, detail?.status]);

  const [uploadError, setUploadError] = useState("");
  // Выбранный, но ещё НЕ отправленный файл. Раньше он улетал в работу сразу при
  // выборе: настройки (язык, модель, движок, пресет, заметки) применить было
  // уже нельзя, а ошибочно выбранный файл приходилось отменять постфактум.
  const [pending, setPending] = useState<File | null>(null);

  function onFile(f: File | undefined) {
    if (!f) return;
    setUploadError("");
    setPending(f);
  }

  async function startRecognition() {
    const f = pending;
    if (!f) return;
    setUploadError("");
    // Бесплатный туннель Cloudflare (*.trycloudflare.com) режет тело запроса
    // на ~100 МБ: файл встречи просто не долетает до сервера, а в логах пусто.
    const viaTunnel = /\.trycloudflare\.com$/i.test(location.hostname);
    const sizeMb = Math.round(f.size / 1024 / 1024);
    if (viaTunnel && f.size > 95 * 1024 * 1024) {
      setUploadError(
        `Файл ${sizeMb} МБ, а вы зашли через временную ссылку trycloudflare — ` +
        "она обрезает загрузки примерно на 100 МБ, файл не дойдёт до сервера. " +
        "Загрузите его с локального адреса (http://localhost:8000 или IP сервера) " +
        "либо сожмите/разбейте запись.");
      return;
    }
    setBusy(true); setProg(0);
    const fd = new FormData();
    fd.append("file", f);
    fd.append("language", opts.language);
    fd.append("model", opts.model);
    fd.append("analyze", String(opts.analyze));
    fd.append("diarize", String(opts.diarize));
    fd.append("capture_screen", String(opts.capture_screen));
    fd.append("identify_speakers", String(opts.identify_speakers));
    fd.append("provider", opts.provider);
    fd.append("context_hint", opts.context_hint || f.name);
    fd.append("preset", opts.preset || "");
    if (opts.user_notes?.trim()) fd.append("user_notes", opts.user_notes.trim());
    try {
      const j = await api.upload("/api/jobs", fd, (p) => setProg(p));
      toast("Файл принят — идёт распознавание");
      setPending(null);
      await loadJobs(); setSel(j.job_id); setTab("protocol");
    } catch (e: any) {
      // Ошибка загрузки НЕ должна сгорать тостом: раньше форма молча
      // сбрасывалась («видео исчезло»), и причина оставалась загадкой.
      const hint = /сеть|network/i.test(e.message) && viaTunnel
        ? " Похоже, туннель trycloudflare оборвал загрузку (лимит ~100 МБ) — " +
          "попробуйте с локального адреса."
        : "";
      setUploadError(`Загрузка не удалась: ${e.message}.${hint}`);
      toast(e.message, true);
    } finally { setBusy(false); setProg(0); }
  }
  async function retry(id: string) { try { await api.post(`/api/jobs/${id}/retry`); loadJobs(); } catch (e: any) { toast(e.message, true); } }
  // Остановка идущей работы. Спрашиваем подтверждение: у длинной записи позади
  // могут быть десятки минут счёта, и случайный клик обидно дорог.
  async function stopJob(id: string) {
    if (!confirm("Остановить? Уже распознанная часть сохранится, продолжить с этого места будет нельзя.")) return;
    try {
      await api.post(`/api/jobs/${id}/cancel`);
      toast("Останавливаю…");
      loadJobs();
    } catch (e: any) { toast(e.message, true); }
  }
  // Delivery only (cloud upload + Weeek link) — no expensive LLM re-run.
  async function redeliver(id: string) {
    try { const r = await api.post(`/api/jobs/${id}/redeliver`); toast(r.detail || "Прикреплено"); loadJobs(); }
    catch (e: any) { toast(e.message, true); }
  }
  async function reanalyze(id: string) {
    try { await api.post(`/api/jobs/${id}/reanalyze`, { provider: opts.provider }); toast("Пересобираю протокол…"); loadJobs(); }
    catch (e: any) { toast(e.message, true); }
  }

  // Д6: participant's live notes on an existing job → save, then «Пересобрать».
  const [notesDraft, setNotesDraft] = useState("");
  const [notesOpen, setNotesOpen] = useState(false);
  useEffect(() => { setNotesDraft(detail?.user_notes || ""); setNotesOpen(false); }, [detail?.id]);
  async function saveNotes() {
    if (!detail) return;
    try {
      await api.post(`/api/jobs/${detail.id}/notes`, { notes: notesDraft });
      toast(notesDraft.trim()
        ? "Заметки сохранены — нажмите «Пересобрать», чтобы протокол их учёл"
        : "Заметки удалены");
    } catch (e: any) { toast(e.message, true); }
  }

  return (
    <Page title="Распознавание и протокол" subtitle="Загрузите запись — получите расшифровку и структурный протокол">
      <div className="grid lg:grid-cols-[.9fr_1.1fr] gap-3.5 items-start">
        {/* LEFT: upload + jobs */}
        <div className="space-y-3.5">
          <Card>
            <div className="flex items-center gap-2 mb-3"><Mic size={17} color="var(--accent)" />
              <div className="font-bold text-[15px]">Новая запись</div></div>
            <div onClick={() => fileRef.current?.click()}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => { e.preventDefault(); onFile(e.dataTransfer.files?.[0]); }}
              className="glass2 rounded-2xl grid place-items-center text-center cursor-pointer transition"
              style={{ padding: "26px 16px", borderStyle: "dashed" }}>
              {busy ? (
                <><Loader2 size={26} className="animate-spin" color="var(--accent)" />
                  <div className="text-[13px] mt-2">
                    {prog >= 100 ? "Сервер принимает и сохраняет файл…" : `Загрузка… ${prog}%`}</div>
                  {prog >= 100 && (
                    <div className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
                      для больших файлов это может занять минуту-другую</div>
                  )}
                  <div className="mt-2 w-full" style={{ height: 6, borderRadius: 6, background: "rgba(120,140,150,.2)", overflow: "hidden" }}>
                    <div style={{ height: "100%", width: `${prog}%`, background: "linear-gradient(90deg,var(--accent),var(--accent2))" }} /></div></>
              ) : pending ? (
                <><Mic size={26} color="var(--accent)" />
                  <div className="text-[13.5px] mt-2 font-semibold break-all">{pending.name}</div>
                  <div className="text-[11.5px] mt-0.5" style={{ color: "var(--muted)" }}>
                    {Math.max(1, Math.round(pending.size / 1024 / 1024))} МБ · проверьте настройки ниже и нажмите «Распознать»</div></>
              ) : (
                <><UploadCloud size={26} color="var(--accent)" />
                  <div className="text-[13.5px] mt-2 font-semibold">Перетащите файл или нажмите</div>
                  <div className="text-[11.5px] mt-0.5" style={{ color: "var(--muted)" }}>mp3 · wav · m4a · mp4 · ogg…</div></>
              )}
            </div>

            {/* Файл выбран, но ещё не отправлен — запуск только по кнопке. */}
            {pending && !busy && (
              <div className="flex gap-2 mt-3">
                <button className="btn flex-1" onClick={startRecognition}>▶ Распознать</button>
                <button className="btn-ghost" onClick={() => { setPending(null); if (fileRef.current) fileRef.current.value = ""; }}>
                  Убрать файл</button>
              </div>
            )}
            <input ref={fileRef} type="file" className="hidden" onChange={(e) => onFile(e.target.files?.[0] || undefined)}
              accept=".mp3,.wav,.m4a,.ogg,.oga,.opus,.flac,.aac,.mp4,.mov,.mkv,.webm,.m4v" />
            {uploadError && (
              <div className="glass2 rounded-2xl p-3 mt-3 flex items-start gap-2.5 text-[12.5px]"
                style={{ color: "#fca5a5", border: "1px solid rgba(248,113,113,.35)" }}>
                <span className="min-w-0 flex-1">⚠ {uploadError}</span>
                <button className="btn-ghost grid place-items-center flex-none"
                  style={{ width: 26, height: 26, borderRadius: 8 }}
                  onClick={() => setUploadError("")}><X size={13} /></button>
              </div>
            )}

            <div className="grid grid-cols-2 gap-3 mt-3">
              <div><label className="lbl">Язык</label>
                <input className="field" value={opts.language} onChange={(e) => setOpts({ ...opts, language: e.target.value })} placeholder="ru" /></div>
              <div><label className="lbl">Модель</label>
                <Select value={opts.model} onChange={(v) => setOpts({ ...opts, model: v })}
                  options={MODELS.map((m) => ({ value: m.v, label: m.l }))} /></div>
            </div>
            {recommend && <div className="text-[11px] mt-1" style={{ color: "var(--muted)" }}>💡 {recommend}</div>}
            <label className="lbl mt-3">Движок протокола</label>
            <Select value={opts.provider} onChange={(v) => setOpts({ ...opts, provider: v })}
              options={[{ value: "auto", label: "Авто" }, ...engines.map((e) => ({ value: e.value, label: e.label }))]} />
            <label className="lbl mt-3">Тип встречи (пресет протокола)</label>
            <Select value={opts.preset} onChange={(v) => setOpts({ ...opts, preset: v })}
              options={presets.length ? presets : [{ value: "universal", label: "Универсальный" }]} />
            <label className="lbl mt-3">Контекст (проект/тема)</label>
            <input className="field" value={opts.context_hint} onChange={(e) => setOpts({ ...opts, context_hint: e.target.value })}
              placeholder="подставит сохранённый контекст проекта" />
            <label className="lbl mt-3">Заметки со встречи</label>
            <textarea className="field" rows={3} value={opts.user_notes || ""}
              onChange={(e) => setOpts({ ...opts, user_notes: e.target.value })}
              placeholder="ваши живые заметки — станут скелетом протокола (можно добавить и после)" />

            <div className="grid grid-cols-2 gap-2 mt-3">
              {[
                { k: "analyze", i: Sparkles, l: "Протокол" },
                { k: "diarize", i: Users, l: "Спикеры (аудио)" },
                { k: "identify_speakers", i: Users, l: "Спикеры (видео)" },
                { k: "capture_screen", i: Monitor, l: "OCR экрана" },
              ].map((o) => (
                <button key={o.k} onClick={() => setOpts({ ...opts, [o.k]: !opts[o.k] })}
                  className="glass2 rounded-xl px-3 py-2.5 flex items-center gap-2 text-left transition"
                  style={opts[o.k] ? { borderColor: "var(--accent)", background: "rgba(45,212,191,.1)" } : {}}>
                  <o.i size={15} color={opts[o.k] ? "var(--accent)" : "var(--muted)"} />
                  <span className="text-[12.5px] font-semibold">{o.l}</span>
                </button>
              ))}
            </div>
          </Card>

          <Card>
            {offline && (
              <div className="glass2 rounded-2xl p-3 mb-2.5 flex items-start gap-2.5 text-[12.5px]"
                style={{ color: "var(--warn)", border: "1px solid rgba(217,119,6,.35)" }}>
                <span className="min-w-0 flex-1">
                  ⚠ Нет связи с сервером — список не обновляется. Работа на сервере
                  при этом продолжается: как только связь вернётся, всё подтянется само.
                </span>
              </div>
            )}
            {/* Кнопка ПОДПИСАНА намеренно: первая версия была безымянной
                иконкой 15 px рядом с жирным заголовком — функцию просто не
                находили, «в истории только строка поиска». */}
            <div className="flex items-center justify-between gap-2 mb-2.5">
              <div className="font-bold text-[14px]">История</div>
              <button className="btn btn-ghost flex-none"
                title="Показать протоколы за выбранный день"
                style={{ padding: "6px 12px",
                  ...(showCal || dayFilter
                    ? { borderColor: "var(--accent)", color: "var(--accent)" } : {}) }}
                onClick={() => setShowCal((v) => !v)}>
                <CalendarDays size={14} />
                {dayFilter ? fmtDayLabel(dayFilter) : "По дате"}
              </button>
            </div>
            {/* Д14: поиск по расшифровкам и протоколам всех встреч команды */}
            <div className="relative mb-2.5">
              <input className="field" placeholder="Поиск по всем встречам…"
                value={searchQ} onChange={(e) => setSearchQ(e.target.value)} />
            </div>
            {showCal && (
              <HistoryCalendar jobs={jobs} value={dayFilter}
                onPick={(k) => { setDayFilter(k); setSearchQ(""); }} />
            )}
            {searchQ.trim().length >= 2 ? (
              searchRes.length ? searchRes.map((r) => (
                <button key={r.job_id} onClick={() => { setSel(r.job_id); setTab("transcript"); }}
                  className="w-full glass2 rounded-2xl px-3.5 py-3 mb-2 text-left transition"
                  style={sel === r.job_id ? { borderColor: "var(--accent)" } : {}}>
                  <div className="font-semibold text-[13px] truncate">{r.title}</div>
                  <div className="text-[11.5px] mt-0.5" style={{ color: "var(--muted)" }}
                    dangerouslySetInnerHTML={{
                      __html: r.snippet
                        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
                        .replace(/⟦([^⟧]{1,80})⟧/g, '<mark style="background:rgba(45,212,191,.25);color:inherit;border-radius:3px">$1</mark>'),
                    }} />
                </button>
              )) : <div className="text-[12.5px] py-2" style={{ color: "var(--muted)" }}>Ничего не найдено.</div>
            ) : shownJobs.length ? shownJobs.map((j) => (
              <button key={j.id} onClick={() => { setSel(j.id); setTab("protocol"); }}
                className="w-full glass2 rounded-2xl px-3.5 py-3 mb-2 flex items-center gap-3 text-left transition"
                style={sel === j.id ? { borderColor: "var(--accent)" } : {}}>
                <div className="grid place-items-center rounded-xl flex-none" style={{ width: 36, height: 36, background: "rgba(45,212,191,.13)" }}>
                  {isBusy(j.status) ? <Loader2 size={16} className="animate-spin" color="var(--accent)" /> : <FileAudio size={16} color="var(--accent)" />}</div>
                <div className="min-w-0 flex-1">
                  <div className="font-semibold text-[13px] truncate">{j.filename}</div>
                  <div className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>{RU_STATUS[j.status] || j.status} · {fmtDateTime(new Date(j.created_at * 1000).toISOString())}</div></div>
                {isBusy(j.status) && <span className="text-[11px] font-semibold" style={{ color: "var(--accent)" }}>{Math.round((j.progress || 0) * 100)}%</span>}
              </button>
            )) : (
              <div className="text-[13px] py-2" style={{ color: "var(--muted)" }}>
                {dayFilter ? "В этот день встреч не было." : "Пока нет задач."}</div>
            )}
          </Card>
        </div>

        {/* RIGHT: detail */}
        <Card className="min-h-[420px]">
          {!detail ? (
            <div className="grid place-items-center text-center h-full py-20">
              <div><FileText size={30} color="var(--muted)" className="mx-auto mb-3" />
                <div className="text-[13.5px]" style={{ color: "var(--muted)" }}>Выберите запись слева или загрузите новую,<br />чтобы увидеть протокол и расшифровку.</div></div>
            </div>
          ) : (
            <>
              <div className="flex items-start gap-3 mb-3">
                <div className="min-w-0 flex-1">
                  <div className="font-bold text-[15px] truncate">{detail.filename}</div>
                  <div className="text-[11.5px] mt-0.5" style={{ color: "var(--muted)" }}>
                    {RU_STATUS[detail.status] || detail.status}
                    {detail.speakers ? ` · спикеров: ${detail.speakers}` : ""}
                    {detail.duration ? ` · ${Math.round(detail.duration / 60)} мин` : ""}</div>
                </div>
                <button className="btn btn-ghost flex-none" onClick={() => setSel(null)}><X size={15} /></button>
              </div>

              {isBusy(detail.status) && (
                <div className="glass2 rounded-2xl p-3 mb-3 flex items-start gap-3">
                  <Loader2 size={17} className="animate-spin flex-none mt-0.5" color="var(--accent)" />
                  <div className="flex-1 min-w-0">
                    <div className="text-[13px] font-semibold">{RU_STATUS[detail.status]}…</div>
                    {/* Live stage of the protocol build / recognition, from /partial */}
                    {detail.status === "analyzing" && (
                      <div className="text-[12px] mt-0.5" style={{ color: "var(--muted)" }}>
                        {live?.analysis?.stage || "готовлю запрос к ИИ"}
                        {live?.analysis?.chars ? ` · получено ${live.analysis.chars} символов` : ""}
                      </div>
                    )}
                    {detail.status === "running" && live?.segments?.length ? (
                      <div className="text-[12px] mt-0.5" style={{ color: "var(--muted)" }}>
                        распознано фрагментов: {live.segments.length}</div>
                    ) : null}
                    <div className="mt-1.5" style={{ height: 6, borderRadius: 6, background: "rgba(120,140,150,.2)", overflow: "hidden" }}>
                      <div style={{ height: "100%",
                        width: detail.status === "analyzing" ? "100%" : `${Math.round((detail.progress || 0) * 100)}%`,
                        background: "linear-gradient(90deg,var(--accent),var(--accent2))",
                        transition: "width .4s",
                        opacity: detail.status === "analyzing" ? 0.55 : 1 }} /></div>
                    {detail.status === "analyzing" && live?.analysis?.text ? (
                      <pre className="text-[11px] mt-2 whitespace-pre-wrap font-sans max-h-24 overflow-y-auto"
                        style={{ color: "var(--muted)" }}>{String(live.analysis.text).slice(-400)}</pre>
                    ) : null}
                  </div>
                  {/* Остановить долгую работу, не дожидаясь конца: часовая запись
                      на medium считается ~40 минут, и ошибочно запущенная задача
                      иначе занимала бы процессор и очередь всё это время. */}
                  <button className="btn-ghost flex-none self-start" onClick={() => stopJob(detail.id)}>
                    ■ Стоп</button>
                </div>
              )}
              {detail.error && <div className="glass2 rounded-2xl p-3 mb-3 text-[12.5px]" style={{ color: "#fca5a5" }}>{detail.error}</div>}
              {detail.analysis_error && <div className="glass2 rounded-2xl p-3 mb-3 text-[12.5px]" style={{ color: "var(--warn)" }}>{detail.analysis_error}</div>}
              {detail.delivery_error && (
                <div className="glass2 rounded-2xl p-3 mb-3 flex items-center gap-3 flex-wrap text-[12.5px]" style={{ color: "var(--warn)" }}>
                  <span className="min-w-0 flex-1">Протокол готов, но не прикрепился: {detail.delivery_error}</span>
                  <button className="btn btn-ghost flex-none" onClick={() => redeliver(detail.id)}>
                    <RotateCcw size={14} /> Прикрепить снова</button>
                </div>
              )}

              {/* Д6: notes editor — the human's live notes outrank the transcript */}
              <div className="glass2 rounded-2xl p-3 mb-3">
                <button type="button" onClick={() => setNotesOpen((o) => !o)}
                  className="w-full flex items-center gap-2 text-left"
                  style={{ cursor: "pointer", background: "none", border: 0, padding: 0 }}>
                  <span className="text-[12.5px] font-semibold">📝 Заметки со встречи</span>
                  <span className="text-[11.5px]" style={{ color: "var(--muted)" }}>
                    {detail.user_notes ? "есть — приоритетный источник протокола" : "нет — добавьте, и протокол станет точнее"}
                  </span>
                </button>
                {notesOpen && (
                  <div className="mt-2">
                    <textarea className="field" rows={5} value={notesDraft}
                      onChange={(e) => setNotesDraft(e.target.value)}
                      placeholder="что решили, кто что взял, ключевые цифры — как записали на встрече" />
                    <div className="flex gap-2 mt-2 justify-end">
                      <button className="btn btn-ghost" onClick={saveNotes}>Сохранить</button>
                      {detail.status === "done" && (
                        <button className="btn btn-primary" onClick={async () => { await saveNotes(); reanalyze(detail.id); }}>
                          <Sparkles size={14} /> Сохранить и пересобрать</button>
                      )}
                    </div>
                  </div>
                )}
              </div>

              <div className="flex items-center gap-2 mb-3 flex-wrap">
                <div className="glass2 rounded-full p-1 flex gap-1">
                  {(["protocol", "transcript"] as const).map((t) => (
                    <button key={t} onClick={() => setTab(t)}
                      className="rounded-full px-3.5 py-1.5 text-[12.5px] font-semibold transition"
                      style={tab === t ? { background: "linear-gradient(90deg,var(--accent),var(--accent2))", color: "var(--accent-ink)" } : { color: "var(--muted)" }}>
                      {t === "protocol" ? "Протокол" : "Расшифровка"}</button>
                  ))}
                </div>
                <div className="w-full lg:w-auto lg:ml-auto flex gap-2 flex-wrap justify-end">
                  {detail.status === "done" && detail.docx_providers?.length ? (
                    <a className="btn btn-ghost" href={`/api/jobs/${detail.id}/result?format=docx`}><Download size={14} /> Word</a>
                  ) : null}
                  {(detail.status === "done" || detail.status === "cancelled") && (
                    <>
                      <Select className="w-[104px]" value={fmt} onChange={setFmt} options={TRANSCRIPT_FORMATS} />
                      <a className="btn btn-ghost" href={`/api/jobs/${detail.id}/result?format=${fmt}`}>
                        <Download size={14} /> Скачать</a>
                    </>
                  )}
                  {detail.status === "error" && <button className="btn btn-ghost" onClick={() => retry(detail.id)}><RotateCcw size={14} /> Повторить</button>}
                  {detail.status === "done" && detail.analysis && tab === "protocol" && (
                    <button className="btn btn-ghost" onClick={() => setEditing((e) => !e)}>
                      <FileText size={14} /> {editing ? "Отменить правки" : "Редактировать"}</button>
                  )}
                  {detail.status === "done" && <button className="btn btn-ghost" onClick={() => reanalyze(detail.id)}><Sparkles size={14} /> Пересобрать</button>}
                </div>
              </div>

              {tab === "protocol" ? (
                detail.analysis ? (
                  editing ? (
                    <ProtocolEditor a={detail.analysis} jobId={detail.id} toast={toast}
                      onCancel={() => setEditing(false)}
                      onSaved={(a) => { setDetail({ ...detail, analysis: a }); setEditing(false); }} />
                  ) : (
                    <>
                      <Protocol a={detail.analysis} />
                      {detail.status === "done" && <AskBlock jobId={detail.id} />}
                    </>
                  )
                ) :
                  <div className="text-[13px] py-6 text-center" style={{ color: "var(--muted)" }}>
                    {isBusy(detail.status) ? "Протокол формируется…" : "Протокол не создавался для этой записи."}</div>
              ) : segments ? (
                <SegmentView segments={segments} />
              ) : transcript ? (
                <pre className="text-[12.5px] leading-relaxed whitespace-pre-wrap font-sans">{transcript}</pre>
              ) : isBusy(detail.status) && live?.segments?.length ? (
                // Живая расшифровка: фрагменты приходят с /partial каждые 2 с.
                // Раньше здесь висело «Расшифровка идёт…», и по часовой записи
                // человек больше часа не видел ни слова — непонятно, работает
                // ли вообще. Теперь текст растёт на глазах.
                <LiveTranscript segments={live.segments} />
              ) : (
                <div className="text-[13px] py-6 text-center" style={{ color: "var(--muted)" }}>
                  {isBusy(detail.status) ? "Расшифровка идёт…" : "Расшифровка недоступна."}</div>
              )}
            </>
          )}
        </Card>
      </div>
    </Page>
  );
}
