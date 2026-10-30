import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

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

// Выбранная основателем команда переживает переход «Weeek» ↔ «Облако».
const TEAM_KEY = "vtx.manage-team";
export function useManagedTeam(): [string, (t: string) => void] {
  const [team, setTeam] = useState<string>(() => {
    try { return sessionStorage.getItem(TEAM_KEY) || ""; } catch { return ""; }
  });
  const choose = (t: string) => {
    try { if (t) sessionStorage.setItem(TEAM_KEY, t); else sessionStorage.removeItem(TEAM_KEY); } catch { /* приватный режим */ }
    setTeam(t);
  };
  return [team, choose];
}

export function useSettings(team?: string) {
  const [s, setS] = useState<Record<string, any> | null>(null);
  const [failed, setFailed] = useState(false);
  const load = useCallback(() => {
    setFailed(false);
    return api.get(`/api/automation/settings${teamQuery(team)}`)
      .then((v) => setS(v))
      .catch(() => setFailed(true));
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
