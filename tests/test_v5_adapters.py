from __future__ import annotations

import gzip
from dataclasses import asdict
from io import BytesIO

import pytest
from PIL import Image

import airfryer_rankings.crawler as crawler
import airfryer_rankings.discovery as discovery
import airfryer_rankings.http as http_client
import airfryer_rankings.media as media
from airfryer_rankings.evidence import jsonld_objects, visible_rating_evidence
from airfryer_rankings.models import RecipeRow, SourceConfig
from airfryer_rankings.observability import build_pipeline_metrics
from airfryer_rankings.qa import detect_anomalies, source_health_summary
from airfryer_rankings.quality_gate import evaluate_publish_gate


class FakeParser:
    def __init__(self, allowed: bool = True):
        self.allowed = allowed

    def can_fetch(self, user_agent: str, url: str) -> bool:
        return self.allowed


class FakeResponse:
    def __init__(
        self, *, status_code: int = 200, text: str = "", content: bytes | None = None, headers: dict | None = None
    ):
        self.status_code = status_code
        self.text = text
        self.content = content if content is not None else text.encode()
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(response=response)


class FakeSession:
    def close(self) -> None:
        pass


def sample_row(recipe_id: str = "a", url: str = "https://example.com/air-fryer-chicken") -> RecipeRow:
    return RecipeRow(
        recipe_id=recipe_id,
        title="Air Fryer Chicken",
        source="example.com",
        url=url,
        rating=4.8,
        rating_count=100,
        best_rating=5.0,
        normalized_rating=4.8,
        retrieved_at="2026-08-18T20:00:00+00:00",
        canonical_url=url,
        ingredients=("chicken", "salt"),
        evidence_confidence=1.0,
        evidence_status="verified",
        page_hash="new-page",
        dom_fingerprint="new-dom",
        schema_signature="new-schema",
        image_url="https://example.com/chicken.jpg",
        categories=("Chicken",),
    )


def test_http_robots_and_recursive_sitemap_parsing(monkeypatch):
    responses = {
        "https://example.com/robots.txt": FakeResponse(
            text="User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n"
        ),
        "https://example.com/sitemap.xml": FakeResponse(
            content=b"<sitemapindex><sitemap><loc>https://example.com/recipes.xml</loc></sitemap></sitemapindex>"
        ),
        "https://example.com/recipes.xml": FakeResponse(
            content=b"<urlset><url><loc>https://example.com/air-fryer-chicken</loc><lastmod>2026-08-18</lastmod></url></urlset>"
        ),
    }

    monkeypatch.setattr(
        http_client,
        "get",
        lambda session, url, timeout=20, headers=None, **kwargs: responses[url],
    )
    parser, sitemaps, robots, status = http_client.robots_and_sitemaps(FakeSession(), SourceConfig("example.com"))
    assert status == "ok"
    assert "Sitemap:" in robots
    assert sitemaps == ["https://example.com/sitemap.xml"]
    assert parser.can_fetch("AirFryerRankingsBot/5.0", "https://example.com/air-fryer-chicken")
    diagnostics = {}
    rows = list(http_client.iter_sitemap_records(FakeSession(), sitemaps[0], diagnostics=diagnostics))
    assert rows == [
        {
            "url": "https://example.com/air-fryer-chicken",
            "lastmod": "2026-08-18",
            "sitemap": "https://example.com/recipes.xml",
        }
    ]
    assert diagnostics == {"attempted": 2, "errors": [], "succeeded": 2}


def test_http_routes_all_sources_through_safe_bounded_transport(monkeypatch):
    calls = []

    def fake_safe_get(session, url, timeout=20, headers=None, *, max_bytes):
        calls.append((url, max_bytes))
        return FakeResponse(text="ok")

    monkeypatch.setattr(http_client, "safe_get", fake_safe_get)
    response = http_client.get_for_source(FakeSession(), "https://example.com/a", SourceConfig("example.com"))
    assert response.text == "ok"
    assert calls == [("https://example.com/a", http_client.DEFAULT_MAX_RESPONSE_BYTES)]


def test_sitemap_decompression_and_entities_fail_closed(monkeypatch):
    compressed = FakeResponse(content=gzip.compress(b"x" * 33))
    with pytest.raises(ValueError, match="decompressed"):
        http_client._xml_bytes(compressed, "https://example.com/sitemap.xml.gz", max_bytes=32)

    entity_xml = b'<!DOCTYPE x [<!ENTITY e "expanded">]><urlset><url><loc>&e;</loc></url></urlset>'
    monkeypatch.setattr(http_client, "get", lambda *args, **kwargs: FakeResponse(content=entity_xml))
    assert list(http_client.iter_sitemap_records(FakeSession(), "https://example.com/sitemap.xml")) == []


def test_discovery_combines_sitemap_and_category_links(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "robots_and_sitemaps",
        lambda session, cfg: (FakeParser(), ["https://example.com/sitemap.xml"], "", "ok"),
    )
    monkeypatch.setattr(
        discovery,
        "iter_sitemap_records",
        lambda session, url, seen=None, max_docs=150, diagnostics=None: iter(
            [
                {"url": "https://example.com/air-fryer-potatoes", "lastmod": "2026-08-18"},
                {"url": "https://other.com/air-fryer-nope", "lastmod": ""},
            ]
        ),
    )
    monkeypatch.setattr(
        discovery,
        "_discovery_get",
        lambda session, url, cfg, timeout=25: FakeResponse(
            text='<html><a href="/recipes/crispy-chicken">Air Fryer Crispy Chicken</a><a href="/about">About</a></html>'
        ),
    )
    state = {"url_catalog": {}}
    cfg = SourceConfig("example.com", discovery_urls=("https://example.com/air-fryer/",))
    result = discovery.discover_source_urls(cfg, state, "daily", "2026-08-18T20:00:00+00:00")
    assert result["status"] == "ok"
    assert result["new_urls"] == 2
    assert "https://example.com/air-fryer-potatoes" in state["url_catalog"]
    assert "https://example.com/recipes/crispy-chicken" in state["url_catalog"]


def test_discovery_reports_sitemap_failure_as_degraded(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "robots_and_sitemaps",
        lambda session, cfg: (FakeParser(), ["https://example.com/broken.xml"], "", "ok"),
    )

    def failed_sitemap(session, sitemap, cfg, seen, max_docs, diagnostics):
        diagnostics["attempted"] += 1
        diagnostics["errors"].append(f"{sitemap}:Timeout")
        return iter(())

    monkeypatch.setattr(discovery, "_sitemap_records", failed_sitemap)
    result = discovery.discover_source_urls(
        SourceConfig("example.com", delay=0),
        {"url_catalog": {}},
        "daily",
        "2026-08-18T20:00:00+00:00",
    )
    assert result["status"] == "degraded"
    assert result["discovery_errors"] == ["https://example.com/broken.xml:Timeout"]


def test_crawler_reuses_304_and_detects_structural_changes(monkeypatch):
    monkeypatch.setattr(crawler, "make_session", FakeSession)
    monkeypatch.setattr(crawler, "robots_and_sitemaps", lambda session, cfg: (FakeParser(), [], "", "ok"))
    existing = asdict(sample_row())
    existing["last_seen_at"] = "2026-08-18T19:00:00+00:00"
    state = {
        "recipes": {"a": existing},
        "url_catalog": {
            existing["url"]: {
                "url": existing["url"],
                "source": "example.com",
                "recipe_id": "a",
                "etag": "abc",
            },
            "https://example.com/air-fryer-new": {
                "url": "https://example.com/air-fryer-new",
                "source": "example.com",
                "page_hash": "old-page",
                "dom_fingerprint": "old-dom",
                "dom_fingerprint_version": 2,
                "schema_signature": "old-schema",
                "schema_signature_version": 2,
            },
        },
    }

    def fake_get(session, url, cfg, timeout=25, headers=None, **kwargs):
        if url == existing["url"]:
            assert headers == {"If-None-Match": "abc"}
            return FakeResponse(status_code=304)
        return FakeResponse(status_code=200, text="<html>new</html>", headers={"ETag": "new"})

    monkeypatch.setattr(crawler, "get_for_source", fake_get)
    new_row = sample_row("b", "https://example.com/air-fryer-new")
    monkeypatch.setattr(
        crawler,
        "extract_recipe_from_html",
        lambda html, url, domain, cfg, headers: (
            new_row,
            {
                "page_hash": "new-page",
                "dom_fingerprint": "new-dom",
                "dom_fingerprint_version": 2,
                "schema_signature": "new-schema",
                "schema_signature_version": 2,
                "recipe_recognized": True,
                "issues": [],
            },
        ),
    )
    targets = list(state["url_catalog"].values())
    rows, coverage, events = crawler.crawl_targets(
        targets, [SourceConfig("example.com", delay=0)], state, "2026-08-18T20:00:00+00:00"
    )
    assert len(rows) == 2
    assert coverage[0]["not_modified"] == 1
    assert coverage[0]["recognized_recipes"] == 2
    assert coverage[0]["dom_structure_changes"] == 1
    assert coverage[0]["schema_structure_changes"] == 1
    assert {event["type"] for event in events} >= {"dom_structure_changed", "schema_structure_changed"}


def test_structure_versions_rebaseline_legacy_hashes_and_detect_removal():
    legacy = {"dom_fingerprint": "old", "schema_signature": "old"}
    current = {
        "dom_fingerprint": "new",
        "dom_fingerprint_version": 2,
        "schema_signature": "",
        "schema_signature_version": 2,
    }
    assert not crawler._versioned_structure_changed(legacy, current, "dom_fingerprint")
    assert not crawler._versioned_structure_changed(legacy, current, "schema_signature")

    versioned = {
        "dom_fingerprint": "old",
        "dom_fingerprint_version": 2,
        "schema_signature": "old",
        "schema_signature_version": 2,
    }
    assert crawler._versioned_structure_changed(versioned, current, "dom_fingerprint")
    assert crawler._versioned_structure_changed(versioned, current, "schema_signature")


def test_crawler_records_robots_denial(monkeypatch):
    monkeypatch.setattr(crawler, "make_session", FakeSession)
    monkeypatch.setattr(crawler, "robots_and_sitemaps", lambda session, cfg: (FakeParser(False), [], "", "ok"))
    state = {"recipes": {}, "url_catalog": {}}
    target = {"url": "https://example.com/air-fryer-x", "source": "example.com"}
    rows, coverage, events = crawler.crawl_targets(
        [target], [SourceConfig("example.com", delay=0)], state, "2026-08-18T20:00:00+00:00"
    )
    assert rows == []
    assert coverage[0]["verified_recipes"] == 0
    assert events[0]["type"] == "robots_denied"


def test_evidence_jsonld_graph_and_visible_microdata():
    from bs4 import BeautifulSoup

    html = """<html><body>
    <span itemprop="ratingValue" content="4.7"></span><span itemprop="ratingCount">123</span>
    <script type="application/ld+json">{"@graph":[{"@type":"Recipe","name":"A"}]}</script>
    </body></html>"""
    soup = BeautifulSoup(html, "lxml")
    objects = list(jsonld_objects(soup))
    assert any(obj.get("@type") == "Recipe" for obj in objects)
    rating, count = visible_rating_evidence(soup, SourceConfig("example.com"))
    assert rating == 4.7
    assert count == 123


def test_media_hash_and_ambiguous_enrichment(monkeypatch):
    image = Image.new("RGB", (16, 16), "white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    fingerprint = media.perceptual_hash_bytes(buffer.getvalue())
    assert len(fingerprint) == 16

    left = sample_row("left", "https://one.com/a")
    right = sample_row("right", "https://two.com/b")
    left.source = "one.com"
    right.source = "two.com"
    monkeypatch.setattr(
        media,
        "candidate_duplicate_pairs",
        lambda recipes, low=0.72, high=0.90, limit=100: [(asdict(left), asdict(right), 0.82)],
    )
    monkeypatch.setattr(media, "fetch_perceptual_hash", lambda url: "f" * 16)
    state = {"recipes": {}}
    stats = media.enrich_ambiguous_perceptual_hashes([left, right], state, max_fetches=2)
    assert stats["image_hash_fetches"] == 2
    assert left.image_perceptual_hash == "f" * 16
    assert right.image_perceptual_hash == "f" * 16


def test_media_fetch_uses_the_bounded_safe_transport(monkeypatch):
    image = Image.new("RGB", (16, 16), "white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    calls = []

    def fake_get(session, url, timeout, headers, *, max_bytes):
        calls.append((url, max_bytes, headers))
        return FakeResponse(content=buffer.getvalue())

    monkeypatch.setattr(media, "make_session", FakeSession)
    monkeypatch.setattr(media, "get", fake_get)
    assert media.fetch_perceptual_hash("https://example.com/image.png", max_bytes=1234)
    assert calls == [("https://example.com/image.png", 1234, {"Accept": "image/*"})]


def test_observability_and_quality_gate_emit_actionable_failures():
    state = {
        "recipes": {"a": {}},
        "migration": {"legacy_evidence_pending": 1},
        "source_history": [
            {
                "run_at": "2026-08-18T19:00:00+00:00",
                "coverage": [{"source": "x.com", "status": "ok"}],
            }
        ],
    }
    coverage = [
        {
            "source": "x.com",
            "targets": 2,
            "fetched": 2,
            "not_modified": 0,
            "recognized_recipes": 2,
            "verified_recipes": 1,
            "errors": 1,
            "elapsed_seconds": 4.0,
            "dom_structure_changes": 1,
            "schema_structure_changes": 0,
        }
    ]
    health = [
        {
            "source": "x.com",
            "healthy_at_last_check": True,
            "checked_within_24h": True,
            "checked_within_7d": True,
        }
    ]
    metrics, rows, alerts = build_pipeline_metrics(
        state,
        coverage,
        [],
        [{"recipe_id": "a"}],
        [],
        health,
        [{"type": "fetch_error", "status": 429}],
        [{"url": "a"}, {"url": "b"}],
        "2026-08-18T20:00:00+00:00",
    )
    assert metrics["crawl_success_rate"] == 1.0
    assert metrics["fetch_success_rate"] == 1.0
    assert metrics["extract_success_rate"] == 1.0
    assert metrics["recipe_verification_rate"] == 0.5
    assert metrics["http_429"] == 1
    assert rows
    assert any(alert["type"] == "publisher_dom_contract_changes" for alert in alerts)

    gate = evaluate_publish_gate(
        {"ranked_recipes": 100, "model_version": 5, "deduplicated_count": 0},
        [{"recipe_id": f"old-{index}"} for index in range(50)],
        [{"recipe_id": f"new-{index}", "rank": index + 1} for index in range(50)],
        {"evidence_conflict_rate": 0.25, "legacy_evidence_pending": 1, "http_429": 20},
        mode="hourly",
        model_version=5,
        deduplicated_count=30,
    )
    assert gate["passed"] is False
    assert len(gate["failures"]) >= 3
    assert gate["warnings"]


def test_qa_detects_structural_event_and_source_freshness():
    state = {
        "recipes": {},
        "anomaly_history": [],
        "source_history": [{"run_at": "2026-08-18T19:00:00+00:00", "coverage": [{"source": "x.com", "status": "ok"}]}],
    }
    anomalies = detect_anomalies(
        state,
        [],
        [{"source": "x.com", "status": "ok"}],
        [{"type": "schema_structure_changed", "source": "x.com", "url": "https://x.com/a"}],
        "2026-08-18T20:00:00+00:00",
    )
    assert anomalies[0]["type"] == "schema_structure_changed"
    health, summary = source_health_summary(
        state,
        [{"source": "x.com", "status": "not_checked_this_run"}],
        [SourceConfig("x.com")],
        "2026-08-18T20:00:00+00:00",
    )
    assert health[0]["healthy_at_last_check"] is True
    assert health[0]["checked_this_run"] is False
    assert summary["sources_checked_within_24h"] == 1
