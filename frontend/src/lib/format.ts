export type Meeting = {
  task_id: string | number; title: string; url?: string; start: string | null;
  state: string; detail?: string; job_id?: string | null; cloud_url?: string | null;
  do_protocol?: boolean; record_flag?: boolean | null;
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
