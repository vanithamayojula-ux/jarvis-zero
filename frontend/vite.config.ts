import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies to the local backend; changeOrigin makes the Host header match the backend's allowlist.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8600", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8600", ws: true, changeOrigin: true },
    },
  },
});
