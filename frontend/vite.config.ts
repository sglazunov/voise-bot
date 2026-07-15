import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA is served under /app/ by FastAPI (StaticFiles), so old Jinja routes
// keep working during the migration. API stays at /api/* (proxied in dev).
export default defineConfig({
  base: "/app/",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
