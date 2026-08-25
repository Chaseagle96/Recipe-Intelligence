from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from airfryer_rankings.models import SourceConfig, load_sources
from airfryer_rankings.source_expansion import (
    SampledPage,
    VerticalContext,
    _apply_qualification_result,
    analyze_recipe_page,
    hard_gate_failures,
    qualification_metrics,
    run_source_expansion,
    score_source_quality,
    update_promoted_source_lifecycle,
)
from airfryer_rankings.source_registry import (
    ACTIVE,
    BLOCKED,
    CANDIDATE,
    DEGRADED,
    PROMOTED,
    QUARANTINED,
    REJECTED,
    SUSPENDED,
    apply_manual_override,
    effective_source_configs,
    empty_source_registry,
    load_source_registry,
    record_candidate_discovery,
    save_source_registry,
    transition_source,
)
from airfryer_rankings.source_security import (
    UnsafeNetworkTarget,
    candidate_domain_from_url,
    is_non_publisher_domain,
    normalize_candidate_domain,
    validate_public_url,
)


def _policy() -> dict:
    return yaml.safe_load(Path("config/source_discovery.yaml").read_text(encoding="utf-8"))


def _strong_metrics(score: float = 90.0) -> dict:
    return {
        "pages_sampled": 8,
        "pages_fetched": 8,
        "recipes_recognized": 8,
        "recipes_extracted": 7,
        "qualifying_vertical_recipe_count": 30,
        "sample_recipe_yield": 1.0,
        "fetch_success_rate": 1.0,
        "recipe_structure_rate": 1.0,
        "vertical_relevance_ratio": 0.875,
        "extraction_success_rate": 0.875,
        "substantive_recipe_ratio": 1.0,
        "field_completeness": 0.95,
        "editorial_provenance_ratio": 0.875,
        "rating_coverage_ratio": 0.75,
        "rating_conflict_ratio": 0.0,
        "mean_rating_evidence_confidence": 0.85,
        "external_canonical_ratio": 0.0,
        "trap_url_ratio": 0.0,
        "within_source_duplicate_ratio": 0.0,
        "novelty_ratio": 0.85,
        "freshness_score": 90.0,
        "robots_status": "ok",
        "suspicious_uniform_rating_evidence": False,
        "quality_score": score,
        "hard_failures": [],
        "temporary_failures": [],
    }


def _context(registry: dict, state: dict | None = None) -> VerticalContext:
    return VerticalContext(
        slug="air_fryer",
        name="Air Fryer",
        base_sources_path=Path("config/sources.yaml"),
        state_path=Path("data/state.json"),
        registry_path=Path("data/source_registry.json"),
        output_dir=Path("output"),
        events_dir=Path("data/source_events"),
        include_pattern=r"(?:air[-_ ]?fry(?:er|ing|ed)|airfry(?:er|ing|ed))",
        allow_unmatched_discovery_links=False,
        query_terms=["air fryer recipes"],
        proteins=[],
        cuisines=[],
        meal_types=[],
        categories=[],
        ingredients=[],
        base_sources=[SourceConfig(domain="trusted.example")],
        state=state or {"recipes": {}, "url_catalog": {}, "source_history": []},
        registry=registry,
    )


def _public_resolver(host: str, port, type=0):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def _private_resolver(host: str, port, type=0):
    return [(2, 1, 6, "", ("127.0.0.1", 0))]


def test_domain_normalization_preserves_meaningful_subdomains() -> None:
    assert normalize_candidate_domain("https://www.Example.com/path?q=1") == "example.com"
    assert normalize_candidate_domain("recipes.example.com") == "recipes.example.com"
    assert normalize_candidate_domain("127.0.0.1") is None
    assert candidate_domain_from_url("https://www.Example.com/a#fragment") == "example.com"
    assert candidate_domain_from_url("file:///etc/passwd") is None
    assert is_non_publisher_domain("images.example.com")
    assert is_non_publisher_domain("www.youtube.com")
    assert not is_non_publisher_domain("seriouseats.example")


def test_ssrf_validation_rejects_private_networks_and_credentials() -> None:
    assert validate_public_url("https://example.com/recipe", resolver=_public_resolver) == "https://example.com/recipe"
    with pytest.raises(UnsafeNetworkTarget):
        validate_public_url("http://example.com/recipe", resolver=_private_resolver)
    with pytest.raises(UnsafeNetworkTarget):
        validate_public_url("http://user:pass@example.com/recipe", resolver=_public_resolver)
    with pytest.raises(UnsafeNetworkTarget):
        validate_public_url("ftp://example.com/recipe", resolver=_public_resolver)
    with pytest.raises(UnsafeNetworkTarget):
        validate_public_url("https://example.com:8443/recipe", resolver=_public_resolver)


def test_candidate_registry_deduplicates_provenance_and_respects_block() -> None:
    registry = empty_source_registry("air_fryer")
    record, is_new = record_candidate_discovery(
        registry,
        domain="www.newrecipes.example",
        provider="unit",
        query="air fryer chicken recipe",
        discovery_url="https://newrecipes.example/air-fryer-chicken",
        timestamp="2026-08-19T00:00:00+00:00",
        base_domains={"trusted.example"},
    )
    assert is_new
    assert record is not None and record["status"] == CANDIDATE
    _, duplicate_new = record_candidate_discovery(
        registry,
        domain="newrecipes.example",
        provider="unit",
        query="air fryer chicken recipe",
        discovery_url="https://newrecipes.example/air-fryer-chicken",
        timestamp="2026-08-19T01:00:00+00:00",
        base_domains={"trusted.example"},
    )
    assert not duplicate_new
    assert registry["candidates"]["newrecipes.example"]["discovery_count"] == 2
    assert len(registry["candidates"]["newrecipes.example"]["discovery_evidence"]) == 1

    apply_manual_override(registry, "newrecipes.example", "block", reason="maintainer denylist")
    assert registry["candidates"]["newrecipes.example"]["status"] == BLOCKED
    _, rediscovered = record_candidate_discovery(
        registry,
        domain="newrecipes.example",
        provider="other",
        query="air fryer recipes",
        discovery_url="https://newrecipes.example/another",
        timestamp="2026-08-20T00:00:00+00:00",
    )
    assert not rediscovered


def test_recipe_structure_is_recognized_without_rating_evidence() -> None:
    html = """
    <html><head><title>Air Fryer Tofu</title>
    <script type="application/ld+json">
    {
      "@context":"https://schema.org", "@type":"Recipe", "name":"Air Fryer Tofu",
      "author":{"@type":"Person","name":"Test Kitchen"},
      "publisher":{"@type":"Organization","name":"Strong Publisher"},
      "datePublished":"2026-07-01", "recipeYield":"4 servings", "totalTime":"PT25M",
      "recipeIngredient":["1 block tofu","1 tbsp oil","1 tsp paprika"],
      "recipeInstructions":[{"@type":"HowToStep","text":"Season the tofu."},{"@type":"HowToStep","text":"Air fry until crisp."}]
    }
    </script></head><body><a href="/about/editorial-policy">Editorial policy</a></body></html>
    """
    page = analyze_recipe_page(
        html,
        url="https://publisher.example/air-fryer-tofu",
        final_url="https://publisher.example/air-fryer-tofu",
        domain="publisher.example",
        include_pattern=r"air[- ]?fry(?:er|ing)",
    )
    assert page.is_recipe
    assert page.vertical_relevant
    assert page.field_completeness == 1.0
    assert not page.has_rating
    assert not page.ranking_extractable
    assert page.editorial_link


def test_recipe_structure_accepts_microdata_layout() -> None:
    html = """
    <article itemscope itemtype="https://schema.org/Recipe">
      <meta itemprop="name" content="Air Fryer Tofu">
      <span itemprop="recipeIngredient">tofu</span>
      <span itemprop="recipeIngredient">oil</span>
      <span itemprop="recipeIngredient">salt</span>
      <div itemprop="recipeInstructions">Air fry until crisp.</div>
      <div itemprop="aggregateRating" itemscope itemtype="https://schema.org/AggregateRating">
        <meta itemprop="ratingValue" content="4.7">
        <meta itemprop="reviewCount" content="80">
      </div>
    </article>
    """
    page = analyze_recipe_page(
        html,
        url="https://publisher.example/air-fryer-tofu",
        final_url="https://publisher.example/air-fryer-tofu",
        domain="publisher.example",
        include_pattern=r"air[- ]?fry(?:er|ing)",
    )
    assert page.is_recipe
    assert page.vertical_relevant
    assert page.has_rating
    assert page.ranking_extractable


def test_rating_mismatch_is_exposed_as_conflict() -> None:
    html = """
    <html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Recipe","name":"Air Fryer Beans",
     "recipeIngredient":["beans","oil","salt"],
     "recipeInstructions":["Season beans","Air fry beans"],
     "aggregateRating":{"ratingValue":"4.9","ratingCount":"100"}}
    </script></head><body>
      <span itemprop="ratingValue" content="2.0"></span>
      <span itemprop="ratingCount" content="12"></span>
    </body></html>
    """
    page = analyze_recipe_page(
        html,
        url="https://publisher.example/air-fryer-beans",
        final_url="https://publisher.example/air-fryer-beans",
        domain="publisher.example",
        include_pattern=r"air[- ]?fry(?:er|ing)",
    )
    assert page.has_rating
    assert page.evidence_status == "conflict"
    assert page.evidence_confidence == 0.25


def test_quality_score_is_transparent_and_missing_ratings_are_not_a_hard_gate() -> None:
    config = _policy()["quality_gate"]
    metrics = _strong_metrics()
    metrics["rating_coverage_ratio"] = 0.0
    metrics["mean_rating_evidence_confidence"] = None
    score, components = score_source_quality(metrics, config)
    permanent, temporary = hard_gate_failures(metrics, config)
    assert score >= config["qualification_score"]
    assert components["rating_integrity"] == 70.0
    assert permanent == []
    assert temporary == []


def test_fixture_catalog_exercises_hard_gate_classes() -> None:
    fixtures = json.loads(Path("tests/fixtures/source_qualification/cases.json").read_text(encoding="utf-8"))
    policy = _policy()["quality_gate"]
    base = _strong_metrics()
    assert set(fixtures) == {
        "strong_recipe_publisher",
        "mediocre_publisher",
        "recipe_aggregator",
        "duplicate_content_source",
        "malformed_recipe_jsonld",
        "rating_mismatch",
        "non_recipe_website",
        "robots_denial",
        "crawler_trap",
        "temporarily_unavailable",
    }
    for name, fixture in fixtures.items():
        metrics = dict(base)
        metrics.update({key: value for key, value in fixture.items() if key != "expected"})
        permanent, temporary = hard_gate_failures(metrics, policy)
        if fixture["expected"] == "reject":
            assert permanent, name
        elif fixture["expected"] == "quarantine" and name != "mediocre_publisher":
            assert temporary, name


def test_candidate_requires_confirmation_then_joins_effective_allowlist(tmp_path: Path) -> None:
    config = _policy()
    registry = empty_source_registry("air_fryer")
    record, _ = record_candidate_discovery(
        registry,
        domain="quality.example",
        provider="unit",
        query="best air fryer recipes",
        discovery_url="https://quality.example/air-fryer-chicken",
        timestamp="2026-08-19T00:00:00+00:00",
    )
    assert record is not None
    context = _context(registry)
    crawl_config = {
        "include_pattern": context.include_pattern,
        "discovery_urls": ["https://quality.example/air-fryer-chicken"],
    }
    first = _apply_qualification_result(
        context,
        record,
        _strong_metrics(),
        crawl_config,
        config,
        "daily",
        "2026-08-19T01:00:00+00:00",
    )
    assert first == QUARANTINED
    second = _apply_qualification_result(
        context,
        record,
        _strong_metrics(),
        crawl_config,
        config,
        "daily",
        "2026-08-20T01:00:00+00:00",
    )
    assert second == PROMOTED
    assert any(event["event"] == "SOURCE_PROMOTED" for event in registry["audit"])

    registry_path = tmp_path / "registry.json"
    save_source_registry(registry_path, registry)
    persisted = load_source_registry(registry_path, "air_fryer")
    effective = effective_source_configs([SourceConfig(domain="trusted.example")], persisted)
    assert [source.domain for source in effective] == ["trusted.example", "quality.example"]
    assert effective[1].origin == "discovered"


def test_spam_candidate_is_rejected_and_never_production_eligible() -> None:
    config = _policy()
    registry = empty_source_registry("air_fryer")
    record, _ = record_candidate_discovery(
        registry,
        domain="mirror.example",
        provider="unit",
        query="air fryer recipes",
        discovery_url="https://mirror.example/air-fryer",
        timestamp="2026-08-19T00:00:00+00:00",
    )
    assert record is not None
    context = _context(registry)
    bad = _strong_metrics(92.0)
    bad["hard_failures"] = ["external_canonical_or_mirror_behavior"]
    bad["external_canonical_ratio"] = 0.9
    result = _apply_qualification_result(
        context,
        record,
        bad,
        {},
        config,
        "daily",
        "2026-08-19T01:00:00+00:00",
    )
    assert result == REJECTED
    assert [source.domain for source in effective_source_configs(context.base_sources, registry)] == ["trusted.example"]


def test_auto_source_degrades_suspends_and_recovers_with_hysteresis() -> None:
    config = _policy()
    registry = empty_source_registry("air_fryer")
    record, _ = record_candidate_discovery(
        registry,
        domain="auto.example",
        provider="unit",
        query="air fryer recipes",
        discovery_url="https://auto.example/air-fryer",
        timestamp="2026-08-01T00:00:00+00:00",
    )
    assert record is not None
    transition_source(registry, "auto.example", ACTIVE, "test activation", timestamp="2026-08-02T00:00:00+00:00")
    state = {
        "recipes": {},
        "url_catalog": {},
        "source_history": [
            {
                "run_at": f"2026-08-{day:02d}T00:00:00+00:00",
                "coverage": [{"source": "auto.example", "status": "degraded"}],
            }
            for day in range(3, 8)
        ],
    }
    context = _context(registry, state)
    update_promoted_source_lifecycle(context, config, "2026-08-08T00:00:00+00:00")
    assert record["status"] == DEGRADED
    update_promoted_source_lifecycle(context, config, "2026-08-08T01:00:00+00:00")
    assert record["status"] == SUSPENDED

    first = _apply_qualification_result(
        context,
        record,
        _strong_metrics(),
        {},
        config,
        "deep",
        "2026-08-10T00:00:00+00:00",
    )
    second = _apply_qualification_result(
        context,
        record,
        _strong_metrics(),
        {},
        config,
        "deep",
        "2026-08-17T00:00:00+00:00",
    )
    assert first == SUSPENDED
    assert second == ACTIVE
    assert record["status"] == ACTIVE
    assert any(event["event"] == "SOURCE_RECOVERED" for event in registry["audit"])


def test_pinned_source_is_never_automatically_suspended() -> None:
    config = _policy()
    registry = empty_source_registry("air_fryer")
    record, _ = record_candidate_discovery(
        registry,
        domain="pinned.example",
        provider="unit",
        query="air fryer recipes",
        discovery_url="https://pinned.example/air-fryer",
        timestamp="2026-08-01T00:00:00+00:00",
    )
    assert record is not None
    record["pinned"] = True
    transition_source(registry, "pinned.example", ACTIVE, "manual pin test", timestamp="2026-08-02T00:00:00+00:00")
    state = {
        "recipes": {},
        "url_catalog": {},
        "source_history": [
            {
                "run_at": f"2026-08-{day:02d}T00:00:00+00:00",
                "coverage": [{"source": "pinned.example", "status": "degraded"}],
            }
            for day in range(3, 10)
        ],
    }
    context = _context(registry, state)
    update_promoted_source_lifecycle(context, config, "2026-08-10T00:00:00+00:00")
    assert record["status"] == ACTIVE
    assert context.counters["pinned_source_warnings"] == 1.0


def test_manual_yaml_sources_win_over_machine_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path
    config_dir = repo / "config"
    data_dir = repo / "data"
    config_dir.mkdir()
    data_dir.mkdir()
    source_path = config_dir / "sources.yaml"
    source_path.write_text("sources:\n  - domain: pinned.example\n", encoding="utf-8")
    registry = empty_source_registry("air_fryer")
    apply_manual_override(registry, "pinned.example", "block", reason="machine record must not shadow base")
    auto, _ = record_candidate_discovery(
        registry,
        domain="auto.example",
        provider="unit",
        query="air fryer recipes",
        discovery_url="https://auto.example/air-fryer",
        timestamp="2026-08-19T00:00:00+00:00",
    )
    assert auto is not None
    transition_source(registry, "auto.example", PROMOTED, "test", timestamp="2026-08-19T01:00:00+00:00")
    save_source_registry(data_dir / "source_registry.json", registry)
    monkeypatch.delenv("RECIPE_INTELLIGENCE_BASE_SOURCES_ONLY", raising=False)
    sources = load_sources(source_path)
    assert [source.domain for source in sources] == ["pinned.example", "auto.example"]
    assert sources[0].pinned and sources[0].origin == "manual"


def _integration_config(tmp_path: Path) -> Path:
    base_sources = tmp_path / "sources.yaml"
    base_sources.write_text("sources:\n  - domain: pinned.example\n", encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"recipes": {}, "url_catalog": {}, "source_history": [], "schema_version": 4}), encoding="utf-8"
    )
    config = {
        "source_gate_version": 2,
        "aggregate_output_path": str(tmp_path / "aggregate.json"),
        "providers": {"brave_search": {"enabled": False}, "google_cse": {"enabled": False}},
        "budgets": {
            "daily": {
                "max_search_queries_per_run": 0,
                "max_candidate_domains_per_run": 3,
                "max_candidate_domains_evaluated": 3,
                "max_candidate_pages_per_domain": 8,
                "max_total_qualification_pages": 24,
                "max_outbound_pages_per_run": 0,
                "max_new_promotions_per_run": 2,
                "max_discovery_seconds": 30,
            }
        },
        "quality_gate": _policy()["quality_gate"],
        "lifecycle": _policy()["lifecycle"],
        "verticals": {
            "air_fryer": {
                "name": "Air Fryer",
                "root_path": str(tmp_path),
                "base_sources_path": str(base_sources),
                "model_config_path": str(tmp_path / "model.yaml"),
                "storage_config_path": str(tmp_path / "storage.yaml"),
                "state_path": str(state),
                "registry_path": str(tmp_path / "registry.json"),
                "output_dir": str(tmp_path / "output"),
                "events_dir": str(tmp_path / "events"),
                "docs_root": str(tmp_path / "docs"),
                "manifest_path": str(tmp_path / "docs/api/manifest.json"),
                "authority_path": str(tmp_path / "output/authority.json"),
                "public_authority_path": str(tmp_path / "docs/api/authority.json"),
                "summary_path": str(tmp_path / "output/summary.json"),
                "include_pattern": r"air[- ]?fry(?:er|ing)",
                "query_terms": ["air fryer recipes"],
            }
        },
    }
    path = tmp_path / "source_discovery.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_source_expansion_resolves_vertical_paths_from_config_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = Path(__file__).resolve().parents[1] / "config/source_discovery.yaml"
    monkeypatch.chdir(tmp_path)

    result = run_source_expansion(
        config_path,
        mode="smoke",
        dry_run=True,
        run_at="2026-08-24T00:00:00+00:00",
    )

    assert set(result["verticals"]) == {"air_fryer", "slow_cooker"}


def test_source_expansion_deadline_starts_before_discovery_network_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import airfryer_rankings.source_expansion as expansion

    clock = {"now": 0.0}
    calls: list[str] = []

    def search(*args, deadline=None, **kwargs) -> None:
        calls.append("search")
        assert deadline == 30.0
        clock["now"] = deadline

    monkeypatch.setattr(expansion.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(expansion, "_discover_search_candidates", search)
    monkeypatch.setattr(
        expansion,
        "_discover_outbound_candidates",
        lambda *args, **kwargs: calls.append("outbound"),
    )

    run_source_expansion(
        _integration_config(tmp_path),
        mode="daily",
        dry_run=True,
        run_at="2026-08-24T00:00:00+00:00",
    )

    assert calls == ["search"]


def test_mocked_integration_unknown_source_persists_and_promotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import airfryer_rankings.source_expansion as expansion

    config_path = _integration_config(tmp_path)
    seed_path = tmp_path / "seed.yaml"
    seed_path.write_text(
        yaml.safe_dump(
            {
                "candidates": [
                    {
                        "vertical": "air_fryer",
                        "url": "https://quality.example/air-fryer-tofu",
                        "provider": "integration_seed",
                        "query": "best air fryer recipes",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECIPE_INTELLIGENCE_BASE_SOURCES_ONLY", "1")
    monkeypatch.setattr(expansion, "_discover_search_candidates", lambda *args, **kwargs: None)
    monkeypatch.setattr(expansion, "_discover_outbound_candidates", lambda *args, **kwargs: None)
    monkeypatch.setattr(expansion, "_cross_seed", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        expansion,
        "qualify_candidate",
        lambda *args, **kwargs: (_strong_metrics(), {"include_pattern": r"air[- ]?fry(?:er|ing)"}),
    )
    monkeypatch.setattr(
        expansion, "_promoted_catalog_discovery", lambda *args, **kwargs: {"status": "ok", "new_urls": 5}
    )

    first = run_source_expansion(config_path, mode="daily", seed_file=seed_path, run_at="2026-08-19T00:00:00+00:00")
    assert first["verticals"]["air_fryer"]["candidate_domains_quarantined"] >= 1
    first_registry = load_source_registry(tmp_path / "registry.json", "air_fryer")
    assert first_registry["candidates"]["quality.example"]["status"] == QUARANTINED

    second = run_source_expansion(config_path, mode="daily", seed_file=seed_path, run_at="2026-08-20T00:00:00+00:00")
    persisted = load_source_registry(tmp_path / "registry.json", "air_fryer")
    assert persisted["candidates"]["quality.example"]["status"] == PROMOTED
    assert second["verticals"]["air_fryer"]["candidate_domains_promoted"] == 1
    assert second["verticals"]["air_fryer"]["manual_source_count"] == 1
    base = load_sources(tmp_path / "sources.yaml", include_discovered=False)
    assert [source.domain for source in effective_source_configs(base, persisted)] == [
        "pinned.example",
        "quality.example",
    ]
    assert (tmp_path / "output" / "source_expansion.json").exists()
    assert list((tmp_path / "events").rglob("*.ndjson"))


def test_qualification_metrics_measure_novelty_and_recipe_yield() -> None:
    page = SampledPage(
        url="https://new.example/air-fryer-carrots",
        fetched=True,
        final_url="https://new.example/air-fryer-carrots",
        http_status=200,
        is_recipe=True,
        vertical_relevant=True,
        title="Air Fryer Carrots",
        ingredients=("carrots", "oil", "salt"),
        instructions=("Season carrots", "Air fry carrots"),
        author="Author",
        field_completeness=0.9,
        ranking_extractable=False,
        recipe_payload={
            "title": "Air Fryer Carrots",
            "source": "new.example",
            "url": "https://new.example/air-fryer-carrots",
            "canonical_url": "https://new.example/air-fryer-carrots",
            "ingredients": ["carrots", "oil", "salt"],
            "instructions": ["Season carrots", "Air fry carrots"],
            "author": "Author",
        },
    )
    metrics = qualification_metrics(
        [page],
        candidate_url_count=12,
        robots_status="ok",
        run_at="2026-08-19T00:00:00+00:00",
        existing_recipes=[],
    )
    assert metrics["sample_recipe_yield"] == 1.0
    assert metrics["vertical_relevance_ratio"] == 1.0
    assert metrics["novelty_ratio"] == 1.0
