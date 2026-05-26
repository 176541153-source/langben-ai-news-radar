#!/usr/bin/env python3
"""Explainable AI relevance scoring for news records."""

from __future__ import annotations

import re
from typing import Any

TRACKED_TOPIC_RULES: tuple[dict[str, str], ...] = (
    {"section": "ai_related", "label": "AI", "pattern": r"(?<![a-zA-Z])ai(?![a-zA-Z])"},
    {"section": "ai_related", "label": "人工智能", "pattern": r"人工智能"},
    {
        "section": "ai_related",
        "label": "OpenAI",
        "pattern": r"OpenAI|奥特曼|山姆|Sora|GPT|ChatGPT|DALL·E|DALL-E|(?<![A-Za-z0-9])o1(?![A-Za-z0-9])|Whisper|\bSam\s+Altman\b|\bGreg\s+Brockman\b",
    },
    {
        "section": "ai_related",
        "label": "Google Gemini",
        "pattern": r"Google Gemini|DeepMind|哈萨比斯|Veo|Nano Banana|Gemma|Bard|Imagen|\bGemini\b|\bDemis\s+Hassabis\b",
    },
    {"section": "ai_related", "label": "Anthropic", "pattern": r"Claude|Anthropic|阿莫迪|\bDario\s+Amodei\b"},
    {"section": "ai_related", "label": "Kimi(月之暗面)", "pattern": r"月之暗面|杨植麟|Kimi|Moonshot|\bMoonshot\s+AI\b"},
    {"section": "ai_related", "label": "MiniMax", "pattern": r"MiniMax|海螺|稀宇|闫俊杰|abab|\bHailuo\b"},
    {"section": "ai_related", "label": "通义千问", "pattern": r"通义|千问|通义千问|通义万相|Qwen|\bTongyi\b"},
    {"section": "ai_related", "label": "豆包", "pattern": r"豆包|字节AI|\bDoubao\b"},
    {"section": "ai_related", "label": "即梦", "pattern": r"即梦|Dreamina|剪映AI|\bJimeng\b"},
    {"section": "ai_related", "label": "腾讯元宝", "pattern": r"腾讯元宝|混元|Hunyuan|\bYuanbao\b"},
    {"section": "ai_related", "label": "可灵", "pattern": r"可灵|快手AI|Kling|\bKling\s+AI\b"},
    {
        "section": "ai_related",
        "label": "DeepSeek",
        "pattern": r"深度求索|幻方量化|梁文锋|DeepSeek|(?<![A-Za-z0-9])R1(?![A-Za-z0-9])|\bDeepSeek\b",
    },
    {
        "section": "ai_related",
        "label": "AI陪聊",
        "pattern": r"AI陪聊|AI伴侣|虚拟恋人|虚拟男友|虚拟女友|\bAI\s+Companion\b|\bCharacter\s+AI\b",
    },
    {"section": "ai_related", "label": "AI音箱", "pattern": r"AI音箱|智能音箱|小爱同学|小度|天猫精灵|\bSmart\s+Speaker\b"},
    {
        "section": "internet",
        "label": "阿里巴巴",
        "pattern": r"阿里|阿里巴巴|马云|蔡崇信|吴泳铭|淘宝|天猫|1688|闲鱼|菜鸟|盒马|阿里云|通义|千问|蚂蚁|支付宝|\bAlibaba\b|\bAliCloud\b|\bTaobao\b|\bTmall\b|\bAlipay\b|\bAnt\s+Group\b|\bCainiao\.me\b|\bQwen\b",
    },
    {"section": "internet", "label": "华为", "pattern": r"华为|任正非|余承东|鸿蒙|海思|昇腾|鲲鹏|\bHUAWEI\b|\bHarmonyOS\b|\bHiSilicon\b"},
    {"section": "internet", "label": "比亚迪", "pattern": r"比亚迪|王传福|方程豹|腾势|仰望|弗迪|刀片电池|云辇|\bBYD\b|\bDenza\b|\bYangwang\b"},
    {"section": "internet", "label": "大疆", "pattern": r"大疆|汪滔|灵眸|如影|\bDJI\b|\bRoboMaster\b|\bMavic\b|\bZenmuse\b"},
    {"section": "internet", "label": "宇树机器人", "pattern": r"宇树|王兴兴|\bUnitree\b"},
    {"section": "internet", "label": "京东", "pattern": r"京东|刘强东|\bJD\b|\bJingdong\b"},
    {"section": "internet", "label": "字节跳动", "pattern": r"字节|张一鸣|梁汝波|抖音|\bByteDance\b|\bTikTok\b|\bDouyin\b|\bLark\b|\bCapCut\b"},
    {"section": "internet", "label": "腾讯", "pattern": r"腾讯|鹅厂|马化腾|微信|QQ|天美|阅文集团|微众银行|\bTencent\b|\bPony\s+Ma\b|\bWeChat\b|\bLightSpeed\b|\bWeBank\b"},
    {"section": "internet", "label": "特斯拉", "pattern": r"特斯拉|马斯克|\bTesla\b|\bElon\s+Musk\b|\bCybertruck\b|\bModel\s+3\b|\bModel\s+Y\b|\bModel\s+S\b|\bModel\s+X\b|\bFSD\b"},
    {"section": "internet", "label": "苹果", "pattern": r"库克|\biPhone\b|\biPad\b|\bMacBook\b|\biOS\b|\bVision\s+Pro\b|\bAirPods\b|\bApple\b|\bTim\s+Cook\b"},
    {"section": "internet", "label": "微软", "pattern": r"微软|\bMicrosoft\b|\bWindows\b|\bAzure\b|\bSatya\s+Nadella\b|\bCopilot\b"},
    {"section": "internet", "label": "谷歌", "pattern": r"谷歌|皮查伊|安卓|油管|\bGoogle\b|\bAlphabet\b|\bAndroid\b|\bChrome\b|\bYouTube\b|\bGemini\b|\bDeepMind\b|\bWaymo\b"},
    {"section": "internet", "label": "猫箱", "pattern": r"猫箱|\bMaoxiang\b"},
    {"section": "internet", "label": "星野", "pattern": r"星野|稀宇|闫俊杰|Glow|Talkie|\bMiniMax\b|\bXingye\b"},
    {"section": "internet", "label": "筑梦岛", "pattern": r"筑梦岛|\bZhumengdao\b"},
)

TRACKED_TOPIC_PATTERNS = tuple(
    (rule, re.compile(rule["pattern"], re.I)) for rule in TRACKED_TOPIC_RULES
)

AI_KEYWORDS = [
    "agent view",
    "agent skills",
    "for agents",
    "parallel agent",
    "并行 agent",
    "known agents",
    "hermes-agent",
    "agentmemory",
    "cursor",
    "aigc",
    "llm",
    "gpt",
    "claude",
    "gemini",
    "deepseek",
    "openai",
    "anthropic",
    "copilot",
    "codex",
    "mcp",
    "hugging face",
    "huggingface",
    "transformer",
    "prompt",
    "diffusion",
    "多模态",
    "交互模型",
    "变换器",
    "语言模型",
    "视觉语言模型",
    "基础模型",
    "本地模型",
    "具身智能",
    "大模型",
    "人工智能",
    "机器学习",
    "深度学习",
    "智能体",
    "算力",
    "推理",
    "微调",
]

TECH_KEYWORDS = [
    "robot",
    "robotics",
    "embodied",
    "autonomous",
    "vision",
    "chip",
    "semiconductor",
    "cuda",
    "npu",
    "gpu",
    "cloud",
    "developer",
    "sandbox",
    "context",
    "开源",
    "技术",
    "编程",
    "软件",
    "沙箱",
    "上下文",
    "芯片",
    "机器人",
    "具身",
]

NOISE_KEYWORDS = [
    "娱乐",
    "明星",
    "八卦",
    "足球",
    "篮球",
    "彩票",
    "情感",
    "旅游",
    "美食",
]

COMMERCE_NOISE_KEYWORDS = [
    "淘宝",
    "天猫",
    "京东",
    "拼多多",
    "券后",
    "热销总榜",
    "促销",
    "优惠",
    "补贴",
    "下单",
    "首发价",
]

TOPHUB_ALLOW_KEYWORDS = [
    "readhub · ai",
    "hacker news",
    "github",
    "product hunt",
    "v2ex",
    "少数派",
    "infoq",
    "36氪",
    "机器之心",
    "量子位",
    "科技",
    "人工智能",
    "机器人",
    "具身",
    "开源",
]

TOPHUB_BLOCK_KEYWORDS = [
    "热销总榜",
    "淘宝",
    "天猫",
    "京东",
    "拼多多",
    "抖音",
    "快手",
    "微博",
    "小红书",
]

EN_SIGNAL_RE = re.compile(
    r"(?i)(?<![a-z0-9])(ai|aigc|llm|gpt|openai|anthropic|deepseek|gemini|claude|robot|robotics|embodied|autonomous|machine learning|artificial intelligence|transformer|diffusion|agent)(?![a-z0-9])"
)
MEANINGFUL_EN_SIGNAL_RE = re.compile(
    r"(?i)(?<![a-z0-9])(ai|aigc|llm|gpt|openai|anthropic|deepseek|gemini|claude|robot|robotics|embodied|autonomous|machine learning|artificial intelligence|transformer|diffusion)(?![a-z0-9])"
)
BROAD_AI_TERMS = {"agent", "模型", "推理"}
AI_RELEVANCE_THRESHOLD = 0.65

SOURCE_PRIORS = {
    "official_ai": 0.35,
    "aibase": 0.45,
    "aihot": 0.45,
    "aihubtoday": 0.45,
    "followbuilders": 0.25,
    "opmlrss": 0.15,
    "xapi": 0.15,
}
AI_DEFAULT_SOURCES = {"aibase", "aihot", "aihubtoday"}

LABEL_KEYWORDS = [
    ("model_release", ["model", "gpt", "claude", "gemini", "deepseek", "llm", "模型", "大模型", "发布", "release"]),
    ("developer_tool", ["copilot", "codex", "mcp", "api", "sdk", "developer", "开发者", "编程", "代码", "coding"]),
    ("agent_workflow", ["agent", "智能体", "workflow", "工作流", "tool use", "function calling"]),
    ("research_paper", ["paper", "arxiv", "research", "benchmark", "eval", "论文", "研究", "评测", "榜单"]),
    ("infra_compute", ["gpu", "npu", "cuda", "chip", "semiconductor", "算力", "芯片", "推理"]),
    ("robotics", ["robot", "robotics", "embodied", "机器人", "具身"]),
    ("industry_business", ["funding", "acquire", "融资", "收购", "估值", "营收", "公司"]),
    ("ai_product_update", ["openai", "anthropic", "google", "perplexity", "cursor", "产品", "上线", "更新"]),
]


def unique_preserve_order(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        s = str(value or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def contains_any_keyword(haystack: str, keywords: list[str]) -> bool:
    h = haystack.lower()
    return any(k in h for k in keywords)


def matched_keywords(haystack: str, keywords: list[str]) -> list[str]:
    h = haystack.lower()
    return sorted({k for k in keywords if k in h})


def contains_meaningful_ai_signal(haystack: str) -> bool:
    h = haystack.lower()
    if MEANINGFUL_EN_SIGNAL_RE.search(h):
        return True
    return any(k in h for k in AI_KEYWORDS if k not in BROAD_AI_TERMS)


def matched_topic_rules(haystack: str) -> list[dict[str, str]]:
    text = str(haystack or "")
    matches: list[dict[str, str]] = []
    for rule, pattern in TRACKED_TOPIC_PATTERNS:
        if pattern.search(text):
            matches.append({"section": rule["section"], "label": rule["label"]})
    return matches


def _label_for_text(text: str, has_tech: bool, topic_matches: list[dict[str, str]] | None = None) -> str:
    topic_labels = {row["label"] for row in topic_matches or []}
    topic_sections = {row["section"] for row in topic_matches or []}
    if "AI陪聊" in topic_labels:
        return "ai_companion"
    if "AI音箱" in topic_labels:
        return "ai_speaker"
    if "internet" in topic_sections and "ai_related" not in topic_sections:
        return "industry_business"
    for label, keywords in LABEL_KEYWORDS:
        if contains_any_keyword(text, keywords):
            return label
    if has_tech:
        return "ai_tech"
    return "ai_general"


def _result(
    *,
    is_ai_related: bool,
    score: float,
    label: str,
    reason: str,
    signals: list[str] | None = None,
    noise: list[str] | None = None,
    topic_matches: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    topics = topic_matches or []
    return {
        "is_ai_related": bool(is_ai_related),
        "score": round(max(0.0, min(1.0, score)), 2),
        "label": label,
        "reason": reason,
        "signals": signals or [],
        "noise": noise or [],
        "topic_labels": unique_preserve_order([row["label"] for row in topics]),
        "topic_sections": unique_preserve_order([row["section"] for row in topics]),
    }


def score_ai_relevance(record: dict[str, Any]) -> dict[str, Any]:
    """Return an explainable relevance score while preserving the old keep/drop behavior."""
    site_id = str(record.get("site_id") or "")
    title = str(record.get("title") or "")
    source = str(record.get("source") or "")
    site_name = str(record.get("site_name") or "")
    url = str(record.get("url") or "")
    text_raw = f"{title} {source} {site_name} {url}"
    text = text_raw.lower()

    topic_text = f"{title} {url}"
    topic_matches = matched_topic_rules(topic_text)
    topic_labels = unique_preserve_order([row["label"] for row in topic_matches])
    topic_sections = {row["section"] for row in topic_matches}
    has_tracked_topic = bool(topic_matches)
    has_tracked_ai = "ai_related" in topic_sections
    has_tracked_internet = "internet" in topic_sections

    ai_signals = unique_preserve_order(matched_keywords(text, AI_KEYWORDS) + topic_labels)
    tech_signals = matched_keywords(text, TECH_KEYWORDS)
    noise = matched_keywords(text, NOISE_KEYWORDS) + matched_keywords(text, COMMERCE_NOISE_KEYWORDS)
    source_prior = SOURCE_PRIORS.get(site_id, 0.0)

    if site_id == "zeli":
        if "24h" in source.lower() or "24h最热" in source:
            return _result(
                is_ai_related=True,
                score=max(AI_RELEVANCE_THRESHOLD, 0.62 + source_prior),
                label="curated_hotlist",
                reason="zeli_24h_hot_allowlist",
                signals=["zeli_24h_hot"],
                noise=noise,
                topic_matches=topic_matches,
            )
        return _result(
            is_ai_related=False,
            score=0.2,
            label="source_scope_drop",
            reason="zeli_only_keeps_24h_hot_source",
            signals=ai_signals + tech_signals,
            noise=noise,
            topic_matches=topic_matches,
        )

    if site_id == "tophub":
        source_l = source.lower()
        if contains_any_keyword(source_l, TOPHUB_BLOCK_KEYWORDS) and not has_tracked_topic:
            return _result(
                is_ai_related=False,
                score=0.05,
                label="noise",
                reason="tophub_blocked_channel",
                signals=ai_signals + tech_signals,
                noise=noise or matched_keywords(source_l, TOPHUB_BLOCK_KEYWORDS),
                topic_matches=topic_matches,
            )
        if not contains_any_keyword(source_l, TOPHUB_ALLOW_KEYWORDS) and not has_tracked_topic:
            return _result(
                is_ai_related=False,
                score=0.12,
                label="source_scope_drop",
                reason="tophub_channel_not_in_allowlist",
                signals=ai_signals + tech_signals,
                noise=noise,
                topic_matches=topic_matches,
            )

    if site_id in AI_DEFAULT_SOURCES:
        return _result(
            is_ai_related=True,
            score=max(AI_RELEVANCE_THRESHOLD, 0.72 + source_prior),
            label=_label_for_text(text, bool(tech_signals), topic_matches),
            reason="trusted_ai_source_default_keep",
            signals=ai_signals or [site_id],
            noise=noise,
            topic_matches=topic_matches,
        )

    has_ai = contains_meaningful_ai_signal(text) or has_tracked_ai
    has_broad_ai = contains_any_keyword(text, list(BROAD_AI_TERMS)) or EN_SIGNAL_RE.search(text) is not None
    has_tech = bool(tech_signals)

    if not (has_ai or has_tracked_topic or (has_broad_ai and has_tech)):
        return _result(
            is_ai_related=False,
            score=source_prior + (0.32 if has_broad_ai else 0.0) + (0.08 if has_tech else 0.0),
            label="not_ai",
            reason="missing_meaningful_ai_signal",
            signals=ai_signals + tech_signals,
            noise=noise,
            topic_matches=topic_matches,
        )

    if contains_any_keyword(text, COMMERCE_NOISE_KEYWORDS) and not (has_ai or has_tracked_topic):
        return _result(
            is_ai_related=False,
            score=0.25 + source_prior,
            label="commerce_noise",
            reason="commerce_noise_without_strong_ai_signal",
            signals=ai_signals + tech_signals,
            noise=noise,
            topic_matches=topic_matches,
        )

    if contains_any_keyword(text, NOISE_KEYWORDS) and not (has_ai or has_tracked_topic):
        return _result(
            is_ai_related=False,
            score=0.25 + source_prior,
            label="noise",
            reason="noise_without_strong_ai_signal",
            signals=ai_signals + tech_signals,
            noise=noise,
            topic_matches=topic_matches,
        )

    score = source_prior + (0.52 if has_ai else 0.34) + min(0.18, 0.04 * len(ai_signals)) + min(0.12, 0.03 * len(tech_signals))
    if noise:
        score -= min(0.18, 0.04 * len(noise))
    if has_broad_ai and has_tech and not has_ai:
        score = max(score, AI_RELEVANCE_THRESHOLD)
    if has_ai:
        score = max(score, AI_RELEVANCE_THRESHOLD)
    if has_tracked_ai:
        score = max(score, 0.78)
    if has_tracked_internet:
        score = max(score, AI_RELEVANCE_THRESHOLD)

    return _result(
        is_ai_related=True,
        score=score,
        label=_label_for_text(text, has_tech, topic_matches),
        reason=(
            "matched_tracked_ai_keyword"
            if has_tracked_ai
            else "matched_tracked_internet_keyword"
            if has_tracked_internet
            else "matched_ai_signal"
            if has_ai
            else "matched_broad_ai_plus_tech_signal"
        ),
        signals=ai_signals + tech_signals,
        noise=noise,
        topic_matches=topic_matches,
    )


def is_ai_related_record(record: dict[str, Any]) -> bool:
    return bool(score_ai_relevance(record)["is_ai_related"])


def add_ai_relevance_fields(record: dict[str, Any]) -> dict[str, Any]:
    relevance = score_ai_relevance(record)
    out = dict(record)
    out["ai_is_related"] = relevance["is_ai_related"]
    out["ai_score"] = relevance["score"]
    out["ai_label"] = relevance["label"]
    out["ai_relevance_reason"] = relevance["reason"]
    out["ai_signals"] = relevance["signals"]
    out["ai_noise"] = relevance["noise"]
    out["topic_labels"] = relevance["topic_labels"]
    out["topic_sections"] = relevance["topic_sections"]
    return out
