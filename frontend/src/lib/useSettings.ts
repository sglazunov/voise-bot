import { useCallback, useEffect, useRef, useState } from "react";
import { api, MANAGED_TEAM_KEY } from "./api";

// Настройки автоматизации: загрузка, точечные правки, сохранение.
//
// Важное про `ready` и `failed`: при неудачной загрузке раньше состояние
// становилось пустым объектом, и форма выглядела как «всё выключено». Нажатие
// «Сохранить» отправляло `!!undefined === false` для десятка тумблеров и
// пустые списки — то есть ЗАТИРАЛО реальные настройки на сервере. Поэтому
// сохранение блокируется, пока настройки не загружены.
//
// `team` — настройки ДРУГОЙ команды (только у основателя сервера: так он
// настраивает клиента). `locked(k)` — ключ под замком «управляется сервером»:
// поле показываем, но менять не даём; сервер такие ключи всё равно не примет.
export function teamQuery(team?: string, sep = "?") {
  return team ? `${sep}team=${encodeURIComponent(team)}` : "";
}

// Выбранная основателем команда переживает переход «Weeek» ↔ «Облако», но не
// выход из аккаунта (`logout`) и не отказ сервера (см. `onTeamGone`).
export function useManagedTeam(): [string, (t: string) => void] {
  const [team, setTeam] = useState<string>(() => {
    try { return sessionStorage.getItem(MANAGED_TEAM_KEY) || ""; } catch { return ""; }
  });
  const choose = useCallback((t: string) => {
    try {
      if (t) sessionStorage.setItem(MANAGED_TEAM_KEY, t);
      else sessionStorage.removeItem(MANAGED_TEAM_KEY);
    } catch { /* приватный режим */ }
    setTeam(t);
  }, []);
  return [team, choose];
}

// `onTeamGone` — сервер ответил 403/404 на выбранную команду (вошёл не
// основатель, команду удалили): страница сбрасывает выбор на свою команду,
// иначе поля оставались неактивными без единой ошибки.
//
// Настройки хранятся ВМЕСТЕ с командой, для которой пришли, и показываются
// только при совпадении с выбранной (ревью 30.10, второй раунд): сразу после
// смены команды форма пуста и «Сохранить» закрыто — а не один кадр до эффекта
// с настройками прежней. `reload` грузит ТЕКУЩУЮ команду, даже если его
// позвали из замыкания прежней (onSave Weeek: `await save(); reload()`), и
// ответ на любую команду, кроме текущей, отбрасывается. Раньше такой reload
// брал новый номер запроса, становился «последним», и настройки A снова
// ложились в форму B.
export function useSettings(team = "", onTeamGone?: () => void) {
  const [loaded, setLoaded] = useState<{ team: string; v: Record<string, any> } | null>(null);
  const [failedFor, setFailedFor] = useState<string | null>(null);
  // Основатель ли смотрит — свойство человека, а не команды: выбор команды
  // не должен пропадать, пока грузятся настройки новой.
  const [canManage, setCanManage] = useState(false);
  // Номер последнего запроса и выбранная СЕЙЧАС команда: ответ принимается,
  // только если он последний и про текущую команду.
  const seq = useRef(0);
  const current = useRef(team);
  current.current = team;
  const gone = useRef(onTeamGone);
  gone.current = onTeamGone;
  const load = useCallback(() => {
    const t = current.current;
    const my = ++seq.current;
    setFailedFor(null);
    const mine = () => my === seq.current && t === current.current;
    return api.get(`/api/automation/settings${teamQuery(t)}`)
      .then((v) => {
        if (!mine()) return;
        setLoaded({ team: t, v });
        setCanManage(!!v?.can_manage_teams);
      })
      .catch((e) => {
        if (!mine()) return;
        setFailedFor(t);
        if (t && (e?.status === 403 || e?.status === 404)) gone.current?.();
      });
  }, []);
  useEffect(() => { load(); }, [team, load]);
  const s = loaded && loaded.team === team ? loaded.v : null;
  // Правка до загрузки ничего не создаёт: раньше одно поле делало форму
  // «загруженной», и «Сохранить» затирало остальное пустыми значениями.
  const set = (k: string, v: any) => setLoaded((p) => (p && p.team === current.current
    ? { team: p.team, v: { ...p.v, [k]: v } } : p));
  const ready = s !== null;
  const save = (patch: Record<string, any>) => {
    if (!ready) {
      return Promise.reject(new Error(
        "Настройки ещё не загружены — сохранение отменено, чтобы не затереть " +
        "их пустыми значениями. Обновите страницу."));
    }
    return api.post(`/api/automation/settings${teamQuery(team)}`, patch);
  };
  const lockedKeys: string[] = (s && s.locked_keys) || [];
  const locked = (k: string) => lockedKeys.includes(k);
  // Какая команда выбрана СЕЙЧАС — для ответов страницы, пришедших после
  // await (списки досок, участники): ответ про прежнюю команду не показывать.
  const teamNow = useCallback(() => current.current, []);
  return { s: s || {}, ready, failed: failedFor === team, set, save, reload: load,
           locked, canManage, teamNow };
}
