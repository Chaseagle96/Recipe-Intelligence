from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from airfryer_rankings.dashboard import _dashboard_html
from airfryer_rankings.discovery import _compile_include_pattern, _looks_recipe_link
from airfryer_rankings.models import SourceConfig, load_sources
from airfryer_rankings.runtime import vertical_name, vertical_output_path, vertical_slug


def test_slow_cooker_registry_has_independent_discovery_pattern() -> None:
    sources = load_sources("config/verticals/slow_cooker/sources.yaml")
    assert len(sources) >= 30
    assert all(not source.allow_unmatched_discovery_links for source in sources)
    pattern = re.compile(sources[0].include_pattern, re.I)
    assert pattern.search("slow-cooker chicken")
    assert pattern.search("Slow Cooker Beef Stew")
    assert pattern.search("crockpot mac and cheese")
    assert pattern.search("crock-pot pulled pork")
    assert not pattern.search("air fryer chicken")


def test_air_fryer_registry_keeps_existing_default_pattern() -> None:
    source = load_sources("config/sources.yaml")[0]
    assert source.allow_unmatched_discovery_links is True
    pattern = re.compile(source.include_pattern, re.I)
    assert pattern.search("air fryer chicken")
    assert not pattern.search("slow cooker chicken")


def test_slow_cooker_category_links_fail_closed_when_semantics_do_not_match() -> None:
    source = load_sources("config/verticals/slow_cooker/sources.yaml")[0]
    pattern = _compile_include_pattern(source)
    assert _looks_recipe_link(
        "https://example.com/recipes/slow-cooker-chicken",
        "Chicken Dinner",
        "example.com",
        pattern,
        allow_unmatched=False,
    )
    assert _looks_recipe_link(
        "https://example.com/recipes/chicken-dinner",
        "Slow Cooker Chicken Dinner",
        "example.com",
        pattern,
        allow_unmatched=False,
    )
    assert not _looks_recipe_link(
        "https://example.com/recipes/oven-chicken",
        "Oven Chicken Dinner",
        "example.com",
        pattern,
        allow_unmatched=False,
    )


def test_invalid_vertical_pattern_fails_closed() -> None:
    with pytest.raises(ValueError):
        _compile_include_pattern(SourceConfig("example.com", include_pattern="["))


def test_runtime_namespaces_slow_cooker_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECIPE_INTELLIGENCE_VERTICAL", "Slow Cooker")
    monkeypatch.setenv("RECIPE_INTELLIGENCE_VERTICAL_SLUG", "slow_cooker")
    assert vertical_name() == "Slow Cooker"
    assert vertical_slug() == "slow_cooker"
    assert vertical_output_path(
        "output/air_fryer_analytics.duckdb", "air_fryer_analytics.duckdb", "analytics.duckdb"
    ) == Path("output/slow_cooker_analytics.duckdb")


def test_air_fryer_output_path_is_backward_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECIPE_INTELLIGENCE_VERTICAL", raising=False)
    monkeypatch.delenv("RECIPE_INTELLIGENCE_VERTICAL_SLUG", raising=False)
    assert vertical_name() == "Air Fryer"
    assert vertical_output_path(
        "output/air_fryer_analytics.duckdb", "air_fryer_analytics.duckdb", "analytics.duckdb"
    ) == Path("output/air_fryer_analytics.duckdb")


def test_dashboard_identity_follows_vertical_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECIPE_INTELLIGENCE_VERTICAL", "Slow Cooker")
    html = _dashboard_html()
    assert "Recipe Intelligence — Slow Cooker Rankings" in html
    assert "Slow Cooker vertical" in html
    assert "Air Fryer" not in html


def test_slow_cooker_storage_policy_is_vertical_local() -> None:
    payload = yaml.safe_load(Path("config/verticals/slow_cooker/storage.yaml").read_text(encoding="utf-8"))
    history = payload["history"]
    assert history["raw_observations_path"] == "data/observations"
    assert history["ranking_history_path"] == "data/rankings"
    assert history["analytical_cache"] == "output/slow_cooker_analytics.duckdb"
    assert history["archive_policy"]["object_storage_uri_env"] == "SLOW_COOKER_HISTORY_ARCHIVE_URI"
