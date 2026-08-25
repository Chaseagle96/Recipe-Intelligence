from __future__ import annotations

import argparse
import json
from pathlib import Path

from .analytics import write_duckdb_cache
from .archive import history_storage_health, load_storage_policy, write_history_parquet
from .backtesting import run_historical_backtest
from .benchmarks import build_dedupe_label_queue, evaluate_dedupe_benchmark
from .calibration import build_empirical_uncertainty, build_historical_metrics
from .contracts import contract_manifest, write_contract_manifest
from .core import (
    bayesian_rank,
    crawl_targets,
    detect_anomalies,
    discover_source_urls,
    load_sources,
    load_state,
    merge_observations,
    now_iso,
    read_recent_records,
    save_state,
    select_refresh_targets,
    source_health_summary,
    source_reliability,
    write_run_records,
)
from .evidence_calibration import apply_evidence_calibration, evaluate_evidence_labels
from .media import enrich_ambiguous_perceptual_hashes
from .model_config import load_model_config
from .observability import build_pipeline_metrics
from .persistence import atomic_write_json, exclusive_lock, load_json_object
from .qa import temporal_anomalies
from .quality_gate import (
    assert_publishable,
    evaluate_publish_gate,
    load_previous_serving_snapshot,
    load_quality_gate_policy,
    write_quality_gate,
)
from .reporting import write_csv_outputs, write_dashboard, write_workbook
from .schemas import validate_observation_record, validate_ranked_recipe, validate_records, validate_source_health

MAX_ANALYTICAL_RECORDS = 250000
MAX_EXCEL_OBSERVATIONS = 10000


def _merge_coverage(sources, discovery: list[dict], crawl: list[dict]) -> list[dict]:
    discovery_by_source = {row.get("source"): row for row in discovery}
    crawl_by_source = {row.get("source"): row for row in crawl}
    output = []
    for cfg in sources:
        row = {"source": cfg.domain}
        row.update(discovery_by_source.get(cfg.domain, {}))
        crawl_row = crawl_by_source.get(cfg.domain, {})
        for key, value in crawl_row.items():
            if key in row and key not in {"source", "status"}:
                row[f"crawl_{key}"] = value
            else:
                row[key] = value
        if cfg.domain not in discovery_by_source and cfg.domain not in crawl_by_source:
            row["status"] = "not_checked_this_run"
        elif crawl_row.get("status") == "degraded":
            row["status"] = "degraded"
        else:
            row["status"] = row.get("status", "ok")
        output.append(row)
    return output


def _read_json(path: str | Path, fallback: dict | None = None) -> dict:
    target = Path(path)
    if not target.exists():
        return dict(fallback or {})
    return load_json_object(target)


def _write_json(path: str | Path, payload: dict) -> str:
    target = Path(path)
    atomic_write_json(target, payload, default=str)
    return str(target)


def _run(args: argparse.Namespace) -> None:
    for folder in (
        "data/observations",
        "data/anomalies",
        "data/rankings",
        "data/coverage",
        "data/model",
        "output",
        "docs",
    ):
        Path(folder).mkdir(parents=True, exist_ok=True)

    run_at = now_iso()
    previous_summary, previous_rankings = load_previous_serving_snapshot("output")
    state = load_state(args.state)
    sources = load_sources(args.sources)
    model_params, model_payload = load_model_config(args.model_config)
    storage_policy = load_storage_policy(args.storage_config)
    quality_policy = load_quality_gate_policy(args.slo_config)
    migration = state.get("migration", {})
    requested_mode = args.mode
    effective_mode = args.mode
    if requested_mode == "hourly" and int(migration.get("legacy_evidence_pending") or 0) > 0:
        effective_mode = "backfill"

    discovery_results: list[dict] = []
    should_discover = effective_mode in {"daily", "deep", "smoke"} or not state.get("url_catalog")
    if should_discover:
        discovery_mode = "deep" if effective_mode == "deep" else "daily"
        for cfg in sources:
            try:
                discovery_results.append(
                    discover_source_urls(cfg, state, discovery_mode, run_at, global_max_urls=args.max_urls)
                )
            except Exception as exc:
                discovery_results.append(
                    {
                        "source": cfg.domain,
                        "discovered_urls": 0,
                        "new_urls": 0,
                        "sitemap_docs": 0,
                        "elapsed_seconds": 0,
                        "status": f"discovery_error:{type(exc).__name__}",
                    }
                )

    target_mode = "daily" if effective_mode == "smoke" else effective_mode
    targets = select_refresh_targets(
        state,
        sources,
        target_mode,
        global_max_urls=args.max_urls,
        hourly_limit=args.hourly_limit,
    )
    rows, crawl_coverage, crawl_events = crawl_targets(targets, sources, state, run_at)
    media_stats = enrich_ambiguous_perceptual_hashes(rows, state, max_fetches=20)

    evidence_calibration, evidence_label_results = evaluate_evidence_labels(
        "data/benchmarks/evidence_labels.json",
        fixture_root="tests/fixtures/real_pages",
    )
    rows = apply_evidence_calibration(rows, evidence_calibration)
    observations = merge_observations(state, rows, run_at)
    validate_records(observations, validate_observation_record)
    coverage = _merge_coverage(sources, discovery_results, crawl_coverage)

    state.setdefault("source_history", []).append({"run_at": run_at, "mode": effective_mode, "coverage": coverage})
    if len(state["source_history"]) > 720:
        del state["source_history"][:-720]

    anomalies = detect_anomalies(state, rows, coverage, crawl_events, run_at)
    prior_observations = read_recent_records("data/observations", limit=MAX_ANALYTICAL_RECORDS)
    prior_rankings = read_recent_records("data/rankings", limit=MAX_ANALYTICAL_RECORDS)
    all_observations = (prior_observations + observations)[-MAX_ANALYTICAL_RECORDS:]
    uncertainty_calibration = build_empirical_uncertainty(all_observations)
    historical_metrics = build_historical_metrics(all_observations, prior_rankings)
    ranked, method = bayesian_rank(
        state,
        stale_days=args.stale_days,
        empirical_calibration=uncertainty_calibration,
        historical_metrics=historical_metrics,
        model_params=model_params,
        model_config_path=args.model_config,
    )
    validate_records(ranked, validate_ranked_recipe)
    temporal = temporal_anomalies(ranked, run_at)
    if temporal:
        anomalies.extend(temporal)
        state.setdefault("anomaly_history", []).extend(temporal)
        if len(state["anomaly_history"]) > 2000:
            del state["anomaly_history"][:-2000]

    reliability = source_reliability(state, coverage, method)
    source_health, health_summary = source_health_summary(state, coverage, sources, run_at)
    validate_records(source_health, validate_source_health)
    pipeline_metrics, pipeline_metric_rows, observability_alerts = build_pipeline_metrics(
        state,
        coverage,
        rows,
        ranked,
        anomalies,
        source_health,
        crawl_events,
        targets,
        run_at,
    )
    if observability_alerts:
        anomalies.extend(
            {
                "timestamp": alert["timestamp"],
                "severity": "medium",
                "type": alert["type"],
                "recipe_id": "",
                "title": "",
                "source": "",
                "url": "",
                "detail": f"{alert['metric']}={alert['value']} threshold={alert['threshold']} {alert['direction']}",
            }
            for alert in observability_alerts
        )

    dedupe_benchmark, dedupe_benchmark_rows = evaluate_dedupe_benchmark("data/benchmarks/dedupe_pairs.json")
    dedupe_label_queue = build_dedupe_label_queue(list(state.get("recipes", {}).values()), limit=250)

    model_version = int(method.get("model_version") or model_payload.get("model_version", 5))
    model_semver = str(model_payload.get("model_semver") or f"{model_version}.0.0")
    component_versions = model_payload.get("component_versions") or {}
    quality_gate = evaluate_publish_gate(
        previous_summary,
        previous_rankings,
        ranked,
        pipeline_metrics,
        mode=effective_mode,
        model_version=model_version,
        model_semver=model_semver,
        deduplicated_count=int(method.get("deduplicated_count") or 0),
        policy=quality_policy,
    )
    if effective_mode != "smoke":
        assert_publishable(quality_gate)

    backtest_path = Path("data/model/backtest_latest.json")
    if effective_mode in {"daily", "deep"}:
        backtest = run_historical_backtest(all_observations, model_params, model_payload)
        _write_json(backtest_path, backtest)
    else:
        backtest = _read_json(
            backtest_path,
            {
                "ready": False,
                "windows": [],
                "configurations": [],
                "recommendation": None,
                "reason": "awaiting_daily_backtest",
            },
        )

    if effective_mode in {"daily", "deep"}:
        storage_health = history_storage_health(
            ("data/observations", "data/rankings", "data/coverage", "data/anomalies"),
            storage_policy,
        )
        _write_json("output/storage_health.json", storage_health)
    else:
        storage_health = _read_json("output/storage_health.json", {"archive_recommended": False})

    history_archive = None
    if effective_mode == "deep":
        history_archive = write_history_parquet("output/history_archive.parquet", all_observations)

    observation_file = write_run_records("data/observations", observations, run_at)
    anomaly_file = write_run_records("data/anomalies", anomalies, run_at)
    coverage_file = write_run_records("data/coverage", coverage, run_at)
    ranking_snapshot = [
        {
            "timestamp": run_at,
            "schema_version": 5,
            "model_version": model_version,
            "model_semver": model_semver,
            **{
                key: row.get(key)
                for key in (
                    "rank",
                    "recipe_id",
                    "title",
                    "source",
                    "url",
                    "rating",
                    "rating_count",
                    "hierarchical_score",
                    "evidence_confidence",
                    "evidence_grade",
                    "rank_confidence",
                    "rank_range_low",
                    "rank_range_high",
                    "duplicate_group_id",
                )
            },
        }
        for row in ranked[:200]
    ]
    ranking_file = write_run_records("data/rankings", ranking_snapshot, run_at)
    recent_rankings = (prior_rankings + ranking_snapshot)[-MAX_ANALYTICAL_RECORDS:]

    calibration_ready = sum(1 for row in uncertainty_calibration.values() if row.get("ready"))
    evidence_calibration_ready = sum(1 for row in evidence_calibration.values() if row.get("ready"))
    migration_after = state.get("migration", {})
    method_row = {
        "generated_at": run_at,
        "requested_mode": requested_mode,
        "mode": effective_mode,
        "model_version": model_version,
        "model_semver": model_semver,
        "component_versions": json.dumps(component_versions, sort_keys=True),
        "active_parameters": json.dumps(method.get("active_parameters", model_params.to_dict()), sort_keys=True),
        "observations_this_run": len(observations),
        "ranked_recipes": len(ranked),
        "configured_sources": len(sources),
        "catalog_urls": len(state.get("url_catalog", {})),
        "targets_this_run": len(targets),
        "global_prior": method.get("global_prior"),
        "volume_prior_m": method.get("volume_prior_m"),
        "candidate_count": method.get("candidate_count"),
        "deduplicated_count": method.get("deduplicated_count"),
        "stale_days": method.get("stale_days", args.stale_days),
        "history_snapshots": method.get("history_snapshots"),
        "robustness_simulations": method.get("robustness", {}).get("simulation_count"),
        "mean_spearman_top200": method.get("robustness", {}).get("mean_spearman_top200"),
        "mean_kendall_top100": method.get("robustness", {}).get("mean_kendall_top100"),
        "uncertainty_buckets_empirically_ready": calibration_ready,
        "evidence_classes_empirically_ready": evidence_calibration_ready,
        "legacy_evidence_pending": migration_after.get("legacy_evidence_pending"),
        "dedupe_benchmark_precision": dedupe_benchmark.get("precision"),
        "dedupe_benchmark_recall": dedupe_benchmark.get("recall"),
        "dedupe_benchmark_f1": dedupe_benchmark.get("f1"),
        "backtest_ready": backtest.get("ready"),
        "backtest_recommendation": (backtest.get("recommendation") or {}).get("config_id"),
        "archive_recommended": storage_health.get("archive_recommended"),
        "formula": method.get("formula"),
        "prior_definition": "Global prior is sqrt(review-count)-weighted. Publisher leniency is estimated from residuals after category baselines, then partially pooled and capped before recipe-level Bayesian shrinkage.",
        "uncertainty_definition": "Uses rating histograms when available, empirically measured historical rating volatility once a volume bucket has enough observations, otherwise a conservative theoretical fallback.",
        "evidence_definition": "Structured and visible rating evidence are cross-checked. Reviewed fixtures can empirically calibrate evidence confidence only after the configured minimum labeled sample size is reached.",
        "dedupe_definition": "Conservative clustering uses title, normalized ingredients, instruction Jaccard/SimHash, author, canonical URL, and bounded perceptual image hashing. Cross-site review counts are never summed.",
        "robustness_definition": "Each production leaderboard is stress-tested across 36 nearby parameter configurations. Historical predictive backtests remain advisory and cannot automatically promote parameters.",
        "history_definition": "Immutable NDJSON is authoritative; DuckDB and Parquet are derived analytical/archival layers. Raw, clean, model, and serving contracts are independently versioned.",
    }

    write_quality_gate("output/quality_gate.json", quality_gate)
    _write_json("output/pipeline_metrics.json", pipeline_metrics)
    contracts = contract_manifest()
    write_contract_manifest("data/contracts.json")

    duplicate_groups = method.get("duplicate_groups", [])
    robustness = method.get("robustness", {})
    write_csv_outputs(
        "output",
        ranked,
        coverage,
        reliability,
        anomalies,
        source_health=source_health,
        robustness=robustness.get("simulations", []),
        dedupe_benchmark=dedupe_benchmark_rows,
        pipeline_metrics=pipeline_metric_rows,
        backtest=backtest,
        evidence_calibration=list(evidence_calibration.values()),
        evidence_label_results=evidence_label_results,
        quality_gate=quality_gate,
        dedupe_label_queue=dedupe_label_queue,
    )
    write_workbook(
        "output/air_fryer_rankings.xlsx",
        ranked,
        coverage,
        reliability,
        all_observations[-MAX_EXCEL_OBSERVATIONS:],
        anomalies,
        duplicate_groups,
        method_row,
        source_health=source_health,
        uncertainty_calibration=list(uncertainty_calibration.values()),
        robustness=robustness.get("simulations", []),
        dedupe_benchmark=dedupe_benchmark_rows,
        pipeline_metrics=pipeline_metric_rows,
        backtest=backtest,
        evidence_calibration=list(evidence_calibration.values()),
        evidence_label_results=evidence_label_results,
        quality_gate=quality_gate,
        dedupe_label_queue=dedupe_label_queue,
        storage_health=storage_health,
        contracts=contracts,
    )
    write_dashboard("docs", run_at, ranked, reliability, anomalies, method_row, len(sources))
    analytics_file = write_duckdb_cache(
        "output/air_fryer_analytics.duckdb",
        ranked=ranked,
        observations=all_observations,
        ranking_records=recent_rankings,
        source_health=source_health,
        source_reliability=reliability,
        anomalies=anomalies,
        calibration=uncertainty_calibration,
        robustness=robustness,
        dedupe_summary=dedupe_benchmark,
        dedupe_results=dedupe_benchmark_rows,
        pipeline_metrics=pipeline_metric_rows,
        backtest=backtest,
        evidence_calibration=evidence_calibration,
        evidence_label_results=evidence_label_results,
        quality_gate=quality_gate,
        storage_health=storage_health,
        contracts=contracts,
        dedupe_label_queue=dedupe_label_queue,
    )

    summary = {
        **method_row,
        **health_summary,
        "migration": migration_after,
        "media_enrichment": media_stats,
        "pipeline_metrics": pipeline_metrics,
        "quality_gate": quality_gate,
        "dedupe_benchmark": dedupe_benchmark,
        "evidence_calibration": evidence_calibration,
        "uncertainty_calibration": uncertainty_calibration,
        "backtest": {key: value for key, value in backtest.items() if key not in {"windows", "configurations"}},
        "storage_health": storage_health,
        "data_contracts": contracts,
        "robustness": {key: value for key, value in robustness.items() if key != "simulations"},
        "observation_file": observation_file,
        "anomaly_file": anomaly_file,
        "coverage_file": coverage_file,
        "ranking_snapshot_file": ranking_file,
        "analytics_cache_file": analytics_file,
        "history_archive_file": history_archive,
        "top10": ranked[:10],
        "coverage": coverage,
        "source_health": source_health,
        "source_reliability": reliability,
        "anomalies": anomalies[:100],
        "source_adjustments": method.get("source_adjustments", {}),
        "category_baselines": method.get("category_baselines", {}),
    }
    atomic_write_json("output/summary.json", summary, default=str)

    save_state(args.state, state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Incremental air-fryer recipe ranking pipeline")
    parser.add_argument("--sources", default="config/sources.yaml")
    parser.add_argument("--state", default="data/state.json")
    parser.add_argument("--model-config", default="config/model.yaml")
    parser.add_argument("--storage-config", default="config/storage.yaml")
    parser.add_argument("--slo-config", default="config/slo.yaml")
    parser.add_argument("--mode", choices=("hourly", "daily", "deep", "smoke", "backfill"), default="hourly")
    parser.add_argument("--max-urls", type=int, default=None, help="Per-source fetch cap override")
    parser.add_argument("--hourly-limit", type=int, default=100, help="Global hourly refresh target cap")
    parser.add_argument("--stale-days", type=int, default=14)
    args = parser.parse_args()
    with exclusive_lock(args.state):
        _run(args)


if __name__ == "__main__":
    main()
