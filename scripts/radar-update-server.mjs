#!/usr/bin/env node
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const port = Number(process.argv[2] || process.env.PORT || 8080);
let updatePromise = null;

const mimeTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".md": "text/markdown; charset=utf-8",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
};

function sendJson(res, statusCode, payload) {
  const body = JSON.stringify(payload, null, 2);
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(body);
}

function pythonBin() {
  const localPython = path.join(root, ".venv", "bin", "python");
  return fs.existsSync(localPython) ? localPython : "python3";
}

function updateArgs() {
  const args = [
    "scripts/update_news.py",
    "--output-dir",
    "data",
    "--window-hours",
    "24",
    "--archive-days",
    "21",
    "--translate-max-new",
    "0",
  ];
  const privateOpml = path.join(root, "feeds", "follow.opml");
  const exampleOpml = path.join(root, "feeds", "follow.example.opml");
  if (fs.existsSync(privateOpml) || fs.existsSync(exampleOpml)) {
    args.push("--rss-opml", fs.existsSync(privateOpml) ? "feeds/follow.opml" : "feeds/follow.example.opml");
    args.push("--rss-max-feeds", process.env.RSS_MAX_FEEDS || "10");
  }
  return args;
}

function runUpdate() {
  if (updatePromise) return updatePromise;
  const startedAt = Date.now();
  updatePromise = new Promise((resolve, reject) => {
    const child = spawn(pythonBin(), updateArgs(), {
      cwd: root,
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let output = "";
    const collect = (chunk) => {
      output += chunk.toString();
      if (output.length > 20000) output = output.slice(-20000);
    };
    child.stdout.on("data", collect);
    child.stderr.on("data", collect);
    child.on("error", reject);
    child.on("close", async (code) => {
      if (code !== 0) {
        reject(new Error(output || `update exited with code ${code}`));
        return;
      }
      const radarPath = path.join(root, "data", "radar-brief.json");
      const radar = JSON.parse(await fsp.readFile(radarPath, "utf8"));
      resolve({
        generated_at: radar.generated_at,
        duration_ms: Date.now() - startedAt,
        output: output.slice(-4000),
      });
    });
  }).finally(() => {
    updatePromise = null;
  });
  return updatePromise;
}

async function serveStatic(req, res, pathname) {
  let target = decodeURIComponent(pathname);
  if (target === "/") target = "/radar.html";
  const filePath = path.normalize(path.join(root, target));
  if (!filePath.startsWith(root + path.sep) && filePath !== root) {
    res.writeHead(403);
    res.end("Forbidden");
    return;
  }
  try {
    const stat = await fsp.stat(filePath);
    const finalPath = stat.isDirectory() ? path.join(filePath, "index.html") : filePath;
    const ext = path.extname(finalPath).toLowerCase();
    res.writeHead(200, {
      "Content-Type": mimeTypes[ext] || "application/octet-stream",
      "Cache-Control": "no-store",
    });
    if (req.method === "HEAD") {
      res.end();
      return;
    }
    fs.createReadStream(finalPath).pipe(res);
  } catch {
    res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("Not found");
  }
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url || "/", `http://${req.headers.host || "127.0.0.1"}`);
  if (url.pathname === "/api/status" && req.method === "GET") {
    sendJson(res, 200, {
      ok: true,
      auto_update: "GitHub Actions cron: every 30 minutes",
      manual_update: true,
      running: Boolean(updatePromise),
    });
    return;
  }
  if (url.pathname === "/api/update" && req.method === "POST") {
    if (updatePromise) {
      sendJson(res, 409, { ok: false, error: "update_already_running" });
      return;
    }
    try {
      const result = await runUpdate();
      sendJson(res, 200, { ok: true, ...result });
    } catch (error) {
      sendJson(res, 500, { ok: false, error: String(error?.message || error) });
    }
    return;
  }
  if (!["GET", "HEAD"].includes(req.method || "")) {
    res.writeHead(405, { "Allow": "GET, HEAD, POST" });
    res.end("Method not allowed");
    return;
  }
  await serveStatic(req, res, url.pathname);
});

server.listen(port, "127.0.0.1", () => {
  console.log(`Radar update server: http://127.0.0.1:${port}/radar.html`);
});
