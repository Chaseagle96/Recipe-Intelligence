from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from .models import parse_dt


def _rate(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return ordered[index]


def build_pipeline_metrics(
    state: dict,
    coverage: list[dict],
    rows: list,
    ranked: list[dict],
    anomalies: list[dict],
    source_health: list[dict],
    crawl_events: list[dict],
    targets: list[dict],
    run_at: str,
) -> tuple[dict, list[dict], list[dict]]:
    total_targets = len(targets)
    fetched = sum(int(row.get("fetched", 0) or 0) for row in coverage)
    not_modified = sum(int(row.get("not_modified", 0) or 0) for row in coverage)
    recognized = sum(int(row.get("recognized_recipes", 0) or 0) for row in coverage)
    verified = sum(int(row.get("verified_recipes", 0) or 0) for row in coverage)
    errors = sum(int(row.get("errors", 0) or 0) for row in coverage)
    conflicts = sum(1 for row in rows if getattr(row, "evidence_status", None) == "conflict")
    eligible_denominator = max(1, len(state.get("recipes", {})))
    transport_successes = fetched + not_modified
    extraction_rate = _rate(recognized, max(1, transport_successes))
    verification_rate = _rate(verified, max(1, transport_successes))
    latencies = [
        float(row.get("elapsed_seconds", 0.0)) / max(1, int(row.get("targets", 0) or 0))
        for row in coverage
        if int(row.get("targets", 0) or 0) > 0
    ]
    status_codes = [event.get("status") for event in crawl_events if event.get("status") is not None]
    now = parse_dt(run_at) or datetime.now(timezone.utc)
    recent_history = []
    for snapshot in state.get("source_history", []):
        timestamp = parse_dt(snapshot.get("run_at"))
        if timestamp and timestamp >= now - timedelta(hours=24):
            recent_history.append(snapshot)
    checked_24h: set[str] = set()
    successful_24h: set[str] = set()
    for snapshot in recent_history:
        for record in snapshot.get("coverage", []):
            status = str(record.get("status") or "")
            if status and status != "not_checked_this_run":
                checked_24h.add(str(record.get("source") or ""))
            if status == "ok":
                successful_24h.add(str(record.get("source") or ""))

    metrics: dict[str, Any] = {
        "generated_at": run_at,
        "crawl_targets": total_targets,
        "pages_fetched": fetched,
        "pages_not_modified": not_modified,
        "recognized_recipe_responses": recognized,
        "verified_recipe_responses": verified,
        "crawl_errors": errors,
        "crawl_success_rate": _rate(transport_successes, max(1, total_targets)),
        "fetch_success_rate": _rate(transport_successes, max(1, total_targets)),
        "extract_success_rate": extraction_rate,
        "recipe_verification_rate": verification_rate,
        "ranking_eligible_rate": _rate(len(ranked), eligible_denominator),
        "evidence_conflict_rate": _rate(conflicts, max(1, len(rows))),
        "robots_denials": sum(1 for event in crawl_events if event.get("type") == "robots_denied"),
        "http_403": sum(1 for status in status_codes if status == 403),
        "http_429": sum(1 for status in status_codes if status == 429),
        "mean_fetch_seconds_per_target": mean(latencies) if latencies else None,
        "p95_fetch_seconds_per_target": _percentile(latencies, 0.95),
        "configured_recipes": len(state.get("recipes", {})),
        "ranked_recipes": len(ranked),
        "anomalies": len(anomalies),
        "legacy_evidence_pending": int(state.get("migration", {}).get("legacy_evidence_pending") or 0),
        "sources_checked_24h": len(checked_24h),
        "sources_successful_24h": len(successful_24h),
        "sources_healthy_last_check": sum(1 for row in source_health if row.get("healthy_at_last_check")),
        "sources_stale_24h": sum(1 for row in source_health if not row.get("checked_within_24h")),
        "sources_stale_7d": sum(1 for row in source_health if not row.get("checked_within_7d")),
        "dom_structure_changes": sum(int(row.get("dom_structure_changes", 0) or 0) for row in coverage),
        "dom_structure_breaks": sum(int(row.get("dom_structure_breaks", 0) or 0) for row in coverage),
        "dom_structure_variances": sum(int(row.get("dom_structure_variances", 0) or 0) for row in coverage),
        "schema_structure_changes": sum(int(row.get("schema_structure_changes", 0) or 0) for row in coverage),
        "schema_structure_breaks": sum(int(row.get("schema_structure_breaks", 0) or 0) for row in coverage),
        "schema_structure_variances": sum(int(row.get("schema_structure_variances", 0) or 0) for row in coverage),
    }
    metric_rows = [
        {"metric": key, "value": value, "generated_at": run_at}
        for key, value in metrics.items()
        if key != "generated_at"
    ]
    alerts: list[dict] = []
    rules: tuple[tuple[str, float | None, float, str], ...] = (
        ("extract_success_rate", metrics["extract_success_rate"], 0.65, "below"),
        ("evidence_conflict_rate", metrics["evidence_conflict_rate"], 0.10, "above"),
        ("ranking_eligible_rate", metrics["ranking_eligible_rate"], 0.60, "below"),
    )
    for metric, value, threshold, direction in rules:
        if value is None:
            continue
        violated = value < threshold if direction == "below" else value > threshold
        if violated:
            alerts.append(
                {
                    "type": "observability_threshold",
                    "metric": metric,
                    "value": value,
                    "threshold": threshold,
                    "direction": direction,
                    "timestamp": run_at,
                }
            )
    if metrics["dom_structure_changes"]:
        alerts.append(
            {
                "type": "publisher_dom_contract_changes",
                "metric": "dom_structure_changes",
                "value": metrics["dom_structure_changes"],
                "threshold": 0,
                "direction": "above",
                "timestamp": run_at,
            }
        )
    if metrics["dom_structure_variances"]:
        alerts.append(
            {
                "type": "publisher_dom_variance_tolerated",
                "metric": "dom_structure_variances",
                "value": metrics["dom_structure_variances"],
                "threshold": 0,
                "direction": "above",
                "timestamp": run_at,
            }
        )
    if metrics["schema_structure_changes"]:
        alerts.append(
            {
                "type": "publisher_schema_contract_changes",
                "metric": "schema_structure_changes",
                "value": metrics["schema_structure_changes"],
                "threshold": 0,
                "direction": "above",
                "timestamp": run_at,
            }
        )
    if metrics["schema_structure_variances"]:
        alerts.append(
            {
                "type": "publisher_schema_variance_tolerated",
                "metric": "schema_structure_variances",
                "value": metrics["schema_structure_variances"],
                "threshold": 0,
                "direction": "above",
                "timestamp": run_at,
            }
        )
    return metrics, metric_rows, alerts
