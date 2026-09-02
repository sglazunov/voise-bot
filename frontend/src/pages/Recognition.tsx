import { useEffect, useRef, useState } from "react";
import {
  Mic, UploadCloud, FileAudio, Sparkles, Users, Monitor, Loader2, RotateCcw,
  Download, FileText, X, CalendarDays,
} from "lucide-react";
import { Page } from "../components/Layout";
import { Card, Ellipsis, Select, useToast } from "../components/ui";
import { api } from "../lib/api";
import { EngineSelect, Engine } from "../components/EngineSelect";
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


// Подкомпоненты вынесены в ./recognition: страница была почти тысяча строк, из
// них треть — календарь, показ расшифровки и протокол, самостоятельные и к
// логике загрузки отношения не имеющие.
import { HistoryCalendar, dayKey, fmtDayLabel } from "./recognition/HistoryCalendar";
import { LiveTranscript, SegmentView } from "./recognition/Transcript";
import { Protocol, ProtocolEditor, AskBlock } from "./recognition/Protocol";

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
  const [docxProv, setDocxProv] = useState("");  // какой движок скачиваем
  const [busy, setBusy] = useState(false);
  const [prog, setProg] = useState(0);
  const [engines, setEngines] = useState<Engine[]>([]);
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
  // Скачана ли выбранная модель. Спрашиваем один раз при смене модели, а
  // переспрашиваем только пока идёт загрузка — ради процентов.
  const [model, setModel] = useState<any>(null);
  const downloading = model?.download?.state === "running";
  useEffect(() => {
    let stop = false;
    const ask = () => api.get(`/api/model/status?name=${encodeURIComponent(opts.model || "")}`)
      .then((d) => { if (!stop) setModel(d); }).catch(() => {});
    ask();
    if (!downloading) return () => { stop = true; };
    const t = setInterval(ask, 3000);
    return () => { stop = true; clearInterval(t); };
  }, [opts.model, downloading]);
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
  // Список для показа. По умолчанию — ТОЛЬКО сегодняшние встречи: история за
  // месяц растягивала страницу на десятки карточек, а нужны в 9 случаях из 10
  // сегодняшние. Старые — по кнопке «Показать ещё», по 10 за нажатие.
  // Выбранный в календаре день показывается целиком.
  const HISTORY_STEP = 10;
  const [extra, setExtra] = useState(0);   // сколько старых встреч раскрыто
  const todayKey = dayKey(Date.now());
  const todayJobs = jobs.filter((j: any) => dayKey((j.created_at || 0) * 1000) === todayKey);
  const olderJobs = jobs.filter((j: any) => dayKey((j.created_at || 0) * 1000) !== todayKey);
  const shownJobs = dayFilter
    ? jobs.filter((j: any) => dayKey((j.created_at || 0) * 1000) === dayFilter)
    : [...todayJobs, ...olderJobs.slice(0, extra)];
  const moreLeft = dayFilter ? 0 : Math.max(0, olderJobs.length - extra);
  useEffect(() => { loadJobs(); const t = setInterval(loadJobs, 4000); return () => clearInterval(t); }, []);
  useEffect(() => { api.get("/api/providers").then((d) => setEngines(d.engines || [])).catch(() => {}); }, []);

  // Смена карточки — чистим тексты предыдущей встречи. ОТДЕЛЬНЫМ эффектом и
  // только по `sel`: у опроса ниже в зависимостях есть detail?.status, и сброс
  // detail внутри него замыкал круг (обнулили → зависимость изменилась →
  // эффект перезапустился → обнулили), из-за чего расшифровка стиралась сразу
  // после загрузки и страница молотила запросы без остановки.
  // Для какой задачи расшифровка уже скачана — чтобы не тянуть её на каждом
  // тике опроса.
  const fetchedFor = useRef<string | null>(null);
  useEffect(() => {
    setTranscript(""); setSegments(null); setLive(null); setDocxProv("");
    fetchedFor.current = null;
  }, [sel]);

  // Load selected job detail + transcript, poll while busy.
  // While it's working we also pull /partial — the LIVE stage of recognition and
  // protocol building (stage + streamed characters), so the user sees progress.
  useEffect(() => {
    if (!sel) { setDetail(null); return; }
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
        // Расшифровка готова уже к началу сборки протокола — показываем её и в
        // состоянии «analyzing». Качаем ОДИН раз: опрос идёт каждые 2 секунды,
        // и раньше на каждом тике заново тянулись оба формата целиком (json и
        // txt) с полной перерисовкой — на часовой встрече это мегабайты в
        // минуту и подтормаживающий интерфейс.
        if ((j.status === "done" || j.status === "cancelled" || j.status === "analyzing")
            && fetchedFor.current !== sel) {
          fetchedFor.current = sel;
          api.get(`/api/jobs/${sel}/result?format=json`)
            .then((d) => { if (alive && d?.segments?.length) setSegments(d.segments); })
            .catch(() => { if (fetchedFor.current === sel) fetchedFor.current = null; });
          api.text(`/api/jobs/${sel}/result?format=txt`)
            .then((t) => alive && setTranscript(t))
            .catch(() => { if (fetchedFor.current === sel) fetchedFor.current = null; });
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
  async function downloadModel() {
    try { setModel(await api.post(`/api/model/download?name=${encodeURIComponent(opts.model)}`)); toast("Скачиваю модель…"); }
    catch (e: any) { toast(e.message, true); }
  }
  // Пауза/продолжение. Воркер проверяет флаг между сегментами, так что счёт
  // замирает и уже распознанное не теряется — можно освободить процессор под
  // срочную задачу и вернуться. Маршруты были, кнопки к ним не существовало.
  async function pauseJob(id: string, resume: boolean) {
    try {
      await api.post(`/api/jobs/${id}/${resume ? "resume" : "pause"}`);
      toast(resume ? "Продолжаю" : "Пауза");
      loadJobs();
    } catch (e: any) { toast(e.message, true); }
  }
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
            {/* Не скачанная модель тянется прямо посреди задачи: у large это
                несколько гигабайт, и человек видит «распознаётся» без движения.
                Проверка и загрузка заранее были на сервере, кнопки — не было. */}
            {model && model.downloaded === false && (
              <div className="glass2 rounded-2xl p-3 mt-2 flex items-center gap-3 flex-wrap text-[12px]">
                <span className="min-w-0 flex-1" style={{ color: "var(--warn)" }}>
                  {model.download?.state === "running"
                    ? `Скачиваю «${model.name}»… ${model.download.percent ?? 0}%`
                    : `Модель «${model.name}» ещё не скачана${model.size_hint ? ` (${model.size_hint})` : ""} — иначе загрузка пойдёт во время распознавания.`}
                </span>
                {model.download?.state !== "running" && (
                  <button className="btn btn-ghost flex-none" onClick={downloadModel}>Скачать заранее</button>
                )}
              </div>
            )}
            {recommend && <div className="text-[11px] mt-1" style={{ color: "var(--muted)" }}>💡 {recommend}</div>}
            <label className="lbl mt-3">Движок протокола</label>
            {/* Два поля: поставщик и его модель. Одним списком это была сотня
                строк «Свой ключ · …», где не отличить OpenRouter от Yandex
                Cloud. */}
            <EngineSelect engines={engines} value={opts.provider}
              onChange={(v) => setOpts({ ...opts, provider: v })} />
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
                onPick={(k) => { setDayFilter(k); setSearchQ(""); setExtra(0); }} />
            )}
            {/* Прокрутка — внутри блока истории, а не всей страницы: правая
                колонка с протоколом остаётся на месте. */}
            <div className="pr-1" style={{ maxHeight: "min(640px, 70vh)", overflowY: "auto" }}>
            {searchQ.trim().length >= 2 ? (
              searchRes.length ? searchRes.map((r) => (
                <button key={r.job_id} onClick={() => { setSel(r.job_id); setTab("transcript"); }}
                  className="w-full glass2 rounded-2xl px-3.5 py-3 mb-2 text-left transition"
                  style={sel === r.job_id ? { borderColor: "var(--accent)" } : {}}>
                  <Ellipsis as="div" className="font-semibold text-[13px]">{r.title}</Ellipsis>
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
                  <Ellipsis as="div" className="font-semibold text-[13px]">{j.filename}</Ellipsis>
                  <div className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>{RU_STATUS[j.status] || j.status} · {fmtDateTime(new Date(j.created_at * 1000).toISOString())}</div></div>
                {isBusy(j.status) && <span className="text-[11px] font-semibold" style={{ color: "var(--accent)" }}>{Math.round((j.progress || 0) * 100)}%</span>}
              </button>
            )) : (
              <div className="text-[13px] py-2" style={{ color: "var(--muted)" }}>
                {dayFilter ? "В этот день встреч не было."
                  : jobs.length ? "Сегодня встреч ещё не было." : "Пока нет задач."}</div>
            )}
            {searchQ.trim().length < 2 && moreLeft > 0 && (
              <button className="btn btn-ghost w-full mt-1" onClick={() => setExtra((n) => n + HISTORY_STEP)}
                title={`Показать ещё ${Math.min(HISTORY_STEP, moreLeft)} из ${moreLeft} прошлых встреч`}>
                Показать ещё {Math.min(HISTORY_STEP, moreLeft)} · осталось {moreLeft}
              </button>
            )}
            </div>
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
                  <Ellipsis as="div" className="font-bold text-[15px]">{detail.filename}</Ellipsis>
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
                    {/* После расшифровки полоса уже на 100%, а работы ещё на
                        десятки минут: разметка говорящих, чтение экрана. Без
                        этой строки выглядит как зависшая задача. */}
                    {detail.status === "running" && detail.stage ? (
                      <div className="text-[12px] mt-0.5" style={{ color: "var(--muted)" }}>
                        {detail.stage}</div>
                    ) : detail.status === "running" && live?.segments?.length ? (
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
                  <div className="flex flex-col gap-1.5 flex-none self-start">
                    <button className="btn-ghost" onClick={() => pauseJob(detail.id, detail.status === "paused")}>
                      {detail.status === "paused" ? "▶ Продолжить" : "⏸ Пауза"}</button>
                    <button className="btn-ghost" onClick={() => stopJob(detail.id)}>
                      ■ Стоп</button>
                  </div>
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
                  {/* Движок — прямо на кнопке. Без него нельзя было понять, чей
                      документ скачался: запрос без указания движка отдаёт
                      ПОСЛЕДНИЙ собранный, и нажатие сразу после «Пересобрать»
                      молча возвращало предыдущую версию. При нескольких
                      версиях выбор ещё и нужен сам по себе — чтобы сравнивать
                      движки на одной встрече. */}
                  {detail.status === "done" && detail.docx_providers?.length ? (
                    detail.docx_providers.length > 1 ? (
                      <>
                        <Select className="w-[150px]" value={docxProv || detail.docx_providers[detail.docx_providers.length - 1]}
                          onChange={setDocxProv}
                          options={detail.docx_providers.map((p: string) => ({ value: p, label: p }))} />
                        <a className="btn btn-ghost"
                          href={`/api/jobs/${detail.id}/result?format=docx&provider=${encodeURIComponent(docxProv || detail.docx_providers[detail.docx_providers.length - 1])}`}>
                          <Download size={14} /> Word</a>
                      </>
                    ) : (
                      <a className="btn btn-ghost"
                        href={`/api/jobs/${detail.id}/result?format=docx&provider=${encodeURIComponent(detail.docx_providers[0])}`}>
                        <Download size={14} /> Word · {detail.docx_providers[0]}</a>
                    )
                  ) : null}
                  {detail.status === "analyzing" && detail.docx_providers?.length ? (
                    <span className="text-[11.5px] self-center" style={{ color: "var(--muted)" }}>
                      собирается новая версия — прежнюю скачаете после</span>
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
                      <Protocol a={detail.analysis} jobId={detail.id} />
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
