// 依存フォルダを共有する開発環境でも、アイコンと本文のフォントを配信できることを確認する。
import assert from "node:assert/strict";
import { copyFile, mkdir, mkdtemp, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { createServer } from "vite";

test("共有した node_modules からアイコン・本文フォントを配信する", async (t) => {
  const frontend = dirname(dirname(fileURLToPath(import.meta.url)));
  const temporary = await mkdtemp(join(tmpdir(), "console-fonts-"));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const fixture = join(temporary, "frontend");
  await mkdir(fixture);
  await writeFile(join(fixture, "package.json"), '{"type":"module","private":true}');
  await copyFile(join(frontend, "vite.config.ts"), join(fixture, "vite.config.ts"));
  await symlink(await realpath(join(frontend, "node_modules")), join(fixture, "node_modules"),
    process.platform === "win32" ? "junction" : "dir");
  const server = await createServer({
    root: fixture,
    configFile: join(fixture, "vite.config.ts"),
    logLevel: "silent",
    server: { port: 0, strictPort: false, watch: null },
  });
  t.after(() => server.close());
  await server.listen();
  const base = `http://127.0.0.1:${server.httpServer.address().port}`;

  for (const stylesheet of [
    "material-symbols/rounded.css",
    "@fontsource-variable/roboto-flex/full.css",
    "@fontsource-variable/noto-sans-jp/index.css",
  ]) {
    const cssResponse = await fetch(`${base}/node_modules/${stylesheet}?direct`);
    assert.equal(cssResponse.status, 200, stylesheet);
    const css = await cssResponse.text();
    const fontUrl = /url\(["']?([^"')]+\.woff2(?:\?[^"')]*)?)["']?\)/.exec(css)?.[1];
    assert.ok(fontUrl, `${stylesheet} のフォント URL が見つからない`);
    const response = await fetch(new URL(fontUrl, cssResponse.url));
    assert.equal(response.status, 200, `${stylesheet} のフォントを配信できない`);
    const bytes = Buffer.from(await response.arrayBuffer());
    assert.equal(bytes.subarray(0, 4).toString(), "wOF2", "フォントの代わりに HTML などが返っている");
  }

  // フォントを許可するために、開発用ルート外の別ファイルまで公開しない。
  const privateFile = join(temporary, "not-a-font.txt");
  await writeFile(privateFile, "配信対象外の検証用ファイル");
  const denied = await fetch(`${base}/@fs/${privateFile.replaceAll("\\", "/")}`);
  assert.equal(denied.status, 403);
});
