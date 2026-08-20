import { useEffect, useRef, useState } from "react";
import { Bot, Play, MessageSquareOff, Users, Timer, LogIn, Volume2 } from "lucide-react";
import { Page } from "../components/Layout";
import { Card, Switch, Select, Modal, useToast } from "../components/ui";
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
  const { s, set, save , ready } = useSettings();
  const toast = useToast();
  const [logs, setLogs] = useState<string[] | null>(null);
  // Вход в Яндекс прямо из интерфейса: браузер работает на сервере, сюда
  // приходит его экран, отсюда уходят клики и клавиши. Пароль вводить не
  // обязательно — на странице Яндекса есть вход по QR, тогда он остаётся на
  // телефоне и через сервер не проходит.
  const [loginOpen, setLoginOpen] = useState(false);
  const [screen, setScreen] = useState("");
  // Размер окна браузера НА СЕРВЕРЕ. Считать координаты клика от naturalWidth
  // ненадёжно: кадр подменяется раз в секунду, и в момент подгрузки нового
  // naturalWidth равен нулю — клик уходил бы в угол экрана.
  const [screenWH, setScreenWH] = useState<[number, number]>([1600, 900]);
  const [authState, setAuthState] = useState<any>(null);
  const [authChecking, setAuthChecking] = useState(false);
  const imgRef = useRef<HTMLImageElement | null>(null);

  async function openLogin() {
    try {
      const r = await api.post("/api/automation/recorder/login/open");
      if (r?.width && r?.height) setScreenWH([r.width, r.height]);
      setLoginOpen(true);
    } catch (e: any) { toast(e.message, true); }
  }
  async function checkLogin() {
    setAuthChecking(true);
    try { setAuthState(await api.get("/api/automation/recorder/login-status")); }
    catch (e: any) { toast(e.message, true); }
    finally { setAuthChecking(false); }
  }
  async function closeLogin() {
    setLoginOpen(false); setScreen("");
    try { await api.post("/api/automation/recorder/login/close"); } catch { /* уже закрыт */ }
    checkLogin();
  }
  // Пока окно открыто — тянем картинку экрана. Метка времени в адресе, иначе
  // браузер отдаёт закэшированный кадр и экран выглядит замершим.
  useEffect(() => {
    if (!loginOpen) return;
    const t = setInterval(() => setScreen(`/api/automation/recorder/login/screen?t=${Date.now()}`), 1200);
    return () => clearInterval(t);
  }, [loginOpen]);

  async function act(body: any) {
    try { await api.post("/api/automation/recorder/login/action", body); }
    catch (e: any) { toast(e.message, true); }
  }
  // Клик по картинке → клик в браузере. Пересчитываем координаты, потому что
  // картинка на экране масштабируется под ширину модального окна.
  function clickScreen(e: React.MouseEvent<HTMLImageElement>) {
    const img = imgRef.current;
    if (!img) return;
    const r = img.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const [w, h] = screenWH;
    act({ kind: "click", x: ((e.clientX - r.left) / r.width) * w,
          y: ((e.clientY - r.top) / r.height) * h });
  }
  const [testing, setTesting] = useState(false);
  // Проверка звука: эндпоинт был, но кнопки к нему не существовало.
  const [audio, setAudio] = useState<any>(null);
  const [audioBusy, setAudioBusy] = useState(false);
  async function audioTest() {
    setAudioBusy(true);
    try { setAudio(await api.get("/api/automation/recorder/audio-test")); }
    catch (e: any) { toast(e.message, true); }
    finally { setAudioBusy(false); }
  }

  async function onSave() {
    try {
      await save({
        bot_join_name: s.bot_join_name, auth_mode: s.auth_mode,
        join_timeout_sec: s.join_timeout_sec, end_when_alone_sec: s.end_when_alone_sec,
        min_participants: s.min_participants, max_meeting_min: s.max_meeting_min,
        chat_stop_word: s.chat_stop_word, capture_video: !!s.capture_video,
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
          {/* Переключателя «Headless» здесь больше нет: записывающий браузер
              всегда запускается с окном внутри Xvfb (иначе нечего захватывать),
              настройка не читалась никем и вводила в заблуждение. */}
          <div className="mt-3">
            <div className="glass2 rounded-2xl px-3.5 py-3 flex items-center justify-between">
              <div><div className="text-[13px] font-semibold">Захват видео</div>
                <div className="text-[11px]" style={{ color: "var(--muted)" }}>mp4, не только звук</div></div>
              <Switch on={!!s.capture_video} onChange={() => set("capture_video", !s.capture_video)} /></div>
          </div>

          {/* Режим входа. Гостю Телемост не показывает чат, поэтому стоп-слово
              работает только под аккаунтом. Вход делается тут же: браузер живёт
              на сервере, а экран отдаётся сюда — раньше он открывался внутрь
              Xvfb и был недоступен удалённо. */}
          <label className="lbl mt-3">Как бот заходит на встречу</label>
          <Select value={s.auth_mode || "guest"} onChange={(v) => set("auth_mode", v)}
            options={[{ value: "guest", label: "Гость — по ссылке, без аккаунта" },
                      { value: "profile", label: "Авторизованный — под аккаунтом Яндекса" }]} />
          <div className="text-[11.5px] mt-1" style={{ color: "var(--muted)" }}>
            Под аккаунтом бот видит чат встречи — тогда работает стоп-слово.
            Гостю Телемост чат не показывает.
          </div>
          <div className="flex gap-2 mt-2 flex-wrap items-center">
            <button className="btn btn-ghost" onClick={openLogin}>
              <LogIn size={15} /> Войти в Яндекс</button>
            <button className="btn btn-ghost" onClick={checkLogin} disabled={authChecking}>
              {authChecking ? "Проверяю…" : "Проверить вход"}</button>
            {authState && <span className="text-[11.5px]" style={{
              color: authState.logged_in ? "#5eead4" : authState.logged_in === false ? "#fbbf24" : "var(--muted)" }}>
              {authState.logged_in ? "Вход выполнен ✓" : authState.detail}</span>}
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

        {/* Полей «аудио-устройство» и «путь к ffmpeg» здесь больше нет: запись
            всегда идёт из монитора своего слота (meet0…meet3.monitor), а ffmpeg
            берётся из образа. Вместо настроек — проверка, что звук доходит. */}
        <Card>
          <div className="flex items-center gap-2 mb-3"><Users size={17} color="var(--accent)" />
            <div className="font-bold text-[15px]">Захват звука</div></div>
          <div className="text-[12px] mb-3" style={{ color: "var(--muted)" }}>
            Звук встречи пишется из PulseAudio-монитора слота записи. Проверка
            слушает его несколько секунд и показывает уровень — так видно, что
            запись не выйдет немой.
          </div>
          <div className="flex gap-2.5 items-center flex-wrap">
            <button className="btn btn-ghost" onClick={audioTest} disabled={audioBusy}>
              <Volume2 size={15} /> {audioBusy ? "Слушаю…" : "Проверить звук"}</button>
            {audio && <span className="text-[11.5px]" style={{
              color: audio.ok === false ? "#fbbf24" : audio.has_sound ? "#5eead4" : "#fbbf24" }}>
              {audio.ok === false ? (audio.error || "Проверка не удалась")
                : audio.has_sound ? `Звук есть ✓ (${audio.mean_db} дБ, ${audio.device})`
                : `Тишина (${audio.mean_db ?? "—"} дБ, ${audio.device}) — на встрече сейчас никто не говорит либо звук не доходит`}</span>}
          </div>
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
      <Modal open={loginOpen} onClose={closeLogin} wide
        title={<span className="flex items-center gap-2"><LogIn size={16} color="var(--accent)" /> Вход в Яндекс для бота</span>}>
        <div className="text-[12.5px] mb-2" style={{ color: "var(--muted)" }}>
          Это браузер на сервере — кликайте прямо по картинке, набирайте текст в
          поле ниже. <b>Надёжнее войти по QR-коду</b>: выберите его на странице
          Яндекса и отсканируйте телефоном — тогда пароль останется на телефоне
          и через сервер не пойдёт.
        </div>
        {screen ? (
          // Картинку тянем на всю доступную высоту: по ней надо попадать
          // кликами, а координаты пересчитываются по фактическому размеру,
          // так что масштаб на точность не влияет.
          <img ref={imgRef} src={screen} onClick={clickScreen} alt="экран браузера"
            className="rounded-xl cursor-pointer block mx-auto"
            style={{ border: "1px solid var(--line)", maxWidth: "100%",
                     maxHeight: "calc(96vh - 210px)" }} />
        ) : (
          <div className="glass2 rounded-xl p-8 text-center text-[12.5px]"
            style={{ color: "var(--muted)", minHeight: "calc(96vh - 210px)",
                     display: "grid", placeItems: "center" }}>
            Браузер запускается…</div>
        )}
        <div className="flex gap-2 mt-3 flex-wrap">
          <input className="field flex-1" placeholder="текст для ввода — Enter отправит"
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              const v = (e.target as HTMLInputElement).value;
              if (v) act({ kind: "type", text: v });
              act({ kind: "key", key: "Enter" });
              (e.target as HTMLInputElement).value = "";
            }} />
          <button className="btn btn-ghost" onClick={() => act({ kind: "scroll", dy: 300 })}>Ниже</button>
          <button className="btn btn-ghost" onClick={() => act({ kind: "goto", url: "" })}>Сначала</button>
          <button className="btn btn-primary" onClick={closeLogin}>Готово</button>
        </div>
      </Modal>

      <div className="mt-3.5"><button className="btn btn-primary" onClick={onSave} disabled={!ready}>Сохранить настройки бота</button></div>
    </Page>
  );
}
