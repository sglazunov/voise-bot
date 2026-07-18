import { useEffect, useState } from "react";
import { CalendarClock, RefreshCw, Filter, Workflow, Send, ChevronDown } from "lucide-react";
import { Page } from "../components/Layout";
import { Card, Switch, Select, useToast } from "../components/ui";
import { useSettings } from "../lib/useSettings";
import { api } from "../lib/api";
import { Status } from "../lib/format";

const DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

/* last_poll comes as a unix timestamp — show it as "N с назад", not a raw float. */
function fmtPoll(ts?: number): string {
  if (!ts) return "ещё не было";
  const sec = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (sec < 60) return `${sec} с назад`;
  if (sec < 3600) return `${Math.round(sec / 60)} мин назад`;
  return new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function Toggle({ title, sub, on, onChange }: any) {
  return (
    <div className="glass2 rounded-2xl px-4 py-3 flex items-center justify-between gap-3">
      <div><div className="text-[13.5px] font-semibold">{title}</div>
        {sub && <div className="text-[11.5px] mt-0.5" style={{ color: "var(--muted)" }}>{sub}</div>}</div>
      <Switch on={!!on} onChange={onChange} />
    </div>
  );
}

export default function Scheduler() {
  const { s, set, save } = useSettings();
  const [st, setSt] = useState<Status | null>(null);
  const [engines, setEngines] = useState<{ value: string; label: string }[]>([]);
  const toast = useToast();
  const load = () => api.get("/api/automation/scheduler/status").then(setSt).catch(() => {});
  useEffect(() => { load(); const t = setInterval(load, 6000); return () => clearInterval(t); }, []);
  useEffect(() => { api.get("/api/providers").then((d) => setEngines(d.engines || [])).catch(() => {}); }, []);

  const days: number[] = Array.isArray(s.rec_days) ? s.rec_days : [];
  const toggleDay = (i: number) => set("rec_days", days.includes(i) ? days.filter((d) => d !== i) : [...days, i].sort());

  // The record-window filters are collapsed by default: in practice the Weeek
  // «Запись встречи» checkbox decides, and it OUTRANKS them (see _passes_filter),
  // so the block only mattered for tasks without that field.
  const [winOpen, setWinOpen] = useState(false);
  const winSummary = () => {
    const p: string[] = [];
    if (s.rec_time_from || s.rec_time_to) p.push(`${s.rec_time_from || "00:00"}–${s.rec_time_to || "23:59"}`);
    if (days.length) p.push(days.map((i) => DAYS[i]).join(","));
    if (s.rec_include) p.push(`вкл: ${s.rec_include}`);
    if (s.rec_exclude) p.push(`искл: ${s.rec_exclude}`);
    if (!s.rec_default_on) p.push("только выбранные");
    return p.length ? p.join(" · ") : "не заданы — решает галочка в Weeek";
  };

  async function pollNow() {
    try { const r = await api.post("/api/automation/scheduler/poll-now"); toast(r.detail || "Опрос выполнен"); load(); }
    catch (e: any) { toast(e.message, true); }
  }
  async function onSave() {
    try {
      await save({
        poll_interval_sec: s.poll_interval_sec, lookahead_min: s.lookahead_min,
        rec_time_from: s.rec_time_from || null, rec_time_to: s.rec_time_to || null, rec_days: days,
        rec_default_on: !!s.rec_default_on, rec_include: s.rec_include || null, rec_exclude: s.rec_exclude || null,
        do_transcribe: !!s.do_transcribe, do_protocol: !!s.do_protocol, ocr_screen: !!s.ocr_screen,
        identify_speakers: !!s.identify_speakers, post_back_to_weeek: !!s.post_back_to_weeek,
        weeek_set_video_field: !!s.weeek_set_video_field, upload_protocol: !!s.upload_protocol,
        weeek_set_protocol_field: !!s.weeek_set_protocol_field,
        strict_verify: s.strict_verify !== false,
        live_transcribe: s.live_transcribe !== false,
        analyze_provider: s.analyze_provider || "auto",
      });
      toast("Планировщик сохранён");
    } catch (e: any) { toast(e.message, true); }
  }

  return (
    <Page title="Планировщик" subtitle="Когда система опрашивает Weeek и что делает после записи"
      actions={<button className="btn btn-ghost" onClick={pollNow}><RefreshCw size={15} /> Опросить сейчас</button>}>
      <Card className="mb-3.5">
        <div className="flex items-center gap-3">
          <div className="grid place-items-center rounded-xl" style={{ width: 40, height: 40, background: st?.running ? "rgba(52,211,153,.16)" : "rgba(120,140,150,.16)" }}>
            <CalendarClock size={19} color={st?.running ? "#5eead4" : "var(--muted)"} /></div>
          <div><div className="font-bold text-[15px]">{st?.running ? "Планировщик работает" : "Планировщик остановлен"}</div>
            <div className="text-[12px]" style={{ color: "var(--muted)" }}>
              Последний опрос: {fmtPoll(st?.last_poll)} · параллельно {st?.active ?? 0}/{st?.max_parallel ?? 1}</div></div>
          <span className="chip ml-auto" style={st?.enabled
            ? { color: "var(--accent-ink)", background: "linear-gradient(90deg,var(--accent),var(--accent2))" }
            : { color: "var(--muted)" }}>{st?.enabled ? "автоматика вкл" : "выкл"}</span>
        </div>
        <div className="grid md:grid-cols-2 gap-3 mt-4">
          <div><label className="lbl">Интервал опроса, сек</label>
            <input className="field" type="number" value={s.poll_interval_sec ?? ""} onChange={(e) => set("poll_interval_sec", Number(e.target.value))} /></div>
          <div><label className="lbl">Смотреть вперёд, мин</label>
            <input className="field" type="number" value={s.lookahead_min ?? ""} onChange={(e) => set("lookahead_min", Number(e.target.value))} /></div>
        </div>
      </Card>

      <Card className="mb-3.5">
        <button type="button" onClick={() => setWinOpen((o) => !o)}
          className="w-full flex items-center gap-2.5 text-left"
          style={{ cursor: "pointer", background: "none", border: 0, padding: 0 }}>
          <Filter size={17} color="var(--accent)" className="flex-none" />
          <div className="min-w-0 flex-1">
            <div className="font-bold text-[15px]">Окно записи</div>
            <div className="text-[11.5px] truncate" style={{ color: "var(--muted)" }}>
              Доп. фильтры · {winSummary()}</div>
          </div>
          <ChevronDown size={16} color="var(--muted)"
            style={{ flex: "0 0 auto", transition: ".18s", transform: winOpen ? "rotate(180deg)" : "none" }} />
        </button>

        {winOpen && (
          <div className="mt-3">
            <div className="glass2 rounded-xl p-2.5 mb-3 text-[12px] leading-relaxed" style={{ color: "var(--muted)" }}>
              Обычно это не нужно: галочка <b style={{ color: "var(--txt)" }}>«{s.weeek_record_field || "Запись встречи"}»</b> в
              задаче Weeek <b style={{ color: "var(--txt)" }}>важнее</b> этих фильтров. Они срабатывают только для задач,
              где такого поля нет. Ручной выбор встречи главнее всего.
            </div>
            <div className="grid md:grid-cols-2 gap-3">
              <div><label className="lbl">С</label>
                <input className="field" type="time" value={s.rec_time_from || ""} onChange={(e) => set("rec_time_from", e.target.value)} /></div>
              <div><label className="lbl">До</label>
                <input className="field" type="time" value={s.rec_time_to || ""} onChange={(e) => set("rec_time_to", e.target.value)} /></div>
            </div>
            <label className="lbl mt-3">Дни недели</label>
            <div className="flex gap-1.5 flex-wrap">
              {DAYS.map((d, i) => (
                <button key={d} onClick={() => toggleDay(i)}
                  className="rounded-full px-3.5 py-1.5 text-[12.5px] font-semibold transition"
                  style={days.includes(i)
                    ? { background: "linear-gradient(90deg,var(--accent),var(--accent2))", color: "var(--accent-ink)" }
                    : { background: "rgba(120,180,190,.08)", color: "var(--muted)" }}>{d}</button>
              ))}
            </div>
            <div className="grid md:grid-cols-2 gap-3 mt-3">
              <div><label className="lbl">Фильтр названий (включить)</label>
                <input className="field" value={s.rec_include || ""} onChange={(e) => set("rec_include", e.target.value)} placeholder="ключевые слова через запятую" /></div>
              <div><label className="lbl">Фильтр названий (исключить)</label>
                <input className="field" value={s.rec_exclude || ""} onChange={(e) => set("rec_exclude", e.target.value)} placeholder="напр. личное, 1:1" /></div>
            </div>
            <div className="mt-3"><Toggle title="Писать по умолчанию" sub="новые встречи включены на запись без ручного решения"
              on={s.rec_default_on} onChange={() => set("rec_default_on", !s.rec_default_on)} /></div>
          </div>
        )}
      </Card>

      <Card>
        <div className="flex items-center gap-2 mb-3"><Workflow size={17} color="var(--accent)" />
          <div className="font-bold text-[15px]">После записи</div></div>
        <div className="grid md:grid-cols-2 gap-2.5">
          <Toggle title="Распознавание речи" sub="faster-whisper → текст" on={s.do_transcribe} onChange={() => set("do_transcribe", !s.do_transcribe)} />
          <Toggle title="Формировать протокол" sub="LLM → структурный протокол Word" on={s.do_protocol} onChange={() => set("do_protocol", !s.do_protocol)} />
          <Toggle title="OCR экрана" sub="распознавать текст со слайдов" on={s.ocr_screen} onChange={() => set("ocr_screen", !s.ocr_screen)} />
          <Toggle title="Определять спикеров" sub="кто что говорил" on={s.identify_speakers} onChange={() => set("identify_speakers", !s.identify_speakers)} />
          <Toggle title="Строгая проверка" sub="пункт без дословной цитаты помечается «⚠ проверьте»"
            on={s.strict_verify !== false} onChange={() => set("strict_verify", s.strict_verify === false)} />
          <Toggle title="Live-расшифровка" sub="текст встречи появляется каждые ~5 минут прямо во время записи"
            on={s.live_transcribe !== false} onChange={() => set("live_transcribe", s.live_transcribe === false)} />
          <Toggle title="Комментарий в Weeek" sub="постить ссылки/итоги в задачу" on={s.post_back_to_weeek} onChange={() => set("post_back_to_weeek", !s.post_back_to_weeek)} />
          <Toggle title="Поле «Видео» в Weeek" on={s.weeek_set_video_field} onChange={() => set("weeek_set_video_field", !s.weeek_set_video_field)} />
          <Toggle title="Выгружать протокол" sub="в облако" on={s.upload_protocol} onChange={() => set("upload_protocol", !s.upload_protocol)} />
          <Toggle title="Поле «Протокол» в Weeek" on={s.weeek_set_protocol_field} onChange={() => set("weeek_set_protocol_field", !s.weeek_set_protocol_field)} />
        </div>
        <div className="mt-3">
          <label className="lbl">Движок протокола (нейросеть · модель)</label>
          <Select value={s.analyze_provider || "auto"} onChange={(v) => set("analyze_provider", v)}
            options={[{ value: "auto", label: "Авто (бесплатные/локальные — в первую очередь)" },
                      ...engines.map((e) => ({ value: e.value, label: e.label }))]} />
          <div className="text-[11.5px] mt-1" style={{ color: "var(--muted)" }}>
            Ключи и список моделей — на вкладке «Нейросети».</div>
        </div>
      </Card>

      <div className="mt-3.5 flex gap-2.5">
        <button className="btn btn-primary" onClick={onSave}><Send size={15} /> Сохранить планировщик</button>
      </div>
    </Page>
  );
}
