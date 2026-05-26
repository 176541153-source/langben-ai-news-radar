#!/usr/bin/env node
import { cpSync, existsSync, mkdirSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const dist = join(root, "dist");

const entries = [
  "index.html",
  "radar.html",
  "assets",
  "cloudflare/_headers",
  "cloudflare/_redirects",
  "cloudflare/_worker.js",
];

const dataFiles = [
  "archive-index.json",
  "daily-brief.zh.md",
  "latest-24h-all.json",
  "latest-24h.json",
  "radar-brief.json",
  "source-status.json",
  "title-zh-cache.json",
  "upstream-hub.json",
  "waytoagi-7d.json",
];

rmSync(dist, { recursive: true, force: true });
mkdirSync(dist, { recursive: true });

for (const entry of entries) {
  const source = join(root, entry);
  if (!existsSync(source)) {
    throw new Error(`Missing required deploy entry: ${entry}`);
  }
  const targetName = entry.startsWith("cloudflare/") ? entry.slice("cloudflare/".length) : entry;
  const target = join(dist, targetName);
  mkdirSync(dirname(target), { recursive: true });
  cpSync(source, target, { recursive: true });
}

cpSync(join(root, "radar.html"), join(dist, "__radar_app"));

mkdirSync(join(dist, "data"), { recursive: true });
for (const file of dataFiles) {
  const source = join(root, "data", file);
  if (!existsSync(source)) {
    throw new Error(`Missing required deploy data file: ${file}`);
  }
  cpSync(source, join(dist, "data", file));
}

console.log(`Cloudflare Pages bundle written to ${dist}`);
