const generatedAtEl = document.getElementById("generatedAt");
const metricGridEl = document.getElementById("metricGrid");
const storyGridEl = document.getElementById("storyGrid");
const dailyBriefEl = document.getElementById("dailyBrief");
const categoryStackEl = document.getElementById("categoryStack");
const chinaHotItemsEl = document.getElementById("chinaHotItems");
const newItemsEl = document.getElementById("newItems");
const updateNowBtnEl = document.getElementById("updateNowBtn");
const updateStatusEl = document.getElementById("updateStatus");
const searchInputEl = document.getElementById("searchInput");
const sourceCatalogEl = document.getElementById("sourceCatalog");
const sourceSummaryEl = document.getElementById("sourceSummary");
const resetSourcesBtnEl = document.getElementById("resetSourcesBtn");
const navLinkEls = Array.from(document.querySelectorAll(".nav-links a[data-channel]"));

const tones = ["cyan", "amber", "violet", "green", "orange"];
const sourceStorageKey = "langben-ai-disabled-sources-v1";
const autoRefreshMs = 5 * 60 * 1000;
const channelConfig = {
  today: { label: "今日", target: "today" },
  model: { label: "模型", target: "today" },
  agent: { label: "Agent", target: "today" },
  product: { label: "产品", target: "today" },
  compute: { label: "算力", target: "today" },
  sources: { label: "来源", target: "sources" },
  daily: { label: "日报", target: "daily" },
};
let currentPayload = null;
let currentDailyMarkdown = "";
let activeChannel = initialChannel();
let disabledSourceKeys = loadDisabledSources();
let autoRefreshTimer = null;

function fmtNumber(value) {
  return new Intl.NumberFormat("zh-CN").format(value || 0);
}

function fmtTime(iso) {
  if (!iso) return "未知时间";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "未知时间";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function clear(node) {
  if (!node) return;
  node.innerHTML = "";
}

function loadDisabledSources() {
  try {
    const raw = localStorage.getItem(sourceStorageKey);
    const parsed = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(parsed) ? parsed.filter(Boolean) : []);
  } catch {
    return new Set();
  }
}

function saveDisabledSources() {
  localStorage.setItem(sourceStorageKey, JSON.stringify(Array.from(disabledSourceKeys)));
}

function initialChannel() {
  const hash = window.location.hash.replace("#", "");
  if (hash === "sources") return "sources";
  if (hash === "daily") return "daily";
  return "today";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function itemTitle(item) {
  return item?.title || "未命名信号";
}

function itemHref(item) {
  return item?.url || "#";
}

function sourceKeyForItem(item) {
  if (!item) return "";
  if (item.source_key) return String(item.source_key);
  if (item.source_ref?.source_key) return String(item.source_ref.source_key);
  if (item.platform_id) return `trendradar::platform::${item.platform_id}`;
  if (item.project_source === "TrendRadar" || item.site_name === "TrendRadar 中文热榜") {
    const id = item.platform_id || String(item.source || "").trim().toLowerCase();
    return id ? `trendradar::platform::${id}` : "";
  }
  if (item.project_source === "Horizon") return "horizon::feed::feed-zh";
  if (item.project_source === "AI HOT") {
    return item.source ? `aihot::feed::${slugForSource(item.source)}` : "";
  }
  if (item.site_id) return `ai_news_radar::collector::${item.site_id}`;
  return "";
}

function slugForSource(value) {
  const raw = String(value || "").trim().toLowerCase();
  return raw.replace(/[^0-9a-z\u4e00-\u9fff]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 72);
}

function isSourceEnabled(item) {
  const key = sourceKeyForItem(item);
  return !key || !disabledSourceKeys.has(key);
}

function isStoryEnabled(story) {
  const refs = Array.isArray(story?.source_refs) ? story.source_refs : [];
  if (refs.length) return refs.some((ref) => !disabledSourceKeys.has(ref.source_key));
  return isSourceEnabled(story?.primary || story);
}

function toneFor(index) {
  return tones[index % tones.length];
}

function searchTextFor(item) {
  return [
    item?.title,
    item?.title_original,
    item?.source,
    item?.site_name,
    item?.category_title,
    item?.project_source,
    ...(Array.isArray(item?.topic_labels) ? item.topic_labels : []),
    ...(Array.isArray(item?.signals) ? item.signals : []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function currentQuery() {
  return String(searchInputEl?.value || "").trim().toLowerCase();
}

function itemMatchesChannel(item, channel = activeChannel) {
  if (!item || channel === "today" || channel === "daily" || channel === "sources") return true;
  const categoryId = String(item.category_id || "");
  const text = searchTextFor(item);
  if (channel === "model") {
    return categoryId === "model_release" || /(模型|大模型|gpt|claude|gemini|deepseek|qwen|sora|llama|推理|上下文)/i.test(text);
  }
  if (channel === "compute") {
    return (
      categoryId === "industry_compute" ||
      /(算力|gpu|芯片|chip|机器人|robot|数据中心|inference|推理成本)/i.test(text)
    );
  }
  if (channel === "agent") {
    return (
      /(agent|智能体|mcp|codex|copilot|workflow|工作流|自动化|助手|skill|tools?)/i.test(text) ||
      (categoryId === "product_tool" && /(智能体|agent|mcp|codex|copilot|workflow|工作流|skill)/i.test(text))
    );
  }
  if (channel === "product") {
    return categoryId === "product_tool" || /(产品|应用|app|工具|api|sdk|发布|上线|商业化)/i.test(text);
  }
  return true;
}

function storyMatchesChannel(story) {
  if (activeChannel === "today" || activeChannel === "daily" || activeChannel === "sources") return true;
  if (itemMatchesChannel(story?.primary || story)) return true;
  return (Array.isArray(story?.related) ? story.related : []).some((item) => itemMatchesChannel(item));
}

function filterItems(items, query) {
  return (Array.isArray(items) ? items : []).filter((item) => {
    if (!isSourceEnabled(item)) return false;
    if (!itemMatchesChannel(item)) return false;
    return !query || searchTextFor(item).includes(query);
  });
}

function visiblePayload(payload) {
  const query = currentQuery();
  const sections = (payload.sections || [])
    .map((section) => {
      const items = filterItems(section.items, query);
      return { ...section, count: items.length, items };
    })
    .filter((section) => section.items.length);
  const visibleAiItems = sections.reduce((sum, section) => sum + Number(section.count || 0), 0);
  return {
    ...payload,
    top_stories: (Array.isArray(payload.top_stories) ? payload.top_stories : []).filter((story) => {
      if (!isStoryEnabled(story)) return false;
      if (!storyMatchesChannel(story)) return false;
      return !query || searchTextFor(story.primary || story).includes(query) || searchTextFor(story).includes(query);
    }),
    sections,
    china_hot_items: filterItems(payload.china_hot_items, query),
    new_items: filterItems(payload.new_items, query),
    summary: {
      ...(payload.summary || {}),
      ai_items:
        activeChannel === "today" && !query && disabledSourceKeys.size === 0
          ? payload.summary?.ai_items
          : visibleAiItems,
    },
  };
}

function scorePercent(score) {
  return clamp(Math.round(Number(score || 0) * 10), 0, 100);
}

function metricCard(label, value, detail, tone = "cyan", ratio = 64) {
  const node = document.createElement("div");
  node.className = `metric-card ${tone}`;
  node.innerHTML = `
    <div class="metric-top">
      <span>${escapeHtml(label)}</span>
      <i aria-hidden="true"></i>
    </div>
    <strong>${escapeHtml(value)}</strong>
    <div class="metric-bar"><b style="width:${clamp(ratio, 8, 100)}%"></b></div>
    <em>${escapeHtml(detail)}</em>
  `;
  return node;
}

function sourceCatalogTotals(payload) {
  const projects = payload?.source_catalog?.projects || [];
  const sourceKeys = new Set();
  const enabledProjectIds = new Set();
  projects.forEach((project) => {
    (project.sources || []).forEach((source) => {
      const key = source.source_key;
      if (!key || disabledSourceKeys.has(key)) return;
      sourceKeys.add(key);
      enabledProjectIds.add(project.id);
    });
  });
  return {
    enabledSources: sourceKeys.size || payload?.source_catalog?.source_count || 0,
    enabledProjects: enabledProjectIds.size || projects.length || 4,
  };
}

function visibleSourceCoverage(payload) {
  const sourceKeys = new Set();
  const projectIds = new Set();
  const addRef = (ref) => {
    const key = ref?.source_key;
    if (!key || disabledSourceKeys.has(key)) return;
    sourceKeys.add(key);
    projectIds.add(ref.project_id || key.split("::")[0]);
  };
  const addItem = (item) => {
    if (!item || !isSourceEnabled(item)) return;
    if (item.source_ref) addRef(item.source_ref);
    const key = sourceKeyForItem(item);
    if (key && !disabledSourceKeys.has(key)) {
      sourceKeys.add(key);
      projectIds.add(item.source_ref?.project_id || key.split("::")[0]);
    }
  };

  (payload.sections || []).forEach((section) => (section.items || []).forEach(addItem));
  (payload.new_items || []).forEach(addItem);
  (payload.china_hot_items || []).forEach(addItem);
  (payload.top_stories || []).forEach((story) => {
    (story.source_refs || []).forEach(addRef);
    addItem(story.primary || story);
  });

  return {
    sourceCount: sourceKeys.size,
    projectCount: projectIds.size,
  };
}

function renderMetrics(payload) {
  const summary = payload.summary || {};
  const channelLabel = channelConfig[activeChannel]?.label || "今日";
  const signalCount = Number(summary.ai_items || 0);
  const storyCount = Array.isArray(payload.top_stories) ? payload.top_stories.length : 0;
  const newCount = Array.isArray(payload.new_items) ? payload.new_items.length : 0;
  const hotCount = Array.isArray(payload.china_hot_items) ? payload.china_hot_items.length : 0;
  const sectionCount = Array.isArray(payload.sections) ? payload.sections.length : 0;
  const highStoryCount = (payload.top_stories || []).filter((story) => Number(story.score || 0) >= 8).length;
  const totals = sourceCatalogTotals(currentPayload || payload);
  const coverage = visibleSourceCoverage(payload);
  const signalRatio = summary.raw_items ? Math.round((signalCount / summary.raw_items) * 100) : Math.min(signalCount, 100);
  const sourceRatio = totals.enabledSources ? Math.round((coverage.sourceCount / totals.enabledSources) * 100) : 0;
  clear(metricGridEl);
  metricGridEl.append(
    metricCard(`${channelLabel}信号`, fmtNumber(signalCount), `${fmtNumber(sectionCount)} 条轨道 · ${fmtNumber(storyCount)} 个重点`, "cyan", signalRatio),
    metricCard("新增 6H", fmtNumber(newCount), "新出现且已去重", "green", Math.min(newCount * 7, 100)),
    metricCard("中文热榜", fmtNumber(hotCount), "TrendRadar 关键词命中", "amber", Math.min(hotCount * 6, 100)),
    metricCard("来源覆盖", `${fmtNumber(coverage.projectCount)}/${fmtNumber(totals.enabledProjects)}`, `${fmtNumber(coverage.sourceCount)}/${fmtNumber(totals.enabledSources)} 个来源贡献 · 高热 ${fmtNumber(highStoryCount)}`, "violet", sourceRatio)
  );
}

function buildStoryCard(story, index) {
  const link = document.createElement("a");
  link.className = `story-card ${index === 0 ? "lead" : "compact"} ${toneFor(index)}`;
  link.href = itemHref(story);
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  const title = itemTitle(story);
  const score = Number(story.score || 0);
  const percent = scorePercent(score);
  const sourceText = `${fmtNumber(story.source_count || 1)} 源 · ${fmtNumber(story.item_count || 1)} 条`;
  link.innerHTML = `
    <div class="rank">${String(index + 1).padStart(2, "0")}</div>
    <div class="story-main">
      <div class="story-top">
        <span>${escapeHtml(story.category_title || "AI 信号")}</span>
        <strong>${score.toFixed(score % 1 ? 1 : 0)}</strong>
      </div>
      <h3>${escapeHtml(title)}</h3>
      <div class="story-foot">
        <span>${escapeHtml(story.time_label || "时间未知")}</span>
        <em>${escapeHtml(sourceText)}</em>
      </div>
      <div class="score-track"><b style="width:${percent}%"></b></div>
    </div>
  `;
  return link;
}

function renderStories(payload) {
  const stories = Array.isArray(payload.top_stories) ? payload.top_stories : [];
  clear(storyGridEl);
  if (!stories.length) {
    storyGridEl.innerHTML = '<div class="empty">暂无故事数据</div>';
    return;
  }
  stories.slice(0, 10).forEach((story, index) => {
    storyGridEl.appendChild(buildStoryCard(story, index));
  });
}

function parseDailyBrief(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const meta = [];
  const sections = [];
  let currentSection = null;
  let currentItem = null;
  let inHighlights = false;

  function pushItem() {
    if (currentSection && currentItem) currentSection.items.push(currentItem);
    currentItem = null;
  }

  function pushSection() {
    pushItem();
    if (currentSection && currentSection.items.length) sections.push(currentSection);
    currentSection = null;
  }

  for (const line of lines) {
    const metaMatch = line.match(/^- (生成时间|24小时AI信号|源健康|原始抓取)：(.+)$/);
    if (metaMatch) {
      meta.push({ label: metaMatch[1], value: metaMatch[2] });
      continue;
    }

    if (line.startsWith("## 今日重点")) {
      inHighlights = true;
      continue;
    }

    if (inHighlights && line.startsWith("## ") && !line.startsWith("## 今日重点")) {
      pushSection();
      break;
    }

    if (!inHighlights) continue;

    const sectionMatch = line.match(/^###\s+(.+)$/);
    if (sectionMatch) {
      pushSection();
      currentSection = { title: sectionMatch[1].trim(), items: [] };
      continue;
    }

    const itemMatch = line.match(/^\d+\.\s+\[([^\]]+)\]\(([^)]+)\)/);
    if (itemMatch && currentSection) {
      pushItem();
      currentItem = {
        title: itemMatch[1].trim(),
        url: itemMatch[2].trim(),
        time: "",
        source: "",
        score: "",
      };
      continue;
    }

    if (!currentItem) continue;
    const detailMatch = line.match(/^\s+-\s+(时间|来源|评分)：(.+)$/);
    if (!detailMatch) continue;
    const key = detailMatch[1];
    const value = detailMatch[2].trim();
    if (key === "时间") currentItem.time = value;
    if (key === "来源") currentItem.source = value;
    if (key === "评分") currentItem.score = value.split("·")[0].trim();
  }

  pushSection();
  return { meta, sections };
}

function renderDailyBrief(markdown) {
  if (!dailyBriefEl) return;
  const brief = parseDailyBrief(markdown);
  const sections = brief.sections.slice(0, 5);
  clear(dailyBriefEl);

  if (!sections.length) {
    dailyBriefEl.innerHTML = '<div class="empty">日报暂未生成</div>';
    return;
  }

  const metaNode = document.createElement("div");
  metaNode.className = "daily-meta";
  brief.meta.slice(0, 4).forEach((item) => {
    const node = document.createElement("div");
    node.innerHTML = `<span>${escapeHtml(item.label)}</span><strong>${escapeHtml(item.value)}</strong>`;
    metaNode.appendChild(node);
  });

  const lanes = document.createElement("div");
  lanes.className = "daily-lanes";
  sections.forEach((section, sectionIndex) => {
    const lane = document.createElement("section");
    lane.className = `daily-lane ${toneFor(sectionIndex)}`;
    const items = section.items
      .slice(0, 3)
      .map(
        (item, itemIndex) => `
          <a class="daily-item" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">
            <span>${String(itemIndex + 1).padStart(2, "0")}</span>
            <div>
              <strong>${escapeHtml(item.title)}</strong>
              <em>${escapeHtml([item.time, item.source, item.score].filter(Boolean).join(" · "))}</em>
            </div>
          </a>
        `
      )
      .join("");
    lane.innerHTML = `
      <header>
        <span>${escapeHtml(section.title)}</span>
        <b>${fmtNumber(section.items.length)}</b>
      </header>
      ${items}
    `;
    lanes.appendChild(lane);
  });

  dailyBriefEl.append(metaNode, lanes);
}

function renderDailyFromPayload(payload) {
  if (!dailyBriefEl) return;
  const sections = (Array.isArray(payload.sections) ? payload.sections : []).slice(0, 5);
  clear(dailyBriefEl);

  if (!sections.length) {
    dailyBriefEl.innerHTML = '<div class="empty">当前来源筛选下暂无日报条目</div>';
    return;
  }

  const metaNode = document.createElement("div");
  metaNode.className = "daily-meta";
  [
    ["生成时间", fmtTime(payload.generated_at)],
    ["可见AI信号", fmtNumber(sections.reduce((sum, section) => sum + Number(section.count || 0), 0))],
    ["可见重点", fmtNumber((payload.top_stories || []).length)],
    ["来源筛选", `${fmtNumber(disabledSourceKeys.size)} 个已删除`],
  ].forEach(([label, value]) => {
    const node = document.createElement("div");
    node.innerHTML = `<span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong>`;
    metaNode.appendChild(node);
  });

  const lanes = document.createElement("div");
  lanes.className = "daily-lanes";
  sections.forEach((section, sectionIndex) => {
    const lane = document.createElement("section");
    lane.className = `daily-lane ${toneFor(sectionIndex)}`;
    const items = (section.items || [])
      .slice(0, 3)
      .map(
        (item, itemIndex) => `
          <a class="daily-item" href="${escapeHtml(itemHref(item))}" target="_blank" rel="noopener noreferrer">
            <span>${String(itemIndex + 1).padStart(2, "0")}</span>
            <div>
              <strong>${escapeHtml(itemTitle(item))}</strong>
              <em>${escapeHtml([item.time_label, item.source || item.site_name, `${Number(item.score || 0).toFixed(1)}/10`].filter(Boolean).join(" · "))}</em>
            </div>
          </a>
        `
      )
      .join("");
    lane.innerHTML = `
      <header>
        <span>${escapeHtml(section.title)}</span>
        <b>${fmtNumber(section.items?.length || 0)}</b>
      </header>
      ${items || '<div class="empty compact-empty">暂无条目</div>'}
    `;
    lanes.appendChild(lane);
  });

  dailyBriefEl.append(metaNode, lanes);
}

function renderDailySection(payload) {
  if (!dailyBriefEl) return;
  if (disabledSourceKeys.size || currentQuery()) {
    renderDailyFromPayload(payload);
    return;
  }
  renderDailyBrief(currentDailyMarkdown);
}

function buildSmallItem(item, index = 0) {
  const link = document.createElement("a");
  link.className = `small-item ${toneFor(index)}`;
  link.href = itemHref(item);
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.innerHTML = `
    <span class="small-time">${escapeHtml(item.time_label || fmtTime(item.published_at))}</span>
    <i aria-hidden="true"></i>
    <span class="small-main">
      <strong>${escapeHtml(itemTitle(item))}</strong>
      <em>${escapeHtml(item.site_name || item.source || "来源")}</em>
    </span>
  `;
  return link;
}

function renderCategories(payload) {
  const sections = Array.isArray(payload.sections) ? payload.sections : [];
  const maxCount = Math.max(...sections.map((section) => section.count || 0), 1);
  clear(categoryStackEl);
  sections.forEach((section, sectionIndex) => {
    const block = document.createElement("section");
    block.className = `category-block ${toneFor(sectionIndex)}`;
    const ratio = Math.round(((section.count || 0) / maxCount) * 100);
    const projectSources = Array.isArray(section.project_sources) ? section.project_sources : [];
    const sourceLabel = projectSources.length ? `融合 ${fmtNumber(projectSources.length)} 源` : "四源融合";
    const chips = (section.items || [])
      .slice(0, 10)
      .map(
        (item, itemIndex) => `
          <a href="${escapeHtml(itemHref(item))}" target="_blank" rel="noopener noreferrer">
            <span>${String(itemIndex + 1).padStart(2, "0")}</span>
            <strong>${escapeHtml(itemTitle(item))}</strong>
          </a>
        `
      )
      .join("");
    block.innerHTML = `
      <header>
        <div>
          <h3>${escapeHtml(section.title)}</h3>
          <em>${escapeHtml(sourceLabel)}</em>
          <div class="lane-track"><b style="width:${clamp(ratio, 6, 100)}%"></b></div>
        </div>
        <span>${fmtNumber(section.count)}</span>
      </header>
      <div class="lane-chips">${chips || '<span class="empty-chip">暂无信号</span>'}</div>
    `;
    categoryStackEl.appendChild(block);
  });
}

function renderNewItems(payload) {
  const items = Array.isArray(payload.new_items) ? payload.new_items : [];
  clear(newItemsEl);
  items.slice(0, 9).forEach((item, index) => newItemsEl.appendChild(buildSmallItem(item, index)));
  if (!newItemsEl.childElementCount) newItemsEl.innerHTML = '<div class="empty compact-empty">暂无新增</div>';
}

function renderChinaHotItems(payload) {
  const items = Array.isArray(payload.china_hot_items) ? payload.china_hot_items : [];
  clear(chinaHotItemsEl);
  items.slice(0, 16).forEach((item, index) => {
    const topicLabels = Array.isArray(item.topic_labels) ? item.topic_labels.filter(Boolean) : [];
    const hitLabel = topicLabels[0] || (item.ai_is_related ? "关键词命中" : "热榜观察");
    const displayRank = String(index + 1).padStart(2, "0");
    const originalRank = Number(item.rank || 0);
    const originalRankText = originalRank > 0 ? `原平台排名 #${originalRank}` : "原平台未提供排名";
    const link = document.createElement("a");
    link.className = `hot-item ${item.ai_is_related ? "ai-hit" : ""} ${toneFor(index)}`;
    link.href = itemHref(item);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = `${item.source || "中文热榜"} · ${originalRankText}`;
    link.innerHTML = `
      <span>${displayRank}</span>
      <div>
        <strong>${escapeHtml(itemTitle(item))}</strong>
        <em>${escapeHtml(item.source || "中文热榜")} · ${escapeHtml(hitLabel)}</em>
      </div>
    `;
    chinaHotItemsEl.appendChild(link);
  });
  if (!chinaHotItemsEl.childElementCount) chinaHotItemsEl.innerHTML = '<div class="empty compact-empty">暂无中文热榜</div>';
}

function fallbackSourceCatalog(payload) {
  const projects = new Map([
    ["ai_news_radar", { id: "ai_news_radar", name: "AI News Radar", sources: new Map() }],
    ["aihot", { id: "aihot", name: "AI HOT", sources: new Map() }],
    ["trendradar", { id: "trendradar", name: "TrendRadar", sources: new Map() }],
    ["horizon", { id: "horizon", name: "Horizon", sources: new Map() }],
  ]);
  const add = (item) => {
    const key = sourceKeyForItem(item);
    if (!key) return;
    const [projectId, kind = "source", sourceId = key] = key.split("::");
    const project = projects.get(projectId) || projects.get("ai_news_radar");
    if (!project.sources.has(key)) {
      project.sources.set(key, {
        id: sourceId,
        name: item?.source_ref?.name || item?.source || item?.site_name || sourceId,
        kind,
        kind_label: item?.source_ref?.kind_label || "来源",
        source_key: key,
        status: "ok",
        item_count: 0,
        ai_24h: 0,
      });
    }
    const source = project.sources.get(key);
    source.item_count += 1;
    source.ai_24h += 1;
  };

  (payload.sections || []).forEach((section) => (section.items || []).forEach(add));
  (payload.new_items || []).forEach(add);
  (payload.china_hot_items || []).forEach(add);

  return {
    projects: Array.from(projects.values()).map((project) => ({
      ...project,
      sources: Array.from(project.sources.values()),
      source_count: project.sources.size,
    })),
  };
}

function catalogProjects(payload) {
  const catalog = payload?.source_catalog;
  if (catalog && Array.isArray(catalog.projects) && catalog.projects.length) return catalog.projects;
  return fallbackSourceCatalog(payload).projects;
}

function renderSourceCatalog(payload) {
  if (!sourceCatalogEl) return;
  const projects = catalogProjects(payload);
  const allSources = projects.flatMap((project) => project.sources || []);
  const visibleSources = allSources.filter((source) => !disabledSourceKeys.has(source.source_key));

  if (sourceSummaryEl) {
    sourceSummaryEl.textContent = `已启用 ${fmtNumber(visibleSources.length)}/${fmtNumber(allSources.length)} 个来源，删除 ${fmtNumber(disabledSourceKeys.size)} 个`;
  }

  clear(sourceCatalogEl);
  projects.forEach((project, projectIndex) => {
    const sources = Array.isArray(project.sources) ? project.sources : [];
    const enabledCount = sources.filter((source) => !disabledSourceKeys.has(source.source_key)).length;
    const projectNode = document.createElement("section");
    projectNode.className = `source-project ${toneFor(projectIndex)}`;
    const rows = sources
      .map((source) => {
        const key = source.source_key;
        const disabled = disabledSourceKeys.has(key);
        const status = source.status === "error" ? "异常" : source.status === "watch" ? "观察" : "在线";
        return `
          <div class="source-row ${disabled ? "is-disabled" : ""}">
            <div class="source-name">
              <strong>${escapeHtml(source.name || source.id || "未命名来源")}</strong>
              <em>${escapeHtml(source.kind_label || source.kind || "来源")} · ${escapeHtml(source.id || "")}</em>
            </div>
            <span>${escapeHtml(status)}</span>
            <b>${fmtNumber(source.ai_24h || 0)} / ${fmtNumber(source.item_count || source.raw_24h || 0)}</b>
            <button type="button" data-source-key="${escapeHtml(key)}">${disabled ? "恢复" : "删除"}</button>
          </div>
        `;
      })
      .join("");
    projectNode.innerHTML = `
      <header>
        <div>
          <h3>${escapeHtml(project.name || project.id)}</h3>
          <em>${fmtNumber(enabledCount)}/${fmtNumber(sources.length)} 已启用 · ${escapeHtml(project.kind || "pipeline")}</em>
        </div>
        <strong>${fmtNumber(project.ai_24h || sources.reduce((sum, source) => sum + Number(source.ai_24h || 0), 0))}</strong>
      </header>
      <div class="source-rows">${rows || '<div class="empty">暂无来源</div>'}</div>
    `;
    sourceCatalogEl.appendChild(projectNode);
  });
}

function setUpdateState(message, isError = false) {
  if (!updateStatusEl) return;
  updateStatusEl.textContent = message;
  updateStatusEl.classList.toggle("error", isError);
}

function updateSetupMessage(payload) {
  if (!payload || !updateStatusEl) return;
  if (payload.manual_update_configured) {
    setUpdateState("自动更新已接入：后台约 30 分钟刷新一次；当前页面会自动同步最新数据。");
    return;
  }
  setUpdateState("自动更新暂未接入：当前只能查看已发布数据。", true);
}

async function loadUpdateStatus() {
  if (!updateStatusEl) return;
  try {
    const response = await fetch(`./api/status?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) return;
    const payload = await response.json().catch(() => null);
    updateSetupMessage(payload);
  } catch {
    // Static previews do not expose /api/status.
  }
}

async function runManualUpdate() {
  if (!updateNowBtnEl) return;
  updateNowBtnEl.disabled = true;
  updateNowBtnEl.textContent = "更新中...";
  setUpdateState("正在抓取中文热榜和 AI 信号，通常需要几十秒。");
  try {
    const response = await fetch("./api/update", { method: "POST" });
    const payload = await response.json().catch(() => ({}));
    if (payload.mode === "manual_update_unconfigured") {
      setUpdateState(payload.message || "手动更新暂不可用：请稍后再试。", true);
      await init();
      return;
    }
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || "更新任务启动失败，请稍后再试。");
    }
    if (payload.mode === "workflow_dispatch") {
      setUpdateState(payload.message || "已提交更新任务，通常 1-3 分钟后自动同步。");
      queueFollowupRefresh();
      return;
    }
    setUpdateState(`已更新：${fmtTime(payload.generated_at)}，正在刷新页面数据。`);
    await init();
  } catch (error) {
    setUpdateState(`更新没有启动：${error.message}`, true);
  } finally {
    updateNowBtnEl.disabled = false;
    updateNowBtnEl.textContent = "立即更新";
  }
}

async function loadDashboardData() {
  const stamp = Date.now();
  const [radarRes, dailyRes] = await Promise.all([
    fetch(`./data/radar-brief.json?t=${stamp}`, { cache: "no-store" }),
    fetch(`./data/daily-brief.zh.md?t=${stamp}`, { cache: "no-store" }),
  ]);
  if (!radarRes.ok) throw new Error(`加载 radar-brief.json 失败: ${radarRes.status}`);
  return {
    payload: await radarRes.json(),
    dailyMarkdown: dailyRes.ok ? await dailyRes.text() : "",
  };
}

function applyDashboardData(payload, dailyMarkdown = currentDailyMarkdown) {
  currentPayload = payload;
  currentDailyMarkdown = dailyMarkdown;
  renderDashboard(visiblePayload(payload));
}

async function refreshDataIfChanged({ force = false } = {}) {
  if (!force && document.hidden) return;
  try {
    const { payload, dailyMarkdown } = await loadDashboardData();
    const currentGeneratedAt = currentPayload?.generated_at || "";
    if (!force && payload.generated_at === currentGeneratedAt) return;
    applyDashboardData(payload, dailyMarkdown);
    if (currentGeneratedAt) {
      setUpdateState(`已自动同步：${fmtTime(payload.generated_at)}。`);
    }
  } catch {
    // Keep the current dashboard visible when a background refresh misses.
  }
}

function startAutoRefresh() {
  if (autoRefreshTimer) clearInterval(autoRefreshTimer);
  autoRefreshTimer = setInterval(() => refreshDataIfChanged(), autoRefreshMs);
}

function queueFollowupRefresh() {
  [45_000, 90_000, 150_000].forEach((delay) => {
    window.setTimeout(() => refreshDataIfChanged({ force: true }), delay);
  });
}

async function init() {
  try {
    const { payload, dailyMarkdown } = await loadDashboardData();
    applyDashboardData(payload, dailyMarkdown);
    await loadUpdateStatus();
    startAutoRefresh();
  } catch (error) {
    generatedAtEl.textContent = "加载失败";
    storyGridEl.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
}

function renderDashboard(payload) {
  generatedAtEl.textContent = fmtTime(payload.generated_at);
  renderActiveNav();
  renderMetrics(payload);
  renderStories(payload);
  renderCategories(payload);
  renderChinaHotItems(payload);
  renderNewItems(payload);
  renderDailySection(payload);
  if (currentPayload) renderSourceCatalog(currentPayload);
}

function renderActiveNav() {
  navLinkEls.forEach((link) => {
    link.classList.toggle("active", link.dataset.channel === activeChannel);
  });
}

function setActiveChannel(channel, shouldScroll = true) {
  if (!channelConfig[channel]) return;
  activeChannel = channel;
  if (currentPayload) renderDashboard(visiblePayload(currentPayload));
  if (!shouldScroll) return;
  const targetId = channelConfig[channel].target || "today";
  const target = document.getElementById(targetId);
  if (target) target.scrollIntoView({ block: "start", behavior: "smooth" });
  if (window.history?.replaceState) {
    window.history.replaceState(null, "", `#${targetId}`);
  }
}

if (updateNowBtnEl) {
  updateNowBtnEl.addEventListener("click", runManualUpdate);
}

navLinkEls.forEach((link) => {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    setActiveChannel(link.dataset.channel || "today", true);
  });
});

if (searchInputEl) {
  searchInputEl.addEventListener("input", () => {
    if (currentPayload) renderDashboard(visiblePayload(currentPayload));
  });
}

if (sourceCatalogEl) {
  sourceCatalogEl.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-source-key]");
    if (!button) return;
    const key = button.getAttribute("data-source-key");
    if (!key) return;
    if (disabledSourceKeys.has(key)) {
      disabledSourceKeys.delete(key);
    } else {
      disabledSourceKeys.add(key);
    }
    saveDisabledSources();
    if (currentPayload) renderDashboard(visiblePayload(currentPayload));
  });
}

if (resetSourcesBtnEl) {
  resetSourcesBtnEl.addEventListener("click", () => {
    disabledSourceKeys = new Set();
    saveDisabledSources();
    if (currentPayload) renderDashboard(visiblePayload(currentPayload));
  });
}

window.addEventListener("focus", () => refreshDataIfChanged());

init();
