import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // The dashboard talks to the FastAPI backend on :8000. Proxying keeps the
    // browser on one origin, so there are no CORS or mixed-origin websocket
    // problems during development.
    //
    // Two details that are easy to get wrong:
    //   * the websocket entry uses an http:// target with `ws: true`. Writing
    //     `ws://` here makes the proxy abort the upgrade with ECONNABORTED.
    //   * the host is 127.0.0.1, not localhost. On Windows, Node resolves
    //     localhost to ::1 first, while uvicorn binds IPv4 by default, so the
    //     proxy would connect to nothing.
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "http://127.0.0.1:8000", ws: true, changeOrigin: true },
    },
  },
});
