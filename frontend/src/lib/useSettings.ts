import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

// Loads the user's automation settings (secrets come back as booleans/redacted)
// and saves partial patches — same /api/automation/settings the old UI used.
export function useSettings() {
  const [s, setS] = useState<Record<string, any> | null>(null);
  const load = useCallback(() => api.get("/api/automation/settings").then(setS).catch(() => setS({})), []);
  useEffect(() => { load(); }, [load]);
  const set = (k: string, v: any) => setS((p) => ({ ...(p || {}), [k]: v }));
  const save = (patch: Record<string, any>) => api.post("/api/automation/settings", patch);
  return { s: s || {}, ready: s !== null, set, save, reload: load };
}
