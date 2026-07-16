import { useEffect, useRef, useState } from "react";
import {
  Mic, UploadCloud, FileAudio, Sparkles, Users, Monitor, Loader2, RotateCcw,
  Download, FileText, X, ChevronRight,
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
const RU_STATUS: Record<string, string> = {
  queued: "в очереди", running: "распознаётся", paused: "пауза", analyzing: "формируется протокол",
  done: "готово", error: "ошибка", cancelled: "отменено",
};
const isBusy = (st: string) => ["queued", "running", "paused", "analyzing"].includes(st);

function List({ title, items }: { title: string; items?: any[] }) {
  if (!items?.length) return null;
  return (
    <div className="mb-4">
      <div className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent)" }}>{title}</div>
      <ul className="space-y-1.5">
        {items.map((it, i) => (
          <li key={i} className="text-[13px] leading-relaxed flex gap-2">
            <ChevronRight size={14} className="flex-none mt-0.5" color="var(--muted)" />
            <span>{typeof it === "string" ? it : `${it.task}${it.owner && it.owner !== "—" ? ` — ${it.owner}` : ""}`}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Protocol({ a }: { a: any }) {
  if (!a) return null;
  return (
    <div>
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
      <List title="Решения" items={a.decisions} />
      <List title="Уже сделано" items={a.done_tasks} />
      <List title="Задачи" items={a.tasks} />
      <List title="Мелкие задачи" items={a.minor_tasks} />
    </div>
  );
}

export default function Recognition() {
  const [jobs, setJobs] = useState<any[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [live, setLive] = useState<any>(null);   // live stage from /partial
  const [transcript, setTranscript] = useState<string>("");
  const [tab, setTab] = useState<"protocol" | "transcript">("protocol");
  const [busy, setBusy] = useState(false);
  const [prog, setProg] = useState(0);
  const [engines, setEngines] = useState<{ value: string; label: string }[]>([]);
  const [opts, setOpts] = useState<any>({ language: "ru", model: "", analyze: true, diarize: false, capture_screen: false, identify_speakers: false, provider: "auto", context_hint: "" });
  const fileRef = useRef<HTMLInputElement>(null);
  const toast = useToast();

  const loadJobs = () => api.get("/api/jobs").then(setJobs).catch(() => {});
  useEffect(() => { loadJobs(); const t = setInterval(loadJobs, 4000); return () => clearInterval(t); }, []);
  useEffect(() => { api.get("/api/providers").then((d) => setEngines(d.engines || [])).catch(() => {}); }, []);

  // Load selected job detail + transcript, poll while busy.
  // While it's working we also pull /partial — the LIVE stage of recognition and
  // protocol building (stage + streamed characters), so the user sees progress.
  useEffect(() => {
    if (!sel) { setDetail(null); setTranscript(""); setLive(null); return; }
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
        if (j.status === "done" || j.status === "cancelled") {
          api.text(`/api/jobs/${sel}/result?format=txt`).then((t) => alive && setTranscript(t)).catch(() => {});
        }
      } catch { /* gone */ }
    };
    tick();
    const t = setInterval(() => { if (detail && isBusy(detail.status)) tick(); }, 2000);
    return () => { alive = false; clearInterval(t); };
  }, [sel, detail?.status]);

  async function onFile(f: File | undefined) {
    if (!f) return;
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
    try {
      const j = await api.upload("/api/jobs", fd, (p) => setProg(p));
      toast("Файл принят — идёт распознавание");
      await loadJobs(); setSel(j.job_id); setTab("protocol");
    } catch (e: any) { toast(e.message, true); } finally { setBusy(false); setProg(0); }
  }
  async function retry(id: string) { try { await api.post(`/api/jobs/${id}/retry`); loadJobs(); } catch (e: any) { toast(e.message, true); } }
  async function reanalyze(id: string) {
    try { await api.post(`/api/jobs/${id}/reanalyze`, { provider: opts.provider }); toast("Пересобираю протокол…"); loadJobs(); }
    catch (e: any) { toast(e.message, true); }
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
                  <div className="text-[13px] mt-2">Загрузка… {prog}%</div>
                  <div className="mt-2 w-full" style={{ height: 6, borderRadius: 6, background: "rgba(120,140,150,.2)", overflow: "hidden" }}>
                    <div style={{ height: "100%", width: `${prog}%`, background: "linear-gradient(90deg,var(--accent),var(--accent2))" }} /></div></>
              ) : (
                <><UploadCloud size={26} color="var(--accent)" />
                  <div className="text-[13.5px] mt-2 font-semibold">Перетащите файл или нажмите</div>
                  <div className="text-[11.5px] mt-0.5" style={{ color: "var(--muted)" }}>mp3 · wav · m4a · mp4 · ogg…</div></>
              )}
            </div>
            <input ref={fileRef} type="file" className="hidden" onChange={(e) => onFile(e.target.files?.[0] || undefined)}
              accept=".mp3,.wav,.m4a,.ogg,.oga,.opus,.flac,.aac,.mp4,.mov,.mkv,.webm,.m4v" />

            <div className="grid grid-cols-2 gap-3 mt-3">
              <div><label className="lbl">Язык</label>
                <input className="field" value={opts.language} onChange={(e) => setOpts({ ...opts, language: e.target.value })} placeholder="ru" /></div>
              <div><label className="lbl">Модель</label>
                <Select value={opts.model} onChange={(v) => setOpts({ ...opts, model: v })}
                  options={MODELS.map((m) => ({ value: m.v, label: m.l }))} /></div>
            </div>
            <label className="lbl mt-3">Движок протокола</label>
            <Select value={opts.provider} onChange={(v) => setOpts({ ...opts, provider: v })}
              options={[{ value: "auto", label: "Авто" }, ...engines.map((e) => ({ value: e.value, label: e.label }))]} />
            <label className="lbl mt-3">Контекст (проект/тема)</label>
            <input className="field" value={opts.context_hint} onChange={(e) => setOpts({ ...opts, context_hint: e.target.value })}
              placeholder="подставит сохранённый контекст проекта" />

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
            <div className="font-bold text-[14px] mb-2.5">История</div>
            {jobs.length ? jobs.map((j) => (
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
            )) : <div className="text-[13px] py-2" style={{ color: "var(--muted)" }}>Пока нет задач.</div>}
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
                </div>
              )}
              {detail.error && <div className="glass2 rounded-2xl p-3 mb-3 text-[12.5px]" style={{ color: "#fca5a5" }}>{detail.error}</div>}
              {detail.analysis_error && <div className="glass2 rounded-2xl p-3 mb-3 text-[12.5px]" style={{ color: "var(--warn)" }}>{detail.analysis_error}</div>}

              <div className="flex items-center gap-2 mb-3 flex-wrap">
                <div className="glass2 rounded-full p-1 flex gap-1">
                  {(["protocol", "transcript"] as const).map((t) => (
                    <button key={t} onClick={() => setTab(t)}
                      className="rounded-full px-3.5 py-1.5 text-[12.5px] font-semibold transition"
                      style={tab === t ? { background: "linear-gradient(90deg,var(--accent),var(--accent2))", color: "var(--accent-ink)" } : { color: "var(--muted)" }}>
                      {t === "protocol" ? "Протокол" : "Расшифровка"}</button>
                  ))}
                </div>
                <div className="ml-auto flex gap-2">
                  {detail.status === "done" && detail.docx_providers?.length ? (
                    <a className="btn btn-ghost" href={`/api/jobs/${detail.id}/result?format=docx`}><Download size={14} /> Word</a>
                  ) : null}
                  {(detail.status === "done" || detail.status === "cancelled") && (
                    <a className="btn btn-ghost" href={`/api/jobs/${detail.id}/result?format=srt`}><Download size={14} /> SRT</a>
                  )}
                  {detail.status === "error" && <button className="btn btn-ghost" onClick={() => retry(detail.id)}><RotateCcw size={14} /> Повторить</button>}
                  {detail.status === "done" && <button className="btn btn-ghost" onClick={() => reanalyze(detail.id)}><Sparkles size={14} /> Пересобрать</button>}
                </div>
              </div>

              {tab === "protocol" ? (
                detail.analysis ? <Protocol a={detail.analysis} /> :
                  <div className="text-[13px] py-6 text-center" style={{ color: "var(--muted)" }}>
                    {isBusy(detail.status) ? "Протокол формируется…" : "Протокол не создавался для этой записи."}</div>
              ) : (
                transcript ? <pre className="text-[12.5px] leading-relaxed whitespace-pre-wrap font-sans">{transcript}</pre> :
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
