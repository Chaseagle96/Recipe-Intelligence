from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from airfryer_rankings import source_expansion as expansion
from airfryer_rankings.models import SourceConfig
from airfryer_rankings.source_registry import (
    ACTIVE,
    CANDIDATE,
    PROMOTED,
    empty_source_registry,
    record_candidate_discovery,
    transition_source,
)
from airfryer_rankings.source_security import UnsafeNetworkTarget

RUN_AT = "2026-08-24T12:00:00+00:00"


class _Response:
    def __init__(
        self,
        *,
        payload: dict[str, Any] | None = None,
        text: str = "",
        url: str = "https://candidate.example/air-fryer-chicken",
        status_code: int = 200,
    ) -> None:
        self._payload = payload or {}
        self.text = text
        self.url = url
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _Parser:
    def can_fetch(self, _user_agent: str, url: str) -> bool:
        if "check-error" in url:
            raise RuntimeError("robots parser failed")
        return "denied" not in url


def _context(
    *,
    slug: str = "air_fryer",
    registry: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    base_sources: list[SourceConfig] | None = None,
) -> expansion.VerticalContext:
    return expansion.VerticalContext(
        slug=slug,
        name=slug.replace("_", " ").title(),
        base_sources_path=Path("config/sources.yaml"),
        state_path=Path("data/state.json"),
        registry_path=Path("data/source_registry.json"),
        output_dir=Path("output"),
        events_dir=Path("data/source_events"),
        include_pattern=r"air[-_ ]?fry(?:er|ing|ed)",
        allow_unmatched_discovery_links=False,
        query_terms=["air fryer recipes", "quick air fryer"],
        proteins=["chicken"],
        cuisines=[],
        meal_types=["dinner"],
        categories=[],
        ingredients=[],
        base_sources=(
            base_sources
            if base_sources is not None
            else [SourceConfig("trusted.example", discovery_urls=("https://trusted.example/category",))]
        ),
        state=state if state is not None else {"recipes": {}, "url_catalog": {}, "source_history": []},
        registry=registry if registry is not None else empty_source_registry(slug),
    )


def test_query_family_and_search_provider_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context()
    assert expansion.build_query_family(context, 8) == [
        "best air fryer recipes",
        "air fryer recipes",
        "best quick air fryer",
        "quick air fryer",
        "air fryer recipes chicken recipe",
        "quick air fryer chicken recipe",
        "air fryer recipes dinner recipe",
        "quick air fryer dinner recipe",
    ]
    assert (
        expansion._providers({"providers": {"brave_search": {"enabled": False}, "google_cse": {"enabled": False}}})
        == []
    )

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_get(url: str, *, params: dict[str, Any], **_kwargs: Any) -> _Response:
        calls.append((url, dict(params)))
        if "brave" in url:
            results = [
                {"url": f"https://brave-{index}.example/recipe", "title": f"Brave {index}"} for index in range(20)
            ]
            results.append({"title": "missing URL"})
            return _Response(payload={"web": {"results": results}})
        start = int(params["start"])
        count = int(params["num"])
        return _Response(
            payload={
                "items": [
                    {"link": f"https://google-{index}.example/recipe", "title": f"Google {index}"}
                    for index in range(start, start + count)
                ]
            }
        )

    monkeypatch.setattr(expansion.requests, "get", fake_get)

    brave = expansion.BraveSearchProvider("secret")
    brave_hits = brave.search("air fryer", 25)
    assert len(brave_hits) == 20
    assert brave_hits[0] == expansion.DiscoveryHit(
        url="https://brave-0.example/recipe",
        provider="brave_search",
        query="air fryer",
        title="Brave 0",
    )
    assert calls[0][1] == {"q": "air fryer", "count": 20, "safesearch": "moderate"}

    google = expansion.GoogleCustomSearchProvider("secret", "search-engine")
    google_hits = google.search("slow cooker", 12)
    assert len(google_hits) == 12
    assert [params["start"] for url, params in calls if "googleapis" in url] == [1, 11]
    assert google_hits[-1].url == "https://google-12.example/recipe"

    calls_before_unavailable = len(calls)
    assert expansion.BraveSearchProvider(" ").search("unused", 5) == []
    assert expansion.GoogleCustomSearchProvider(" ", " ").search("unused", 5) == []
    assert len(calls) == calls_before_unavailable


def test_search_discovery_records_only_bounded_publishers() -> None:
    context = _context()

    class UnavailableProvider:
        name = "unavailable"

        @staticmethod
        def available() -> bool:
            return False

    class ErrorProvider:
        name = "broken"

        @staticmethod
        def available() -> bool:
            return True

        @staticmethod
        def search(_query: str, _limit: int) -> list[expansion.DiscoveryHit]:
            raise RuntimeError("provider down")

    class WorkingProvider:
        name = "working"

        @staticmethod
        def available() -> bool:
            return True

        @staticmethod
        def search(query: str, _limit: int) -> list[expansion.DiscoveryHit]:
            return [
                expansion.DiscoveryHit("https://trusted.example/air-fryer", "working", query),
                expansion.DiscoveryHit("https://amazon.com/air-fryer", "working", query),
                expansion.DiscoveryHit("https://alpha.example/air-fryer", "working", query),
                expansion.DiscoveryHit("https://alpha.example/air-fryer-two", "working", query),
                expansion.DiscoveryHit("https://beta.example/air-fryer", "working", query),
            ]

    expansion._discover_search_candidates(
        context,
        {},
        {"max_search_queries_per_run": 1, "max_candidate_domains_per_run": 2, "search_results_per_query": 10},
        [UnavailableProvider(), ErrorProvider(), WorkingProvider()],
        RUN_AT,
    )

    assert context.new_candidate_domains == {"alpha.example", "beta.example"}
    assert set(context.registry["candidates"]) == {"alpha.example", "beta.example"}
    assert context.registry["candidates"]["alpha.example"]["discovery_count"] == 2
    assert context.provider_status == [
        {"provider": "unavailable", "status": "unavailable_missing_configuration"},
        {"provider": "broken", "status": "error:RuntimeError", "detail": "provider down"},
        {"provider": "working", "status": "ok", "hits": 5},
    ]


def test_outbound_discovery_uses_stable_pages_and_filters_links(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context(
        state={
            "recipes": {},
            "source_history": [],
            "url_catalog": {
                "existing": {
                    "source": "trusted.example",
                    "url": "https://trusted.example/air-fryer-existing",
                },
                "invalid": "not a catalog row",
            },
        }
    )
    assert expansion._discovery_pages_for_outbound(context, 2) == [
        (context.base_sources[0], "https://trusted.example/category"),
        (context.base_sources[0], "https://trusted.example/air-fryer-existing"),
    ]

    html = """
    <a href="https://fresh.example/air-fryer-chicken">Air fryer chicken</a>
    <a href="https://trusted.example/air-fryer-own">Air fryer own</a>
    <a href="https://amazon.com/air-fryer">Air fryer store</a>
    <a href="https://blocked.example/air-fryer">Air fryer blocked</a>
    <a href="https://irrelevant.example/about">About</a>
    """
    monkeypatch.setattr(expansion, "make_session", object)
    monkeypatch.setattr(expansion, "get", lambda _session, url, _timeout: _Response(text=html, url=url))

    expansion._discover_outbound_candidates(
        context,
        {"blocked_domain_suffixes": ["blocked.example"]},
        {"max_outbound_pages_per_run": 2, "max_candidate_domains_per_run": 3},
        RUN_AT,
    )

    assert context.new_candidate_domains == {"fresh.example"}
    assert set(context.registry["candidates"]) == {"fresh.example"}
    assert context.registry["candidates"]["fresh.example"]["discovery_count"] == 2
    assert context.provider_status[-1] == {
        "provider": "trusted_outbound_link",
        "status": "ok",
        "pages_fetched": 2,
        "hits": 2,
    }


def test_cross_seed_preserves_evidence_and_filters_target_sources() -> None:
    target = _context()
    peer_registry = empty_source_registry("slow_cooker")
    for domain, url in (
        ("fresh.example", "https://fresh.example/air-fryer-stew"),
        ("trusted.example", "https://trusted.example/air-fryer"),
        ("blocked.example", "https://blocked.example/air-fryer"),
    ):
        record_candidate_discovery(
            peer_registry,
            domain=domain,
            provider="peer",
            query="slow cooker recipes",
            discovery_url=url,
            timestamp=RUN_AT,
        )
    peer = _context(slug="slow_cooker", registry=peer_registry, base_sources=[SourceConfig("peer.example")])

    expansion._cross_seed([target, peer], {"blocked_domain_suffixes": ["blocked.example"]}, RUN_AT)

    assert set(target.registry["candidates"]) == {"fresh.example"}
    evidence = target.registry["candidates"]["fresh.example"]["discovery_evidence"]
    assert evidence == [
        {
            "provider": "cross_vertical",
            "query": "candidate observed in slow_cooker",
            "url": "https://fresh.example/air-fryer-stew",
            "first_seen_at": RUN_AT,
        }
    ]


def test_candidate_url_acquisition_is_safe_filtered_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context()
    record = {
        "domain": "candidate.example",
        "pinned": False,
        "discovery_evidence": [
            {"url": "https://candidate.example/air-fryer-evidence"},
            {"url": "https://sub.candidate.example/air-fryer-subdomain"},
            {"url": "https://other.example/air-fryer-off-domain"},
        ],
    }
    parser = _Parser()
    sitemaps = ["https://candidate.example/sitemap.xml"]
    monkeypatch.setattr(expansion, "make_session", object)
    monkeypatch.setattr(
        expansion,
        "robots_and_sitemaps",
        lambda _session, _cfg: (parser, sitemaps, "", "ok"),
    )
    monkeypatch.setattr(
        expansion,
        "iter_sitemap_records",
        lambda *_args, **_kwargs: iter(
            [
                {"url": "https://candidate.example/air-fryer-sitemap"},
                {"url": "https://candidate.example/air-fryer-denied"},
                {"url": "https://candidate.example/air-fryer-check-error"},
                {"url": "https://other.example/air-fryer-off-domain"},
                {"url": "https://candidate.example/about"},
                {"url": ""},
            ]
        ),
    )
    fetched: list[str] = []

    def fake_safe_get(_session: object, url: str, _timeout: int) -> _Response:
        fetched.append(url)
        return _Response(
            url=url,
            text=(
                '<a href="/air-fryer-linked">Air fryer linked</a>'
                '<a href="https://other.example/air-fryer-off-domain">Air fryer external</a>'
                '<a href="/about">About</a>'
            ),
        )

    monkeypatch.setattr(expansion, "safe_get", fake_safe_get)
    budget = {
        "auto_source_max_urls": 25,
        "auto_source_delay": 0,
        "candidate_url_scan_cap": 10,
        "max_candidate_sitemap_docs": 4,
        "max_candidate_pages_per_domain": 8,
    }

    cfg, returned_parser, urls, status, returned_sitemaps = expansion._candidate_urls(context, record, budget)

    assert cfg.origin == "discovered"
    assert cfg.discovery_urls == (
        "https://candidate.example/air-fryer-evidence",
        "https://sub.candidate.example/air-fryer-subdomain",
    )
    assert returned_parser is parser
    assert status == "ok"
    assert returned_sitemaps == sitemaps
    assert urls == [
        "https://candidate.example/air-fryer-evidence",
        "https://sub.candidate.example/air-fryer-subdomain",
        "https://candidate.example/air-fryer-sitemap",
        "https://candidate.example/air-fryer-linked",
        "https://sub.candidate.example/air-fryer-linked",
    ]
    assert fetched == [
        "https://candidate.example/air-fryer-evidence",
        "https://sub.candidate.example/air-fryer-subdomain",
        "https://candidate.example/",
    ]

    monkeypatch.setattr(
        expansion,
        "robots_and_sitemaps",
        lambda _session, _cfg: (parser, sitemaps, "", "unreachable:http_503"),
    )
    _, _, unavailable_urls, unavailable_status, unavailable_sitemaps = expansion._candidate_urls(
        context, record, budget
    )
    assert unavailable_urls == []
    assert unavailable_status == "unreachable:http_503"
    assert unavailable_sitemaps == sitemaps


def test_novelty_ratio_handles_empty_duplicate_and_unrelated_recipes() -> None:
    duplicate = expansion.SampledPage(
        url="https://candidate.example/duplicate",
        recipe_payload={
            "title": "Air Fryer Chicken",
            "canonical_url": "https://trusted.example/air-fryer-chicken",
            "ingredients": ["chicken", "salt"],
        },
    )
    novel = expansion.SampledPage(
        url="https://candidate.example/novel",
        recipe_payload={
            "title": "Crispy Tofu Bites",
            "canonical_url": "https://candidate.example/crispy-tofu",
            "ingredients": ["tofu", "soy sauce"],
        },
    )
    empty = expansion.SampledPage(url="https://candidate.example/not-a-recipe")
    existing = [
        {
            "title": "Air Fryer Chicken",
            "canonical_url": "https://trusted.example/air-fryer-chicken",
            "ingredients": ["chicken", "salt"],
        }
    ]

    assert expansion._novelty_ratio([empty], existing) == 0.0
    assert expansion._novelty_ratio([duplicate, novel], []) == 1.0
    assert expansion._novelty_ratio([duplicate, novel, empty], existing) == 0.5


def test_qualification_classifies_transport_failures_and_samples_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context()
    record = {"domain": "candidate.example"}
    budget = {"max_candidate_pages_per_domain": 3, "candidate_page_delay": 0}

    def unsafe_urls(*_args: Any, **_kwargs: Any) -> Any:
        raise UnsafeNetworkTarget("private address")

    monkeypatch.setattr(expansion, "_candidate_urls", unsafe_urls)
    metrics, crawl_config = expansion.qualify_candidate(context, record, {}, budget, RUN_AT)
    assert metrics["hard_failures"] == ["unsafe_network_target"]
    assert metrics["error"] == "private address"
    assert crawl_config == {}

    cfg = SourceConfig("candidate.example", origin="discovered", pinned=False)
    parser = _Parser()
    monkeypatch.setattr(
        expansion,
        "_candidate_urls",
        lambda *_args: (cfg, parser, [], "unreachable:ConnectionError", ["https://candidate.example/sitemap.xml"]),
    )
    metrics, crawl_config = expansion.qualify_candidate(context, record, {}, budget, RUN_AT)
    assert metrics["temporary_failures"] == ["robots_unavailable"]
    assert metrics["robots_status"] == "unreachable:ConnectionError"
    assert crawl_config == {}

    urls = [
        "https://candidate.example/air-fryer-denied",
        "https://candidate.example/air-fryer-check-error",
        "https://candidate.example/air-fryer-chicken",
    ]
    monkeypatch.setattr(
        expansion,
        "_candidate_urls",
        lambda *_args: (cfg, parser, urls, "ok", ["https://candidate.example/sitemap.xml"]),
    )
    recipe_html = """
    <script type="application/ld+json">
    {
      "@type": "Recipe",
      "name": "Air Fryer Chicken",
      "recipeIngredient": ["1 pound chicken", "salt"],
      "recipeInstructions": [{"@type": "HowToStep", "text": "Air fry until crisp."}],
      "author": {"@type": "Person", "name": "Test Cook"},
      "aggregateRating": {"ratingValue": "4.8", "ratingCount": "100", "bestRating": "5"}
    }
    </script>
    """
    fetched: list[str] = []

    def fake_safe_get(_session: object, url: str, _timeout: int) -> _Response:
        fetched.append(url)
        return _Response(text=recipe_html, url=url)

    monkeypatch.setattr(expansion, "make_session", object)
    monkeypatch.setattr(expansion, "safe_get", fake_safe_get)
    policy = {"quality_gate": {"weights": {"vertical_relevance": 1.0}}}
    metrics, crawl_config = expansion.qualify_candidate(context, record, policy, budget, RUN_AT)

    assert fetched == ["https://candidate.example/air-fryer-chicken"]
    assert metrics["pages_sampled"] == 3
    assert metrics["pages_fetched"] == 1
    assert metrics["recipes_recognized"] == 1
    assert metrics["qualifying_vertical_recipe_count"] == 3
    assert crawl_config == {
        "max_urls": 250,
        "delay": 0.25,
        "sitemap_urls": ["https://candidate.example/sitemap.xml"],
        "discovery_urls": urls,
        "include_pattern": context.include_pattern,
        "allow_unmatched_discovery_links": False,
    }


def test_lifecycle_activates_promotions_and_warns_for_pinned_failures() -> None:
    registry = empty_source_registry("air_fryer")
    promoted, _ = record_candidate_discovery(
        registry,
        domain="promoted.example",
        provider="unit",
        query="air fryer",
        discovery_url="https://promoted.example/air-fryer",
        timestamp=RUN_AT,
    )
    candidate, _ = record_candidate_discovery(
        registry,
        domain="candidate.example",
        provider="unit",
        query="air fryer",
        discovery_url="https://candidate.example/air-fryer",
        timestamp=RUN_AT,
    )
    assert promoted is not None and candidate is not None
    transition_source(registry, "promoted.example", PROMOTED, "qualified", timestamp=RUN_AT)
    context = _context(
        registry=registry,
        state={
            "recipes": {},
            "url_catalog": {},
            "source_history": [{"coverage": [{"source": "promoted.example", "status": "ok"}]}],
        },
    )
    policy = {
        "lifecycle": {
            "degrade_after_consecutive_failures": 2,
            "suspend_after_consecutive_failures": 3,
            "recover_after_consecutive_successes": 2,
        }
    }

    expansion.update_promoted_source_lifecycle(context, policy, RUN_AT)

    assert promoted["status"] == ACTIVE
    assert candidate["status"] == CANDIDATE
    assert any(event["event"] == "SOURCE_ACTIVE" for event in registry["audit"])

    pinned_registry = empty_source_registry("air_fryer")
    pinned, _ = record_candidate_discovery(
        pinned_registry,
        domain="pinned.example",
        provider="unit",
        query="air fryer",
        discovery_url="https://pinned.example/air-fryer",
        timestamp=RUN_AT,
    )
    assert pinned is not None
    pinned["pinned"] = True
    transition_source(pinned_registry, "pinned.example", ACTIVE, "manual pin", timestamp=RUN_AT)
    pinned_context = _context(
        registry=pinned_registry,
        state={
            "recipes": {},
            "url_catalog": {},
            "source_history": [
                {"coverage": [{"source": "pinned.example", "status": "degraded"}]},
                {"coverage": [{"source": "pinned.example", "status": "timeout"}]},
            ],
        },
    )

    expansion.update_promoted_source_lifecycle(pinned_context, policy, RUN_AT)

    assert pinned["status"] == ACTIVE
    assert pinned_context.counters["pinned_source_warnings"] == 1.0


def test_evaluation_queue_applies_status_priority_cooldown_and_mode() -> None:
    records = {
        "suspended.example": {"domain": "suspended.example", "status": expansion.SUSPENDED},
        "qualified.example": {"domain": "qualified.example", "status": expansion.QUALIFIED},
        "quarantined.example": {"domain": "quarantined.example", "status": expansion.QUARANTINED},
        "candidate.example": {
            "domain": "candidate.example",
            "status": expansion.CANDIDATE,
            "discovery_count": 9,
        },
        "rejected-ready.example": {
            "domain": "rejected-ready.example",
            "status": expansion.REJECTED,
            "rejection_cooldown_until": "2026-08-23T00:00:00+00:00",
        },
        "rejected-cooling.example": {
            "domain": "rejected-cooling.example",
            "status": expansion.REJECTED,
            "rejection_cooldown_until": "2026-08-25T00:00:00+00:00",
        },
        "blocked.example": {
            "domain": "blocked.example",
            "status": expansion.CANDIDATE,
            "rediscovery_blocked": True,
        },
        "active.example": {"domain": "active.example", "status": expansion.ACTIVE},
    }
    context = _context(registry={"candidates": records, "audit": []})
    budget = {"max_candidate_domains_evaluated": 3}

    daily = expansion._evaluation_queue(context, {}, budget, "daily", RUN_AT)
    deep = expansion._evaluation_queue(context, {}, budget, "deep", RUN_AT)

    assert [row["domain"] for row in daily] == [
        "qualified.example",
        "quarantined.example",
        "candidate.example",
    ]
    assert [row["domain"] for row in deep] == [
        "suspended.example",
        "qualified.example",
        "quarantined.example",
    ]
