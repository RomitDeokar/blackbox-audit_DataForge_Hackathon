import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Phase 7A FastAPI backend (api/app.py) is run separately with:
//   uvicorn api.app:app --reload --port 8000
// This dev server proxies /ws and /api so the frontend can be addressed as
// a single origin during development without hardcoding a backend host in
// application code. See src/lib/config.js for how the browser resolves the
// WebSocket URL in both dev (proxied) and preview/build (direct) modes.
const BACKEND_ORIGIN = process.env.VITE_BACKEND_ORIGIN || "http://localhost:8000";
const BACKEND_WS_ORIGIN = BACKEND_ORIGIN.replace(/^http/, "ws");

const proxy = {
  // Matches the routes api/app.py actually exposes (see docs/UI_SPEC.md
  // section 3 and api/app.py's module docstring) — there is no /api
  // prefix in Phase 7A, so we proxy each real route individually.
  "/ws": {
    target: BACKEND_WS_ORIGIN,
    ws: true,
  },
  "/health": {
    target: BACKEND_ORIGIN,
    changeOrigin: true,
  },
  "/demo": {
    target: BACKEND_ORIGIN,
    changeOrigin: true,
  },
  "/calls": {
    target: BACKEND_ORIGIN,
    changeOrigin: true,
  },
};

export default defineConfig({
  plugins: [react()],
  // Same proxy for `vite` (dev) and `vite preview` (built bundle), so the
  // production build can be verified against the real backend too.
  // host/allowedHosts let the console be opened from another machine
  // (e.g. a projector laptop or a tunnelled demo URL).
  server: { port: 5173, host: true, allowedHosts: true, proxy },
  preview: { port: 4173, host: true, allowedHosts: true, proxy },
});
