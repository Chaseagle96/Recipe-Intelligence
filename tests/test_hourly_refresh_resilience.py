from airfryer_rankings.crawler import select_refresh_targets
from airfryer_rankings.extract import extract_recipe_from_html
from airfryer_rankings.models import SourceConfig
from airfryer_rankings.observability import build_pipeline_metrics


def test_hourly_selector_balances_validated_urls_and_caps_exploration():
    state = {"recipes": {}, "url_catalog": {}}
    sources = [SourceConfig(f"source{i}.com") for i in range(12)]

    for source_index in range(12):
        for index in range(20):
            url = f"https://source{source_index}.com/recipe-{index}"
            recipe_id = f"recipe-{source_index}-{index}"
            state["recipes"][recipe_id] = {"recipe_id": recipe_id}
            state["url_catalog"][url] = {
                "url": url,
                "source": f"source{source_index}.com",
                "recipe_id": recipe_id,
                "last_checked": "2026-08-18T18:00:00+00:00",
            }

    for index in range(80):
        url = f"https://source0.com/new-{index}"
        state["url_catalog"][url] = {
            "url": url,
            "source": "source0.com",
            "first_discovered": "2026-08-19T18:00:00+00:00",
        }

    targets = select_refresh_targets(state, sources, "hourly", hourly_limit=100)
    exploratory = [target for target in targets if not target.get("recipe_id") and not target.get("recipe_recognized")]
    counts = {}
    for target in targets:
        counts[target["source"]] = counts.get(target["source"], 0) + 1

    assert len(targets) == 92
    assert len(exploratory) == 2
    assert all(target["source"] == "source0.com" for target in exploratory)
    assert len(counts) >= 10


def test_hourly_unvalidated_catalog_cannot_fill_production_budget():
    state = {"recipes": {}, "url_catalog": {}}
    for index in range(100):
        url = f"https://newsource.com/candidate-{index}"
        state["url_catalog"][url] = {
            "url": url,
            "source": "newsource.com",
            "first_discovered": "2026-08-19T18:00:00+00:00",
        }

    targets = select_refresh_targets(state, [SourceConfig("newsource.com")], "hourly", hourly_limit=100)

    assert len(targets) == 2
    assert all(not target.get("recipe_id") for target in targets)


def test_recent_unverified_urls_are_deprioritized():
    state = {
        "recipes": {},
        "url_catalog": {
            "https://x.com/unverified": {
                "url": "https://x.com/unverified",
                "source": "x.com",
                "last_checked": "2026-08-19T18:00:00+00:00",
                "last_status": "no_verified_rating",
                "first_discovered": "2026-08-19T18:00:00+00:00",
            },
            "https://x.com/healthy": {
                "url": "https://x.com/healthy",
                "source": "x.com",
                "last_checked": "2026-08-18T18:00:00+00:00",
            },
        },
    }

    targets = select_refresh_targets(state, [SourceConfig("x.com")], "hourly", hourly_limit=1)
    assert targets[0]["url"] == "https://x.com/healthy"


def test_ratingless_jsonld_recipe_is_structurally_recognized():
    html = """
    <html><head>
      <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": "Slow Cooker Soup",
        "recipeIngredient": ["1 onion", "2 cups broth"],
        "recipeInstructions": ["Combine ingredients", "Cook until tender"]
      }
      </script>
    </head><body></body></html>
    """
    row, metadata = extract_recipe_from_html(
        html,
        "https://example.com/slow-cooker-soup",
        "example.com",
    )

    assert row is None
    assert metadata["recipe_recognized"] is True


def test_crawl_success_and_extraction_are_independent_of_rating_verification():
    coverage = [
        {
            "source": "example.com",
            "targets": 100,
            "fetched": 97,
            "not_modified": 3,
            "recognized_recipes": 90,
            "verified_recipes": 38,
            "errors": 0,
            "elapsed_seconds": 50.0,
            "dom_structure_changes": 0,
            "schema_structure_changes": 0,
        }
    ]
    metrics, _, _ = build_pipeline_metrics(
        state={"recipes": {}, "source_history": [], "migration": {}},
        coverage=coverage,
        rows=[],
        ranked=[],
        anomalies=[],
        source_health=[],
        crawl_events=[],
        targets=[{"url": f"https://example.com/{index}"} for index in range(100)],
        run_at="2026-08-19T19:00:00+00:00",
    )

    assert metrics["crawl_success_rate"] == 1.0
    assert metrics["fetch_success_rate"] == 1.0
    assert metrics["extract_success_rate"] == 0.90
    assert metrics["recipe_verification_rate"] == 0.38
    assert metrics["recognized_recipe_responses"] == 90
