import { useEffect, useState } from "react";

export function useTheme() {
  const [theme, setTheme] = useState<string>(
    () => document.documentElement.dataset.theme || (localStorage.getItem("vtx-theme") ?? "")
  );
  useEffect(() => {
    document.documentElement.dataset.theme = theme || "";
    try { localStorage.setItem("vtx-theme", theme); } catch { /* ignore */ }
  }, [theme]);
  return { theme, toggle: () => setTheme((t) => (t === "light" ? "" : "light")) };
}
