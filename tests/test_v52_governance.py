from __future__ import annotations

from pathlib import Path

from airfryer_rankings.model_config import load_model_config
from airfryer_rankings.quality_gate import evaluate_publish_gate, load_quality_gate_policy


def _ranked(count: int = 100) -> list[dict]:
    return [{"recipe_id": f"r{i}", "rank": i + 1, "hierarchical_score": 4.5} for i in range(count)]


def test_model_config_exposes_semver_and_component_versions():
    _, payload = load_model_config("config/model.yaml")
    assert payload["model_version"] == 5
    assert payload["model_semver"] == "5.2.0"
    assert payload["component_versions"]["ranking_schema"] == 5
    assert payload["component_versions"]["uncertainty_calibration"] >= 2


def test_slo_policy_is_versioned_in_repository():
    policy = load_quality_gate_policy("config/slo.yaml")
    assert policy["crawl_success_warn_below"] == 0.90
    assert policy["crawl_success_fail_below"] == 0.70
    assert policy["top50_overlap_fail_below"] < policy["top50_overlap_warn_below"]


def test_gate_warns_on_degraded_crawl_and_dom_churn_without_blocking():
    previous = {"ranked_recipes": 100, "model_version": 5, "model_semver": "5.2.0", "deduplicated_count": 2}
    previous_rankings = _ranked(50)
    metrics = {
        "evidence_conflict_rate": 0.0,
        "legacy_evidence_pending": 0,
        "http_429": 0,
        "crawl_success_rate": 0.87,
        "extract_success_rate": 0.93,
        "ranking_eligible_rate": 0.99,
        "crawl_targets": 100,
        "dom_structure_changes": 16,
        "anomalies": 5,
        "sources_stale_24h": 0,
    }
    result = evaluate_publish_gate(
        previous,
        previous_rankings,
        _ranked(100),
        metrics,
        mode="hourly",
        model_version=5,
        model_semver="5.2.0",
        deduplicated_count=2,
    )
    assert result["passed"] is True
    assert any("crawl success rate is degraded" in warning for warning in result["warnings"])
    assert any("DOM structure changes broke extraction" in warning for warning in result["warnings"])


def test_gate_fails_on_catastrophic_crawl_health():
    result = evaluate_publish_gate(
        {"ranked_recipes": 100, "model_version": 5, "model_semver": "5.2.0"},
        _ranked(50),
        _ranked(100),
        {
            "evidence_conflict_rate": 0.0,
            "crawl_success_rate": 0.50,
            "extract_success_rate": 0.95,
            "ranking_eligible_rate": 0.99,
            "crawl_targets": 100,
            "dom_structure_changes": 0,
            "anomalies": 0,
            "sources_stale_24h": 0,
        },
        mode="hourly",
        model_version=5,
        model_semver="5.2.0",
        deduplicated_count=0,
    )
    assert result["passed"] is False
    assert any("crawl success rate fell" in failure for failure in result["failures"])


def test_semver_change_explains_model_churn_without_hiding_warning():
    previous = {"ranked_recipes": 100, "model_version": 5, "model_semver": "5.1.0"}
    previous_rankings = _ranked(50)
    current = [{"recipe_id": f"new{i}", "rank": i + 1, "hierarchical_score": 4.5} for i in range(100)]
    result = evaluate_publish_gate(
        previous,
        previous_rankings,
        current,
        {
            "evidence_conflict_rate": 0.0,
            "crawl_success_rate": 1.0,
            "extract_success_rate": 1.0,
            "ranking_eligible_rate": 1.0,
            "crawl_targets": 100,
            "dom_structure_changes": 0,
            "anomalies": 0,
            "sources_stale_24h": 0,
        },
        mode="hourly",
        model_version=5,
        model_semver="5.2.0",
        deduplicated_count=0,
    )
    assert result["passed"] is True
    assert result["metrics"]["model_identity_changed"] is True
    assert any("model identity changed" in warning for warning in result["warnings"])


def test_missing_slo_file_falls_back_to_safe_defaults(tmp_path: Path):
    policy = load_quality_gate_policy(tmp_path / "missing.yaml")
    assert policy["corpus_retention_fail_below"] == 0.80
    assert policy["http_429_warn_above"] == 3
