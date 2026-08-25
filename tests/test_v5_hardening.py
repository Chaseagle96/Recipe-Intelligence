from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from airfryer_rankings.archive import history_storage_health, write_history_parquet
from airfryer_rankings.backtesting import run_historical_backtest
from airfryer_rankings.contracts import contract_manifest
from airfryer_rankings.evidence_calibration import evaluate_evidence_labels
from airfryer_rankings.model_config import ModelParameters
from airfryer_rankings.quality_gate import evaluate_publish_gate
from airfryer_rankings.schemas import validate_observation_record, validate_ranked_recipe
from airfryer_rankings.structure import dom_structure_fingerprint, schema_signature


def test_contract_manifest_has_explicit_raw_clean_model_serving_lineage():
    manifest = contract_manifest()
    names = {row["name"] for row in manifest["contracts"]}
    assert names == {"raw_observation", "clean_recipe", "ranking", "serving"}
    raw = next(row for row in manifest["contracts"] if row["name"] == "raw_observation")
    assert raw["authoritative"] is True
    assert len(manifest["lineage"]) == 3


def test_runtime_schema_validation_rejects_invalid_confidence_and_rank():
    observation = {
        "recipe_id": "a",
        "timestamp": "2026-08-18T20:00:00+00:00",
        "source": "example.com",
        "url": "https://example.com/a",
        "title": "A",
        "rating": 4.8,
        "rating_count": 100,
        "evidence_confidence": 0.8,
        "evidence_status": "schema_only",
        "extraction_method": "jsonld",
        "page_hash": "abc",
        "schema_version": 2,
    }
    validate_observation_record(observation)
    invalid = {**observation, "evidence_confidence": 1.5}
    try:
        validate_observation_record(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid confidence should fail validation")

    ranked = {
        "recipe_id": "a",
        "title": "A",
        "source": "example.com",
        "url": "https://example.com/a",
        "rating": 4.8,
        "rating_count": 100,
        "hierarchical_score": 4.5,
        "rank": 1,
        "evidence_confidence": 0.8,
        "evidence_status": "schema_only",
        "rank_confidence": 0.9,
    }
    validate_ranked_recipe(ranked)


def test_checked_in_evidence_labels_are_measurable_but_not_prematurely_calibrated():
    calibration, outcomes = evaluate_evidence_labels("data/benchmarks/evidence_labels.json")
    assert len(outcomes) >= 7
    assert all(row.get("correct") is True for row in outcomes)
    assert calibration["verified"]["sample_count"] >= 3
    assert calibration["schema_only"]["sample_count"] >= 4
    assert calibration["verified"]["ready"] is False
    assert calibration["schema_only"]["ready"] is False


def test_structural_fingerprints_change_when_rating_schema_contract_changes():
    first = '<html><body><span itemprop="ratingCount"></span><script type="application/ld+json">{"@type":"Recipe","aggregateRating":{"ratingValue":4.8,"ratingCount":10}}</script></body></html>'
    second = '<html><body><span itemprop="reviewCount"></span><script type="application/ld+json">{"@type":"Recipe","aggregateRating":{"ratingValue":4.8,"reviewCount":10}}</script></body></html>'
    assert dom_structure_fingerprint(first) != dom_structure_fingerprint(second)
    assert schema_signature(first) != schema_signature(second)


def test_structural_fingerprints_ignore_layout_and_jsonld_order():
    first = '<main><div class="ad"></div><span itemprop="ratingValue"></span></main><script type="application/ld+json">[{"@type":"Recipe"},{"@type":"Person"}]</script>'
    second = '<nav></nav><main><section><span itemprop="ratingValue"></span></section></main><script type="application/ld+json">[{"@type":"Person"},{"@type":"Recipe"}]</script>'
    assert dom_structure_fingerprint(first) == dom_structure_fingerprint(second)
    assert schema_signature(first) == schema_signature(second)


def test_historical_backtest_refuses_short_history():
    active = ModelParameters()
    payload = {"promotion_policy": {"minimum_history_days": 30}, "backtest_grid": {}}
    observations = [
        {"recipe_id": "a", "timestamp": "2026-08-18T00:00:00+00:00", "rating": 4.8, "rating_count": 100},
        {"recipe_id": "a", "timestamp": "2026-08-19T00:00:00+00:00", "rating": 4.8, "rating_count": 101},
    ]
    result = run_historical_backtest(observations, active, payload)
    assert result["ready"] is False
    assert result["reason"] == "insufficient_longitudinal_history"


def test_historical_backtest_can_evaluate_future_quality_when_history_exists():
    active = ModelParameters()
    payload = {
        "promotion_policy": {
            "automatic_parameter_promotion": False,
            "minimum_history_days": 30,
            "minimum_backtest_windows": 1,
            "minimum_backtest_recipes": 5,
        },
        "backtest_grid": {
            "max_source_bias": [0.15],
            "evidence_penalty_scale": [0.20],
            "source_prior_strength": [20.0],
            "category_prior_strength": [20.0],
            "uncertainty_cap": [0.25],
            "volume_prior_multiplier": [1.0],
        },
    }
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    observations = []
    for index in range(10):
        for day, count in ((0, 100 + index), (40, 500 + index * 10), (80, 800 + index * 20)):
            observations.append(
                {
                    "recipe_id": f"r{index}",
                    "timestamp": (start + timedelta(days=day)).isoformat(),
                    "source": "example.com",
                    "url": f"https://example.com/r{index}",
                    "title": f"Air Fryer Chicken {index}",
                    "rating": 4.4 + index * 0.04,
                    "rating_count": count,
                    "evidence_confidence": 1.0,
                    "evidence_status": "verified",
                    "categories": ["Chicken"],
                }
            )
    result = run_historical_backtest(observations, active, payload, horizons=(30,), max_windows=1)
    assert result["ready"] is True
    assert result["automatic_parameter_promotion"] is False
    assert result["recommendation"] is not None
    assert result["recommendation"]["windows"] >= 1


def test_publication_gate_fails_closed_on_catastrophic_corpus_loss():
    previous_summary = {"ranked_recipes": 1000, "model_version": 5, "deduplicated_count": 2}
    previous_rankings = [{"recipe_id": f"r{index}"} for index in range(50)]
    current = [
        {
            "recipe_id": f"r{index}",
            "rank": index + 1,
            "hierarchical_score": 4.5,
        }
        for index in range(100)
    ]
    result = evaluate_publish_gate(
        previous_summary,
        previous_rankings,
        current,
        {"evidence_conflict_rate": 0.0, "legacy_evidence_pending": 0, "http_429": 0},
        mode="hourly",
        model_version=5,
        deduplicated_count=2,
    )
    assert result["passed"] is False
    assert any("retained only" in failure for failure in result["failures"])


def test_history_archive_health_and_parquet_export(tmp_path: Path):
    root = tmp_path / "observations"
    root.mkdir()
    (root / "sample.ndjson").write_text('{"recipe_id":"a"}\n{"recipe_id":"b"}\n', encoding="utf-8")
    policy = {
        "history": {
            "archive_policy": {
                "recommendation_record_threshold": 2,
                "recommendation_bytes_threshold": 999999,
                "object_storage_uri_env": "UNSET_TEST_URI",
                "upload_enabled": False,
            }
        }
    }
    health = history_storage_health([root], policy)
    assert health["ndjson_records"] == 2
    assert health["archive_recommended"] is True
    parquet = write_history_parquet(
        tmp_path / "archive.parquet",
        [{"recipe_id": "a", "rating": 4.8, "rating_count": 100}],
    )
    assert parquet is not None and Path(parquet).exists()
