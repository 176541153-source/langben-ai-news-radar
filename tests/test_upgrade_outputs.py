from scripts.update_news import (
    build_archive_index_payload,
    build_daily_brief_markdown,
    build_radar_payload,
    build_source_catalog,
    build_source_quality_metrics,
    parse_horizon_feed_items,
)


def test_build_source_quality_metrics_recommends_keep_and_downgrade():
    statuses = [
        {"site_id": "official_ai", "site_name": "Official AI Updates", "ok": True, "item_count": 25, "duration_ms": 100},
        {"site_id": "noisy", "site_name": "Noisy Source", "ok": True, "item_count": 150, "duration_ms": 100},
        {"site_id": "broken", "site_name": "Broken Source", "ok": False, "item_count": 0, "duration_ms": 100, "error": "boom"},
    ]
    latest_ai = [
        {"site_id": "official_ai", "site_name": "Official AI Updates"},
        {"site_id": "official_ai", "site_name": "Official AI Updates"},
        {"site_id": "noisy", "site_name": "Noisy Source"},
    ]
    latest_all = (
        [{"site_id": "official_ai", "site_name": "Official AI Updates"} for _ in range(5)]
        + [{"site_id": "noisy", "site_name": "Noisy Source"} for _ in range(150)]
        + [{"site_id": "broken", "site_name": "Broken Source"}]
    )

    rows, summary = build_source_quality_metrics(statuses, latest_ai, latest_all)
    by_id = {row["site_id"]: row for row in rows}

    assert by_id["official_ai"]["decision"] == "keep"
    assert by_id["noisy"]["decision"] == "downgrade"
    assert by_id["broken"]["decision"] == "fix_fetch"
    assert summary["keep"] == 1
    assert summary["downgrade"] == 1
    assert summary["fix_fetch"] == 1


def test_build_archive_index_payload_counts_days_and_sites():
    archive = {
        "a": {
            "site_id": "official_ai",
            "site_name": "Official AI Updates",
            "published_at": "2026-05-25T00:00:00Z",
        },
        "b": {
            "site_id": "aibase",
            "site_name": "AIbase",
            "published_at": "2026-05-26T00:00:00Z",
        },
    }

    payload = build_archive_index_payload(archive, "2026-05-26T01:00:00Z", 21)

    assert payload["total_items"] == 2
    assert payload["archive_days"] == 21
    assert payload["oldest_event_at"] == "2026-05-25T00:00:00Z"
    assert payload["newest_event_at"] == "2026-05-26T00:00:00Z"
    assert payload["day_counts"][0] == {"date": "2026-05-26", "count": 1}
    assert payload["top_sites"][0]["count"] == 1


def test_build_daily_brief_markdown_contains_items_and_quality():
    latest_ai = [
        {
            "site_id": "official_ai",
            "site_name": "Official AI Updates",
            "title": "OpenAI ships a Codex update",
            "url": "https://example.com/codex",
            "published_at": "2026-05-26T00:00:00Z",
            "ai_score": 0.95,
            "ai_label": "model_release",
            "ai_relevance_reason": "matched_ai_signal",
        }
    ]
    source_quality = [
        {
            "site_name": "Official AI Updates",
            "ai_24h": 1,
            "raw_24h": 2,
            "ai_hit_rate": 0.5,
            "decision_reason": "高信号源，建议保留",
        }
    ]
    status_payload = {
        "successful_sites": 1,
        "sites": [{"site_id": "official_ai"}],
        "fetched_raw_items": 2,
        "failed_sites": [],
    }

    markdown = build_daily_brief_markdown(
        latest_ai,
        source_quality,
        status_payload,
        "2026-05-26T01:00:00Z",
    )

    assert "# AI News Radar 日报" in markdown
    assert "### 产品与开发者工具" in markdown
    assert "[OpenAI ships a Codex update](https://example.com/codex)" in markdown
    assert "| Official AI Updates | 1/2 | 50.0% | 高信号源，建议保留 |" in markdown


def test_build_radar_payload_groups_sections_and_stories():
    latest_ai = [
        {
            "id": "a",
            "site_id": "official_ai",
            "site_name": "Official AI Updates",
            "source": "OpenAI",
            "title": "OpenAI releases a new GPT model",
            "url": "https://example.com/a",
            "published_at": "2026-05-26T00:00:00Z",
            "first_seen_at": "2026-05-26T00:30:00Z",
            "ai_score": 0.9,
            "ai_label": "model_release",
        },
        {
            "id": "b",
            "site_id": "opmlrss",
            "site_name": "OPML RSS",
            "source": "Builder Blog",
            "title": "Codex workflow guide for agents",
            "url": "https://example.com/b",
            "published_at": "2026-05-26T01:00:00Z",
            "first_seen_at": "2026-05-26T01:30:00Z",
            "ai_score": 0.8,
            "ai_label": "agent_workflow",
        },
    ]
    source_quality = [
        {"site_id": "official_ai", "site_name": "Official AI Updates", "ai_24h": 1, "raw_24h": 1, "ai_hit_rate": 1.0}
    ]
    status_payload = {
        "window_hours": 24,
        "items_before_topic_filter": 2,
        "fetched_raw_items": 2,
        "successful_sites": 1,
        "sites": [{"site_id": "official_ai"}],
        "source_quality_summary": {"keep": 1},
    }

    payload = build_radar_payload(
        latest_ai,
        source_quality,
        status_payload,
        __import__("datetime").datetime.fromisoformat("2026-05-26T02:00:00+00:00"),
    )

    assert payload["summary"]["ai_items"] == 2
    assert payload["sections"][0]["id"] == "model_release"
    assert payload["sections"][0]["items"][0]["score"] == 9.0
    assert payload["top_stories"]
    assert payload["new_items"]


def test_build_source_catalog_lists_projects_and_subsources():
    config = {
        "sources": [
            {"id": "trendradar", "name": "TrendRadar", "platforms": [{"id": "ithome", "name": "IT之家"}]},
            {"id": "aihot", "name": "AI HOT"},
            {"id": "horizon", "name": "Horizon"},
            {"id": "ai_news_radar", "name": "AI News Radar"},
        ]
    }
    upstream_hub = {
        "sources": [
            {
                "id": "aihot",
                "status": "ok",
                "item_count": 1,
                "items": [{"upstream_id": "aihot", "source": "IT之家（RSS）", "title": "Qwen update"}],
            },
            {
                "id": "trendradar",
                "status": "ok",
                "item_count": 1,
                "items": [
                    {
                        "upstream_id": "trendradar",
                        "source": "IT之家",
                        "extra": {"platform_id": "ithome", "platform_name": "IT之家"},
                    }
                ],
            },
            {"id": "horizon", "status": "ok", "item_count": 1, "items": []},
        ]
    }
    source_quality = [
        {
            "site_id": "aibase",
            "site_name": "AIbase",
            "ok": True,
            "fetch_item_count": 10,
            "raw_24h": 10,
            "ai_24h": 2,
            "ai_hit_rate": 0.2,
        }
    ]
    radar_items = [
        {"site_id": "aibase", "site_name": "AIbase", "source": "AIbase", "title": "OpenAI news"},
        {"upstream_id": "trendradar", "source": "IT之家", "extra": {"platform_id": "ithome", "platform_name": "IT之家"}},
    ]

    catalog = build_source_catalog(config, upstream_hub, source_quality, radar_items)
    projects = {project["id"]: project for project in catalog["projects"]}

    assert catalog["source_count"] >= 4
    assert projects["ai_news_radar"]["sources"][0]["source_key"] == "ai_news_radar::collector::aibase"
    assert projects["trendradar"]["sources"][0]["source_key"] == "trendradar::platform::ithome"
    assert projects["trendradar"]["sources"][0]["ai_24h"] == 1


def test_parse_horizon_feed_items_extracts_scored_entries():
    xml = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Horizon Summary: 2026-05-25 (ZH)</title>
    <link href="https://example.com/summary.html"/>
    <updated>2026-05-25T00:00:00+00:00</updated>
    <content type="html"><![CDATA[
      <ol>
        <li><a href="#item-1">LLM agents improve backend code generation</a> \xe2\xad\x90\xef\xb8\x8f 9.0/10</li>
        <li><a href="#item-2">Memory dominates AI chip costs</a> \xe2\xad\x90\xef\xb8\x8f 8.0/10</li>
      </ol>
    ]]></content>
  </entry>
</feed>"""

    items = parse_horizon_feed_items(
        xml,
        "https://example.com/feed.xml",
        __import__("datetime").datetime.fromisoformat("2026-05-26T02:00:00+00:00"),
    )

    assert len(items) == 2
    assert items[0]["upstream_id"] == "horizon"
    assert items[0]["score"] == 9.0
    assert items[0]["url"] == "https://example.com/summary.html#item-1"
