import pkg from "./package.json";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  // The name mark reads the real version, so it stops drifting from
  // package.json the way two hardcoded copies of "v1.0.2" did.
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8101",
      "/media": "http://localhost:8101",
      "/ws": {
        target: "ws://localhost:8101",
        ws: true,
      },
    },
  },
});
