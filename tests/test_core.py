from pathlib import Path

from airfryer_rankings.core import (
    RecipeRow,
    SourceConfig,
    bayesian_rank,
    build_empirical_uncertainty,
    build_historical_metrics,
    categorize_recipe,
    dedupe_current,
    detect_anomalies,
    duplicate_similarity,
    evaluate_dedupe_benchmark,
    extract_recipe_from_html,
    ingredient_signature,
    merge_observations,
    migrate_state,
    read_recent_records,
    select_refresh_targets,
    source_health_summary,
    write_run_records,
)


def recipe(rid, title, source, rating, count, sig="", ingredients=(), status="schema_only", confidence=0.9):
    return RecipeRow(
        rid,
        title,
        source,
        f"https://{source}/{rid}",
        rating,
        count,
        5,
        rating,
        "2026-08-18T20:00:00+00:00",
        ingredient_signature=sig,
        canonical_url=f"https://{source}/{rid}",
        ingredients=tuple(ingredients),
        evidence_confidence=confidence,
        evidence_status=status,
        categories=categorize_recipe(title, ingredients),
    )


def empty_state():
    return {
        "recipes": {},
        "rank_history": [],
        "source_history": [],
        "url_catalog": {},
        "anomaly_history": [],
        "schema_version": 4,
        "migration": {},
    }


def test_bayesian_rewards_volume_without_ignoring_rating():
    state = empty_state()
    rows = [
        recipe("a", "Tiny Perfect", "x.com", 5.0, 2),
        recipe("b", "Huge Great", "x.com", 4.9, 5000),
        recipe("c", "Solid", "x.com", 4.7, 500),
    ]
    merge_observations(state, rows, "2026-08-18T20:00:00+00:00")
    ranked, method = bayesian_rank(state, stale_days=10000)
    assert ranked[0]["recipe_id"] == "b"
    assert method["volume_prior_m"] >= 50
    assert "uncertainty_penalty" in ranked[0]
    assert "evidence_penalty" in ranked[0]
    assert "rank_confidence" in ranked[0]
    assert "rank_provenance" in ranked[0]
    assert method["robustness"]["simulation_count"] == 36


def test_cross_site_duplicate_does_not_double_count_review_population():
    ingredients = ["1 lb chicken breast", "1 tsp salt", "1 tbsp olive oil"]
    recipes = [
        {
            **recipe(
                "a", "Air Fryer Chicken", "one.com", 4.8, 100, ingredient_signature(ingredients), ingredients
            ).__dict__,
            "last_seen_at": "2026-08-18T20:00:00+00:00",
        },
        {
            **recipe(
                "b",
                "Crispy Air Fryer Chicken",
                "two.com",
                5.0,
                50,
                ingredient_signature(["1 pound chicken breast", "salt", "olive oil"]),
                ["1 pound chicken breast", "salt", "olive oil"],
            ).__dict__,
            "last_seen_at": "2026-08-18T20:00:00+00:00",
        },
    ]
    deduped, count, groups = dedupe_current(recipes, detailed=True)
    assert count == 1
    assert len(deduped) == 1
    assert deduped[0]["rating_count"] == 100
    assert len(groups) == 2
    assert deduped[0]["duplicate_confidence"] >= 0.88


def test_fuzzy_duplicate_similarity_uses_ingredients():
    a = recipe(
        "a",
        "Air Fryer Chicken Breast",
        "one.com",
        4.8,
        100,
        ingredients=["1 lb chicken breast", "1 tbsp olive oil", "salt"],
    ).__dict__
    b = recipe(
        "b",
        "Crispy Air Fryer Chicken Breasts",
        "two.com",
        4.9,
        80,
        ingredients=["1 pound chicken breasts", "olive oil", "kosher salt"],
    ).__dict__
    assert duplicate_similarity(a, b) >= 0.88


def test_ingredient_signature_is_order_independent():
    assert ingredient_signature(["Salt", "Chicken"]) == ingredient_signature(["chicken", "salt"])


def test_evidence_conflict_is_quarantined():
    html = """
    <html><head><title>Air Fryer Test</title><link rel="canonical" href="https://x.com/test"></head><body>
    <span itemprop="ratingValue">4.1</span><span itemprop="ratingCount">100</span>
    <script type="application/ld+json">{
      "@type":"Recipe","name":"Air Fryer Test",
      "recipeIngredient":["chicken","salt"],
      "aggregateRating":{"ratingValue":"4.9","ratingCount":"100","bestRating":"5"}
    }</script></body></html>"""
    row, _ = extract_recipe_from_html(html, "https://x.com/test", "x.com", SourceConfig("x.com"))
    assert row is not None
    assert row.evidence_status == "conflict"
    assert row.evidence_confidence < 0.6
    state = empty_state()
    merge_observations(state, [row], "2026-08-18T20:00:00+00:00")
    ranked, _ = bayesian_rank(state, stale_days=10000)
    assert ranked == []


def test_verified_dual_evidence_gets_high_confidence():
    html = """
    <html><head><title>Air Fryer Test</title></head><body>
    <span itemprop="ratingValue">4.8</span><span itemprop="ratingCount">101</span>
    <script type="application/ld+json">{
      "@type":"Recipe","name":"Air Fryer Test",
      "recipeIngredient":["chicken","salt"],
      "aggregateRating":{"ratingValue":"4.8","ratingCount":"100","bestRating":"5"}
    }</script></body></html>"""
    row, _ = extract_recipe_from_html(html, "https://x.com/test", "x.com", SourceConfig("x.com"))
    assert row.evidence_status == "verified"
    assert row.evidence_confidence == 1.0


def test_schema_only_evidence_is_rankable_but_penalized():
    html = """
    <html><head><title>Air Fryer Schema Only</title></head><body>
    <script type="application/ld+json">{
      "@type":"Recipe","name":"Air Fryer Schema Only",
      "recipeIngredient":["potato","salt"],
      "aggregateRating":{"ratingValue":"4.9","ratingCount":"250","bestRating":"5"}
    }</script></body></html>"""
    row, _ = extract_recipe_from_html(html, "https://x.com/schema", "x.com", SourceConfig("x.com"))
    assert row is not None
    assert row.evidence_status == "schema_only"
    assert row.evidence_confidence == 0.65
    state = empty_state()
    merge_observations(state, [row], "2026-08-18T20:00:00+00:00")
    ranked, _ = bayesian_rank(state, stale_days=10000)
    assert ranked[0]["recipe_id"] == row.recipe_id
    assert ranked[0]["evidence_penalty"] > 0


def test_legacy_state_migration_removes_favorable_point85_default():
    state = {
        "schema_version": 3,
        "recipes": {
            "a": {
                "recipe_id": "a",
                "title": "Legacy",
                "source": "x.com",
                "url": "https://x.com/a",
                "canonical_url": "https://x.com/a",
                "normalized_rating": 4.9,
                "rating_count": 500,
                "evidence_confidence": 0.85,
                "evidence_status": "",
                "last_seen_at": "2026-08-18T20:00:00+00:00",
            }
        },
        "url_catalog": {},
        "rank_history": [],
        "source_history": [],
        "anomaly_history": [],
    }
    migrated = migrate_state(state)
    row = migrated["recipes"]["a"]
    assert migrated["schema_version"] == 4
    assert row["evidence_status"] == "legacy_unverified"
    assert row["evidence_confidence"] == 0.60
    assert row["needs_evidence_backfill"] is True
    assert migrated["migration"]["legacy_evidence_pending"] == 1


def test_backfill_selector_prioritizes_only_legacy_records():
    state = {
        "recipes": {
            "legacy": {"evidence_status": "legacy_unverified", "needs_evidence_backfill": True},
            "fresh": {"evidence_status": "verified", "needs_evidence_backfill": False},
        },
        "url_catalog": {
            "https://x.com/legacy": {"url": "https://x.com/legacy", "source": "x.com", "recipe_id": "legacy"},
            "https://x.com/fresh": {"url": "https://x.com/fresh", "source": "x.com", "recipe_id": "fresh"},
        },
    }
    targets = select_refresh_targets(state, [SourceConfig("x.com")], "backfill")
    assert [x["url"] for x in targets] == ["https://x.com/legacy"]


def test_publisher_bias_is_partially_pooled():
    state = empty_state()
    rows = []
    for i in range(8):
        rows.append(recipe(f"a{i}", f"Air Fryer A {i}", "inflated.com", 4.95, 1000 + i))
        rows.append(recipe(f"b{i}", f"Air Fryer B {i}", "strict.com", 4.40, 1000 + i))
    merge_observations(state, rows, "2026-08-18T20:00:00+00:00")
    _, method = bayesian_rank(state, stale_days=10000)
    assert method["source_adjustments"]["inflated.com"]["bias"] > 0
    assert method["source_adjustments"]["strict.com"]["bias"] < 0


def test_publisher_bias_correction_is_capped_before_adjusting_recipe_rating():
    state = empty_state()
    rows = []
    for i in range(20):
        rows.append(recipe(f"strict-{i}", f"Strict Recipe {i}", "strict.com", 4.0, 500 + i))
        rows.append(recipe(f"inflated-{i}", f"Inflated Recipe {i}", "inflated.com", 5.0, 500 + i))
    rows.append(recipe("target", "Target Air Fryer Potatoes", "strict.com", 4.82, 500))
    merge_observations(state, rows, "2026-08-18T20:00:00+00:00")
    ranked, method = bayesian_rank(state, stale_days=10000)
    adjustment = method["source_adjustments"]["strict.com"]
    target = next(row for row in ranked if row["recipe_id"] == "target")
    assert adjustment["shrunk_category_adjusted_bias"] < -0.15
    assert adjustment["bias"] == -0.15
    assert adjustment["bias_capped"] is True
    assert abs(target["adjusted_rating"] - 4.97) < 1e-9
    assert target["adjusted_rating"] < 5.0


def test_category_aware_normalization_reports_category_baselines():
    state = empty_state()
    rows = []
    for i in range(10):
        rows.append(
            recipe(f"c{i}", f"Air Fryer Chicken {i}", "mixed.com", 4.9, 300 + i, ingredients=["chicken breast", "salt"])
        )
        rows.append(
            recipe(f"p{i}", f"Air Fryer Potatoes {i}", "other.com", 4.5, 300 + i, ingredients=["potatoes", "salt"])
        )
    merge_observations(state, rows, "2026-08-18T20:00:00+00:00")
    _, method = bayesian_rank(state, stale_days=10000)
    assert "Chicken" in method["category_baselines"]
    assert "Potatoes" in method["category_baselines"]
    assert "raw_category_adjusted_bias" in method["source_adjustments"]["mixed.com"]


def test_hourly_selector_prioritizes_top_rank_and_new_urls():
    state = {
        "recipes": {"top": {"last_rank": 1, "rating_count": 1000, "previous_rating_count": 990}},
        "url_catalog": {
            "https://x.com/top": {
                "url": "https://x.com/top",
                "source": "x.com",
                "recipe_id": "top",
                "last_checked": "2026-08-18T19:00:00+00:00",
            },
            "https://x.com/old": {
                "url": "https://x.com/old",
                "source": "x.com",
                "last_checked": "2026-08-18T19:00:00+00:00",
            },
            "https://x.com/new": {
                "url": "https://x.com/new",
                "source": "x.com",
                "first_discovered": "2026-08-18T20:00:00+00:00",
            },
        },
    }
    targets = select_refresh_targets(state, [SourceConfig("x.com")], "hourly", hourly_limit=2)
    urls = [x["url"] for x in targets]
    assert "https://x.com/top" in urls
    assert "https://x.com/new" in urls


def test_anomaly_detection_flags_review_count_decrease():
    state = empty_state()
    first = recipe("a", "Air Fryer Chicken", "x.com", 4.8, 100)
    merge_observations(state, [first], "2026-08-18T19:00:00+00:00")
    second = recipe("a", "Air Fryer Chicken", "x.com", 4.8, 90)
    merge_observations(state, [second], "2026-08-18T20:00:00+00:00")
    anomalies = detect_anomalies(state, [second], [], [], "2026-08-18T20:00:00+00:00")
    assert any(x["type"] == "review_count_decrease" for x in anomalies)


def test_immutable_run_observation_files(tmp_path: Path):
    records = [{"recipe_id": "a", "rating": 4.9, "rating_count": 100}]
    first = write_run_records(tmp_path, records, "2026-08-18T20:00:00+00:00")
    second = write_run_records(tmp_path, records, "2026-08-18T21:00:00+00:00")
    assert first != second
    assert Path(first).exists() and Path(second).exists()
    recent = read_recent_records(tmp_path, limit=10)
    assert len(recent) == 2


def test_category_classification_is_multilabel():
    cats = categorize_recipe("Air Fryer Chicken Breakfast Bites", ["eggs", "chicken breast"])
    assert "Chicken" in cats
    assert "Breakfast" in cats
    assert "Snacks" in cats


def test_jsonld_histogram_is_preserved_and_used():
    html = """
    <html><head><title>Histogram Recipe</title></head><body>
    <script type="application/ld+json">{
      "@type":"Recipe","name":"Histogram Recipe",
      "recipeIngredient":["potato","salt"],
      "aggregateRating":{"ratingValue":"4.5","ratingCount":"100","bestRating":"5",
      "ratingHistogram":{"5":70,"4":20,"3":5,"2":3,"1":2}}
    }</script></body></html>"""
    row, _ = extract_recipe_from_html(html, "https://x.com/h", "x.com", SourceConfig("x.com"))
    assert row.rating_histogram["5"] == 70
    state = empty_state()
    merge_observations(state, [row], "2026-08-18T20:00:00+00:00")
    ranked, _ = bayesian_rank(state, stale_days=10000)
    assert ranked[0]["uncertainty_penalty"] < 0.25
    assert ranked[0]["uncertainty_method"] == "rating_histogram"


def test_empirical_uncertainty_requires_temporal_and_cross_recipe_maturity():
    observations = []
    for i in range(32):
        observations.append(
            {
                "recipe_id": "a",
                "timestamp": f"2026-08-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00",
                "rating": 4.7 + (0.01 if i % 2 else 0.0),
                "rating_count": 100 + i,
            }
        )
    calibration = build_empirical_uncertainty(observations)
    bucket = calibration["100-499"]
    assert bucket["sample_pairs"] == 1
    assert bucket["unique_recipes"] == 1
    assert bucket["meets_pair_count"] is False
    assert bucket["meets_unique_recipe_count"] is False
    assert bucket["meets_history_span"] is False
    assert bucket["ready"] is False


def test_review_velocity_and_longitudinal_metrics_are_exposed():
    state = empty_state()
    first = recipe("a", "Air Fryer Chicken", "x.com", 4.8, 100)
    merge_observations(state, [first], "2026-08-17T20:00:00+00:00")
    second = recipe("a", "Air Fryer Chicken", "x.com", 4.81, 110)
    merge_observations(state, [second], "2026-08-18T20:00:00+00:00")
    observations = [
        {"recipe_id": "a", "timestamp": "2026-08-17T20:00:00+00:00", "rating": 4.8, "rating_count": 100},
        {"recipe_id": "a", "timestamp": "2026-08-18T20:00:00+00:00", "rating": 4.81, "rating_count": 110},
    ]
    rankings = [
        {"recipe_id": "a", "timestamp": "2026-08-17T20:00:00+00:00", "rank": 8},
        {"recipe_id": "a", "timestamp": "2026-08-18T20:00:00+00:00", "rank": 6},
    ]
    history = build_historical_metrics(observations, rankings)
    ranked, _ = bayesian_rank(state, stale_days=10000, historical_metrics=history)
    assert round(ranked[0]["review_velocity_per_day"], 6) == 10.0
    assert ranked[0]["peak_rank"] == 6
    assert ranked[0]["days_in_top10"] == 2


def test_source_health_does_not_treat_not_checked_as_failure():
    state = empty_state()
    state["source_history"] = [
        {
            "run_at": "2026-08-18T20:00:00+00:00",
            "coverage": [
                {"source": "x.com", "status": "ok"},
                {"source": "y.com", "status": "not_checked_this_run"},
            ],
        }
    ]
    health, summary = source_health_summary(
        state,
        [{"source": "x.com", "status": "ok"}, {"source": "y.com", "status": "not_checked_this_run"}],
        [SourceConfig("x.com"), SourceConfig("y.com")],
        "2026-08-18T20:00:00+00:00",
    )
    assert summary["sources_configured"] == 2
    assert summary["sources_checked_this_run"] == 1
    assert summary["sources_successful_this_run"] == 1
    y = next(x for x in health if x["source"] == "y.com")
    assert y["checked_this_run"] is False


def test_checked_in_dedupe_benchmark_is_measurable():
    summary, rows = evaluate_dedupe_benchmark("data/benchmarks/dedupe_pairs.json")
    assert summary["benchmark_pairs"] >= 20
    assert len(rows) == summary["benchmark_pairs"]
    assert summary["precision"] is not None
    assert summary["recall"] is not None
