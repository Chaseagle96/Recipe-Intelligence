from __future__ import annotations

from pathlib import Path

from .calibration import evidence_grade
from .dedupe import dedupe_current
from .model_config import DEFAULT_MODEL_PARAMETERS, ModelParameters, load_model_config
from .models import load_sources, now_iso, parse_dt
from .ranking_components import eligible_current, rank_provenance, robustness_lab, score_current

MAX_SOURCE_BIAS = DEFAULT_MODEL_PARAMETERS.max_source_bias
EVIDENCE_CONFIDENCE_TARGET = DEFAULT_MODEL_PARAMETERS.evidence_confidence_target
EVIDENCE_PENALTY_SCALE = DEFAULT_MODEL_PARAMETERS.evidence_penalty_scale
DEFAULT_UNCERTAINTY_CAP = DEFAULT_MODEL_PARAMETERS.uncertainty_cap
SOURCE_PRIOR_STRENGTH = DEFAULT_MODEL_PARAMETERS.source_prior_strength
CATEGORY_PRIOR_STRENGTH = DEFAULT_MODEL_PARAMETERS.category_prior_strength


def _strict_source_patterns(model_config_path: str) -> dict[str, str]:
    """Resolve strict vertical patterns from the source config beside the model config."""

    source_path = Path(model_config_path).with_name("sources.yaml")
    try:
        sources = load_sources(source_path)
    except Exception:
        return {}
    return {
        source.domain.lower().strip(): source.include_pattern
        for source in sources
        if not source.allow_unmatched_discovery_links and source.include_pattern
    }


def bayesian_rank(
    state: dict,
    stale_days: int = 14,
    history_limit: int = 168,
    empirical_calibration: dict[str, dict] | None = None,
    historical_metrics: dict[str, dict] | None = None,
    model_params: ModelParameters | None = None,
    model_config_path: str = "config/model.yaml",
    allowed_sources: set[str] | None = None,
    required_source_patterns: dict[str, str] | None = None,
) -> tuple[list[dict], dict]:
    params, model_payload = load_model_config(model_config_path)
    if model_params is not None:
        params = model_params

    if allowed_sources is None:
        persisted_scope = state.get("effective_source_domains")
        if isinstance(persisted_scope, list):
            allowed_sources = {str(domain) for domain in persisted_scope if str(domain)}
    if required_source_patterns is None and allowed_sources is not None:
        required_source_patterns = _strict_source_patterns(model_config_path)

    current = eligible_current(
        state,
        stale_days,
        allowed_sources=allowed_sources,
        required_source_patterns=required_source_patterns,
    )
    current, deduplicated, duplicate_rows = dedupe_current(current, detailed=True)
    if not current:
        return [], {
            "global_prior": 0.0,
            "volume_prior_m": 0.0,
            "candidate_count": 0,
            "deduplicated_count": deduplicated,
            "source_adjustments": {},
            "category_baselines": {},
            "duplicate_groups": duplicate_rows,
            "robustness": {"simulation_count": 0},
            "model_version": int(model_payload.get("model_version", 5)),
            "active_parameters": params.to_dict(),
        }

    ranked, scored_method = score_current(current, empirical_calibration, params)
    robustness_by_recipe, robustness_summary = robustness_lab(current, ranked, empirical_calibration, params)

    previous_snapshot = state.get("rank_history", [])[-1] if state.get("rank_history") else None
    previous = {
        row["recipe_id"]: int(row["rank"])
        for row in (previous_snapshot or {}).get("top200", (previous_snapshot or {}).get("top50", []))
    }
    historical_metrics = historical_metrics or {}

    for row in ranked:
        item = row.pop("_source_item")
        recipe_id = row["recipe_id"]
        row.update(robustness_by_recipe.get(recipe_id, {}))
        row.update(historical_metrics.get(recipe_id, {}))
        raw_rating = float(row["rating"])
        previous_count = item.get("previous_rating_count")
        previous_seen = parse_dt(item.get("previous_seen_at"))
        current_seen = parse_dt(item.get("last_seen_at") or item.get("retrieved_at"))
        velocity = None
        if previous_count is not None and previous_seen and current_seen and current_seen > previous_seen:
            days = (current_seen - previous_seen).total_seconds() / 86400.0
            if days > 0:
                velocity = (int(row["rating_count"]) - int(previous_count)) / days
        row["rating_change"] = (
            None if item.get("previous_rating") is None else raw_rating - float(item["previous_rating"])
        )
        row["review_count_change"] = None if previous_count is None else int(row["rating_count"]) - int(previous_count)
        row["review_velocity_per_day"] = velocity
        row["evidence_grade"] = evidence_grade(item)
        row["previous_rank"] = previous.get(recipe_id)
        row["movement"] = previous[recipe_id] - row["rank"] if recipe_id in previous else None
        row["rank_provenance"] = rank_provenance(row)

        row["canonical_url"] = str(item.get("canonical_url") or item.get("url") or row.get("url") or "")
        row["image_url"] = str(item.get("image_url") or "")
        row["ingredients"] = list(item.get("ingredients") or [])
        row["has_instructions"] = bool(item.get("instructions"))
        row["instruction_count"] = len(item.get("instructions") or [])

        if recipe_id in state.get("recipes", {}):
            state["recipes"][recipe_id]["last_rank"] = row["rank"]

    run_at = now_iso()
    snapshot = {
        "run_at": run_at,
        "model_version": int(model_payload.get("model_version", 5)),
        "top200": [
            {
                "recipe_id": row["recipe_id"],
                "rank": row["rank"],
                "hierarchical_score": round(row["hierarchical_score"], 8),
                "rating": row["rating"],
                "rating_count": row["rating_count"],
                "rank_confidence": row.get("rank_confidence"),
            }
            for row in ranked[:200]
        ],
    }
    history = state.setdefault("rank_history", [])
    history.append(snapshot)
    if len(history) > history_limit:
        del history[:-history_limit]

    return ranked, {
        "global_prior": scored_method["global_prior"],
        "base_volume_prior_m": scored_method["base_volume_prior_m"],
        "volume_prior_m": scored_method["volume_prior_m"],
        "candidate_count": len(current),
        "deduplicated_count": deduplicated,
        "stale_days": stale_days,
        "history_snapshots": len(history),
        "category_baselines": scored_method["category_baselines"],
        "source_adjustments": scored_method["source_adjustments"],
        "duplicate_groups": duplicate_rows,
        "model_version": int(model_payload.get("model_version", 5)),
        "active_parameters": params.to_dict(),
        "robustness": robustness_summary,
        "formula": "hierarchical_score = BayesianPosterior(category-aware capped source adjustment) - calibrated uncertainty - evidence penalty",
    }
