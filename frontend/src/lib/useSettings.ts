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
export function useSettings(team?: string, onTeamGone?: () => void) {
  const [s, setS] = useState<Record<string, any> | null>(null);
  const [failed, setFailed] = useState(false);
  // Номер последнего запроса. Ответ на ПРЕЖНЮЮ команду, пришедший после
  // переключения, отбрасывается: иначе форма показывала клиента A при
  // выбранном B, и «Сохранить» писало настройки A в B.
  const seq = useRef(0);
  const gone = useRef(onTeamGone);
  gone.current = onTeamGone;
  const load = useCallback(() => {
    const my = ++seq.current;
    setFailed(false);
    return api.get(`/api/automation/settings${teamQuery(team)}`)
      .then((v) => { if (my === seq.current) setS(v); })
      .catch((e) => {
        if (my !== seq.current) return;
        setFailed(true);
        if (team && (e?.status === 403 || e?.status === 404)) gone.current?.();
      });
  }, [team]);
  useEffect(() => { setS(null); load(); }, [load]);
  const set = (k: string, v: any) => setS((p) => ({ ...(p || {}), [k]: v }));
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
  return { s: s || {}, ready, failed, set, save, reload: load, locked };
}
