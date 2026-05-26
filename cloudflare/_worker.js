const WORKFLOW_ID = "update-news.yml";
const REQUIRED_GITHUB_ENV = ["GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO"];
const LIVE_DATA_FILES = new Set([
  "daily-brief.zh.md",
  "radar-brief.json",
  "source-status.json",
  "upstream-hub.json",
  "waytoagi-7d.json",
  "latest-24h.json",
  "latest-24h-all.json",
  "archive-index.json",
]);

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload, null, 2), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function envValue(env, key, fallback = "") {
  return String(env[key] || fallback).trim();
}

function githubDispatchConfig(env) {
  const missing = REQUIRED_GITHUB_ENV.filter((key) => !envValue(env, key));
  return {
    configured: missing.length === 0,
    missing,
    owner: envValue(env, "GITHUB_OWNER"),
    repo: envValue(env, "GITHUB_REPO"),
    ref: envValue(env, "GITHUB_REF", "main"),
    workflow_id: envValue(env, "GITHUB_WORKFLOW_ID", WORKFLOW_ID),
  };
}

async function serveRadarPage(request, env) {
  const url = new URL("/__radar_app", request.url);
  const assetRequest = new Request(url, {
    method: "GET",
    headers: request.headers,
  });
  const assetResponse = await env.ASSETS.fetch(assetRequest);
  const headers = new Headers(assetResponse.headers);
  headers.set("content-type", "text/html; charset=utf-8");

  return new Response(request.method === "HEAD" ? null : assetResponse.body, {
    status: assetResponse.status,
    headers,
  });
}

function dataContentType(file) {
  if (file.endsWith(".json")) return "application/json; charset=utf-8";
  if (file.endsWith(".md")) return "text/markdown; charset=utf-8";
  return "text/plain; charset=utf-8";
}

async function serveLiveData(request, env, file) {
  const config = githubDispatchConfig(env);
  if (!config.configured || !LIVE_DATA_FILES.has(file)) {
    return env.ASSETS.fetch(request);
  }

  const response = await fetch(
    `https://api.github.com/repos/${config.owner}/${config.repo}/contents/data/${encodeURIComponent(file)}?ref=${encodeURIComponent(config.ref)}`,
    {
      headers: {
        accept: "application/vnd.github.raw",
        authorization: `Bearer ${envValue(env, "GITHUB_TOKEN")}`,
        "user-agent": "langben-ai-news-radar",
        "x-github-api-version": "2022-11-28",
      },
      cf: { cacheTtl: 30, cacheEverything: true },
    }
  );

  if (!response.ok) {
    return env.ASSETS.fetch(request);
  }

  const headers = new Headers(response.headers);
  headers.set("content-type", dataContentType(file));
  headers.set("cache-control", "no-store");
  headers.set("x-langben-data-source", "github");
  return new Response(request.method === "HEAD" ? null : response.body, {
    status: response.status,
    headers,
  });
}

async function triggerGithubWorkflow(env) {
  const token = envValue(env, "GITHUB_TOKEN");
  const config = githubDispatchConfig(env);

  if (!config.configured) {
    return json(
      {
        ok: false,
        mode: "manual_update_unconfigured",
        error: "missing_github_dispatch_config",
        missing: config.missing,
        generated_at: new Date().toISOString(),
        message: "手动更新暂不可用：自动更新通道尚未接入。",
      },
      501
    );
  }

  const response = await fetch(
    `https://api.github.com/repos/${config.owner}/${config.repo}/actions/workflows/${config.workflow_id}/dispatches`,
    {
      method: "POST",
      headers: {
        accept: "application/vnd.github+json",
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        "user-agent": "langben-ai-news-radar",
        "x-github-api-version": "2022-11-28",
      },
      body: JSON.stringify({ ref: config.ref }),
    }
  );

  if (!response.ok) {
    return json(
      {
        ok: false,
        error: "github_dispatch_failed",
        status: response.status,
        detail: await response.text(),
        message: "更新任务启动失败，请稍后再试。",
      },
      502
    );
  }

  return json({
    ok: true,
    mode: "workflow_dispatch",
    generated_at: new Date().toISOString(),
    message: "已提交更新任务；通常 1-3 分钟后刷新可见。",
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/api/status") {
      const config = githubDispatchConfig(env);
      return json({
        ok: true,
        auto_update: config.configured ? "GitHub Actions cron: every 30 minutes" : "not_configured",
        auto_update_configured: config.configured,
        manual_update: config.configured,
        manual_update_configured: config.configured,
        missing: config.missing,
        workflow_id: config.workflow_id,
        ref: config.ref,
        running: false,
        mode: "cloudflare_pages",
        domain: "ainews.caodiu.com",
      });
    }

    if (url.pathname === "/api/update") {
      if (request.method !== "POST") {
        return json({ ok: false, error: "method_not_allowed" }, 405);
      }
      return triggerGithubWorkflow(env);
    }

    if (url.pathname === "/") {
      return serveRadarPage(request, env);
    }

    if (url.pathname.startsWith("/data/")) {
      const file = decodeURIComponent(url.pathname.slice("/data/".length));
      return serveLiveData(request, env, file);
    }

    if (url.pathname === "/radar" || url.pathname === "/radar/" || url.pathname === "/radar.html") {
      return Response.redirect(new URL("/", url), 308);
    }

    if (url.pathname === "/daily") {
      return Response.redirect(new URL("/data/daily-brief.zh.md", url), 302);
    }

    return env.ASSETS.fetch(request);
  },
};
