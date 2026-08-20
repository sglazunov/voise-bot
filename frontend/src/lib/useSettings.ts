import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

// Настройки автоматизации: загрузка, точечные правки, сохранение.
//
// Важное про `ready` и `failed`: при неудачной загрузке раньше состояние
// становилось пустым объектом, и форма выглядела как «всё выключено». Нажатие
// «Сохранить» отправляло `!!undefined === false` для десятка тумблеров и
// пустые списки — то есть ЗАТИРАЛО реальные настройки на сервере. Поэтому
// сохранение блокируется, пока настройки не загружены.
export function useSettings() {
  const [s, setS] = useState<Record<string, any> | null>(null);
  const [failed, setFailed] = useState(false);
  const load = useCallback(() => {
    setFailed(false);
    return api.get("/api/automation/settings")
      .then((v) => setS(v))
      .catch(() => setFailed(true));
  }, []);
  useEffect(() => { load(); }, [load]);
  const set = (k: string, v: any) => setS((p) => ({ ...(p || {}), [k]: v }));
  const ready = s !== null;
  const save = (patch: Record<string, any>) => {
    if (!ready) {
      return Promise.reject(new Error(
        "Настройки ещё не загружены — сохранение отменено, чтобы не затереть " +
        "их пустыми значениями. Обновите страницу."));
    }
    return api.post("/api/automation/settings", patch);
  };
  return { s: s || {}, ready, failed, set, save, reload: load };
}
