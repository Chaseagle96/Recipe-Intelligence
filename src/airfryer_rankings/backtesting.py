from __future__ import annotations

import hashlib
import itertools
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from .model_config import ModelParameters
from .models import parse_dt
from .ranking_components import score_current, spearman

BASELINE_FAMILIES = ("raw_rating", "confidence_lcb", "simple_bayesian")


def _normalized_observations(observations: list[dict]) -> list[dict]:
    output: list[dict] = []
    for row in observations:
        timestamp = parse_dt(row.get("timestamp"))
        recipe_id = str(row.get("recipe_id") or "")
        rating_value = row.get("rating")
        count_value = row.get("rating_count")
        if not timestamp or not recipe_id or rating_value is None or count_value is None:
            continue
        try:
            rating = float(rating_value)
            rating_count = int(count_value)
        except (TypeError, ValueError):
            continue
        if rating_count <= 0 or not 0.0 <= rating <= 5.05:
            continue
        output.append({**row, "timestamp_dt": timestamp, "rating": rating, "rating_count": rating_count})
    return sorted(output, key=lambda row: row["timestamp_dt"])


def history_span_days(observations: list[dict]) -> float:
    rows = _normalized_observations(observations)
    if len(rows) < 2:
        return 0.0
    return max(0.0, (rows[-1]["timestamp_dt"] - rows[0]["timestamp_dt"]).total_seconds() / 86400.0)


def _asof_current(rows: list[dict], cutoff: datetime) -> list[dict]:
    latest: dict[str, dict] = {}
    for row in rows:
        if row["timestamp_dt"] > cutoff:
            break
        latest[row["recipe_id"]] = row
    current = []
    for row in latest.values():
        current.append(
            {
                "recipe_id": row["recipe_id"],
                "title": row.get("title") or row["recipe_id"],
                "source": row.get("source", ""),
                "url": row.get("url") or row.get("canonical_url", ""),
                "canonical_url": row.get("canonical_url") or row.get("url", ""),
                "normalized_rating": row["rating"],
                "rating_count": row["rating_count"],
                "evidence_confidence": float(row.get("evidence_confidence", 0.60)),
                "evidence_status": row.get("evidence_status", "schema_only"),
                "last_seen_at": row["timestamp_dt"].isoformat(),
                "categories": row.get("categories", []),
                "rating_histogram": row.get("rating_histogram", {}),
            }
        )
    return current


def _future_targets(
    rows: list[dict],
    cutoff: datetime,
    horizon_days: int,
    tolerance_days: int = 10,
    minimum_future_count: int = 250,
) -> dict[str, dict]:
    target = cutoff + timedelta(days=horizon_days)
    deadline = target + timedelta(days=tolerance_days)
    candidates: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        timestamp = row["timestamp_dt"]
        if target <= timestamp <= deadline and row["rating_count"] >= minimum_future_count:
            candidates[row["recipe_id"]].append(row)
    return {
        recipe_id: min(values, key=lambda row: abs((row["timestamp_dt"] - target).total_seconds()))
        for recipe_id, values in candidates.items()
    }


def _configuration_grid(
    model_payload: dict[str, Any], active: ModelParameters, max_configs: int = 96
) -> list[ModelParameters]:
    raw_grid = model_payload.get("backtest_grid", {}) or {}
    values = {
        "max_source_bias": raw_grid.get("max_source_bias", [active.max_source_bias]),
        "evidence_penalty_scale": raw_grid.get("evidence_penalty_scale", [active.evidence_penalty_scale]),
        "source_prior_strength": raw_grid.get("source_prior_strength", [active.source_prior_strength]),
        "category_prior_strength": raw_grid.get("category_prior_strength", [active.category_prior_strength]),
        "uncertainty_cap": raw_grid.get("uncertainty_cap", [active.uncertainty_cap]),
        "volume_prior_multiplier": raw_grid.get("volume_prior_multiplier", [1.0]),
    }
    names = list(values)
    raw_combinations = list(itertools.product(*(values[name] for name in names)))
    combinations: list[ModelParameters] = [active]
    for combo in raw_combinations:
        overrides = {name: float(value) for name, value in zip(names, combo, strict=True)}
        candidate = active.with_overrides(**overrides)
        if candidate not in combinations:
            combinations.append(candidate)
    if len(combinations) <= max_configs:
        return combinations
    ranked = sorted(
        combinations[1:],
        key=lambda item: hashlib.sha256(repr(sorted(item.to_dict().items())).encode()).hexdigest(),
    )
    step = max(1, len(ranked) // (max_configs - 1))
    sampled = ranked[::step][: max_configs - 1]
    return [active, *sampled]


def _config_id(params: ModelParameters) -> str:
    payload = repr(sorted(params.to_dict().items())).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _prediction_metrics(predicted_rows: list[dict], future: dict[str, dict]) -> dict | None:
    predicted = {row["recipe_id"]: row for row in predicted_rows if row["recipe_id"] in future}
    ids = list(predicted)
    if len(ids) < 5:
        return None
    future_order = sorted(
        ids,
        key=lambda recipe_id: (future[recipe_id]["rating"], future[recipe_id]["rating_count"]),
        reverse=True,
    )
    future_positions = {recipe_id: index for index, recipe_id in enumerate(future_order, 1)}
    predicted_positions = {recipe_id: int(predicted[recipe_id]["rank"]) for recipe_id in ids}
    rank_corr = spearman(future_positions, predicted_positions, ids)
    posterior_mae = mean(
        abs(float(predicted[recipe_id]["posterior_mean"]) - float(future[recipe_id]["rating"])) for recipe_id in ids
    )
    score_mae = mean(
        abs(float(predicted[recipe_id]["hierarchical_score"]) - float(future[recipe_id]["rating"])) for recipe_id in ids
    )
    top10_future = set(future_order[:10])
    top10_predicted = {recipe_id for recipe_id in ids if predicted_positions[recipe_id] <= 10}
    return {
        "recipes": len(ids),
        "spearman_future_quality": rank_corr,
        "posterior_mae": posterior_mae,
        "score_mae": score_mae,
        "top10_overlap": len(top10_future & top10_predicted) / max(1, min(10, len(top10_future))),
    }


def _evaluate_window(current: list[dict], future: dict[str, dict], params: ModelParameters) -> dict | None:
    if not current or not future:
        return None
    ranked, _ = score_current(current, calibration=None, params=params)
    return _prediction_metrics(ranked, future)


def _baseline_predictions(current: list[dict], family: str) -> list[dict]:
    if family not in BASELINE_FAMILIES or not current:
        return []
    ratings = [float(row["normalized_rating"]) for row in current]
    counts = [max(1, int(row["rating_count"])) for row in current]
    weights = [math.sqrt(count) for count in counts]
    global_prior = sum(rating * weight for rating, weight in zip(ratings, weights, strict=True)) / sum(weights)
    rows: list[dict] = []
    for item in current:
        rating = float(item["normalized_rating"])
        count = max(1, int(item["rating_count"]))
        if family == "raw_rating":
            posterior = rating
            score = rating
        elif family == "confidence_lcb":
            posterior = rating
            # A distribution-free lower-confidence analogue for bounded 1-5 star ratings.
            score = max(0.0, rating - min(0.50, 1.96 * 2.5 / math.sqrt(count)))
        else:
            m = 50.0
            posterior = (count / (count + m)) * rating + (m / (count + m)) * global_prior
            score = posterior
        rows.append(
            {
                "recipe_id": item["recipe_id"],
                "rating_count": count,
                "posterior_mean": posterior,
                "hierarchical_score": score,
            }
        )
    rows.sort(key=lambda row: (row["hierarchical_score"], math.log1p(row["rating_count"])), reverse=True)
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows


def _summarize_windows(window_results: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in window_results:
        grouped[row["config_id"]].append(row)
    summaries: list[dict] = []
    for config_id, values in grouped.items():
        correlations = [row["spearman_future_quality"] for row in values if row["spearman_future_quality"] is not None]
        summaries.append(
            {
                "config_id": config_id,
                "model_family": values[0].get("model_family", "hierarchical"),
                "is_active": bool(values[0].get("is_active")),
                "windows": len(values),
                "total_recipe_evaluations": sum(int(row["recipes"]) for row in values),
                "mean_spearman_future_quality": mean(correlations) if correlations else None,
                "mean_posterior_mae": mean(float(row["posterior_mae"]) for row in values),
                "mean_score_mae": mean(float(row["score_mae"]) for row in values),
                "mean_top10_overlap": mean(float(row["top10_overlap"]) for row in values),
                "parameters": values[0].get("parameters", {}),
            }
        )
    return summaries


def run_historical_backtest(
    observations: list[dict],
    active: ModelParameters,
    model_payload: dict[str, Any],
    *,
    horizons: tuple[int, ...] = (30, 60, 90),
    max_windows: int = 6,
) -> dict:
    rows = _normalized_observations(observations)
    span_days = history_span_days(observations)
    policy = model_payload.get("promotion_policy", {}) or {}
    minimum_history_days = int(policy.get("minimum_history_days", 30))
    minimum_windows = int(policy.get("minimum_backtest_windows", 4))
    minimum_recipes = int(policy.get("minimum_backtest_recipes", 75))
    if len(rows) < 2 or span_days < minimum_history_days:
        return {
            "ready": False,
            "history_span_days": span_days,
            "minimum_history_days": minimum_history_days,
            "windows": [],
            "configurations": [],
            "baseline_configurations": [],
            "recommendation": None,
            "reason": "insufficient_longitudinal_history",
        }

    first = rows[0]["timestamp_dt"]
    last = rows[-1]["timestamp_dt"]
    candidate_cutoffs: list[tuple[datetime, int]] = []
    for horizon in horizons:
        latest_cutoff = last - timedelta(days=horizon + 2)
        earliest_cutoff = first + timedelta(days=min(14, max(1, horizon // 3)))
        if latest_cutoff <= earliest_cutoff:
            continue
        count = min(max_windows, max(1, int((latest_cutoff - earliest_cutoff).days // 7) + 1))
        for index in range(count):
            fraction = index / max(1, count - 1)
            cutoff = earliest_cutoff + (latest_cutoff - earliest_cutoff) * fraction
            candidate_cutoffs.append((cutoff, horizon))

    configurations = _configuration_grid(model_payload, active)
    window_results: list[dict] = []
    baseline_results: list[dict] = []
    for cutoff, horizon in candidate_cutoffs:
        current = _asof_current(rows, cutoff)
        future = _future_targets(rows, cutoff, horizon)
        for params in configurations:
            metrics = _evaluate_window(current, future, params)
            if not metrics:
                continue
            window_results.append(
                {
                    "cutoff": cutoff.isoformat(),
                    "horizon_days": horizon,
                    "config_id": _config_id(params),
                    "model_family": "hierarchical",
                    "is_active": params == active,
                    "parameters": params.to_dict(),
                    **metrics,
                }
            )
        for family in BASELINE_FAMILIES:
            metrics = _prediction_metrics(_baseline_predictions(current, family), future)
            if not metrics:
                continue
            baseline_results.append(
                {
                    "cutoff": cutoff.isoformat(),
                    "horizon_days": horizon,
                    "config_id": f"baseline:{family}",
                    "model_family": family,
                    "is_active": False,
                    "parameters": {},
                    **metrics,
                }
            )

    summaries = _summarize_windows(window_results)
    baseline_summaries = _summarize_windows(baseline_results)
    eligible = [
        row
        for row in summaries
        if row["windows"] >= minimum_windows and row["total_recipe_evaluations"] >= minimum_recipes
    ]
    eligible.sort(
        key=lambda row: (
            -(row["mean_spearman_future_quality"] if row["mean_spearman_future_quality"] is not None else -1.0),
            row["mean_posterior_mae"],
            -row["mean_top10_overlap"],
        )
    )
    recommendation = eligible[0] if eligible else None
    all_summaries = [*summaries, *baseline_summaries]
    all_summaries.sort(
        key=lambda row: (
            -(row["mean_spearman_future_quality"] if row["mean_spearman_future_quality"] is not None else -1.0),
            row["mean_posterior_mae"],
        )
    )
    best_model_family = all_summaries[0]["model_family"] if all_summaries else None
    return {
        "ready": bool(recommendation),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "history_span_days": span_days,
        "minimum_history_days": minimum_history_days,
        "minimum_windows": minimum_windows,
        "minimum_recipes": minimum_recipes,
        "automatic_parameter_promotion": bool(policy.get("automatic_parameter_promotion", False)),
        "windows": [*window_results, *baseline_results],
        "configurations": all_summaries,
        "baseline_configurations": baseline_summaries,
        "best_observed_model_family": best_model_family,
        # Recommendation is deliberately restricted to hierarchical parameter sets.
        # Baseline families are comparison controls, never automatic promotion targets.
        "recommendation": recommendation,
        "reason": None if recommendation else "insufficient_evaluable_windows",
    }
