#!/usr/bin/env python3
"""Aggregate updates from multiple AI news sites and produce 24h snapshot data."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parseaddr
import hashlib
import html as html_lib
import json
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from scripts.ai_relevance import add_ai_relevance_fields, score_ai_relevance
except ModuleNotFoundError:  # pragma: no cover - direct `python scripts/update_news.py`
    from ai_relevance import add_ai_relevance_fields, score_ai_relevance

try:
    import feedparser
except ModuleNotFoundError:
    feedparser = None

UTC = timezone.utc
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
SH_TZ = ZoneInfo("Asia/Shanghai")
WAYTOAGI_DEFAULT = (
    "https://waytoagi.feishu.cn/wiki/QPe5w5g7UisbEkkow8XcDmOpn8e?fromScene=spaceOverview"
)
WAYTOAGI_HISTORY_FALLBACK = "https://waytoagi.feishu.cn/wiki/FjiOwWp2giA7hRk6jjfcPioCnAc"

RSS_FEED_REPLACEMENTS: dict[str, str] = {
    "https://rsshub.app/infoq/recommend": "https://www.infoq.cn/feed",
    "https://rsshub.app/huggingface/blog-zh": "https://huggingface.co/blog/feed.xml",
    "https://rsshub.app/readhub/daily": "https://readhub.cn/rss",
    "https://rsshub.app/36kr/hot-list": "https://36kr.com/feed",
    "https://rsshub.app/sspai/index": "https://sspai.com/feed",
    "https://rsshub.app/sspai/matrix": "https://sspai.com/feed",
    "https://rsshub.app/meituan/tech": "https://tech.meituan.com/feed",
    "https://mjg59.dreamwidth.org/data/rss": "http://mjg59.dreamwidth.org/data/rss",
}

RSS_FEED_SKIP_PREFIXES: tuple[str, ...] = (
    "https://rsshub.app/telegram/channel/",
    "https://rsshub.app/jike/",
    "https://rsshub.app/bilibili/",
    "https://rsshub.app/zhihu/",
    "https://rsshub.app/xiaoyuzhou/podcast/",
    "https://rsshub.app/xyzrank",
    "https://rsshub.app/mittrchina/hot",
    "https://wechat2rss.bestblogs.dev/",
    "https://werss.bestblogs.dev/",
    "http://47.122.94.119:18080/",
)

RSS_FEED_SKIP_EXACT: set[str] = {
    "https://rachelbythebay.com/w/atom.xml",
    "https://flak.tedunangst.com/rss",
}

OFFICIAL_AI_FEEDS: tuple[dict[str, str], ...] = (
    {
        "title": "OpenAI News",
        "xml_url": "https://openai.com/news/rss.xml",
        "html_url": "https://openai.com/news",
    },
    {
        "title": "Google DeepMind",
        "xml_url": "https://deepmind.google/blog/rss.xml",
        "html_url": "https://deepmind.google/blog",
    },
    {
        "title": "Google AI Blog",
        "xml_url": "https://blog.google/innovation-and-ai/technology/ai/rss/",
        "html_url": "https://blog.google/innovation-and-ai/technology/ai/",
    },
    {
        "title": "Hugging Face Blog",
        "xml_url": "https://huggingface.co/blog/feed.xml",
        "html_url": "https://huggingface.co/blog",
    },
    {
        "title": "GitHub AI & ML",
        "xml_url": "https://github.blog/ai-and-ml/feed/",
        "html_url": "https://github.blog/ai-and-ml/",
    },
    {
        "title": "GitHub Changelog",
        "xml_url": "https://github.blog/changelog/feed/",
        "html_url": "https://github.blog/changelog/",
    },
    {
        "title": "OpenAI Skills",
        "xml_url": "https://github.com/openai/skills/commits/main.atom",
        "html_url": "https://github.com/openai/skills",
        "include_keywords": "hatch,pet,migrate-to-codex",
    },
)
OFFICIAL_AI_MAX_AGE_DAYS = 45
AIBREAKFAST_JINA_URL = "https://r.jina.ai/https://aibreakfast.beehiiv.com/"
AIHOT_FEED_URL = "https://aihot.virxact.com/feed.xml"
AIHOT_FALLBACK_FEED_URLS = (
    "https://aihot.virxact.com/rss.xml",
    "https://aihot.virxact.com/feed",
    "https://aihot.virxact.com/feed/daily.xml",
)
FOLLOW_BUILDERS_FEED_BASE = "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main"
AGENTMAIL_API_BASE_DEFAULT = "https://api.agentmail.to"
AGENTMAIL_DIGEST_FILE = "email-digest.json"
AGENTMAIL_DEFAULT_LIMIT = 50
X_API_BASE_DEFAULT = "https://api.x.com"
X_API_POST_READ_COST_USD = 0.005
X_API_DEFAULT_QUERY = '(AI OR "artificial intelligence" OR "large language model" OR LLM) lang:en -is:retweet has:links'
X_API_DEFAULT_MAX_RESULTS = 20
X_API_MAX_QUERY_CHARS = 512
UPSTREAM_CONFIG_DEFAULT = "config/upstream-sources.json"


@dataclass
class RawItem:
    site_id: str
    site_name: str
    source: str
    title: str
    url: str
    published_at: datetime | None
    meta: dict[str, Any]


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def iso(dt: datetime | None) -> str | None:
    if not dt:
        return None
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    try:
        dt = dtparser.parse(dt_str)
    except Exception:
        return None
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def normalize_url(raw_url: str) -> str:
    try:
        parsed = urlparse(raw_url.strip())
        if not parsed.scheme:
            return raw_url.strip()
        query = []
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            lk = k.lower()
            if lk.startswith("utm_"):
                continue
            if lk in {
                "ref",
                "spm",
                "fbclid",
                "gclid",
                "igshid",
                "mkt_tok",
                "mc_cid",
                "mc_eid",
                "_hsenc",
                "_hsmi",
            }:
                continue
            query.append((k, v))
        parsed = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            fragment="",
            query=urlencode(query, doseq=True),
        )
        normalized = urlunparse(parsed)
        return normalized.rstrip("/")
    except Exception:
        return raw_url.strip()


def host_of_url(raw_url: str) -> str:
    try:
        return urlparse(raw_url).netloc.lower()
    except Exception:
        return ""


def first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        s = str(value).strip()
        if s:
            return s
    return ""


def maybe_fix_mojibake(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return s
    # Common mojibake signature from UTF-8 bytes decoded as Latin-1.
    if re.search(r"[Ãâåèæïð]|[\x80-\x9f]|æ|ç|å|é", s) is None:
        return s
    for enc in ("latin1", "cp1252"):
        try:
            fixed = s.encode(enc).decode("utf-8")
            if fixed and fixed != s:
                return fixed
        except Exception:
            continue
    return s


def has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def is_mostly_english(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    if has_cjk(s):
        return False
    letters = re.findall(r"[A-Za-z]", s)
    return len(letters) >= max(6, len(s) // 4)


def parse_feed_entries_via_xml(feed_xml: bytes) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    try:
        root = ET.fromstring(feed_xml)
    except Exception:
        return out

    for tag in (".//item", ".//{*}item", ".//entry", ".//{*}entry"):
        for node in root.findall(tag):
            title = (
                node.findtext("title")
                or node.findtext("{*}title")
                or ""
            ).strip()
            link = ""
            link_node = node.find("link")
            if link_node is None:
                link_node = node.find("{*}link")
            if link_node is not None:
                link = (link_node.get("href") or link_node.text or "").strip()
            if not link:
                link = (node.findtext("{*}link") or node.findtext("link") or "").strip()
            published = (
                node.findtext("pubDate")
                or node.findtext("{*}pubDate")
                or node.findtext("published")
                or node.findtext("{*}published")
                or node.findtext("updated")
                or node.findtext("{*}updated")
            )
            if title and link:
                key = (title, link)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"title": title, "link": link, "published": published})
    return out


def make_item_id(site_id: str, source: str, title: str, url: str) -> str:
    key = "||".join(
        [
            site_id.strip().lower(),
            source.strip().lower(),
            title.strip().lower(),
            normalize_url(url),
        ]
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def parse_unix_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        n = float(value)
    except Exception:
        return None
    if n > 10_000_000_000:
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n, tz=UTC)
    except Exception:
        return None


def parse_relative_time_zh(text: str, now: datetime) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None

    m = re.search(r"(\d+)\s*分钟前", text)
    if m:
        return now - timedelta(minutes=int(m.group(1)))

    m = re.search(r"(\d+)\s*小时前", text)
    if m:
        return now - timedelta(hours=int(m.group(1)))

    m = re.search(r"(\d+)\s*天前", text)
    if m:
        return now - timedelta(days=int(m.group(1)))

    if "刚刚" in text:
        return now

    if "昨天" in text:
        return now - timedelta(days=1)

    m = re.fullmatch(r"(?:今天)?\s*(\d{1,2}):(\d{2})", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > now + timedelta(minutes=5):
            candidate -= timedelta(days=1)
        return candidate

    m = re.fullmatch(r"昨天\s*(\d{1,2}):(\d{2})", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        return (now - timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)

    m = re.fullmatch(r"(?:\d{4}年\s*)?(\d{1,2})月(\d{1,2})日", text)
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        year = now.year
        try:
            candidate = datetime(year, month, day, tzinfo=UTC)
            if candidate > now + timedelta(days=2):
                candidate = datetime(year - 1, month, day, tzinfo=UTC)
            return candidate
        except Exception:
            return None

    return None


def parse_date_any(value: Any, now: datetime) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.astimezone(UTC)

    if isinstance(value, (int, float)):
        return parse_unix_timestamp(value)

    s = str(value).strip()
    if not s:
        return None

    if s.startswith("$D"):
        s = s[2:]

    if re.fullmatch(r"\d{12,}", s):
        return parse_unix_timestamp(int(s))

    if re.fullmatch(r"\d{9,11}", s):
        return parse_unix_timestamp(int(s))

    dt = parse_relative_time_zh(s, now)
    if dt:
        return dt

    # TechURLs format: 2026-02-19 11:54:21AM UTC
    m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2}[AP]M)\s+UTC", s)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%d %I:%M:%S%p")
            return dt.replace(tzinfo=UTC)
        except Exception:
            pass

    try:
        dt = dtparser.parse(s, tzinfos={"UT": 0, "UTC": 0, "GMT": 0})
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def decode_escaped_json(raw: str) -> dict[str, Any] | None:
    s = raw.replace('\\"', '"').replace("\\/", "/")
    try:
        return json.loads(s)
    except Exception:
        return None


def extract_waytoagi_history_url(root_html: str) -> str:
    pattern = r'\{\\"id\\":\\"[^\"]+\\",\\"type\\":\\"mention_doc\\",\\"data\\":\{[^\}]+\}\}'
    for raw in re.findall(pattern, root_html):
        obj = decode_escaped_json(raw)
        if not obj:
            continue
        data = obj.get("data", {})
        title = str(data.get("title") or "")
        if "历史更新" in title or "更新日志" in title:
            raw_url = str(data.get("raw_url") or "").strip()
            if raw_url:
                return raw_url
    return WAYTOAGI_HISTORY_FALLBACK


def extract_feishu_client_vars(page_html: str) -> dict[str, Any]:
    marker = "window.DATA = Object.assign({}, window.DATA, { clientVars: Object("
    idx = page_html.find(marker)
    if idx == -1:
        raise ValueError("Cannot locate Feishu clientVars marker")

    start = idx + len(marker)
    depth = 1
    in_str = False
    escaped = False
    end = None

    for i, ch in enumerate(page_html[start:], start):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue

        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end is None:
        raise ValueError("Cannot parse Feishu clientVars payload")

    payload = page_html[start:end]
    return json.loads(payload)


def block_text(block_data: dict[str, Any]) -> str:
    text_obj = block_data.get("text", {}) if isinstance(block_data, dict) else {}
    initial = text_obj.get("initialAttributedTexts", {}).get("text", {}) if isinstance(text_obj, dict) else {}
    if not isinstance(initial, dict):
        return ""

    def key_int(k: Any) -> int:
        try:
            return int(k)
        except Exception:
            return 0

    return "".join(str(v) for k, v in sorted(initial.items(), key=lambda kv: key_int(kv[0]))).strip()


def clean_update_title(text: str) -> str:
    text = text.replace("《 》", "").replace("《》", "")
    return re.sub(r"\s+", " ", text).strip()


def parse_ym_heading(text: str) -> tuple[int, int] | None:
    m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def parse_md_heading(text: str) -> tuple[int, int] | None:
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def infer_shanghai_year_for_month_day(now_sh: datetime, month: int, day: int) -> int | None:
    year = now_sh.year
    try:
        candidate = date(year, month, day)
    except Exception:
        return None
    if candidate > (now_sh.date() + timedelta(days=2)):
        year -= 1
    return year


def extract_waytoagi_recent_updates_from_block_map(
    block_map: dict[str, Any],
    now_sh: datetime,
    page_url: str,
) -> list[dict[str, Any]]:
    if not isinstance(block_map, dict) or not block_map:
        return []

    ym_by_heading2: dict[str, tuple[int, int]] = {}
    near_log_parent_ids: set[str] = set()

    for bid, block in block_map.items():
        bd = block.get("data", {})
        btype = bd.get("type")
        if btype not in {"heading1", "heading2", "heading3"}:
            continue
        heading_text = block_text(bd)
        if "近7日更新日志" in heading_text or "近 7 日更新日志" in heading_text:
            parent_id = str(bd.get("parent_id") or "").strip()
            if parent_id:
                near_log_parent_ids.add(parent_id)

    heading3_dates: dict[str, date] = {}

    for bid, block in block_map.items():
        bd = block.get("data", {})
        if bd.get("type") != "heading2":
            continue
        ym = parse_ym_heading(block_text(bd))
        if ym:
            ym_by_heading2[bid] = ym

    for bid, block in block_map.items():
        bd = block.get("data", {})
        if bd.get("type") != "heading3":
            continue
        md = parse_md_heading(block_text(bd))
        if not md:
            continue
        month, day = md
        parent = bd.get("parent_id")
        if near_log_parent_ids and parent not in near_log_parent_ids:
            continue
        year = ym_by_heading2.get(parent, (now_sh.year, month))[0]
        inferred = infer_shanghai_year_for_month_day(now_sh, month, day)
        if inferred is not None:
            year = inferred
        try:
            heading3_dates[bid] = date(year, month, day)
        except Exception:
            continue

    parent_map: dict[str, str] = {}
    for bid, block in block_map.items():
        bd = block.get("data", {})
        parent = str(bd.get("parent_id") or "").strip()
        if parent:
            parent_map[bid] = parent

    def nearest_heading_date(block_id: str) -> date | None:
        cur = parent_map.get(block_id)
        hops = 0
        while cur and hops < 20:
            if cur in heading3_dates:
                return heading3_dates[cur]
            cur = parent_map.get(cur)
            hops += 1
        return None

    updates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for bid, block in block_map.items():
        bd = block.get("data", {})
        if bd.get("type") not in {"bullet", "text", "todo", "ordered"}:
            continue

        day = nearest_heading_date(bid)
        if not day:
            continue
        title = clean_update_title(block_text(bd))
        if not title:
            continue
        key = (day.isoformat(), title)
        if key in seen:
            continue
        seen.add(key)
        updates.append({"date": day.isoformat(), "title": title, "url": page_url})

    return updates


def fetch_waytoagi_recent_7d(session: requests.Session, now_utc: datetime, root_url: str) -> dict[str, Any]:
    now_sh = now_utc.astimezone(SH_TZ)
    root_html = session.get(root_url, timeout=30).text
    history_url = extract_waytoagi_history_url(root_html)

    root_client_vars = extract_feishu_client_vars(root_html)
    root_block_map = root_client_vars.get("data", {}).get("block_map", {})
    updates: list[dict[str, Any]] = extract_waytoagi_recent_updates_from_block_map(root_block_map, now_sh, root_url)

    if history_url and history_url != root_url:
        try:
            history_html = session.get(history_url, timeout=30).text
            history_client_vars = extract_feishu_client_vars(history_html)
            history_block_map = history_client_vars.get("data", {}).get("block_map", {})
            updates.extend(
                extract_waytoagi_recent_updates_from_block_map(history_block_map, now_sh, history_url)
            )
        except Exception:
            pass

    dedup_updates: dict[tuple[str, str], dict[str, Any]] = {}
    for item in updates:
        key = (str(item.get("date") or ""), str(item.get("title") or ""))
        if key[0] and key[1] and key not in dedup_updates:
            dedup_updates[key] = item

    start_date = now_sh.date() - timedelta(days=6)
    end_date = now_sh.date()
    recent = [
        u
        for u in dedup_updates.values()
        if start_date <= date.fromisoformat(str(u.get("date") or "1970-01-01")) <= end_date
    ]
    recent.sort(key=lambda x: (x["date"], x["title"]), reverse=True)
    latest_date = recent[0]["date"] if recent else None
    updates_today = [u for u in recent if u.get("date") == latest_date] if latest_date else []

    warning = "近7日未解析到更新条目" if not recent else None
    return {
        "generated_at": iso(now_utc),
        "timezone": "Asia/Shanghai",
        "root_url": root_url,
        "history_url": history_url,
        "window_days": 7,
        "latest_date": latest_date,
        "count_today": len(updates_today),
        "updates_today": updates_today,
        "count_7d": len(recent),
        "updates_7d": recent,
        "warning": warning,
        "has_error": False,
        "error": None,
    }


def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    return session


def extract_next_f_merged(html: str) -> str:
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html, re.S)
    if not chunks:
        return ""
    merged = "".join(chunks)
    try:
        return bytes(merged, "utf-8").decode("unicode_escape")
    except Exception:
        return merged


def extract_balanced_json(decoded: str, key: str) -> Any:
    idx = decoded.find(key)
    if idx == -1:
        raise ValueError(f"Key not found: {key}")

    start = idx + len(key)
    while start < len(decoded) and decoded[start] != ":":
        start += 1
    start += 1
    while start < len(decoded) and decoded[start] not in "[{":
        start += 1

    open_ch = decoded[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    end = None

    for i, ch in enumerate(decoded[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

    if end is None:
        raise ValueError(f"Cannot parse JSON block for key: {key}")

    snippet = decoded[start:end]
    snippet = snippet.replace("$undefined", "null")
    snippet = re.sub(r'"\$D([^\"]+)"', r'"\1"', snippet)
    return json.loads(snippet)


def extract_next_data_payload(html: str) -> dict[str, Any] | None:
    m = re.search(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>\s*(\{.*?\})\s*</script>',
        html,
        re.S,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def fetch_techurls(session: requests.Session, now: datetime) -> list[RawItem]:
    site_id = "techurls"
    site_name = "TechURLs"
    r = session.get("https://techurls.com/", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    out: list[RawItem] = []
    for block in soup.select("div.publisher-block"):
        primary = (
            block.select_one(".publisher-text .primary").get_text(strip=True)
            if block.select_one(".publisher-text .primary")
            else block.get("data-publisher", "unknown")
        )
        secondary = (
            block.select_one(".publisher-text .secondary").get_text(strip=True)
            if block.select_one(".publisher-text .secondary")
            else ""
        )
        source = f"{primary} · {secondary}" if secondary and secondary != primary else primary

        for link_row in block.select("div.publisher-link"):
            a = link_row.select_one("a.article-link")
            if not a or not a.get("href"):
                continue
            title = a.get_text(" ", strip=True)
            url = a["href"].strip()

            time_hint = ""
            aside = link_row.select_one(".aside .text")
            if aside:
                time_hint = aside.get("title", "") or aside.get_text(" ", strip=True)

            published = parse_date_any(time_hint, now)
            out.append(
                RawItem(
                    site_id=site_id,
                    site_name=site_name,
                    source=source,
                    title=title,
                    url=url,
                    published_at=published,
                    meta={"time_hint": time_hint},
                )
            )

    return out


def fetch_buzzing(session: requests.Session, now: datetime) -> list[RawItem]:
    site_id = "buzzing"
    site_name = "Buzzing"
    r = session.get("https://www.buzzing.cc/feed.json", timeout=30)
    r.raise_for_status()
    payload = r.json()
    items = payload.get("items", [])

    out: list[RawItem] = []
    for it in items:
        title = (it.get("title") or "").strip()
        url = (it.get("url") or "").strip()
        if not title or not url:
            continue
        source = first_non_empty(
            it.get("source"),
            it.get("site_name"),
            it.get("channel"),
            it.get("category"),
            host_of_url(url),
            site_name,
        )
        published = parse_date_any(it.get("date_published") or it.get("date_modified"), now)
        out.append(
            RawItem(
                site_id=site_id,
                site_name=site_name,
                source=source,
                title=title,
                url=url,
                published_at=published,
                meta={"raw": {k: it.get(k) for k in ("source", "site_name", "channel", "category")}},
            )
        )
    return out


def fetch_iris(session: requests.Session, now: datetime) -> list[RawItem]:
    site_id = "iris"
    site_name = "Info Flow"

    r = session.get("https://iris.findtruman.io/web/info_flow", timeout=30)
    r.raise_for_status()
    html = r.text

    m = re.search(r"const\s+feeds\s*=\s*\[(.*?)\]\s*;", html, re.S)
    if not m:
        return []

    section = m.group(1)
    feeds = re.findall(
        r"\{\s*name:\s*'([^']+)'\s*,\s*url:\s*'([^']+)'\s*\}",
        section,
        re.S,
    )

    out: list[RawItem] = []
    for feed_name, feed_url in feeds:
        try:
            if feedparser is not None:
                parsed = feedparser.parse(feed_url)
                source_name = str(feed_name or getattr(parsed, "feed", {}).get("title") or "Iris Feed")
                for entry in parsed.entries:
                    title = str(entry.get("title", "")).strip()
                    url = str(entry.get("link", "")).strip()
                    if not title or not url:
                        continue
                    published = (
                        parse_date_any(entry.get("published"), now)
                        or parse_date_any(entry.get("updated"), now)
                        or parse_date_any(entry.get("pubDate"), now)
                    )
                    out.append(
                        RawItem(
                            site_id=site_id,
                            site_name=site_name,
                            source=source_name,
                            title=title,
                            url=url,
                            published_at=published,
                            meta={"feed_url": feed_url},
                        )
                    )
                continue

            feed_resp = session.get(feed_url, timeout=30)
            feed_resp.raise_for_status()
            entries = parse_feed_entries_via_xml(feed_resp.content)
            source_name = str(feed_name or "Iris Feed")
            for entry in entries:
                out.append(
                    RawItem(
                        site_id=site_id,
                        site_name=site_name,
                        source=source_name,
                        title=entry["title"],
                        url=entry["link"],
                        published_at=parse_date_any(entry.get("published"), now),
                        meta={"feed_url": feed_url},
                    )
                )
        except Exception:
            # Skip blocked/broken sub feeds and keep remaining feeds.
            continue
    return out


def fetch_bestblogs(session: requests.Session, now: datetime) -> list[RawItem]:
    site_id = "bestblogs"
    site_name = "BestBlogs"

    api = "https://api.bestblogs.dev/api/newsletter/list"
    out: list[RawItem] = []
    seen: set[str] = set()

    try:
        current_page = 1
        page_count = 1

        while current_page <= page_count and current_page <= 12:
            payload = {
                "currentPage": current_page,
                "pageSize": 20,
                "userLanguage": "en",
            }
            r = session.post(api, json=payload, timeout=30)
            r.raise_for_status()
            body = r.json()
            data = body.get("data", {})
            page_count = int(data.get("pageCount", 1) or 1)

            for issue in data.get("dataList", []):
                issue_id = str(issue.get("id", "")).strip()
                title = str(issue.get("title", "")).strip()
                if not issue_id or not title:
                    continue
                url = f"https://www.bestblogs.dev/en/newsletter#{issue_id}"
                if url in seen:
                    continue
                seen.add(url)

                published = parse_unix_timestamp(issue.get("createdTimestamp"))
                out.append(
                    RawItem(
                        site_id=site_id,
                        site_name=site_name,
                        source="Weekly Newsletter",
                        title=title,
                        url=url,
                        published_at=published,
                        meta={
                            "issue_id": issue_id,
                            "article_count": issue.get("articleCount"),
                        },
                    )
                )
            current_page += 1
    except Exception:
        pass

    if out:
        return out

    r = session.get("https://www.bestblogs.dev/en/newsletter", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    for a in soup.select("a[href*='/newsletter']"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        url = href if href.startswith("http") else urljoin("https://www.bestblogs.dev", href)
        title = a.get_text(" ", strip=True)
        if len(title) < 8:
            continue
        if url in seen:
            continue
        seen.add(url)
        dt = None
        time_tag = a.select_one("time")
        if time_tag:
            dt = parse_date_any(time_tag.get("datetime") or time_tag.get_text(" ", strip=True), now)
        out.append(
            RawItem(
                site_id=site_id,
                site_name=site_name,
                source="Weekly Newsletter",
                title=title,
                url=url,
                published_at=dt,
                meta={},
            )
        )

    return out


def fetch_tophub(session: requests.Session, now: datetime) -> list[RawItem]:
    site_id = "tophub"
    site_name = "TopHub"

    r = session.get("https://tophub.today/", timeout=30)
    r.raise_for_status()
    html = r.content.decode("utf-8", errors="replace")
    if "�" in html:
        for enc in ("gb18030", "utf-8"):
            try:
                candidate = r.content.decode(enc, errors="replace")
                if candidate.count("�") < html.count("�"):
                    html = candidate
            except Exception:
                continue
    soup = BeautifulSoup(html, "html.parser")

    out: list[RawItem] = []
    for block in soup.select(".cc-cd"):
        source_name_tag = block.select_one(".cc-cd-lb span")
        board_tag = block.select_one(".cc-cd-sb-st")
        source_name = source_name_tag.get_text(" ", strip=True) if source_name_tag else "TopHub"
        board_name = board_tag.get_text(" ", strip=True) if board_tag else ""
        source_name = maybe_fix_mojibake(source_name)
        board_name = maybe_fix_mojibake(board_name)
        source = f"{source_name} · {board_name}" if board_name else source_name

        for a in block.select(".cc-cd-cb-l a"):
            href = a.get("href", "").strip()
            row = a.select_one(".cc-cd-cb-ll")
            title_tag = row.select_one(".t") if row else None
            metric_tag = row.select_one(".e") if row else None

            title = (
                title_tag.get_text(" ", strip=True)
                if title_tag
                else a.get_text(" ", strip=True)
            )
            title = maybe_fix_mojibake(title)
            if not title or not href:
                continue

            full_url = href if href.startswith("http") else urljoin("https://tophub.today", href)
            row_text = row.get_text(" ", strip=True) if row else title
            published = parse_relative_time_zh(row_text, now)

            out.append(
                RawItem(
                    site_id=site_id,
                    site_name=site_name,
                    source=source,
                    title=title,
                    url=full_url,
                    published_at=published,
                    meta={"metric": metric_tag.get_text(" ", strip=True) if metric_tag else ""},
                )
            )

    return out


def fetch_zeli(session: requests.Session, now: datetime) -> list[RawItem]:
    site_id = "zeli"
    site_name = "Zeli"
    out: list[RawItem] = []

    url = "https://zeli.app/api/hacker-news?type=hot24h"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    body = r.json()
    posts = body.get("posts", [])
    for p in posts:
        title = str(p.get("title", "")).strip()
        link = str(p.get("url", "")).strip()
        if not title or not link:
            continue
        published = parse_unix_timestamp(p.get("time")) or now
        out.append(
            RawItem(
                site_id=site_id,
                site_name=site_name,
                source="Hacker News · 24h最热",
                title=title,
                url=link,
                published_at=published,
                meta={"hn_id": p.get("id")},
            )
        )

    return out


def parse_anthropic_news_items(page_html: str, now: datetime) -> list[RawItem]:
    site_id = "official_ai"
    site_name = "Official AI Updates"
    soup = BeautifulSoup(page_html, "html.parser")
    out: list[RawItem] = []
    seen: set[str] = set()

    for a in soup.select('a[href^="/news/"]'):
        href = str(a.get("href") or "").strip()
        if not href or href == "/news/" or href == "/news":
            continue

        title_tag = a.select_one("h1, h2, h3, h4")
        title = title_tag.get_text(" ", strip=True) if title_tag else ""
        title = maybe_fix_mojibake(title)
        if not title or title.lower() == "news":
            continue

        url = urljoin("https://www.anthropic.com", href)
        if url in seen:
            continue
        seen.add(url)

        time_tag = a.select_one("time")
        published = None
        if time_tag:
            published = parse_date_any(time_tag.get("datetime") or time_tag.get_text(" ", strip=True), now)
        if not published:
            continue
        if now and published < now - timedelta(days=OFFICIAL_AI_MAX_AGE_DAYS):
            continue

        out.append(
            RawItem(
                site_id=site_id,
                site_name=site_name,
                source="Anthropic News",
                title=title,
                url=url,
                published_at=published,
                meta={"provider": "Anthropic"},
            )
        )

    return out


def parse_openai_codex_changelog_items(page_html: str, now: datetime) -> list[RawItem]:
    site_id = "official_ai"
    site_name = "Official AI Updates"
    soup = BeautifulSoup(page_html, "html.parser")
    out: list[RawItem] = []
    seen: set[str] = set()

    for node in soup.select("#codex-changelog-content li[id], li[id]"):
        item_id = str(node.get("id") or "").strip()
        if not item_id or item_id in seen:
            continue

        time_tag = node.select_one("time")
        title_tag = node.select_one("h3")
        if not time_tag or not title_tag:
            continue

        title = maybe_fix_mojibake(title_tag.get_text(" ", strip=True))
        published = parse_date_any(time_tag.get("datetime") or time_tag.get_text(" ", strip=True), now)
        if not title or not published:
            continue
        if now and published < now - timedelta(days=OFFICIAL_AI_MAX_AGE_DAYS):
            continue

        seen.add(item_id)
        out.append(
            RawItem(
                site_id=site_id,
                site_name=site_name,
                source="OpenAI Codex Changelog",
                title=title,
                url=f"https://developers.openai.com/codex/changelog#{item_id}",
                published_at=published,
                meta={"provider": "OpenAI"},
            )
        )

    return out


def fetch_feed_as_official_items(
    session: requests.Session,
    feed: dict[str, str],
    now: datetime,
) -> list[RawItem]:
    site_id = "official_ai"
    site_name = "Official AI Updates"
    feed_url = feed["xml_url"]
    feed_title = feed["title"]

    resp = session.get(
        feed_url,
        timeout=20,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    resp.raise_for_status()

    entries: list[dict[str, Any]]
    if feedparser is not None:
        parsed = feedparser.parse(resp.content)
        entries = list(parsed.entries)
    else:
        entries = parse_feed_entries_via_xml(resp.content)

    out: list[RawItem] = []
    include_keywords = [
        keyword.strip().lower()
        for keyword in str(feed.get("include_keywords") or "").split(",")
        if keyword.strip()
    ]
    for entry in entries:
        title = str(entry.get("title", "")).strip()
        link = str(entry.get("link", "")).strip()
        if not title or not link:
            continue
        if include_keywords:
            haystack = f"{title} {link}".lower()
            if not any(keyword in haystack for keyword in include_keywords):
                continue
        published = (
            parse_date_any(entry.get("published"), now)
            or parse_date_any(entry.get("updated"), now)
            or parse_date_any(entry.get("pubDate"), now)
        )
        if not published:
            continue
        if published < now - timedelta(days=OFFICIAL_AI_MAX_AGE_DAYS):
            continue

        out.append(
            RawItem(
                site_id=site_id,
                site_name=site_name,
                source=feed_title,
                title=maybe_fix_mojibake(title),
                url=link,
                published_at=published,
                meta={
                    "feed_url": feed_url,
                    "feed_home": feed.get("html_url") or "",
                },
            )
        )

    return out


def fetch_official_ai_updates(session: requests.Session, now: datetime) -> list[RawItem]:
    out: list[RawItem] = []

    for feed in OFFICIAL_AI_FEEDS:
        try:
            out.extend(fetch_feed_as_official_items(session, feed, now))
        except Exception:
            continue

    try:
        r = session.get("https://www.anthropic.com/news", timeout=20)
        r.raise_for_status()
        out.extend(parse_anthropic_news_items(r.text, now))
    except Exception:
        pass

    try:
        r = session.get("https://developers.openai.com/codex/changelog", timeout=20)
        r.raise_for_status()
        out.extend(parse_openai_codex_changelog_items(r.text, now))
    except Exception:
        pass

    if not out:
        raise ValueError("No official AI update sources returned items")

    return out


def parse_ai_breakfast_items(markdown_text: str, now: datetime) -> list[RawItem]:
    site_id = "aibreakfast"
    site_name = "AI Breakfast"
    out: list[RawItem] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s+•\s+\d+\s+min read\s+###\s+\*\*(.*?)\*\*.*?"
        r"\]\((https?://aibreakfast\.beehiiv\.com/p/[^)]+)\)",
        re.S,
    )

    for date_text, title_text, url in pattern.findall(markdown_text or ""):
        url = url.strip()
        if not url or url in seen:
            continue
        published = parse_date_any(date_text, now)
        if not published:
            continue
        if now and published < now - timedelta(days=OFFICIAL_AI_MAX_AGE_DAYS):
            continue

        seen.add(url)
        title = re.sub(r"\s+", " ", title_text).strip()
        out.append(
            RawItem(
                site_id=site_id,
                site_name=site_name,
                source="AI Breakfast",
                title=maybe_fix_mojibake(title),
                url=url,
                published_at=published,
                meta={"feed_home": "https://aibreakfast.beehiiv.com/"},
            )
        )

    return out


def fetch_ai_breakfast(session: requests.Session, now: datetime) -> list[RawItem]:
    resp = session.get(
        AIBREAKFAST_JINA_URL,
        timeout=25,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/plain, */*",
        },
    )
    resp.raise_for_status()
    out = parse_ai_breakfast_items(resp.text, now)
    if not out:
        raise ValueError("No AI Breakfast items parsed")
    return out


def parse_follow_builders_items(feeds: dict[str, dict[str, Any]], now: datetime) -> list[RawItem]:
    site_id = "followbuilders"
    site_name = "Follow Builders"
    out: list[RawItem] = []

    for builder in feeds.get("x", {}).get("x", []) or []:
        name = str(builder.get("name") or builder.get("handle") or "").strip()
        handle = str(builder.get("handle") or "").strip()
        source = f"Follow Builders · X · {name or handle}".strip(" ·")
        for tweet in builder.get("tweets", []) or []:
            text = str(tweet.get("text") or "").strip()
            url = str(tweet.get("url") or "").strip()
            published = parse_date_any(tweet.get("createdAt"), now)
            if not text or not url or not published:
                continue
            title = re.sub(r"\s+", " ", text)
            if len(title) > 220:
                title = title[:217].rstrip() + "..."
            out.append(
                RawItem(
                    site_id=site_id,
                    site_name=site_name,
                    source=source,
                    title=maybe_fix_mojibake(title),
                    url=url,
                    published_at=published,
                    meta={"handle": handle, "feed": "feed-x.json"},
                )
            )

    for article in feeds.get("blogs", {}).get("blogs", []) or []:
        title = str(article.get("title") or "").strip()
        url = str(article.get("url") or "").strip()
        published = parse_date_any(article.get("publishedAt"), now) or parse_date_any(
            feeds.get("blogs", {}).get("generatedAt"), now
        )
        if not title or not url or not published:
            continue
        out.append(
            RawItem(
                site_id=site_id,
                site_name=site_name,
                source=f"Follow Builders · Blog · {article.get('name') or 'Blog'}",
                title=maybe_fix_mojibake(title),
                url=url,
                published_at=published,
                meta={"feed": "feed-blogs.json"},
            )
        )

    for episode in feeds.get("podcasts", {}).get("podcasts", []) or []:
        title = str(episode.get("title") or "").strip()
        url = str(episode.get("url") or "").strip()
        published = parse_date_any(episode.get("publishedAt"), now) or parse_date_any(
            feeds.get("podcasts", {}).get("generatedAt"), now
        )
        if not title or not url or not published:
            continue
        out.append(
            RawItem(
                site_id=site_id,
                site_name=site_name,
                source=f"Follow Builders · Podcast · {episode.get('name') or 'Podcast'}",
                title=maybe_fix_mojibake(title),
                url=url,
                published_at=published,
                meta={"feed": "feed-podcasts.json"},
            )
        )

    return out


def fetch_follow_builders(session: requests.Session, now: datetime) -> list[RawItem]:
    feeds: dict[str, dict[str, Any]] = {}
    for key, filename in (
        ("x", "feed-x.json"),
        ("blogs", "feed-blogs.json"),
        ("podcasts", "feed-podcasts.json"),
    ):
        resp = session.get(
            f"{FOLLOW_BUILDERS_FEED_BASE}/{filename}",
            timeout=20,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "application/json, */*",
            },
        )
        resp.raise_for_status()
        feeds[key] = resp.json()

    out = parse_follow_builders_items(feeds, now)
    if not out:
        raise ValueError("No Follow Builders items parsed")
    return out


def is_hubtoday_placeholder_title(title: str) -> bool:
    t = (title or "").strip()
    if not t:
        return True
    if "详情见官方介绍" in t:
        return True
    return t in {"原文链接", "查看详情", "点击查看", "详情"}


def is_hubtoday_generic_anchor_title(title: str) -> bool:
    t = (title or "").strip()
    if not t:
        return True
    if is_hubtoday_placeholder_title(t):
        return True
    return bool(re.search(r"\(AI资讯\)\s*$", t))


def normalize_aihubtoday_records(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_url: dict[str, list[dict[str, Any]]] = {}
    keep: list[dict[str, Any]] = []

    for item in items:
        if str(item.get("site_id") or "") != "aihubtoday":
            keep.append(item)
            continue
        url = normalize_url(str(item.get("url") or ""))
        if not url:
            continue
        by_url.setdefault(url, []).append(item)

    for group in by_url.values():
        if not group:
            continue
        preferred = [g for g in group if not is_hubtoday_generic_anchor_title(str(g.get("title") or ""))]
        source = preferred if preferred else group
        best = max(
            source,
            key=lambda x: (
                event_time(x) or datetime.min.replace(tzinfo=UTC),
                str(x.get("id") or ""),
            ),
        )
        keep.append(best)

    keep.sort(key=lambda x: event_time(x) or datetime.min.replace(tzinfo=UTC), reverse=True)
    return keep


def fetch_ai_hubtoday(session: requests.Session, now: datetime) -> list[RawItem]:
    site_id = "aihubtoday"
    site_name = "AI HubToday"

    r = session.get("https://ai.hubtoday.app/", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    issue_date = None
    text = soup.get_text(" ", strip=True)
    m = re.search(r"AI资讯日报\s*(\d{4})/(\d{1,2})/(\d{1,2})", text)
    if not m:
        m = re.search(r"AI资讯日报\s*(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        issue_date = datetime(
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3)),
            tzinfo=UTC,
        )

    out: list[RawItem] = []
    seen_urls: set[str] = set()

    def add_item(title: str, href: str, source: str = "Daily Digest", fallback_title: str | None = None) -> None:
        title = (title or "").strip()
        href = (href or "").strip()
        fallback_title = (fallback_title or "").strip()
        if is_hubtoday_generic_anchor_title(title) and fallback_title:
            title = fallback_title
        if len(title) < 5 or not href.startswith("http"):
            return
        if title in {"自媒体账号"} or "source.hubtoday.app" in href or is_hubtoday_generic_anchor_title(title):
            return
        key_url = normalize_url(href)
        if key_url in seen_urls:
            return
        seen_urls.add(key_url)
        out.append(
            RawItem(
                site_id=site_id,
                site_name=site_name,
                source=source,
                title=title,
                url=href,
                published_at=issue_date,
                meta={},
            )
        )

    for p in soup.select("article .content li p"):
        link = p.select_one("a[href^='http']")
        if not link:
            continue
        strong = p.find("strong")
        strong_title = strong.get_text(" ", strip=True) if strong else ""
        add_item(strong_title, link.get("href") or "", source="Daily Digest")

    for a in soup.select("article .content a[target='_blank']"):
        fallback_title = ""
        p = a.find_parent("p")
        if p:
            strong = p.find("strong")
            if strong:
                fallback_title = strong.get_text(" ", strip=True)
        add_item(a.get_text(" ", strip=True), a.get("href") or "", fallback_title=fallback_title)

    # include article-level links without target='_blank' (e.g. GitHub 链接)
    for a in soup.select("article a[href^='http']"):
        fallback_title = ""
        p = a.find_parent("p")
        if p:
            strong = p.find("strong")
            if strong:
                fallback_title = strong.get_text(" ", strip=True)
        add_item(a.get_text(" ", strip=True), a.get("href") or "", fallback_title=fallback_title)

    if not out:
        # fallback: parse all external links in page when article container changes
        for a in soup.select("a[href^='http']"):
            fallback_title = ""
            p = a.find_parent("p")
            if p:
                strong = p.find("strong")
                if strong:
                    fallback_title = strong.get_text(" ", strip=True)
            add_item(
                a.get_text(" ", strip=True),
                a.get("href") or "",
                source="Page Fallback",
                fallback_title=fallback_title,
            )

    return out


def fetch_aibase(session: requests.Session, now: datetime) -> list[RawItem]:
    site_id = "aibase"
    site_name = "AIbase"

    r = session.get("https://www.aibase.com/zh/news", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    out: list[RawItem] = []
    for a in soup.select("a[href^='/news/']"):
        h3 = a.select_one("h3")
        if not h3:
            continue
        title = h3.get_text(" ", strip=True)
        href = a.get("href", "").strip()
        if not title or not href:
            continue

        time_text = ""
        time_tag = a.select_one("div.text-sm.text-gray-400 span")
        if time_tag:
            time_text = time_tag.get_text(" ", strip=True)

        published = parse_date_any(time_text, now)
        out.append(
            RawItem(
                site_id=site_id,
                site_name=site_name,
                source=site_name,
                title=title,
                url=urljoin("https://www.aibase.com", href),
                published_at=published,
                meta={"time_hint": time_text},
            )
        )

    return out


def parse_aihot_feed_items(feed_content: bytes, now: datetime, feed_url: str = AIHOT_FEED_URL) -> list[RawItem]:
    site_id = "aihot"
    site_name = "AI HOT"
    source_name = site_name
    if feedparser is not None:
        parsed = feedparser.parse(feed_content)
        entries = list(parsed.entries)
        source_name = first_non_empty(getattr(parsed, "feed", {}).get("title"), site_name)
    else:
        entries = parse_feed_entries_via_xml(feed_content)

    out: list[RawItem] = []
    seen_urls: set[str] = set()
    for entry in entries:
        title = maybe_fix_mojibake(str(entry.get("title") or "").strip())
        link = str(entry.get("link") or "").strip()
        if not title or not link:
            continue
        normalized_url = normalize_url(link)
        if normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        published = (
            parse_date_any(entry.get("published"), now)
            or parse_date_any(entry.get("updated"), now)
            or parse_date_any(entry.get("pubDate"), now)
        )
        if not published:
            continue
        author_detail = entry.get("author_detail") or {}
        entry_source = first_non_empty(
            author_detail.get("name") if isinstance(author_detail, dict) else "",
            entry.get("author"),
            source_name,
        )
        out.append(
            RawItem(
                site_id=site_id,
                site_name=site_name,
                source=maybe_fix_mojibake(entry_source),
                title=title,
                url=link,
                published_at=published,
                meta={"feed_url": feed_url},
            )
        )

    return out


def fetch_aihot(session: requests.Session, now: datetime) -> list[RawItem]:
    last_error: Exception | None = None
    for feed_url in (AIHOT_FEED_URL, *AIHOT_FALLBACK_FEED_URLS):
        try:
            r = session.get(
                feed_url,
                timeout=30,
                headers={
                    "User-Agent": BROWSER_UA,
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept": "application/rss+xml, application/xml, text/xml, */*",
                },
            )
            r.raise_for_status()
            items = parse_aihot_feed_items(r.content, now, feed_url=feed_url)
            if items:
                return items
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return []


def extract_newsnow_source_ids(js: str) -> list[str]:
    marker = "{v2ex:vL"
    start = js.find(marker)
    if start == -1:
        return ["hackernews", "producthunt", "github", "sspai", "juejin", "36kr"]

    # Locate beginning "{" and parse until matching "}"
    block_start = start
    depth = 0
    end = None
    in_str = False
    esc = False

    for i, ch in enumerate(js[block_start:], block_start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end is None:
        return ["hackernews", "producthunt", "github", "sspai", "juejin", "36kr"]

    obj = js[block_start:end]
    all_keys = [m.group(2) for m in re.finditer(r'(["\']?)([a-zA-Z0-9_-]+)\1\s*:', obj)]

    ignore = {
        "name",
        "column",
        "home",
        "https",
        "color",
        "interval",
        "title",
        "type",
        "redirect",
        "desc",
    }

    source_ids: list[str] = []
    for key in all_keys:
        if key in ignore:
            continue
        if key not in source_ids:
            source_ids.append(key)

    # API currently returns around 57 source ids successfully.
    return source_ids


def fetch_newsnow(session: requests.Session, now: datetime) -> list[RawItem]:
    site_id = "newsnow"
    site_name = "NewsNow"

    home = session.get("https://newsnow.busiyi.world/", timeout=30)
    home.raise_for_status()
    soup = BeautifulSoup(home.text, "html.parser")

    bundle = None
    for script in soup.select("script[src]"):
        src = script.get("src", "")
        if "/assets/index-" in src and src.endswith(".js"):
            bundle = urljoin("https://newsnow.busiyi.world/", src)
            break

    source_ids = ["hackernews", "producthunt", "github", "sspai", "juejin", "36kr"]
    if bundle:
        js = session.get(bundle, timeout=30).text
        source_ids = extract_newsnow_source_ids(js)

    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://newsnow.busiyi.world",
        "Referer": "https://newsnow.busiyi.world/",
    }

    response = session.post(
        "https://newsnow.busiyi.world/api/s/entire",
        json={"sources": source_ids},
        headers=headers,
        timeout=45,
    )

    if response.status_code != 200:
        # fallback to per-source API
        source_blocks = []
        for sid in source_ids:
            rr = session.get(f"https://newsnow.busiyi.world/api/s?id={sid}", headers=headers, timeout=20)
            if rr.status_code == 200:
                try:
                    source_blocks.append(rr.json())
                except Exception:
                    pass
    else:
        body = response.json()
        source_blocks = body.get("data") if isinstance(body, dict) else body
    if not isinstance(source_blocks, list):
        source_blocks = []

    out: list[RawItem] = []
    for block in source_blocks:
        sid = str(block.get("id") or "unknown")
        source_title = first_non_empty(block.get("title"), block.get("name"), block.get("desc"), sid)
        source_label = f"{source_title} ({sid})" if source_title != sid else sid
        updated = parse_unix_timestamp(block.get("updatedTime")) or now
        items = block.get("items") or []
        for it in items:
            title = str(it.get("title") or "").strip()
            url = str(it.get("url") or "").strip()
            if not title or not url:
                continue

            published = None
            published = published or parse_date_any(it.get("pubDate"), now)
            if not published:
                extra = it.get("extra") or {}
                if isinstance(extra, dict):
                    published = parse_date_any(extra.get("date"), now)
            if not published:
                published = updated

            out.append(
                RawItem(
                    site_id=site_id,
                    site_name=site_name,
                    source=source_label,
                    title=title,
                    url=url,
                    published_at=published,
                    meta={},
                )
            )

    return out


def collect_all(session: requests.Session, now: datetime) -> tuple[list[RawItem], list[dict[str, Any]]]:
    tasks = [
        ("official_ai", "Official AI Updates", fetch_official_ai_updates),
        ("aibreakfast", "AI Breakfast", fetch_ai_breakfast),
        ("followbuilders", "Follow Builders", fetch_follow_builders),
        ("techurls", "TechURLs", fetch_techurls),
        ("buzzing", "Buzzing", fetch_buzzing),
        ("iris", "Info Flow", fetch_iris),
        ("bestblogs", "BestBlogs", fetch_bestblogs),
        ("tophub", "TopHub", fetch_tophub),
        ("zeli", "Zeli", fetch_zeli),
        ("aihubtoday", "AI HubToday", fetch_ai_hubtoday),
        ("aibase", "AIbase", fetch_aibase),
        ("aihot", "AI HOT", fetch_aihot),
        ("newsnow", "NewsNow", fetch_newsnow),
    ]

    raw_items: list[RawItem] = []
    statuses: list[dict[str, Any]] = []

    for site_id, site_name, fn in tasks:
        start = time.perf_counter()
        error = None
        count = 0
        try:
            items = fn(session, now)
            count = len(items)
            raw_items.extend(items)
        except Exception as exc:
            error = str(exc)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        statuses.append(
            {
                "site_id": site_id,
                "site_name": site_name,
                "ok": error is None,
                "item_count": count,
                "duration_ms": elapsed_ms,
                "error": error,
            }
        )

    return raw_items, statuses


def parse_opml_subscriptions(opml_path: Path) -> list[dict[str, str]]:
    root = ET.parse(opml_path).getroot()
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    for outline in root.findall(".//outline"):
        xml_url = str(outline.attrib.get("xmlUrl") or "").strip()
        if not xml_url:
            continue
        if xml_url in seen:
            continue
        seen.add(xml_url)
        title = first_non_empty(
            outline.attrib.get("title"),
            outline.attrib.get("text"),
            host_of_url(xml_url),
            xml_url,
        )
        html_url = str(outline.attrib.get("htmlUrl") or "").strip()
        out.append(
            {
                "title": title,
                "xml_url": xml_url,
                "html_url": html_url,
            }
        )
    return out


def resolve_official_rss_url(feed_url: str) -> tuple[str | None, str | None]:
    src = (feed_url or "").strip()
    if not src:
        return None, "empty_url"
    if src in RSS_FEED_SKIP_EXACT:
        return None, "no_official_rss_or_unreachable"
    for prefix in RSS_FEED_SKIP_PREFIXES:
        if src.startswith(prefix):
            return None, "no_official_rss_for_source_type"
    replaced = RSS_FEED_REPLACEMENTS.get(src)
    if replaced:
        return replaced, "official_replacement"
    return src, None


def resolve_opml_bridge_source(feed_url: str, html_url: str = "") -> dict[str, str] | None:
    src = (feed_url or "").strip()
    parsed = urlparse(src)
    path = parsed.path.strip("/")
    parts = [p for p in path.split("/") if p]

    if parsed.netloc == "rsshub.app" and len(parts) >= 3 and parts[:2] == ["telegram", "channel"]:
        slug = parts[2]
        return {
            "bridge_type": "telegram",
            "bridge_slug": slug,
            "url": f"https://t.me/s/{slug}",
        }

    if parsed.netloc == "rsshub.app" and len(parts) >= 3 and parts[0] == "jike":
        kind = parts[1]
        ident = parts[2]
        if kind == "topic":
            return {
                "bridge_type": "jike",
                "bridge_kind": "topic",
                "bridge_slug": ident,
                "url": f"https://m.okjike.com/topics/{ident}",
            }
        if kind == "user":
            return {
                "bridge_type": "jike",
                "bridge_kind": "user",
                "bridge_slug": ident,
                "url": f"https://m.okjike.com/users/{ident}",
            }

    html = (html_url or "").strip()
    if html.startswith("https://t.me/s/"):
        slug = html.rstrip("/").split("/")[-1]
        return {"bridge_type": "telegram", "bridge_slug": slug, "url": html}
    if html.startswith("https://m.okjike.com/topics/"):
        ident = html.rstrip("/").split("/")[-1]
        return {"bridge_type": "jike", "bridge_kind": "topic", "bridge_slug": ident, "url": html}
    if html.startswith("https://m.okjike.com/users/"):
        ident = html.rstrip("/").split("/")[-1]
        return {"bridge_type": "jike", "bridge_kind": "user", "bridge_slug": ident, "url": html}

    return None


def compact_title(text: str, limit: int = 96) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def parse_telegram_public_items(
    html: str,
    *,
    now: datetime,
    source_name: str,
    slug: str,
) -> list[RawItem]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[RawItem] = []
    for msg in soup.select(".tgme_widget_message"):
        data_post = str(msg.get("data-post") or "").strip()
        if not data_post:
            continue
        text_node = msg.select_one(".tgme_widget_message_text")
        text = text_node.get_text(" ", strip=True) if text_node else ""
        if not text:
            preview_title = msg.select_one(".tgme_widget_message_link_preview_title")
            text = preview_title.get_text(" ", strip=True) if preview_title else ""
        if not text:
            continue
        time_node = msg.select_one("time[datetime]")
        published = parse_date_any(time_node.get("datetime") if time_node else None, now)
        if not published:
            continue
        url = f"https://t.me/{data_post}"
        out.append(
            RawItem(
                site_id="opmlrss",
                site_name="OPML RSS",
                source=source_name,
                title=compact_title(text),
                url=url,
                published_at=published,
                meta={"bridge_type": "telegram", "bridge_slug": slug, "feed_home": f"https://t.me/s/{slug}"},
            )
        )
    return out


def parse_jike_public_items(
    html: str,
    *,
    now: datetime,
    source_name: str,
    source_url: str,
) -> list[RawItem]:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None or not script.string:
        return []
    try:
        payload = json.loads(script.string)
    except Exception:
        return []
    page_props = payload.get("props", {}).get("pageProps", {})
    posts = page_props.get("posts") or []
    out: list[RawItem] = []
    for post in posts:
        if not isinstance(post, dict):
            continue
        post_id = str(post.get("id") or "").strip()
        text = str(post.get("content") or "").strip()
        if not post_id or not text:
            continue
        published = parse_date_any(post.get("createdAt") or post.get("actionTime"), now)
        if not published:
            continue
        out.append(
            RawItem(
                site_id="opmlrss",
                site_name="OPML RSS",
                source=source_name,
                title=compact_title(text),
                url=f"https://m.okjike.com/originalPosts/{post_id}",
                published_at=published,
                meta={"bridge_type": "jike", "feed_home": source_url},
            )
        )
    return out


def fetch_opml_rss(
    now: datetime,
    opml_path: Path,
    max_feeds: int = 0,
) -> tuple[list[RawItem], dict[str, Any], list[dict[str, Any]]]:
    feeds = parse_opml_subscriptions(opml_path)
    if max_feeds > 0:
        feeds = feeds[:max_feeds]

    out: list[RawItem] = []
    feed_statuses: list[dict[str, Any]] = []
    resolved_feeds: list[dict[str, str]] = []

    for feed in feeds:
        original_url = feed["xml_url"]
        bridge = resolve_opml_bridge_source(original_url, feed.get("html_url") or "")
        if bridge:
            record = dict(feed)
            record["xml_url_original"] = original_url
            record["xml_url"] = bridge["url"]
            record["replaced"] = True
            record.update(bridge)
            resolved_feeds.append(record)
            continue

        resolved_url, skip_reason = resolve_official_rss_url(original_url)
        if not resolved_url:
            feed_id = hashlib.sha1(original_url.encode("utf-8")).hexdigest()[:10]
            feed_statuses.append(
                {
                    "site_id": f"opmlrss:{feed_id}",
                    "site_name": "OPML RSS",
                    "feed_title": feed["title"],
                    "feed_url": original_url,
                    "effective_feed_url": None,
                    "ok": True,
                    "item_count": 0,
                    "duration_ms": 0,
                    "error": None,
                    "skipped": True,
                    "skip_reason": skip_reason or "skipped",
                    "replaced": False,
                }
            )
            continue
        record = dict(feed)
        record["xml_url_original"] = original_url
        record["xml_url"] = resolved_url
        record["replaced"] = bool(resolved_url != original_url)
        resolved_feeds.append(record)

    def fetch_single_feed(feed: dict[str, str]) -> tuple[list[RawItem], dict[str, Any]]:
        feed_url = feed["xml_url"]
        original_feed_url = str(feed.get("xml_url_original") or feed_url)
        feed_title = feed["title"]
        feed_id = hashlib.sha1(feed_url.encode("utf-8")).hexdigest()[:10]
        start = time.perf_counter()
        error = None
        local_items: list[RawItem] = []

        try:
            resp = requests.get(
                feed_url,
                timeout=12,
                headers={
                    "User-Agent": BROWSER_UA,
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            resp.raise_for_status()

            bridge_type = str(feed.get("bridge_type") or "")
            if bridge_type == "telegram":
                local_items = parse_telegram_public_items(
                    resp.text,
                    now=now,
                    source_name=feed_title,
                    slug=str(feed.get("bridge_slug") or ""),
                )
            elif bridge_type == "jike":
                local_items = parse_jike_public_items(
                    resp.text,
                    now=now,
                    source_name=feed_title,
                    source_url=feed_url,
                )
            elif feedparser is not None:
                parsed = feedparser.parse(resp.content)
                source_name = first_non_empty(
                    feed_title,
                    getattr(parsed, "feed", {}).get("title"),
                    host_of_url(feed_url),
                )
                entries = parsed.entries
                for entry in entries:
                    title = str(entry.get("title", "")).strip()
                    link = str(entry.get("link", "")).strip()
                    if not title or not link:
                        continue
                    published = (
                        parse_date_any(entry.get("published"), now)
                        or parse_date_any(entry.get("updated"), now)
                        or parse_date_any(entry.get("pubDate"), now)
                    )
                    if not published:
                        continue
                    local_items.append(
                        RawItem(
                            site_id="opmlrss",
                            site_name="OPML RSS",
                            source=source_name,
                            title=title,
                            url=link,
                            published_at=published,
                            meta={
                                "feed_url": feed_url,
                                "feed_home": feed.get("html_url") or "",
                            },
                        )
                    )
            else:
                source_name = first_non_empty(feed_title, host_of_url(feed_url))
                entries = parse_feed_entries_via_xml(resp.content)
                for entry in entries:
                    published = parse_date_any(entry.get("published"), now)
                    if not published:
                        continue
                    local_items.append(
                        RawItem(
                            site_id="opmlrss",
                            site_name="OPML RSS",
                            source=source_name,
                            title=entry.get("title", ""),
                            url=entry.get("link", ""),
                            published_at=published,
                            meta={
                                "feed_url": feed_url,
                                "feed_home": feed.get("html_url") or "",
                            },
                        )
                    )
        except Exception as exc:
            error = str(exc)

        duration_ms = int((time.perf_counter() - start) * 1000)
        status = {
            "site_id": f"opmlrss:{feed_id}",
            "site_name": "OPML RSS",
            "feed_title": feed_title,
            "feed_url": original_feed_url,
            "effective_feed_url": feed_url,
            "ok": error is None,
            "item_count": len(local_items),
            "duration_ms": duration_ms,
            "error": error,
            "skipped": False,
            "skip_reason": None,
            "replaced": bool(original_feed_url != feed_url),
            "bridge_type": feed.get("bridge_type"),
        }
        return local_items, status

    if resolved_feeds:
        worker_count = min(20, max(4, len(resolved_feeds)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(fetch_single_feed, feed) for feed in resolved_feeds]
            for future in as_completed(futures):
                items, status = future.result()
                out.extend(items)
                feed_statuses.append(status)

    feed_statuses.sort(key=lambda x: str(x.get("feed_title") or x.get("feed_url") or ""))
    total_duration_ms = sum(int(s.get("duration_ms") or 0) for s in feed_statuses)
    ok_feeds = sum(1 for s in feed_statuses if s["ok"])
    failed_feeds = sum(1 for s in feed_statuses if not s["ok"])
    skipped_feeds = sum(1 for s in feed_statuses if s.get("skipped"))
    replaced_feeds = sum(1 for s in feed_statuses if s.get("replaced"))

    summary_status = {
        "site_id": "opmlrss",
        "site_name": "OPML RSS",
        "ok": ok_feeds > 0,
        "partial_failures": failed_feeds,
        "item_count": len(out),
        "duration_ms": total_duration_ms,
        "error": None if failed_feeds == 0 else f"{failed_feeds} feeds failed",
        "feed_count": len(feeds),
        "effective_feed_count": len(resolved_feeds),
        "ok_feed_count": ok_feeds,
        "failed_feed_count": failed_feeds,
        "skipped_feed_count": skipped_feeds,
        "replaced_feed_count": replaced_feeds,
    }
    return out, summary_status, feed_statuses


def load_archive(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    items = payload.get("items", [])
    out: dict[str, dict[str, Any]] = {}
    if isinstance(items, list):
        for it in items:
            item_id = it.get("id")
            if item_id:
                out[item_id] = it
    elif isinstance(items, dict):
        for item_id, it in items.items():
            if isinstance(it, dict):
                it["id"] = item_id
                out[item_id] = it
    return out


def event_time(record: dict[str, Any]) -> datetime | None:
    # RSS sources must rely on the source's publish time only.
    # first_seen_at is fetch time and would falsely mark historical items as "24h".
    if str(record.get("site_id") or "") == "opmlrss":
        return parse_iso(record.get("published_at"))
    return parse_iso(record.get("published_at")) or parse_iso(record.get("first_seen_at"))


AI_KEYWORDS = [
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
    "agent",
    "多模态",
    "大模型",
    "模型",
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
    "开源",
    "技术",
    "编程",
    "软件",
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

EN_SIGNAL_RE = re.compile(
    r"(?i)(?<![a-z0-9])(ai|aigc|llm|gpt|openai|anthropic|deepseek|gemini|claude|robot|robotics|embodied|autonomous|machine learning|artificial intelligence|transformer|diffusion|agent)(?![a-z0-9])"
)

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


MEANINGFUL_EN_SIGNAL_RE = re.compile(
    r"(?i)(?<![a-z0-9])(ai|aigc|llm|gpt|openai|anthropic|deepseek|gemini|claude|robot|robotics|embodied|autonomous|machine learning|artificial intelligence|transformer|diffusion)(?![a-z0-9])"
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SECRET_LIKE_RE = re.compile(r"\b(sk-(?!hynix\b)[A-Za-z0-9_-]{12,}|(?:api[_-]?key|secret|token)=([^\s&]{6,}))\b", re.I)
BROAD_AI_TERMS = {"agent", "模型", "推理"}


def contains_any_keyword(haystack: str, keywords: list[str]) -> bool:
    h = haystack.lower()
    return any(k in h for k in keywords)


def contains_meaningful_ai_signal(haystack: str) -> bool:
    h = haystack.lower()
    if MEANINGFUL_EN_SIGNAL_RE.search(h):
        return True
    return any(k in h for k in AI_KEYWORDS if k not in BROAD_AI_TERMS)


def redact_public_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    text = EMAIL_RE.sub("[redacted-email]", text)
    return SECRET_LIKE_RE.sub("[redacted-secret]", text)


def sanitize_public_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_public_text(value)
    if isinstance(value, list):
        return [sanitize_public_value(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_public_value(val) for key, val in value.items()}
    return value


def sanitize_public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return sanitize_public_value(payload)


def compact_public_snippet(text: str, max_chars: int = 240) -> str:
    """Return a short redacted snippet suitable for public/static JSON."""
    snippet = re.sub(r"\s+", " ", str(text or "")).strip()
    snippet = redact_public_text(snippet)
    if len(snippet) <= max_chars:
        return snippet
    return snippet[: max_chars - 1].rstrip() + "…"


def sender_domain_from_address(raw_sender: str) -> str | None:
    """Extract only the sender domain; never expose the raw email address."""
    _, email_addr = parseaddr(str(raw_sender or ""))
    if "@" not in email_addr:
        return None
    domain = email_addr.rsplit("@", 1)[-1].strip().lower().strip(">")
    return domain or None


def parse_domain_filter(raw: str) -> list[str]:
    """Parse a comma-separated sender-domain allowlist for private newsletter demos."""
    domains: list[str] = []
    for part in re.split(r"[,\s]+", str(raw or "")):
        domain = part.strip().lower().lstrip("@")
        if domain and re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", domain):
            domains.append(domain)
    return sorted(set(domains))


def domain_matches_filter(sender_domain: str | None, allowed_domains: list[str]) -> bool:
    if not allowed_domains:
        return True
    domain = str(sender_domain or "").lower().strip()
    return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in allowed_domains)


def filter_agentmail_messages_by_domain(
    messages: list[dict[str, Any]],
    allowed_domains: list[str],
) -> list[dict[str, Any]]:
    if not allowed_domains:
        return messages
    return [
        msg
        for msg in messages
        if domain_matches_filter(sender_domain_from_address(str(msg.get("from") or "")), allowed_domains)
    ]


def safe_agentmail_item(message: dict[str, Any]) -> dict[str, Any]:
    """Convert an AgentMail MessageItem into a metadata-only public digest item."""
    message_id = str(message.get("message_id") or "")
    stable_id = hashlib.sha1(message_id.encode("utf-8")).hexdigest()[:12] if message_id else "unknown"
    domain = sender_domain_from_address(str(message.get("from") or ""))
    attachments = message.get("attachments") or []
    return {
        "id": f"agentmail:{stable_id}",
        "source_type": "email_newsletter",
        "source": f"AgentMail · {domain}" if domain else "AgentMail",
        "sender_domain": domain,
        "subject": compact_public_snippet(str(message.get("subject") or ""), max_chars=180),
        "preview": compact_public_snippet(str(message.get("preview") or ""), max_chars=240),
        "received_at": message.get("timestamp") or message.get("created_at"),
        "has_attachments": bool(attachments),
        "attachment_count": len(attachments) if isinstance(attachments, list) else 0,
    }


def build_agentmail_digest_payload(
    messages: list[dict[str, Any]],
    generated_at: str,
    window_hours: int,
    allowed_sender_domains: list[str] | None = None,
) -> dict[str, Any]:
    """Build a privacy-preserving digest from AgentMail list-message results."""
    filtered_messages = filter_agentmail_messages_by_domain(messages, allowed_sender_domains or [])
    items = [safe_agentmail_item(msg) for msg in filtered_messages]
    return sanitize_public_payload(
        {
            "generated_at": generated_at,
            "source": "agentmail",
            "enabled": True,
            "window_hours": window_hours,
            "privacy": "metadata_only_no_body",
            "allowed_sender_domains": allowed_sender_domains or [],
            "total_messages": len(items),
            "items": items,
        }
    )


def fetch_agentmail_digest(
    session: requests.Session,
    api_key: str,
    inbox_id: str,
    generated_at: str,
    after: str,
    limit: int = AGENTMAIL_DEFAULT_LIMIT,
    base_url: str = AGENTMAIL_API_BASE_DEFAULT,
    window_hours: int = 24,
    allowed_sender_domains: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch AgentMail MessageItem metadata; deliberately does not request bodies or raw .eml."""
    base = (base_url or AGENTMAIL_API_BASE_DEFAULT).rstrip("/")
    url = f"{base}/v0/inboxes/{inbox_id}/messages"
    response = session.get(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        params={
            "limit": max(1, min(int(limit or AGENTMAIL_DEFAULT_LIMIT), 100)),
            "after": after,
            "ascending": "false",
            "include_spam": "false",
            "include_trash": "false",
            "include_blocked": "false",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    messages = payload.get("messages") if isinstance(payload, dict) else []
    if not isinstance(messages, list):
        messages = []
    return build_agentmail_digest_payload(
        messages,
        generated_at=generated_at,
        window_hours=window_hours,
        allowed_sender_domains=allowed_sender_domains,
    )


def env_flag(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name) or default).strip() or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name) or default).strip() or default)
    except ValueError:
        return default


def maybe_fetch_agentmail_digest(
    session: requests.Session,
    generated_at: str,
    after: str,
    window_hours: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Fetch AgentMail only when explicitly enabled and fully configured."""
    status: dict[str, Any] = {
        "enabled": env_flag("EMAIL_DIGEST_ENABLED"),
        "ok": None,
        "item_count": 0,
        "privacy": "metadata_only_no_body",
        "published_by_default": False,
    }
    if not status["enabled"]:
        return None, status

    agentmail_api_key = str(os.environ.get("AGENTMAIL_API_KEY") or "").strip()
    agentmail_inbox_id = str(os.environ.get("AGENTMAIL_INBOX_ID") or "").strip()
    agentmail_base_url = str(os.environ.get("AGENTMAIL_API_BASE_URL") or AGENTMAIL_API_BASE_DEFAULT).strip()
    agentmail_limit = env_int("AGENTMAIL_LIMIT", AGENTMAIL_DEFAULT_LIMIT)
    allowed_sender_domains = parse_domain_filter(str(os.environ.get("AGENTMAIL_ALLOWED_SENDER_DOMAINS") or ""))
    status["allowed_sender_domains"] = allowed_sender_domains
    if not (agentmail_api_key and agentmail_inbox_id):
        status["ok"] = False
        status["error"] = "missing_agentmail_credentials"
        return None, status

    try:
        payload = fetch_agentmail_digest(
            session,
            api_key=agentmail_api_key,
            inbox_id=agentmail_inbox_id,
            generated_at=generated_at,
            after=after,
            limit=agentmail_limit,
            base_url=agentmail_base_url,
            window_hours=window_hours,
            allowed_sender_domains=allowed_sender_domains,
        )
        status["ok"] = True
        status["item_count"] = int(payload.get("total_messages") or 0)
        return payload, status
    except Exception as exc:
        status["ok"] = False
        status["error"] = type(exc).__name__
        return None, status


def x_api_should_run_now(now: datetime) -> bool:
    """Gate paid X API reads so a 30-minute cron does not spend every run."""
    if env_flag("X_API_FORCE_RUN"):
        return True
    run_hour = max(0, min(env_int("X_API_RUN_UTC_HOUR", 0), 23))
    minute_max = max(0, min(env_int("X_API_RUN_UTC_MINUTE_MAX", 10), 59))
    return now.astimezone(UTC).hour == run_hour and now.astimezone(UTC).minute <= minute_max


def x_api_status_base(now: datetime) -> dict[str, Any]:
    daily_post_limit = max(0, env_int("X_API_DAILY_POST_LIMIT", X_API_DEFAULT_MAX_RESULTS))
    max_results = max(10, min(env_int("X_API_MAX_RESULTS", X_API_DEFAULT_MAX_RESULTS), 100))
    effective_cap = min(max_results, daily_post_limit) if daily_post_limit else 0
    return {
        "enabled": env_flag("X_API_ENABLED"),
        "ok": None,
        "item_count": 0,
        "privacy": "public_posts_metadata_only",
        "published_by_default": False,
        "official_free_read_quota": False,
        "unit_cost_usd_per_post_read": X_API_POST_READ_COST_USD,
        "daily_post_limit": daily_post_limit,
        "max_results_per_run": max_results,
        "effective_result_cap": effective_cap,
        "estimated_max_cost_usd_per_run": round(effective_cap * X_API_POST_READ_COST_USD, 4),
        "run_utc_hour": max(0, min(env_int("X_API_RUN_UTC_HOUR", 0), 23)),
        "generated_date_utc": now.astimezone(UTC).date().isoformat(),
    }


def fetch_x_api_recent_search(
    session: requests.Session,
    bearer_token: str,
    query: str,
    now: datetime,
    max_results: int,
    base_url: str = X_API_BASE_DEFAULT,
) -> list[RawItem]:
    """Fetch public recent-search Posts from X API v2; no writes and no DMs."""
    query = re.sub(r"\s+", " ", (query or X_API_DEFAULT_QUERY).strip())
    if len(query) > X_API_MAX_QUERY_CHARS:
        raise ValueError("x_query_too_long")
    capped_max_results = max(10, min(int(max_results or X_API_DEFAULT_MAX_RESULTS), 100))
    url = f"{(base_url or X_API_BASE_DEFAULT).rstrip('/')}/2/tweets/search/recent"
    response = session.get(
        url,
        headers={"Authorization": f"Bearer {bearer_token}"},
        params={
            "query": query,
            "max_results": capped_max_results,
            "tweet.fields": "created_at,author_id,public_metrics,lang",
            "expansions": "author_id",
            "user.fields": "username,name,verified",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    users = {
        str(user.get("id")): user
        for user in (payload.get("includes", {}) or {}).get("users", [])
        if isinstance(user, dict) and user.get("id")
    }
    out: list[RawItem] = []
    for post in payload.get("data") or []:
        if not isinstance(post, dict):
            continue
        post_id = str(post.get("id") or "").strip()
        text = compact_public_snippet(str(post.get("text") or ""), max_chars=220)
        if not (post_id and text):
            continue
        user = users.get(str(post.get("author_id") or ""), {})
        username = str(user.get("username") or "i/web").strip() or "i/web"
        published = parse_iso(str(post.get("created_at") or "")) or now
        out.append(
            RawItem(
                site_id="xapi",
                site_name="X API",
                source=f"@{username}",
                title=text,
                url=f"https://x.com/{username}/status/{post_id}",
                published_at=published,
                meta={
                    "post_id": post_id,
                    "lang": post.get("lang"),
                    "public_metrics": post.get("public_metrics") or {},
                },
            )
        )
    return out


def maybe_fetch_x_api_updates(
    session: requests.Session,
    now: datetime,
) -> tuple[list[RawItem], dict[str, Any]]:
    """Fetch X only when explicitly enabled, credentialed, scheduled, and capped."""
    status = x_api_status_base(now)
    if not status["enabled"]:
        return [], status

    if status["effective_result_cap"] < 10:
        status["ok"] = False
        status["error"] = "x_daily_post_limit_below_api_minimum"
        return [], status

    if not x_api_should_run_now(now):
        status["skipped"] = True
        status["skip_reason"] = "outside_x_api_daily_window"
        return [], status

    bearer_token = str(os.environ.get("X_BEARER_TOKEN") or os.environ.get("X_API_BEARER_TOKEN") or "").strip()
    if not bearer_token:
        status["ok"] = False
        status["error"] = "missing_x_bearer_token"
        return [], status

    query = str(os.environ.get("X_API_QUERY") or X_API_DEFAULT_QUERY).strip()
    base_url = str(os.environ.get("X_API_BASE_URL") or X_API_BASE_DEFAULT).strip()
    try:
        items = fetch_x_api_recent_search(
            session,
            bearer_token=bearer_token,
            query=query,
            now=now,
            max_results=int(status["effective_result_cap"]),
            base_url=base_url,
        )
        status["ok"] = True
        status["item_count"] = len(items)
        status["estimated_cost_usd"] = round(len(items) * X_API_POST_READ_COST_USD, 4)
        return items, status
    except Exception as exc:
        status["ok"] = False
        status["error"] = type(exc).__name__
        return [], status


def has_mojibake_noise(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r"(Ã|Â|â€|æ·|�)", text))


def normalize_source_for_display(site_id: str, source: str, url: str) -> str:
    src = (source or "").strip()
    if not src:
        host = host_of_url(url)
        if host.startswith("www."):
            host = host[4:]
        return host or "未分区"
    if site_id == "buzzing" and src.lower() == "buzzing":
        host = host_of_url(url)
        if host.startswith("www."):
            host = host[4:]
        return host or src
    return src


def is_ai_related_record(record: dict[str, Any]) -> bool:
    if has_mojibake_noise(str(record.get("source") or "")) or has_mojibake_noise(str(record.get("title") or "")):
        return False
    return bool(score_ai_relevance(record)["is_ai_related"])


def load_title_zh_cache(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if str(k).strip() and str(v).strip()}
    except Exception:
        pass
    return {}


def translate_to_zh_cn(session: requests.Session, text: str) -> str | None:
    s = (text or "").strip()
    if not s:
        return None
    try:
        r = session.get(
            "https://translate.googleapis.com/translate_a/single",
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": "zh-CN",
                "dt": "t",
                "q": s,
            },
            timeout=12,
        )
        r.raise_for_status()
        payload = r.json()
        if not isinstance(payload, list) or not payload:
            return None
        segs = payload[0]
        if not isinstance(segs, list):
            return None
        translated = "".join(str(seg[0]) for seg in segs if isinstance(seg, list) and seg and seg[0])
        translated = translated.strip()
        if translated and translated != s:
            return translated
    except Exception:
        return None
    return None


def add_bilingual_fields(
    items_ai: list[dict[str, Any]],
    items_all: list[dict[str, Any]],
    session: requests.Session,
    cache: dict[str, str],
    max_new_translations: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    zh_by_url: dict[str, str] = {}
    for it in items_all:
        title = str(it.get("title") or "").strip()
        url = normalize_url(str(it.get("url") or ""))
        if title and url and has_cjk(title):
            zh_by_url[url] = title

    translated_now = 0

    def enrich(item: dict[str, Any], allow_translate: bool) -> dict[str, Any]:
        nonlocal translated_now
        out = dict(item)
        title = str(out.get("title") or "").strip()
        url = normalize_url(str(out.get("url") or ""))

        out["title_original"] = title
        out["title_en"] = None
        out["title_zh"] = None
        out["title_bilingual"] = title

        if has_cjk(title):
            out["title_zh"] = title
            return out

        if not is_mostly_english(title):
            return out

        out["title_en"] = title

        zh_title = zh_by_url.get(url)
        if not zh_title:
            zh_title = cache.get(title)
        if not zh_title and allow_translate and translated_now < max_new_translations:
            tr = translate_to_zh_cn(session, title)
            if tr and has_cjk(tr):
                zh_title = tr
                cache[title] = tr
                translated_now += 1

        if zh_title:
            out["title_zh"] = zh_title
            out["title_bilingual"] = f"{zh_title} / {title}"
        return out

    ai_out = [enrich(it, allow_translate=True) for it in items_ai]
    all_out = [enrich(it, allow_translate=False) for it in items_all]
    return ai_out, all_out, cache


def dedupe_items_by_title_url(items: list[dict[str, Any]], random_pick: bool = True) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        site_id = str(item.get("site_id") or "").strip().lower()
        title = str(item.get("title_original") or item.get("title") or "").strip().lower()
        url = normalize_url(str(item.get("url") or ""))
        if site_id == "aihubtoday":
            key = f"url::{url}"
        else:
            key = f"{title}||{url}"
        groups.setdefault(key, []).append(item)

    out: list[dict[str, Any]] = []
    for values in groups.values():
        if random_pick:
            out.append(random.choice(values))
        else:
            chosen = max(
                values,
                key=lambda x: (
                    event_time(x) or datetime.min.replace(tzinfo=UTC),
                    str(x.get("id") or ""),
                ),
            )
            out.append(chosen)

    out.sort(key=lambda x: event_time(x) or datetime.min.replace(tzinfo=UTC), reverse=True)
    return out


def build_latest_payloads(latest_payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split initial AI payload from bulky all-mode lists for lazy browser loading."""
    slim_payload = dict(latest_payload)
    all_payload = {
        "generated_at": latest_payload.get("generated_at"),
        "window_hours": latest_payload.get("window_hours"),
        "topic_filter": latest_payload.get("topic_filter"),
        "ai_relevance_threshold": latest_payload.get("ai_relevance_threshold"),
        "total_items_raw": latest_payload.get("total_items_raw"),
        "total_items_all_mode": latest_payload.get("total_items_all_mode"),
        "items_all": latest_payload.get("items_all", []),
        "items_all_raw": latest_payload.get("items_all_raw", []),
    }
    slim_payload.pop("items_all", None)
    slim_payload.pop("items_all_raw", None)
    slim_payload["all_mode_data_url"] = "data/latest-24h-all.json"
    return slim_payload, all_payload


def pct(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round(max(0.0, min(1.0, float(numerator) / float(denominator))), 3)


def source_quality_decision(ok: bool, raw_24h: int, ai_24h: int, hit_rate: float) -> tuple[str, str]:
    if not ok:
        return "fix_fetch", "抓取失败，先修复源"
    if raw_24h == 0:
        return "watch", "24小时没有新内容"
    if raw_24h >= 100 and hit_rate < 0.03:
        return "downgrade", "内容很多但AI命中率偏低"
    if ai_24h >= 20 or hit_rate >= 0.2:
        return "keep", "高信号源，建议保留"
    if ai_24h > 0:
        return "watch", "有少量有效信号，继续观察"
    return "watch", "暂未贡献AI信号"


def build_source_quality_metrics(
    statuses: list[dict[str, Any]],
    latest_items_ai: list[dict[str, Any]],
    latest_items_all: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ai_count_by_site: dict[str, int] = {}
    raw_count_by_site: dict[str, int] = {}
    site_name_by_id: dict[str, str] = {}

    for item in latest_items_ai:
        sid = str(item.get("site_id") or "")
        if not sid:
            continue
        ai_count_by_site[sid] = ai_count_by_site.get(sid, 0) + 1
        site_name_by_id[sid] = str(item.get("site_name") or sid)

    for item in latest_items_all:
        sid = str(item.get("site_id") or "")
        if not sid:
            continue
        raw_count_by_site[sid] = raw_count_by_site.get(sid, 0) + 1
        site_name_by_id.setdefault(sid, str(item.get("site_name") or sid))

    status_by_site: dict[str, dict[str, Any]] = {}
    for status in statuses:
        sid = str(status.get("site_id") or "")
        if not sid:
            continue
        status_by_site[sid] = status
        site_name_by_id.setdefault(sid, str(status.get("site_name") or sid))

    rows: list[dict[str, Any]] = []
    for sid in sorted(set(site_name_by_id) | set(status_by_site)):
        status = status_by_site.get(sid, {})
        raw_24h = raw_count_by_site.get(sid, 0)
        ai_24h = ai_count_by_site.get(sid, 0)
        hit_rate = pct(ai_24h, raw_24h)
        ok = bool(status.get("ok", True))
        decision, reason = source_quality_decision(ok, raw_24h, ai_24h, hit_rate)
        fetch_count = int(status.get("item_count") or 0)
        rows.append(
            {
                "site_id": sid,
                "site_name": site_name_by_id.get(sid, sid),
                "ok": ok,
                "fetch_item_count": fetch_count,
                "raw_24h": raw_24h,
                "ai_24h": ai_24h,
                "ai_hit_rate": hit_rate,
                "duration_ms": int(status.get("duration_ms") or 0),
                "decision": decision,
                "decision_reason": reason,
                "error": status.get("error"),
            }
        )

    rows.sort(
        key=lambda row: (
            0 if row["ok"] else 1,
            row["decision"] == "downgrade",
            -int(row["ai_24h"]),
            -float(row["ai_hit_rate"]),
            str(row["site_name"]),
        )
    )
    summary = {
        "source_count": len(rows),
        "keep": sum(1 for row in rows if row["decision"] == "keep"),
        "watch": sum(1 for row in rows if row["decision"] == "watch"),
        "downgrade": sum(1 for row in rows if row["decision"] == "downgrade"),
        "fix_fetch": sum(1 for row in rows if row["decision"] == "fix_fetch"),
        "average_ai_hit_rate": round(
            sum(float(row["ai_hit_rate"]) for row in rows) / len(rows),
            3,
        )
        if rows
        else 0.0,
    }
    return rows, summary


def build_archive_index_payload(
    archive: dict[str, dict[str, Any]],
    generated_at: str | None,
    archive_days: int,
) -> dict[str, Any]:
    day_counts: dict[str, int] = {}
    site_counts: dict[str, dict[str, Any]] = {}
    timestamps: list[datetime] = []

    for record in archive.values():
        ts = event_time(record) or parse_iso(record.get("first_seen_at")) or parse_iso(record.get("last_seen_at"))
        if ts:
            timestamps.append(ts)
            day = ts.astimezone(UTC).date().isoformat()
            day_counts[day] = day_counts.get(day, 0) + 1

        sid = str(record.get("site_id") or "unknown")
        if sid not in site_counts:
            site_counts[sid] = {
                "site_id": sid,
                "site_name": str(record.get("site_name") or sid),
                "count": 0,
            }
        site_counts[sid]["count"] += 1

    return {
        "generated_at": generated_at,
        "archive_days": archive_days,
        "total_items": len(archive),
        "oldest_event_at": iso(min(timestamps)) if timestamps else None,
        "newest_event_at": iso(max(timestamps)) if timestamps else None,
        "day_counts": [
            {"date": day, "count": count}
            for day, count in sorted(day_counts.items(), reverse=True)
        ],
        "top_sites": sorted(site_counts.values(), key=lambda row: int(row["count"]), reverse=True)[:20],
    }


def markdown_link(title: str, url: str) -> str:
    safe_title = re.sub(r"[\r\n]+", " ", title or "").strip().replace("[", "【").replace("]", "】")
    safe_url = (url or "").strip()
    if not safe_url:
        return safe_title
    return f"[{safe_title}]({safe_url})"


def brief_item_score(item: dict[str, Any]) -> float:
    score = item.get("ai_score")
    try:
        return float(score or 0.0)
    except (TypeError, ValueError):
        return 0.0


RADAR_CATEGORIES: tuple[dict[str, Any], ...] = (
    {
        "id": "model_release",
        "title": "模型发布与能力更新",
        "description": "新模型、推理能力、上下文、价格和API更新。",
        "labels": {"model_release"},
        "keywords": ["model", "gpt", "claude", "gemini", "deepseek", "qwen", "llama", "模型", "大模型", "推理", "上下文"],
    },
    {
        "id": "product_tool",
        "title": "产品与开发者工具",
        "description": "Agent、开发工具、MCP、应用和工作流。",
        "labels": {"developer_tool", "agent_workflow", "ai_product_update", "ai_companion", "ai_speaker"},
        "keywords": ["agent", "codex", "copilot", "cursor", "mcp", "api", "sdk", "workflow", "工具", "智能体", "产品"],
    },
    {
        "id": "research_paper",
        "title": "论文研究与评测",
        "description": "论文、benchmark、评测、开源实验和研究进展。",
        "labels": {"research_paper"},
        "keywords": ["paper", "arxiv", "benchmark", "eval", "research", "论文", "研究", "评测", "榜单"],
    },
    {
        "id": "industry_compute",
        "title": "产业、算力与机器人",
        "description": "公司、融资、芯片、算力、机器人和政策生态。",
        "labels": {"industry_business", "infra_compute", "robotics", "ai_tech"},
        "keywords": ["funding", "gpu", "chip", "robot", "融资", "收购", "算力", "芯片", "机器人", "政策", "企业"],
    },
    {
        "id": "practice_insight",
        "title": "实践技巧与观点",
        "description": "教程、经验、观点、方法论和可落地案例。",
        "labels": {"ai_general"},
        "keywords": ["prompt", "guide", "tutorial", "case", "技巧", "教程", "经验", "观点", "方法", "案例", "实践"],
    },
)


UPSTREAM_PROJECT_NAMES = {
    "aihot": "AI HOT",
    "ai_news_radar": "AI News Radar",
    "trendradar": "TrendRadar",
    "horizon": "Horizon",
}

CURATED_AI_UPSTREAMS = {"aihot", "ai_news_radar", "horizon"}

PROJECT_CATALOG_ORDER = ("ai_news_radar", "aihot", "trendradar", "horizon")
PROJECT_DISPLAY_NAMES = {
    "ai_news_radar": "AI News Radar",
    "aihot": "AI HOT",
    "trendradar": "TrendRadar",
    "horizon": "Horizon",
}
SOURCE_KIND_LABELS = {
    "collector": "采集器",
    "feed": "订阅源",
    "platform": "热榜平台",
}


def stable_source_slug(value: Any, fallback: str = "source") -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        raw = fallback
    slug = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", raw).strip("-")
    if not slug:
        slug = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return slug[:72]


def project_source_id_for_item(item: dict[str, Any]) -> str:
    upstream_id = str(item.get("upstream_id") or "").strip()
    if upstream_id in UPSTREAM_PROJECT_NAMES:
        return upstream_id
    site_name = str(item.get("site_name") or "").strip().lower()
    if "ai hot" in site_name:
        return "aihot"
    if "trendradar" in site_name:
        return "trendradar"
    if "horizon" in site_name:
        return "horizon"
    return "ai_news_radar"


def source_ref_for_item(item: dict[str, Any]) -> dict[str, Any]:
    project_id = project_source_id_for_item(item)
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    source_name = str(
        extra.get("platform_name")
        or extra.get("source")
        or item.get("source")
        or item.get("site_name")
        or PROJECT_DISPLAY_NAMES.get(project_id)
        or "来源"
    ).strip()

    if project_id == "trendradar":
        kind = "platform"
        source_id = str(extra.get("platform_id") or item.get("platform_id") or "").strip()
        if not source_id:
            source_id = stable_source_slug(source_name, "trendradar")
    elif project_id == "horizon":
        kind = "feed"
        source_id = "feed-zh"
        source_name = "Horizon 中文 Atom Feed"
    elif project_id == "aihot":
        kind = "feed"
        source_id = stable_source_slug(source_name, "aihot")
    else:
        kind = "collector"
        source_id = str(item.get("site_id") or "").strip() or stable_source_slug(source_name, "ai-news-radar")

    return {
        "project_id": project_id,
        "project_name": PROJECT_DISPLAY_NAMES.get(project_id, project_id),
        "kind": kind,
        "kind_label": SOURCE_KIND_LABELS.get(kind, kind),
        "source_id": source_id,
        "name": source_name or PROJECT_DISPLAY_NAMES.get(project_id, project_id),
        "source_key": f"{project_id}::{kind}::{source_id}",
    }


def project_source_name_for_item(item: dict[str, Any]) -> str:
    upstream_id = str(item.get("upstream_id") or "").strip()
    if upstream_id in UPSTREAM_PROJECT_NAMES:
        return UPSTREAM_PROJECT_NAMES[upstream_id]
    site_name = str(item.get("site_name") or "").strip().lower()
    if "ai hot" in site_name:
        return "AI HOT"
    if "trendradar" in site_name:
        return "TrendRadar"
    if "horizon" in site_name:
        return "Horizon"
    return "AI News Radar"


def category_for_item(item: dict[str, Any]) -> dict[str, Any]:
    label = str(item.get("ai_label") or "")
    text = f"{item.get('title_zh') or item.get('title') or ''} {item.get('source') or ''}".lower()
    for category_id in ("research_paper", "industry_compute", "product_tool", "model_release", "practice_insight"):
        category = next(row for row in RADAR_CATEGORIES if row["id"] == category_id)
        if contains_any_keyword(text, category["keywords"]):
            return category
    for category in RADAR_CATEGORIES:
        if label in category["labels"]:
            return category
    return RADAR_CATEGORIES[-1]


def score_10(item: dict[str, Any], source_count: int = 1) -> float:
    base = brief_item_score(item)
    if base <= 1:
        base *= 10
    bonus = min(1.2, max(0, source_count - 1) * 0.4)
    return round(max(0.0, min(10.0, base + bonus)), 1)


def blended_project_items(items: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for project_name in ("AI News Radar", "AI HOT", "TrendRadar", "Horizon"):
        for item in items:
            item_id = str(item.get("id") or id(item))
            if item_id in selected_ids:
                continue
            if project_source_name_for_item(item) == project_name:
                selected.append(item)
                selected_ids.add(item_id)
                break
    for item in items:
        if len(selected) >= limit:
            break
        item_id = str(item.get("id") or id(item))
        if item_id in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(item_id)
    return selected[:limit]


def time_label_for_item(item: dict[str, Any], now: datetime) -> str:
    ts = event_time(item) or parse_iso(item.get("first_seen_at"))
    if not ts:
        return "时间未知"
    local = ts.astimezone(SH_TZ)
    now_local = now.astimezone(SH_TZ)
    delta = now_local - local
    if timedelta(0) <= delta < timedelta(minutes=60):
        minutes = max(1, int(delta.total_seconds() // 60))
        return f"{minutes}分钟前"
    if timedelta(0) <= delta < timedelta(hours=12):
        hours = max(1, int(delta.total_seconds() // 3600))
        return f"{hours}小时前"
    if local.date() == now_local.date():
        return f"今天 {local:%H:%M}"
    if local.date() == now_local.date() - timedelta(days=1):
        return f"昨天 {local:%H:%M}"
    return f"{local:%m-%d %H:%M}"


def radar_title(item: dict[str, Any]) -> str:
    return str(item.get("title_zh") or item.get("title") or item.get("title_en") or "未命名更新").strip()


def slim_radar_item(item: dict[str, Any], now: datetime, source_count: int = 1) -> dict[str, Any]:
    category = category_for_item(item)
    source_ref = source_ref_for_item(item)
    return {
        "id": item.get("id"),
        "title": radar_title(item),
        "title_original": item.get("title_original") or item.get("title"),
        "url": item.get("url"),
        "site_id": item.get("site_id"),
        "site_name": item.get("site_name"),
        "source": item.get("source"),
        "published_at": item.get("published_at"),
        "first_seen_at": item.get("first_seen_at"),
        "time_label": time_label_for_item(item, now),
        "category_id": category["id"],
        "category_title": category["title"],
        "score": score_10(item, source_count=source_count),
        "ai_score": item.get("ai_score"),
        "ai_label": item.get("ai_label"),
        "reason": item.get("ai_relevance_reason"),
        "signals": item.get("ai_signals") or [],
        "topic_labels": item.get("topic_labels") or [],
        "topic_sections": item.get("topic_sections") or [],
        "project_source": project_source_name_for_item(item),
        "source_ref": source_ref,
        "source_key": source_ref.get("source_key"),
    }


def normalize_story_key_text(text: str) -> str:
    normalized = re.sub(r"https?://\S+", "", str(text or "").lower())
    normalized = re.sub(r"[\s\u3000]+", "", normalized)
    normalized = re.sub(r"[，。、“”‘’：:；;！!？?（）()\[\]【】《》<>·.,/\\|_\-]", "", normalized)
    return normalized


def story_cluster_key(item: dict[str, Any]) -> str:
    title = radar_title(item)
    normalized = normalize_story_key_text(title)
    entity = re.search(
        r"(openai|anthropic|claude|gemini|deepseek|qwen|grok|codex|cursor|copilot|llama|mcp|gpt[0-9a-z.]*)",
        normalized,
        re.I,
    )
    if entity:
        return f"entity:{entity.group(1).lower()}:{normalized[:24]}"
    return f"title:{normalized[:36]}"


def build_story_clusters(latest_items_ai: list[dict[str, Any]], now: datetime, limit: int = 18) -> list[dict[str, Any]]:
    clusters: dict[str, dict[str, Any]] = {}
    for item in latest_items_ai:
        key = story_cluster_key(item)
        cluster = clusters.setdefault(
            key,
            {
                "story_id": hashlib.sha1(key.encode("utf-8")).hexdigest()[:12],
                "items": [],
                "sources": set(),
                "source_refs": {},
                "primary": item,
            },
        )
        cluster["items"].append(item)
        cluster["sources"].add(str(item.get("site_name") or item.get("source") or "来源"))
        source_ref = source_ref_for_item(item)
        cluster["source_refs"][source_ref["source_key"]] = source_ref
        primary = cluster["primary"]
        if (brief_item_score(item), event_time(item) or datetime.min.replace(tzinfo=UTC)) > (
            brief_item_score(primary),
            event_time(primary) or datetime.min.replace(tzinfo=UTC),
        ):
            cluster["primary"] = item

    stories: list[dict[str, Any]] = []
    for cluster in clusters.values():
        primary = cluster["primary"]
        sources = sorted(cluster["sources"])
        slim = slim_radar_item(primary, now, source_count=len(sources))
        related = sorted(
            cluster["items"],
            key=lambda item: event_time(item) or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )[:4]
        stories.append(
            {
                "story_id": cluster["story_id"],
                "title": slim["title"],
                "url": slim["url"],
                "category_id": slim["category_id"],
                "category_title": slim["category_title"],
                "score": slim["score"],
                "time_label": slim["time_label"],
                "source_count": len(sources),
                "item_count": len(cluster["items"]),
                "sources": sources[:6],
                "source_refs": list(cluster["source_refs"].values())[:8],
                "primary": slim,
                "related": [slim_radar_item(item, now, source_count=len(sources)) for item in related],
            }
        )

    stories.sort(
        key=lambda story: (
            int(story["source_count"]),
            float(story["score"]),
            event_time(story["primary"]) or datetime.min.replace(tzinfo=UTC),
        ),
        reverse=True,
    )
    return stories[:limit]


def build_source_catalog(
    config: dict[str, Any],
    upstream_hub_payload: dict[str, Any],
    source_quality: list[dict[str, Any]],
    radar_items: list[dict[str, Any]],
) -> dict[str, Any]:
    config_by_project = {
        str(source.get("id")): source
        for source in config.get("sources", [])
        if isinstance(source, dict)
    }
    upstream_by_project = {
        str(source.get("id")): source
        for source in upstream_hub_payload.get("sources", [])
        if isinstance(source, dict)
    }

    ai_count_by_key: dict[str, int] = {}
    sample_by_key: dict[str, dict[str, Any]] = {}
    for item in radar_items:
        if not isinstance(item, dict):
            continue
        ref = source_ref_for_item(item)
        key = str(ref.get("source_key") or "")
        if not key:
            continue
        ai_count_by_key[key] = ai_count_by_key.get(key, 0) + 1
        sample_by_key.setdefault(key, ref)

    def base_project(project_id: str) -> dict[str, Any]:
        cfg = config_by_project.get(project_id, {})
        upstream = upstream_by_project.get(project_id, {})
        return {
            "id": project_id,
            "name": PROJECT_DISPLAY_NAMES.get(project_id, cfg.get("name") or project_id),
            "configured": bool(cfg),
            "enabled": bool(cfg.get("enabled", True)) if cfg else True,
            "kind": cfg.get("kind") or upstream.get("kind") or "local_pipeline",
            "homepage": cfg.get("homepage") or upstream.get("homepage"),
            "description": cfg.get("description") or upstream.get("description") or upstream.get("capability") or "",
            "status": upstream.get("status") or "ok",
            "item_count": int(upstream.get("item_count") or 0),
            "sources": [],
        }

    projects: dict[str, dict[str, Any]] = {project_id: base_project(project_id) for project_id in PROJECT_CATALOG_ORDER}

    for row in source_quality:
        site_id = str(row.get("site_id") or "").strip()
        if not site_id or site_id == "aihot":
            continue
        key = f"ai_news_radar::collector::{site_id}"
        projects["ai_news_radar"]["sources"].append(
            {
                "id": site_id,
                "name": row.get("site_name") or site_id,
                "kind": "collector",
                "kind_label": SOURCE_KIND_LABELS["collector"],
                "source_key": key,
                "enabled": True,
                "status": "ok" if row.get("ok", True) else "error",
                "item_count": int(row.get("fetch_item_count") or row.get("raw_24h") or 0),
                "raw_24h": int(row.get("raw_24h") or 0),
                "ai_24h": int(row.get("ai_24h") or ai_count_by_key.get(key, 0)),
                "ai_hit_rate": float(row.get("ai_hit_rate") or 0.0),
                "decision": row.get("decision"),
                "error": row.get("error"),
            }
        )

    aihot_counts: dict[str, dict[str, Any]] = {}
    for item in (upstream_by_project.get("aihot", {}).get("items") or []):
        if not isinstance(item, dict):
            continue
        ref = source_ref_for_item(item)
        key = str(ref.get("source_key"))
        bucket = aihot_counts.setdefault(
            key,
            {
                "id": ref["source_id"],
                "name": ref["name"],
                "kind": ref["kind"],
                "kind_label": ref["kind_label"],
                "source_key": key,
                "enabled": True,
                "status": "ok",
                "item_count": 0,
                "raw_24h": 0,
                "ai_24h": ai_count_by_key.get(key, 0),
            },
        )
        bucket["item_count"] += 1
        bucket["raw_24h"] += 1

    for key, ref in sample_by_key.items():
        if ref.get("project_id") != "aihot":
            continue
        aihot_counts.setdefault(
            key,
            {
                "id": ref["source_id"],
                "name": ref["name"],
                "kind": ref["kind"],
                "kind_label": ref["kind_label"],
                "source_key": key,
                "enabled": True,
                "status": "ok",
                "item_count": ai_count_by_key.get(key, 0),
                "raw_24h": ai_count_by_key.get(key, 0),
                "ai_24h": ai_count_by_key.get(key, 0),
            },
        )
    projects["aihot"]["sources"] = list(aihot_counts.values()) or [
        {
            "id": "public-api",
            "name": "AI HOT 公开 API",
            "kind": "feed",
            "kind_label": SOURCE_KIND_LABELS["feed"],
            "source_key": "aihot::feed::public-api",
            "enabled": True,
            "status": projects["aihot"]["status"],
            "item_count": projects["aihot"]["item_count"],
            "raw_24h": projects["aihot"]["item_count"],
            "ai_24h": 0,
        }
    ]

    trend_cfg = config_by_project.get("trendradar", {})
    trend_items = upstream_by_project.get("trendradar", {}).get("items") or []
    trend_raw_counts: dict[str, int] = {}
    for item in trend_items:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        platform_id = str(extra.get("platform_id") or "").strip()
        if platform_id:
            trend_raw_counts[platform_id] = trend_raw_counts.get(platform_id, 0) + 1
    for platform in trend_cfg.get("platforms") or []:
        if not isinstance(platform, dict):
            continue
        platform_id = str(platform.get("id") or "").strip()
        if not platform_id:
            continue
        key = f"trendradar::platform::{platform_id}"
        projects["trendradar"]["sources"].append(
            {
                "id": platform_id,
                "name": platform.get("name") or platform_id,
                "kind": "platform",
                "kind_label": SOURCE_KIND_LABELS["platform"],
                "source_key": key,
                "enabled": True,
                "status": "ok" if trend_raw_counts.get(platform_id, 0) else "watch",
                "item_count": trend_raw_counts.get(platform_id, 0),
                "raw_24h": trend_raw_counts.get(platform_id, 0),
                "ai_24h": ai_count_by_key.get(key, 0),
            }
        )

    horizon_key = "horizon::feed::feed-zh"
    projects["horizon"]["sources"] = [
        {
            "id": "feed-zh",
            "name": "Horizon 中文 Atom Feed",
            "kind": "feed",
            "kind_label": SOURCE_KIND_LABELS["feed"],
            "source_key": horizon_key,
            "enabled": True,
            "status": projects["horizon"]["status"],
            "item_count": projects["horizon"]["item_count"],
            "raw_24h": projects["horizon"]["item_count"],
            "ai_24h": ai_count_by_key.get(horizon_key, 0),
        }
    ]

    for project in projects.values():
        project["sources"].sort(
            key=lambda source: (
                -int(source.get("ai_24h") or 0),
                -int(source.get("item_count") or 0),
                str(source.get("name") or ""),
            )
        )
        project["source_count"] = len(project["sources"])
        project["ai_24h"] = sum(int(source.get("ai_24h") or 0) for source in project["sources"])
        project["raw_24h"] = sum(int(source.get("raw_24h") or source.get("item_count") or 0) for source in project["sources"])

    return {
        "mode": "browser_local_picker_v1",
        "note": "前台删除会保存在当前浏览器，用于过滤页面展示；云端抓取配置需要接入 GitHub Actions 后写回仓库。",
        "project_count": len(projects),
        "source_count": sum(int(project.get("source_count") or 0) for project in projects.values()),
        "projects": [projects[project_id] for project_id in PROJECT_CATALOG_ORDER],
    }


def build_radar_payload(
    latest_items_ai: list[dict[str, Any]],
    source_quality: list[dict[str, Any]],
    status_payload: dict[str, Any],
    now: datetime,
    upstream_hub_url: str = "data/upstream-hub.json",
    china_hot_items: list[dict[str, Any]] | None = None,
    source_catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sorted_items = sorted(
        latest_items_ai,
        key=lambda item: (brief_item_score(item), event_time(item) or datetime.min.replace(tzinfo=UTC)),
        reverse=True,
    )
    sections = []
    for category in RADAR_CATEGORIES:
        category_items = [item for item in sorted_items if category_for_item(item)["id"] == category["id"]]
        project_sources = sorted({project_source_name_for_item(item) for item in category_items})
        section_items = blended_project_items(category_items, limit=10)
        sections.append(
            {
                "id": category["id"],
                "title": category["title"],
                "description": category["description"],
                "count": len(category_items),
                "project_sources": project_sources,
                "items": [slim_radar_item(item, now) for item in section_items],
            }
        )

    new_cutoff = now - timedelta(hours=6)
    new_items = [
        item
        for item in sorted_items
        if (parse_iso(item.get("first_seen_at")) or datetime.min.replace(tzinfo=UTC)) >= new_cutoff
    ]

    return {
        "generated_at": iso(now),
        "timezone": "Asia/Shanghai",
        "window_hours": status_payload.get("window_hours", 24),
        "upstream_hub_url": upstream_hub_url,
        "summary": {
            "ai_items": len(latest_items_ai),
            "raw_items": status_payload.get("items_before_topic_filter", 0),
            "fetched_raw_items": status_payload.get("fetched_raw_items", 0),
            "successful_sites": status_payload.get("successful_sites", 0),
            "site_count": len(status_payload.get("sites", [])),
            "source_quality": status_payload.get("source_quality_summary", {}),
        },
        "top_stories": build_story_clusters(sorted_items, now),
        "sections": sections,
        "china_hot_items": china_hot_items or [],
        "new_items": [slim_radar_item(item, now) for item in new_items[:20]],
        "source_catalog": source_catalog or {},
        "source_quality": source_quality,
    }


def load_upstream_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"sources": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"sources": []}
    if not isinstance(data, dict):
        return {"sources": []}
    sources = data.get("sources")
    if not isinstance(sources, list):
        data["sources"] = []
    return data


def upstream_time_label(dt_str: str | None, now: datetime) -> str:
    dt = parse_iso(dt_str)
    if not dt:
        return "刚刚抓取"
    local = dt.astimezone(SH_TZ)
    delta = now.astimezone(SH_TZ) - local
    if timedelta(0) <= delta < timedelta(hours=1):
        return f"{max(1, int(delta.total_seconds() // 60))}分钟前"
    if timedelta(0) <= delta < timedelta(hours=24):
        return f"{max(1, int(delta.total_seconds() // 3600))}小时前"
    return f"{local:%m-%d %H:%M}"


def compact_summary(text: str, limit: int = 180) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def normalize_upstream_item(
    *,
    upstream_id: str,
    upstream_name: str,
    title: str,
    url: str,
    now: datetime,
    source: str = "",
    summary: str = "",
    published_at: str | None = None,
    category: str = "",
    score: float | None = None,
    rank: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    title = str(title or "").strip()
    url = str(url or "").strip()
    if not title or not url:
        return None
    item_id = hashlib.sha1(f"{upstream_id}|{title}|{url}".encode("utf-8")).hexdigest()[:16]
    return {
        "id": item_id,
        "upstream_id": upstream_id,
        "upstream_name": upstream_name,
        "title": title,
        "url": url if "#" in url else normalize_url(url),
        "source": source or upstream_name,
        "summary": compact_summary(summary),
        "published_at": published_at,
        "time_label": upstream_time_label(published_at, now),
        "category": category or "signal",
        "score": score,
        "rank": rank,
        "extra": extra or {},
    }


def fetch_aihot_upstream(session: requests.Session, source_cfg: dict[str, Any], now: datetime) -> dict[str, Any]:
    url = source_cfg.get("items_url") or "https://aihot.virxact.com/api/public/items?mode=selected&take=30"
    resp = session.get(
        url,
        timeout=20,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "application/json, text/plain, */*",
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    items = []
    category_map = {
        "ai-models": "模型发布",
        "ai-products": "产品工具",
        "industry": "行业动态",
        "paper": "论文研究",
        "tip": "技巧观点",
    }
    for index, item in enumerate(payload.get("items") or [], start=1):
        if not isinstance(item, dict):
            continue
        normalized = normalize_upstream_item(
            upstream_id="aihot",
            upstream_name="AI HOT Skill",
            title=str(item.get("title") or item.get("title_en") or ""),
            url=str(item.get("url") or ""),
            source=str(item.get("source") or "AI HOT"),
            summary=str(item.get("summary") or ""),
            published_at=iso(parse_iso(item.get("publishedAt"))),
            category=category_map.get(str(item.get("category") or ""), str(item.get("category") or "精选")),
            score=9.0 if item.get("aiSelected", True) else 7.2,
            rank=index,
            now=now,
        )
        if normalized:
            items.append(normalized)
    return {
        "id": "aihot",
        "name": "AI HOT Skill",
        "kind": "public_api",
        "homepage": "https://aihot.virxact.com",
        "status": "ok",
        "item_count": len(items),
        "items": items,
        "capability": "公开 API 拉取中文 AI 精选、分类和滚动时间窗。",
    }


def fetch_ai_news_radar_upstream(latest_items_ai: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    items = []
    for index, item in enumerate(latest_items_ai[:30], start=1):
        normalized = normalize_upstream_item(
            upstream_id="ai_news_radar",
            upstream_name="AI News Radar",
            title=radar_title(item),
            url=str(item.get("url") or ""),
            source=str(item.get("site_name") or item.get("source") or "AI News Radar"),
            summary=str(item.get("ai_relevance_reason") or ""),
            published_at=item.get("published_at") or item.get("first_seen_at"),
            category=category_for_item(item)["title"],
            score=score_10(item),
            rank=index,
            now=now,
            extra={
                "signals": item.get("ai_signals") or [],
                "source": item.get("source"),
            },
        )
        if normalized:
            items.append(normalized)
    return {
        "id": "ai_news_radar",
        "name": "AI News Radar",
        "kind": "local_pipeline",
        "homepage": "https://github.com/LearnPrompt/ai-news-radar",
        "status": "ok",
        "item_count": len(items),
        "items": items,
        "capability": "本地 24 小时 AI 信号抓取、AI 相关性过滤、源健康和静态页面发布。",
    }


def fetch_trendradar_upstream(session: requests.Session, source_cfg: dict[str, Any], now: datetime) -> dict[str, Any]:
    api_base = str(source_cfg.get("api_base") or "https://newsnow.busiyi.world/api/s").strip()
    platforms = source_cfg.get("platforms") or []
    if not isinstance(platforms, list):
        platforms = []
    max_platforms = max(1, min(int(source_cfg.get("max_platforms") or 15), 30))
    max_items = max(1, min(int(source_cfg.get("max_items") or 120), 800))
    items = []
    failures = []
    for platform in platforms[:max_platforms]:
        if not isinstance(platform, dict):
            continue
        pid = str(platform.get("id") or "").strip()
        pname = str(platform.get("name") or pid).strip()
        if not pid:
            continue
        try:
            resp = session.get(
                f"{api_base}?id={pid}&latest",
                timeout=12,
                headers={"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*"},
            )
            resp.raise_for_status()
            payload = resp.json()
            updated = parse_unix_timestamp(payload.get("updatedTime"))
            for index, item in enumerate(payload.get("items") or [], start=1):
                if not isinstance(item, dict):
                    continue
                extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
                normalized = normalize_upstream_item(
                    upstream_id="trendradar",
                    upstream_name="TrendRadar",
                    title=str(item.get("title") or ""),
                    url=str(item.get("url") or item.get("mobileUrl") or ""),
                    source=pname,
                    summary=str(extra.get("hover") or extra.get("info") or ""),
                    published_at=iso(updated),
                    category="多平台热榜",
                    score=round(max(5.5, 8.8 - min(index, 20) * 0.12), 1),
                    rank=index,
                    now=now,
                    extra={"platform_id": pid, "platform_name": pname, "info": extra.get("info")},
                )
                if normalized:
                    items.append(normalized)
        except Exception as exc:
            failures.append({"id": pid, "name": pname, "error": type(exc).__name__})
    return {
        "id": "trendradar",
        "name": "TrendRadar",
        "kind": "newsnow_hotlist",
        "homepage": "https://github.com/sansan0/TrendRadar",
        "status": "ok" if items else "error",
        "item_count": len(items),
        "items": items[:max_items],
        "failures": failures,
        "capability": "多平台热榜聚合、关键词/AI 筛选、增量推送和 MCP。",
    }


def parse_horizon_feed_items(feed_xml: bytes, source_url: str, now: datetime) -> list[dict[str, Any]]:
    root = ET.fromstring(feed_xml)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = []
    for entry in root.findall("atom:entry", ns)[:3]:
        entry_title = first_non_empty(entry.findtext("atom:title", default="", namespaces=ns), "Horizon Summary")
        link_node = entry.find("atom:link", ns)
        entry_url = str(link_node.attrib.get("href") if link_node is not None else source_url)
        updated = parse_date_any(entry.findtext("atom:updated", default="", namespaces=ns), now)
        content = entry.findtext("atom:content", default="", namespaces=ns)
        content = html_lib.unescape(content or "")
        matches = re.findall(
            r'<li>\s*<a[^>]*>(.*?)</a>\s*⭐️\s*(\d+(?:\.\d+)?)\/10',
            content,
            flags=re.S,
        )
        if matches:
            for index, (title_html, score_raw) in enumerate(matches[:10], start=1):
                title = re.sub(r"<[^>]+>", "", html_lib.unescape(title_html)).strip()
                normalized = normalize_upstream_item(
                    upstream_id="horizon",
                    upstream_name="Horizon",
                    title=title,
                    url=f"{entry_url}#item-{index}",
                    source=entry_title,
                    summary="Horizon 评分日报条目",
                    published_at=iso(updated),
                    category="AI评分日报",
                    score=float(score_raw),
                    rank=index,
                    now=now,
                )
                if normalized:
                    items.append(normalized)
        else:
            normalized = normalize_upstream_item(
                upstream_id="horizon",
                upstream_name="Horizon",
                title=entry_title,
                url=entry_url,
                source="Horizon Daily",
                summary="Horizon 双语日报",
                published_at=iso(updated),
                category="双语日报",
                score=8.0,
                rank=len(items) + 1,
                now=now,
            )
            if normalized:
                items.append(normalized)
    return items


def fetch_horizon_upstream(session: requests.Session, source_cfg: dict[str, Any], now: datetime) -> dict[str, Any]:
    feed_url = source_cfg.get("feed_url") or "https://thysrael.github.io/Horizon/feed-zh.xml"
    resp = session.get(str(feed_url), timeout=15, headers={"User-Agent": BROWSER_UA})
    resp.raise_for_status()
    items = parse_horizon_feed_items(resp.content, str(feed_url), now)
    return {
        "id": "horizon",
        "name": "Horizon",
        "kind": "atom_feed",
        "homepage": "https://thysrael.github.io/Horizon/",
        "status": "ok",
        "item_count": len(items),
        "items": items,
        "capability": "AI 评分、故事去重、双语日报、GitHub Pages 发布和 MCP。",
    }


def build_upstream_hub_payload(
    session: requests.Session,
    latest_items_ai: list[dict[str, Any]],
    now: datetime,
    config_path: Path,
) -> dict[str, Any]:
    config = load_upstream_config(config_path)
    enabled = {
        str(source.get("id")): source
        for source in config.get("sources", [])
        if isinstance(source, dict) and source.get("enabled", True)
    }
    fetchers = {
        "aihot": lambda cfg: fetch_aihot_upstream(session, cfg, now),
        "ai_news_radar": lambda cfg: fetch_ai_news_radar_upstream(latest_items_ai, now),
        "trendradar": lambda cfg: fetch_trendradar_upstream(session, cfg, now),
        "horizon": lambda cfg: fetch_horizon_upstream(session, cfg, now),
    }
    sources = []
    for source_id in ("aihot", "ai_news_radar", "trendradar", "horizon"):
        cfg = enabled.get(source_id)
        if cfg is None:
            continue
        try:
            payload = fetchers[source_id](cfg)
            payload["configured"] = True
            payload["description"] = cfg.get("description") or payload.get("capability")
        except Exception as exc:
            payload = {
                "id": source_id,
                "name": cfg.get("name") or source_id,
                "kind": cfg.get("kind") or "unknown",
                "homepage": cfg.get("homepage"),
                "status": "error",
                "item_count": 0,
                "items": [],
                "error": type(exc).__name__,
                "configured": True,
                "description": cfg.get("description") or "",
            }
        sources.append(payload)
    all_items = []
    for source in sources:
        all_items.extend(source.get("items") or [])
    all_items.sort(
        key=lambda item: (
            float(item.get("score") or 0),
            parse_iso(item.get("published_at")) or datetime.min.replace(tzinfo=UTC),
        ),
        reverse=True,
    )
    return {
        "generated_at": iso(now),
        "timezone": "Asia/Shanghai",
        "source_count": len(sources),
        "ok_count": sum(1 for source in sources if source.get("status") == "ok"),
        "total_items": len(all_items),
        "sources": sources,
        "items": all_items[:120],
        "config_path": str(config_path),
    }


def upstream_item_to_radar_record(item: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    upstream_id = str(item.get("upstream_id") or "").strip()
    if upstream_id not in UPSTREAM_PROJECT_NAMES:
        return None
    platform_id = str((item.get("extra") or {}).get("platform_id") or "").strip()
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    if not title:
        return None
    published_at = item.get("published_at") or iso(now)
    project_name = UPSTREAM_PROJECT_NAMES[upstream_id]
    platform_name = str((item.get("extra") or {}).get("platform_name") or item.get("source") or project_name).strip()
    score = item.get("score")
    try:
        upstream_score = float(score or 0)
    except (TypeError, ValueError):
        upstream_score = 0.0
    record = {
        "id": f"{upstream_id}:{item.get('id') or hashlib.sha1((title + url).encode('utf-8')).hexdigest()[:16]}",
        "site_id": upstream_id,
        "site_name": project_name,
        "source": platform_name,
        "title": title,
        "title_zh": title,
        "title_original": title,
        "url": url,
        "published_at": published_at,
        "first_seen_at": published_at,
        "summary": item.get("summary") or "",
        "upstream_id": upstream_id,
        "upstream_name": project_name,
        "platform_id": platform_id,
        "rank": item.get("rank"),
        "ai_score": min(1.0, max(0.0, upstream_score / 10)) if upstream_score else None,
    }
    record = add_ai_relevance_fields(record)
    if not record.get("ai_is_related") and upstream_id in CURATED_AI_UPSTREAMS:
        record["ai_is_related"] = True
        record["ai_score"] = max(float(record.get("ai_score") or 0), 0.72)
        record["ai_label"] = record.get("ai_label") or "ai_general"
        signals = list(record.get("ai_signals") or [])
        if project_name not in signals:
            signals.append(project_name)
        record["ai_signals"] = signals
        record["ai_relevance_reason"] = "curated_upstream_signal"
    if not record.get("ai_is_related"):
        return None
    return record


def compact_title_key(title: str) -> str:
    return re.sub(r"\s+", "", str(title or "").strip().lower())


def merge_upstream_items_for_radar(
    latest_items_ai: list[dict[str, Any]],
    upstream_hub_payload: dict[str, Any],
    now: datetime,
    limit: int = 80,
) -> list[dict[str, Any]]:
    existing_keys = {
        compact_title_key(str(item.get("title_zh") or item.get("title") or ""))
        for item in latest_items_ai
    }
    additions: list[dict[str, Any]] = []
    upstream_items = []
    for source in upstream_hub_payload.get("sources") or []:
        if isinstance(source, dict):
            upstream_items.extend(source.get("items") or [])
    for item in upstream_items:
        if not isinstance(item, dict):
            continue
        record = upstream_item_to_radar_record(item, now)
        if not record:
            continue
        key = compact_title_key(str(record.get("title_zh") or record.get("title") or ""))
        if not key or key in existing_keys:
            continue
        existing_keys.add(key)
        additions.append(record)
    additions.sort(
        key=lambda item: (
            brief_item_score(item),
            event_time(item) or datetime.min.replace(tzinfo=UTC),
        ),
        reverse=True,
    )
    return latest_items_ai + additions[:limit]


def build_china_hot_items(upstream_hub_payload: dict[str, Any], now: datetime, limit: int = 16) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    upstream_items = []
    for source in upstream_hub_payload.get("sources") or []:
        if isinstance(source, dict):
            upstream_items.extend(source.get("items") or [])
    for item in upstream_items:
        if not isinstance(item, dict) or item.get("upstream_id") != "trendradar":
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        platform_name = str(extra.get("platform_name") or item.get("source") or "").strip()
        platform_id = str(extra.get("platform_id") or "").strip()
        if platform_name in {"GitHub Trending", "Hacker News", "Product Hunt", "V2EX"}:
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        record = {
            "id": item.get("id"),
            "title": title,
            "url": item.get("url"),
            "source": platform_name or "中文热榜",
            "site_name": "TrendRadar 中文热榜",
            "published_at": item.get("published_at") or iso(now),
            "first_seen_at": item.get("published_at") or iso(now),
            "rank": item.get("rank"),
            "platform_id": platform_id,
        }
        relevance = score_ai_relevance({
            "site_id": "trendradar",
            "site_name": "TrendRadar 中文热榜",
            "source": platform_name,
            "title": title,
            "url": item.get("url"),
        })
        record["ai_is_related"] = relevance["is_ai_related"]
        record["ai_score"] = relevance["score"]
        record["ai_signals"] = relevance["signals"]
        record["topic_labels"] = relevance.get("topic_labels") or []
        record["topic_sections"] = relevance.get("topic_sections") or []
        source_ref = source_ref_for_item(
            {
                "upstream_id": "trendradar",
                "source": platform_name,
                "platform_id": platform_id,
                "extra": {"platform_id": platform_id, "platform_name": platform_name},
            }
        )
        record["source_ref"] = source_ref
        record["source_key"] = source_ref["source_key"]
        items.append(record)

    items.sort(
        key=lambda item: (
            bool(item.get("ai_is_related")),
            float(item.get("ai_score") or 0),
            -int(item.get("rank") or 999),
        ),
        reverse=True,
    )
    return items[:limit]


def build_daily_brief_markdown(
    latest_items_ai: list[dict[str, Any]],
    source_quality: list[dict[str, Any]],
    status_payload: dict[str, Any],
    generated_at: str | None,
) -> str:
    now = parse_iso(generated_at) or utc_now()
    sorted_items = sorted(
        latest_items_ai,
        key=lambda item: (
            brief_item_score(item),
            event_time(item) or datetime.min.replace(tzinfo=UTC),
        ),
        reverse=True,
    )
    quality_top = sorted(
        source_quality,
        key=lambda row: (int(row.get("ai_24h") or 0), float(row.get("ai_hit_rate") or 0.0)),
        reverse=True,
    )[:8]

    lines = [
        "# AI News Radar 日报",
        "",
        f"- 生成时间：{generated_at or 'unknown'}",
        f"- 24小时AI信号：{len(latest_items_ai)} 条",
        f"- 源健康：{status_payload.get('successful_sites', 0)}/{len(status_payload.get('sites', []))}",
        f"- 原始抓取：{status_payload.get('fetched_raw_items', 0)} 条",
        "",
        "## 今日重点",
        "",
    ]
    if not sorted_items:
        lines.append("暂无AI强相关信号。")
    rank = 1
    for category in RADAR_CATEGORIES:
        category_items = [item for item in sorted_items if category_for_item(item)["id"] == category["id"]][:5]
        if not category_items:
            continue
        lines.extend(["", f"### {category['title']}", ""])
        for item in category_items:
            title = radar_title(item)
            source = str(item.get("site_name") or item.get("source") or "来源")
            reason = str(item.get("ai_relevance_reason") or "matched")
            lines.append(f"{rank}. {markdown_link(title, str(item.get('url') or ''))}")
            lines.append(f"   - 时间：{time_label_for_item(item, now)}")
            lines.append(f"   - 来源：{source}")
            lines.append(f"   - 评分：{score_10(item):.1f}/10 · {reason}")
            rank += 1

    lines.extend(["", "## 源质量建议", ""])
    if quality_top:
        lines.append("| 来源 | AI/全量 | 命中率 | 建议 |")
        lines.append("| --- | ---: | ---: | --- |")
        for row in quality_top:
            hit_pct = round(float(row.get("ai_hit_rate") or 0.0) * 100, 1)
            lines.append(
                f"| {row.get('site_name')} | {row.get('ai_24h')}/{row.get('raw_24h')} | {hit_pct}% | {row.get('decision_reason')} |"
            )
    else:
        lines.append("暂无源质量数据。")

    lines.extend(["", "## 维护提示", ""])
    failed_sites = status_payload.get("failed_sites") or []
    if failed_sites:
        lines.append(f"- 需要优先修复失败源：{', '.join(str(site) for site in failed_sites)}")
    else:
        lines.append("- 当前内置源抓取正常。")
    lines.append("- 低命中、高产出的聚合源建议先降权观察，不要继续盲目加源。")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate AI news updates from multiple sources")
    parser.add_argument("--output-dir", default="data", help="Directory for output JSON files")
    parser.add_argument("--window-hours", type=int, default=24, help="24h window size")
    parser.add_argument("--archive-days", type=int, default=21, help="Keep archive for N days")
    parser.add_argument("--translate-max-new", type=int, default=80, help="Max new EN->ZH title translations per run")
    parser.add_argument("--rss-opml", default="", help="Optional OPML file path to include RSS sources")
    parser.add_argument("--rss-max-feeds", type=int, default=0, help="Optional max OPML RSS feeds to fetch (0 means all)")
    parser.add_argument("--upstream-config", default=UPSTREAM_CONFIG_DEFAULT, help="JSON config for upstream project integrations")
    args = parser.parse_args()

    now = utc_now()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    archive_path = output_dir / "archive.json"
    latest_path = output_dir / "latest-24h.json"
    latest_all_path = output_dir / "latest-24h-all.json"
    status_path = output_dir / "source-status.json"
    archive_index_path = output_dir / "archive-index.json"
    daily_brief_path = output_dir / "daily-brief.zh.md"
    radar_brief_path = output_dir / "radar-brief.json"
    upstream_hub_path = output_dir / "upstream-hub.json"
    waytoagi_path = output_dir / "waytoagi-7d.json"
    title_cache_path = output_dir / "title-zh-cache.json"
    email_digest_path = output_dir / AGENTMAIL_DIGEST_FILE

    archive = load_archive(archive_path)

    session = create_session()
    raw_items, statuses = collect_all(session, now)
    rss_feed_statuses: list[dict[str, Any]] = []
    email_digest_payload, agentmail_status = maybe_fetch_agentmail_digest(
        session,
        generated_at=iso(now),
        after=iso(now - timedelta(hours=args.window_hours)),
        window_hours=args.window_hours,
    )
    x_api_items, x_api_status = maybe_fetch_x_api_updates(session, now)
    if x_api_status.get("enabled"):
        raw_items.extend(x_api_items)
        statuses.append(
            {
                "site_id": "xapi",
                "site_name": "X API",
                "ok": bool(x_api_status.get("ok")) if x_api_status.get("ok") is not None else True,
                "item_count": int(x_api_status.get("item_count") or 0),
                "duration_ms": 0,
                "error": x_api_status.get("error"),
                "skipped": bool(x_api_status.get("skipped")),
                "skip_reason": x_api_status.get("skip_reason"),
            }
        )

    if args.rss_opml:
        opml_path = Path(args.rss_opml).expanduser()
        if opml_path.exists():
            rss_items, rss_summary_status, rss_feed_statuses = fetch_opml_rss(
                now,
                opml_path,
                max_feeds=max(0, int(args.rss_max_feeds)),
            )
            raw_items.extend(rss_items)
            statuses.append(rss_summary_status)
        else:
            statuses.append(
                {
                    "site_id": "opmlrss",
                    "site_name": "OPML RSS",
                    "ok": False,
                    "item_count": 0,
                    "duration_ms": 0,
                    "error": f"OPML not found: {opml_path}",
                    "feed_count": 0,
                    "ok_feed_count": 0,
                    "failed_feed_count": 0,
                }
            )

    seen_this_run: set[str] = set()

    for raw in raw_items:
        title = raw.title.strip()
        url = normalize_url(raw.url)
        if not title or not url:
            continue
        if not url.startswith("http"):
            continue

        item_id = make_item_id(raw.site_id, raw.source, title, url)
        seen_this_run.add(item_id)

        existing = archive.get(item_id)
        if existing is None:
            archive[item_id] = {
                "id": item_id,
                "site_id": raw.site_id,
                "site_name": raw.site_name,
                "source": raw.source,
                "title": title,
                "url": url,
                "published_at": iso(raw.published_at),
                "first_seen_at": iso(now),
                "last_seen_at": iso(now),
            }
        else:
            existing["site_id"] = raw.site_id
            existing["site_name"] = raw.site_name
            existing["source"] = raw.source
            existing["title"] = title
            existing["url"] = url
            if raw.published_at:
                # OPML RSS may fix previously wrong publish times; allow overwrite.
                if raw.site_id == "opmlrss" or not existing.get("published_at"):
                    existing["published_at"] = iso(raw.published_at)
            existing["last_seen_at"] = iso(now)

    # Prune old archive
    keep_after = now - timedelta(days=args.archive_days)
    pruned: dict[str, dict[str, Any]] = {}
    for item_id, record in archive.items():
        ts = (
            parse_iso(record.get("last_seen_at"))
            or parse_iso(record.get("published_at"))
            or parse_iso(record.get("first_seen_at"))
            or now
        )
        if ts >= keep_after:
            pruned[item_id] = record
    archive = pruned

    # 24h view
    window_start = now - timedelta(hours=args.window_hours)
    latest_items_all: list[dict[str, Any]] = []
    for record in archive.values():
        ts = event_time(record)
        if not ts:
            continue
        if ts >= window_start:
            normalized = dict(record)
            normalized["title"] = maybe_fix_mojibake(str(normalized.get("title") or ""))
            normalized["source"] = maybe_fix_mojibake(normalize_source_for_display(
                str(normalized.get("site_id") or ""),
                str(normalized.get("source") or ""),
                str(normalized.get("url") or ""),
            ))
            if str(normalized.get("site_id") or "") == "aihubtoday" and is_hubtoday_placeholder_title(
                str(normalized.get("title") or "")
            ):
                continue
            normalized = add_ai_relevance_fields(normalized)
            latest_items_all.append(normalized)

    latest_items_all = normalize_aihubtoday_records(latest_items_all)

    latest_items_all.sort(key=lambda x: event_time(x) or datetime.min.replace(tzinfo=UTC), reverse=True)
    latest_items = [record for record in latest_items_all if record.get("ai_is_related", is_ai_related_record(record))]
    title_cache = load_title_zh_cache(title_cache_path)
    latest_items, latest_items_all, title_cache = add_bilingual_fields(
        latest_items,
        latest_items_all,
        session,
        title_cache,
        max_new_translations=max(0, args.translate_max_new),
    )
    latest_items_ai_dedup = dedupe_items_by_title_url(latest_items, random_pick=False)
    latest_items_all_dedup = dedupe_items_by_title_url(latest_items_all, random_pick=True)

    # site stats
    site_stat: dict[str, dict[str, Any]] = {}
    raw_count_by_site: dict[str, int] = {}
    for record in latest_items_all:
        sid = record["site_id"]
        raw_count_by_site[sid] = raw_count_by_site.get(sid, 0) + 1

    site_name_by_id: dict[str, str] = {}
    for record in latest_items_all:
        site_name_by_id[record["site_id"]] = record["site_name"]
    for s in statuses:
        sid = s["site_id"]
        if sid not in site_name_by_id:
            site_name_by_id[sid] = s.get("site_name") or sid

    for record in latest_items_ai_dedup:
        sid = record["site_id"]
        if sid not in site_stat:
            site_stat[sid] = {
                "site_id": sid,
                "site_name": record["site_name"],
                "count": 0,
                "raw_count": raw_count_by_site.get(sid, 0),
            }
        site_stat[sid]["count"] += 1

    for sid, site_name in site_name_by_id.items():
        if sid in site_stat:
            continue
        site_stat[sid] = {
            "site_id": sid,
            "site_name": site_name,
            "count": 0,
            "raw_count": raw_count_by_site.get(sid, 0),
        }

    source_quality, source_quality_summary = build_source_quality_metrics(
        statuses,
        latest_items_ai_dedup,
        latest_items_all,
    )

    latest_payload = {
        "generated_at": iso(now),
        "window_hours": args.window_hours,
        "total_items": len(latest_items_ai_dedup),
        "total_items_ai_raw": len(latest_items),
        "total_items_raw": len(latest_items_all),
        "total_items_all_mode": len(latest_items_all_dedup),
        "topic_filter": "langben_tracked_keywords_v0_5",
        "ai_relevance_threshold": 0.65,
        "archive_total": len(archive),
        "archive_index_url": "data/archive-index.json",
        "daily_brief_url": "data/daily-brief.zh.md",
        "radar_brief_url": "data/radar-brief.json",
        "upstream_hub_url": "data/upstream-hub.json",
        "site_count": len(site_stat),
        "source_count": len({f"{i['site_id']}::{i['source']}" for i in latest_items_ai_dedup}),
        "site_stats": sorted(site_stat.values(), key=lambda x: x["count"], reverse=True),
        "source_quality_summary": source_quality_summary,
        "items": latest_items_ai_dedup,
        "items_ai": latest_items_ai_dedup,
        "items_all_raw": latest_items_all,
        "items_all": latest_items_all_dedup,
    }

    archive_payload = {
        "generated_at": iso(now),
        "total_items": len(archive),
        "items": sorted(
            archive.values(),
            key=lambda x: parse_iso(x.get("last_seen_at")) or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        ),
    }

    archive_index_payload = build_archive_index_payload(archive, iso(now), args.archive_days)

    status_payload = {
        "generated_at": iso(now),
        "window_hours": args.window_hours,
        "sites": statuses,
        "successful_sites": sum(1 for s in statuses if s["ok"]),
        "failed_sites": [s["site_id"] for s in statuses if not s["ok"]],
        "zero_item_sites": [s["site_id"] for s in statuses if s.get("ok") and int(s.get("item_count") or 0) == 0],
        "fetched_raw_items": len(raw_items),
        "items_before_topic_filter": len(latest_items_all),
        "items_in_24h": len(latest_items_ai_dedup),
        "rss_opml": {
            "enabled": bool(args.rss_opml),
            "path": "configured" if args.rss_opml else None,
            "feed_total": len(rss_feed_statuses),
            "effective_feed_total": sum(1 for s in rss_feed_statuses if not s.get("skipped")),
            "ok_feeds": sum(1 for s in rss_feed_statuses if s["ok"] and not s.get("skipped")),
            "failed_feeds": [s.get("effective_feed_url") or s["feed_url"] for s in rss_feed_statuses if not s["ok"]],
            "zero_item_feeds": [
                s.get("effective_feed_url") or s["feed_url"]
                for s in rss_feed_statuses
                if s["ok"] and not s.get("skipped") and int(s.get("item_count") or 0) == 0
            ],
            "skipped_feeds": [
                {"feed_url": s["feed_url"], "reason": s.get("skip_reason")}
                for s in rss_feed_statuses
                if s.get("skipped")
            ],
            "replaced_feeds": [
                {"from": s["feed_url"], "to": s.get("effective_feed_url")}
                for s in rss_feed_statuses
                if s.get("replaced") and s.get("effective_feed_url")
            ],
            "feeds": rss_feed_statuses,
        },
        "agentmail": agentmail_status,
        "x_api": x_api_status,
        "source_quality": source_quality,
        "source_quality_summary": source_quality_summary,
    }

    upstream_hub_payload = build_upstream_hub_payload(
        session,
        latest_items_ai_dedup,
        now,
        Path(args.upstream_config),
    )
    radar_items = merge_upstream_items_for_radar(latest_items_ai_dedup, upstream_hub_payload, now)
    china_hot_items = build_china_hot_items(upstream_hub_payload, now)
    upstream_config = load_upstream_config(Path(args.upstream_config))
    source_catalog = build_source_catalog(
        upstream_config,
        upstream_hub_payload,
        source_quality,
        radar_items,
    )
    daily_brief_markdown = build_daily_brief_markdown(
        radar_items,
        source_quality,
        status_payload,
        iso(now),
    )
    radar_payload = build_radar_payload(
        radar_items,
        source_quality,
        status_payload,
        now,
        upstream_hub_url="data/upstream-hub.json",
        china_hot_items=china_hot_items,
        source_catalog=source_catalog,
    )

    try:
        waytoagi_payload = fetch_waytoagi_recent_7d(session, now, WAYTOAGI_DEFAULT)
    except Exception as exc:
        waytoagi_payload = {
            "generated_at": iso(now),
            "timezone": "Asia/Shanghai",
            "root_url": WAYTOAGI_DEFAULT,
            "history_url": None,
            "window_days": 7,
            "count_7d": 0,
            "updates_7d": [],
            "warning": "WaytoAGI 近7日更新抓取失败",
            "has_error": True,
            "error": str(exc),
        }

    latest_payload, latest_all_payload = build_latest_payloads(latest_payload)

    latest_path.write_text(json.dumps(sanitize_public_payload(latest_payload), ensure_ascii=False, indent=2), encoding="utf-8")
    latest_all_path.write_text(json.dumps(sanitize_public_payload(latest_all_payload), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    archive_path.write_text(
        json.dumps(sanitize_public_payload(archive_payload), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    archive_index_path.write_text(
        json.dumps(sanitize_public_payload(archive_index_payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    daily_brief_path.write_text(redact_public_text(daily_brief_markdown), encoding="utf-8")
    radar_brief_path.write_text(
        json.dumps(sanitize_public_payload(radar_payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    upstream_hub_path.write_text(
        json.dumps(sanitize_public_payload(upstream_hub_payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    status_path.write_text(json.dumps(sanitize_public_payload(status_payload), ensure_ascii=False, indent=2), encoding="utf-8")
    if email_digest_payload is not None:
        email_digest_path.write_text(
            json.dumps(sanitize_public_payload(email_digest_payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    waytoagi_path.write_text(json.dumps(sanitize_public_payload(waytoagi_payload), ensure_ascii=False, indent=2), encoding="utf-8")
    title_cache_path.write_text(json.dumps(sanitize_public_payload(title_cache), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote: {latest_path} ({len(latest_items)} items)")
    print(f"Wrote: {latest_all_path} ({len(latest_items_all_dedup)} all-mode items)")
    print(f"Wrote: {archive_path} ({len(archive)} items)")
    print(f"Wrote: {archive_index_path} ({archive_index_payload.get('total_items', 0)} indexed items)")
    print(f"Wrote: {daily_brief_path} ({len(radar_items)} AI items)")
    print(f"Wrote: {radar_brief_path} ({len(radar_payload.get('top_stories', []))} top stories)")
    print(f"Wrote: {upstream_hub_path} ({upstream_hub_payload.get('total_items', 0)} upstream items)")
    print(f"Wrote: {status_path}")
    if email_digest_payload is not None:
        print(f"Wrote: {email_digest_path} ({email_digest_payload.get('total_messages', 0)} email items)")
    print(f"Wrote: {waytoagi_path} ({waytoagi_payload.get('count_7d', 0)} items)")
    print(f"Wrote: {title_cache_path} ({len(title_cache)} entries)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
