import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The SPA is the whole app UI, served at the site root by FastAPI. Hashed
// assets live under /assets; API stays at /api/* (proxied in dev).
export default defineConfig({
  base: "/",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
