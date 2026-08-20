// Thin fetch wrapper for the existing FastAPI backend. All endpoints are the
// same ones the old vanilla-JS pages used, so no backend changes are required.
const json = { "Content-Type": "application/json" };

// FastAPI отдаёт detail либо строкой, либо СПИСКОМ (ошибки валидации, 422) —
// в этом случае строка «[object Object]» и попадала в тост вместо причины.
function message(data: any, status: number): string {
  const d = data?.detail ?? data?.error;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d.map((e) => {
      const field = Array.isArray(e?.loc) ? e.loc[e.loc.length - 1] : "";
      return field ? `${field}: ${e?.msg || "неверное значение"}` : (e?.msg || "");
    }).filter(Boolean).join("; ") || `HTTP ${status}`;
  }
  return `HTTP ${status}`;
}

async function handle(r: Response) {
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) {
    // Сессия истекла. Без этого поллинги страниц крутились вечно и молча:
    // ошибки в консоль, экран прежний, входа никто не предлагает.
    if (!location.pathname.startsWith("/login")) location.href = "/login";
    throw new Error("Сессия истекла — войдите заново.");
  }
  if (!r.ok) throw new Error(message(data, r.status));
  return data;
}

export const api = {
  get: (p: string) => fetch(p).then(handle),
  post: (p: string, body?: unknown) =>
    fetch(p, { method: "POST", headers: json, body: body == null ? undefined : JSON.stringify(body) }).then(handle),
  patch: (p: string, body?: unknown) =>
    fetch(p, { method: "PATCH", headers: json, body: body == null ? undefined : JSON.stringify(body) }).then(handle),
  // raw text (transcript downloads)
  text: (p: string) => fetch(p).then((r) => (r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`)))),
  // multipart upload with progress (transcription jobs)
  upload(p: string, form: FormData, onProgress?: (pct: number) => void): Promise<any> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", p);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100)); };
      xhr.onload = () => {
        let d: any = {}; try { d = JSON.parse(xhr.responseText); } catch { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(d);
        else reject(new Error(d.detail || `HTTP ${xhr.status}`));
      };
      xhr.onerror = () => reject(new Error("Сеть недоступна"));
      xhr.send(form);
    });
  },
};

export async function logout() {
  await fetch("/api/auth/logout", { method: "POST" });
  window.location.href = "/login";
}
