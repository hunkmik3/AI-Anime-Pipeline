import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Review-only dev server config (does NOT touch the committed vite.config.ts).
// Runs on a free port and proxies API/media/ws to the isolated review backend
// on :8103. Used via:  npx vite --config vite.review.config.ts
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      "/api": "http://localhost:8103",
      "/media": "http://localhost:8103",
      "/ws": {
        target: "ws://localhost:8103",
        ws: true,
      },
    },
  },
});
