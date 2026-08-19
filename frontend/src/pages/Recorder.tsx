import { useState } from "react";
import { Bot, Play, MessageSquareOff, Users, Timer } from "lucide-react";
import { Page } from "../components/Layout";
import { Card, Switch, useToast } from "../components/ui";
import { useSettings } from "../lib/useSettings";
import { api } from "../lib/api";

function Num({ label, k, s, set, hint }: any) {
  return (
    <div><label className="lbl">{label}</label>
      <input className="field" type="number" value={s[k] ?? ""} onChange={(e) => set(k, e.target.value === "" ? null : Number(e.target.value))} />
      {hint && <div className="text-[11.5px] mt-1" style={{ color: "var(--muted)" }}>{hint}</div>}</div>
  );
}

export default function Recorder() {
  const { s, set, save } = useSettings();
  const toast = useToast();
  const [logs, setLogs] = useState<string[] | null>(null);
  const [testing, setTesting] = useState(false);

  async function onSave() {
    try {
      await save({
        bot_join_name: s.bot_join_name, headless: !!s.headless, auth_mode: s.auth_mode,
        join_timeout_sec: s.join_timeout_sec, end_when_alone_sec: s.end_when_alone_sec,
        min_participants: s.min_participants, max_meeting_min: s.max_meeting_min,
        chat_stop_word: s.chat_stop_word, capture_video: !!s.capture_video,
        audio_device: s.audio_device || null, ffmpeg_path: s.ffmpeg_path || null,
      });
      toast("Настройки бота сохранены");
    } catch (e: any) { toast(e.message, true); }
  }
  // Кнопка звала /recorder/test-join — такого маршрута нет, тест всегда падал
  // с 404. Настоящий эндпоинт синхронный: он пишет несколько секунд и отдаёт
  // подробный лог захода. Лог показываем — ради него тест и запускают.
  async function testJoin() {
    const url = prompt("Ссылка на Телемост для теста подключения:");
    if (!url) return;
    setTesting(true); setLogs(null);
    try {
      // 60 с — чтобы успеть написать в чат стоп-слово и увидеть в логе,
      // разглядел ли его бот.
      const r = await api.post("/api/automation/recorder/test", { url, seconds: 60 });
      setLogs(r.logs?.length ? r.logs : ["Лог пуст — бот не дошёл до записи."]);
      toast(r.ok ? "Тест завершён" : (r.error || "Тест не удался"), !r.ok);
    } catch (e: any) { toast(e.message, true); }
    finally { setTesting(false); }
  }

  return (
    <Page title="Бот-рекордер" subtitle="Как бот подключается к встрече и когда её покидает"
      actions={<button className="btn btn-ghost" onClick={testJoin} disabled={testing}>
        <Play size={15} /> {testing ? "Идёт тест…" : "Тест подключения"}</button>}>
      <div className="grid lg:grid-cols-2 gap-3.5">
        <Card>
          <div className="flex items-center gap-3 mb-3">
            <div className="grid place-items-center rounded-xl" style={{ width: 40, height: 40, background: "rgba(45,212,191,.13)" }}>
              <Bot size={19} color="var(--accent)" /></div>
            <div><div className="font-bold text-[15px]">Личность бота</div>
              <div className="text-[12px]" style={{ color: "var(--muted)" }}>Под каким именем заходит на встречу</div></div>
          </div>
          <label className="lbl">Имя бота на встрече</label>
          <input className="field" value={s.bot_join_name || ""} onChange={(e) => set("bot_join_name", e.target.value)} placeholder="Ассистент (запись)" />
          <div className="grid grid-cols-2 gap-3 mt-3">
            <div className="glass2 rounded-2xl px-3.5 py-3 flex items-center justify-between">
              <div><div className="text-[13px] font-semibold">Headless</div>
                <div className="text-[11px]" style={{ color: "var(--muted)" }}>Без окна (Xvfb)</div></div>
              <Switch on={!!s.headless} onChange={() => set("headless", !s.headless)} /></div>
            <div className="glass2 rounded-2xl px-3.5 py-3 flex items-center justify-between">
              <div><div className="text-[13px] font-semibold">Захват видео</div>
                <div className="text-[11px]" style={{ color: "var(--muted)" }}>mp4, не только звук</div></div>
              <Switch on={!!s.capture_video} onChange={() => set("capture_video", !s.capture_video)} /></div>
          </div>
        </Card>

        <Card>
          <div className="flex items-center gap-2 mb-3"><MessageSquareOff size={17} color="var(--accent)" />
            <div className="font-bold text-[15px]">Выход из встречи</div></div>
          <label className="lbl">Стоп-слово в чате</label>
          <input className="field" value={s.chat_stop_word || ""} onChange={(e) => set("chat_stop_word", e.target.value)} placeholder="стоп" />
          {/* Гостю Телемост чат не показывает — панель у бота пустая. Молчать
              об этом нельзя: настройка выглядит рабочей, а команда не доходит. */}
          {s.chat_stop_word && (s.auth_mode || "guest") !== "profile" ? (
            <div className="text-[11.5px] mt-1" style={{ color: "#fbbf24" }}>
              В режиме входа «Гость» это НЕ работает: Телемост не показывает чат
              участникам без аккаунта, у бота он пустой. Останавливайте кнопкой
              «Стоп» на карточке встречи — либо переключите вход на
              «Авторизованный».
            </div>
          ) : (
            <div className="text-[11.5px] mt-1" style={{ color: "var(--muted)" }}>Бот следит за чатом и выходит, увидев это слово.</div>
          )}
          <div className="grid grid-cols-2 gap-3 mt-3">
            <Num label="Один в комнате, сек" k="end_when_alone_sec" s={s} set={set} hint="выйти, если остался один" />
            <Num label="Мин. участников" k="min_participants" s={s} set={set} />
          </div>
        </Card>

        <Card>
          <div className="flex items-center gap-2 mb-3"><Timer size={17} color="var(--accent)" />
            <div className="font-bold text-[15px]">Тайминги</div></div>
          <div className="grid grid-cols-2 gap-3">
            <Num label="Ожидание входа, сек" k="join_timeout_sec" s={s} set={set} />
            <Num label="Макс. длина, мин" k="max_meeting_min" s={s} set={set} />
          </div>
        </Card>

        <Card>
          <div className="flex items-center gap-2 mb-3"><Users size={17} color="var(--accent)" />
            <div className="font-bold text-[15px]">Захват звука (Linux)</div></div>
          <label className="lbl">Аудио-устройство (PulseAudio)</label>
          <input className="field" value={s.audio_device || ""} onChange={(e) => set("audio_device", e.target.value)} placeholder="default.monitor" />
          <label className="lbl mt-3">Путь к ffmpeg</label>
          <input className="field" value={s.ffmpeg_path || ""} onChange={(e) => set("ffmpeg_path", e.target.value)} placeholder="/usr/bin/ffmpeg" />
        </Card>
      </div>
      {logs && (
        <Card className="mt-3.5">
          <div className="font-bold text-[15px] mb-2">Лог теста</div>
          <div className="text-[11.5px] mb-2" style={{ color: "var(--muted)" }}>
            Строки со словом «Чат» показывают, открыл ли бот панель чата и сколько
            стоп-слов он в ней видит.
          </div>
          <pre className="glass2 rounded-2xl p-3 text-[11.5px] whitespace-pre-wrap"
            style={{ maxHeight: 320, overflow: "auto" }}>{logs.join("\n")}</pre>
        </Card>
      )}
      <div className="mt-3.5"><button className="btn btn-primary" onClick={onSave}>Сохранить настройки бота</button></div>
    </Page>
  );
}
