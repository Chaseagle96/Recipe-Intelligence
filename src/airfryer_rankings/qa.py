from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from .models import RecipeRow, parse_dt


def detect_anomalies(
    state: dict, rows: Iterable[RecipeRow], coverage: Iterable[dict], events: Iterable[dict], run_at: str
) -> list[dict]:
    anomalies: list[dict] = []
    for row in rows:
        stored = state.get("recipes", {}).get(row.recipe_id, {})
        previous_rating = stored.get("previous_rating")
        previous_count = stored.get("previous_rating_count")
        if previous_count is not None:
            delta = int(row.rating_count) - int(previous_count)
            if delta < 0:
                anomalies.append(
                    {
                        "timestamp": run_at,
                        "severity": "high",
                        "type": "review_count_decrease",
                        "recipe_id": row.recipe_id,
                        "title": row.title,
                        "source": row.source,
                        "url": row.canonical_url or row.url,
                        "detail": f"{previous_count} -> {row.rating_count}",
                    }
                )
            elif int(previous_count) > 0 and delta > max(250, int(previous_count) * 0.50):
                previous_seen = parse_dt(stored.get("previous_seen_at"))
                current_seen = parse_dt(run_at)
                hours = (
                    (current_seen - previous_seen).total_seconds() / 3600 if previous_seen and current_seen else None
                )
                anomaly_type = (
                    "hourly_review_count_spike" if hours is not None and hours <= 2.0 else "review_count_spike"
                )
                anomalies.append(
                    {
                        "timestamp": run_at,
                        "severity": "medium",
                        "type": anomaly_type,
                        "recipe_id": row.recipe_id,
                        "title": row.title,
                        "source": row.source,
                        "url": row.canonical_url or row.url,
                        "detail": f"+{delta} reviews" + (f" in {hours:.1f}h" if hours is not None else ""),
                    }
                )
        if previous_rating is not None and abs(row.normalized_rating - float(previous_rating)) >= 0.25:
            anomalies.append(
                {
                    "timestamp": run_at,
                    "severity": "medium",
                    "type": "rating_shift",
                    "recipe_id": row.recipe_id,
                    "title": row.title,
                    "source": row.source,
                    "url": row.canonical_url or row.url,
                    "detail": f"{float(previous_rating):.2f} -> {row.normalized_rating:.2f}",
                }
            )
        if row.evidence_status == "conflict" or row.evidence_confidence < 0.60:
            anomalies.append(
                {
                    "timestamp": run_at,
                    "severity": "high",
                    "type": "evidence_conflict",
                    "recipe_id": row.recipe_id,
                    "title": row.title,
                    "source": row.source,
                    "url": row.canonical_url or row.url,
                    "detail": f"confidence={row.evidence_confidence:.2f}",
                }
            )

    canonical_map: dict[str, list[dict]] = defaultdict(list)
    for recipe in state.get("recipes", {}).values():
        canonical = recipe.get("canonical_url") or recipe.get("url")
        if canonical:
            canonical_map[canonical].append(recipe)
    for canonical, group in canonical_map.items():
        recipe_ids = {item.get("recipe_id") for item in group}
        if len(recipe_ids) > 1:
            anomalies.append(
                {
                    "timestamp": run_at,
                    "severity": "medium",
                    "type": "canonical_collision",
                    "recipe_id": "",
                    "title": "",
                    "source": " | ".join(sorted({item.get("source", "") for item in group})),
                    "url": canonical,
                    "detail": f"{len(recipe_ids)} recipe IDs share canonical URL",
                }
            )

    for item in coverage:
        if item.get("status") not in ("ok", None, "not_checked_this_run"):
            anomalies.append(
                {
                    "timestamp": run_at,
                    "severity": "high",
                    "type": "source_failure",
                    "recipe_id": "",
                    "title": "",
                    "source": item.get("source", ""),
                    "url": "",
                    "detail": str(item.get("status")),
                }
            )
    tracked_events = {
        "recipe_disappeared",
        "malformed_rating_scale",
        "rating_evidence_conflict",
        "fetch_error",
        "dom_structure_changed",
        "schema_structure_changed",
    }
    for event in events:
        event_type = event.get("type")
        if event_type not in tracked_events:
            continue
        high_severity = {"recipe_disappeared", "rating_evidence_conflict", "schema_structure_changed"}
        anomalies.append(
            {
                "timestamp": run_at,
                "severity": "high" if event_type in high_severity else "medium",
                "type": event_type,
                "recipe_id": "",
                "title": "",
                "source": event.get("source", ""),
                "url": event.get("url", ""),
                "detail": str(event.get("status") or event.get("error") or "publisher markup contract changed"),
            }
        )

    history = state.setdefault("anomaly_history", [])
    history.extend(anomalies)
    if len(history) > 2000:
        del history[:-2000]
    return anomalies


def temporal_anomalies(ranked: Iterable[dict], run_at: str) -> list[dict]:
    anomalies = []
    for row in ranked:
        if row.get("rating_change_point_30d"):
            anomalies.append(
                {
                    "timestamp": run_at,
                    "severity": "medium",
                    "type": "rating_change_point",
                    "recipe_id": row.get("recipe_id", ""),
                    "title": row.get("title", ""),
                    "source": row.get("source", ""),
                    "url": row.get("url", ""),
                    "detail": f"recent-vs-prior mean shift {float(row.get('rating_change_point_delta') or 0.0):+.3f}",
                }
            )
    return anomalies


def _last_checked_by_source(state: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for run in state.get("source_history", []):
        run_at = parse_dt(run.get("run_at"))
        if not run_at:
            continue
        for row in run.get("coverage", []):
            source = row.get("source", "")
            status = row.get("status")
            if not source or status == "not_checked_this_run":
                continue
            previous = result.get(source)
            if previous is None or run_at > previous["timestamp"]:
                result[source] = {"timestamp": run_at, "status": status or "ok"}
    return result


def source_health_summary(
    state: dict, coverage: Iterable[dict], configured_sources: Iterable, run_at: str
) -> tuple[list[dict], dict]:
    now = parse_dt(run_at) or datetime.now(timezone.utc)
    current = {item.get("source", ""): item for item in coverage}
    last_checked = _last_checked_by_source(state)
    rows: list[dict] = []

    for cfg in configured_sources:
        source = getattr(cfg, "domain", str(cfg))
        current_row = current.get(source, {})
        checked_this_run = current_row.get("status") != "not_checked_this_run" and bool(current_row)
        current_success = checked_this_run and current_row.get("status") == "ok"
        current_degraded = checked_this_run and current_row.get("status") not in {"ok", None}
        latest = last_checked.get(source)
        latest_time = latest.get("timestamp") if latest else None
        hours_since = (now - latest_time).total_seconds() / 3600.0 if latest_time else None
        last_status = latest.get("status") if latest else "never_checked"
        healthy_last_check = last_status == "ok"
        rows.append(
            {
                "source": source,
                "checked_this_run": bool(checked_this_run),
                "successful_this_run": bool(current_success),
                "degraded_this_run": bool(current_degraded),
                "last_checked_at": latest_time.isoformat() if latest_time else None,
                "hours_since_last_check": hours_since,
                "healthy_at_last_check": healthy_last_check,
                "last_check_status": last_status,
                "checked_within_24h": hours_since is not None and hours_since <= 24,
                "checked_within_7d": hours_since is not None and hours_since <= 168,
                "current_targets": current_row.get("targets"),
                "current_verified": current_row.get("verified_recipes"),
                "current_dom_structure_changes": current_row.get("dom_structure_changes", 0),
                "current_schema_structure_changes": current_row.get("schema_structure_changes", 0),
            }
        )

    total = len(rows)
    checked = sum(1 for row in rows if row["checked_this_run"])
    success = sum(1 for row in rows if row["successful_this_run"])
    degraded = sum(1 for row in rows if row["degraded_this_run"])
    healthy = sum(1 for row in rows if row["healthy_at_last_check"])
    within_24h = sum(1 for row in rows if row["checked_within_24h"])
    within_7d = sum(1 for row in rows if row["checked_within_7d"])
    summary = {
        "sources_configured": total,
        "sources_checked_this_run": checked,
        "sources_successful_this_run": success,
        "sources_degraded_this_run": degraded,
        "sources_healthy_at_last_check": healthy,
        "sources_checked_within_24h": within_24h,
        "sources_checked_within_7d": within_7d,
        "corpus_coverage_freshness_24h": within_24h / total if total else None,
        "corpus_coverage_freshness_7d": within_7d / total if total else None,
    }
    return rows, summary


def source_reliability(state: dict, coverage: Iterable[dict], method: dict) -> list[dict]:
    current_coverage = {item.get("source", ""): item for item in coverage}
    history = state.get("source_history", [])[-30:]
    sources = set(current_coverage)
    for run in history:
        sources.update(item.get("source", "") for item in run.get("coverage", []))
    sources.update(item.get("source", "") for item in state.get("recipes", {}).values())
    adjustments = method.get("source_adjustments", {})
    anomaly_history = state.get("anomaly_history", [])[-1000:]
    last_checked = _last_checked_by_source(state)
    now = datetime.now(timezone.utc)
    result = []
    for source in sorted(value for value in sources if value):
        run_rows = [
            item
            for run in history
            for item in run.get("coverage", [])
            if item.get("source") == source and item.get("status") != "not_checked_this_run"
        ]
        successful = sum(1 for item in run_rows if item.get("status") == "ok")
        recipe_rows = [item for item in state.get("recipes", {}).values() if item.get("source") == source]
        confidences = [float(item.get("evidence_confidence", 0.0)) for item in recipe_rows]
        anomaly_count = sum(1 for item in anomaly_history if item.get("source") == source)
        current = current_coverage.get(source, {})
        adjustment = adjustments.get(source, {})
        latest = last_checked.get(source)
        latest_time = latest.get("timestamp") if latest else None
        hours_since = (now - latest_time).total_seconds() / 3600.0 if latest_time else None
        result.append(
            {
                "source": source,
                "runs_sampled": len(run_rows),
                "run_success_rate": successful / len(run_rows) if run_rows else None,
                "known_recipes": len(recipe_rows),
                "mean_evidence_confidence": sum(confidences) / len(confidences) if confidences else None,
                "legacy_evidence_pending": sum(1 for item in recipe_rows if item.get("needs_evidence_backfill")),
                "anomalies_recent": anomaly_count,
                "source_rating_mean": adjustment.get("raw_mean"),
                "category_adjusted_source_bias": adjustment.get("bias"),
                "last_checked_at": latest_time.isoformat() if latest_time else None,
                "hours_since_last_check": hours_since,
                "healthy_at_last_check": latest.get("status") == "ok" if latest else False,
                "current_targets": current.get("targets"),
                "current_verified": current.get("verified_recipes"),
                "current_status": current.get("status", "not_checked"),
                "current_dom_structure_changes": current.get("dom_structure_changes", 0),
                "current_schema_structure_changes": current.get("schema_structure_changes", 0),
            }
        )
    return result
