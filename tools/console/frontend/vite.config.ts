import { defineConfig, normalizePath } from "vite";
import react from "@vitejs/plugin-react";

// 開発中は /api への要求をバックエンド（127.0.0.1:8765）へ中継する（WebSocket も含む）
export default defineConfig({
  plugins: [react(), {
    name: "console-local-fonts",
    async configureServer(server) {
      // worktree などで依存フォルダを共有していても、フォントの実体だけを配信する。
      // Vite の解決結果を使い、通常の配信制限は維持する。
      for (const stylesheet of [
        "@fontsource-variable/roboto-flex/full.css",
        "@fontsource-variable/noto-sans-jp/index.css",
        "material-symbols/rounded.css",
      ]) {
        const resolved = await server.pluginContainer.resolveId(stylesheet);
        if (!resolved) throw new Error("フォントが見つかりません。npm ci で依存関係を準備してください。");
        const path = normalizePath(resolved.id);
        server.config.server.fs.allow.push(path.slice(0, path.lastIndexOf("/")));
      }
    },
  }],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", ws: true },
    },
  },
});
