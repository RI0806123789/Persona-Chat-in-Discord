import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 開発中は /api への要求をバックエンド（127.0.0.1:8765）へ中継する（WebSocket も含む）
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", ws: true },
    },
  },
});
