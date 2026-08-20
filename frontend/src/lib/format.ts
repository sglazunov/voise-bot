export type Meeting = {
  task_id: string | number; title: string; url?: string; start: string | null;
  state: string; detail?: string; job_id?: string | null; cloud_url?: string | null;
  do_protocol?: boolean; record_flag?: boolean | null;
  has_recording?: boolean; // лежит ли на диске готовая запись этой встречи
  logs?: string[];        // хвост лога рекордера — что бот видел на встрече
};
export type Status = {
  running: boolean; enabled: boolean; recording: boolean; active: number;
  max_parallel: number; last_poll: number; meetings: Meeting[];
};

export function isToday(iso: string | null): boolean {
  if (!iso) return false;
  const d = new Date(iso), n = new Date();
  return d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
}
export function fmtDateTime(iso: string | null): string {
  if (!iso) return "без времени";
  try {
    return new Date(iso).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch { return iso; }
}

/** Русское склонение по числу: plural(2, "ключ", "ключа", "ключей") -> "ключа".
 *  Было написано дважды — в Overview и вручную тернарниками в Providers. */
export function plural(n: number, one: string, few: string, many: string): string {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
  return many;
}
