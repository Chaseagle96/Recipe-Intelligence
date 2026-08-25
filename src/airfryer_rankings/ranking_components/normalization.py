from __future__ import annotations

import math
from collections import defaultdict

from ..model_config import ModelParameters
from ..models import categorize_recipe


def recipe_categories(item: dict) -> tuple[str, ...]:
    raw = item.get("categories")
    if isinstance(raw, str):
        values = tuple(value.strip() for value in raw.split("|") if value.strip())
        if values:
            return values
    if raw:
        return tuple(raw)
    return tuple(categorize_recipe(item.get("title", ""), item.get("ingredients", [])))


def category_baselines(current: list[dict], global_prior: float, params: ModelParameters) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in current:
        for category in recipe_categories(item):
            grouped[category].append(item)
    result: dict[str, dict] = {}
    for category, items in grouped.items():
        weights = [math.sqrt(max(1, int(item.get("rating_count", 1)))) for item in items]
        ratings = [float(item.get("normalized_rating", 0.0)) for item in items]
        raw_mean = sum(rating * weight for rating, weight in zip(ratings, weights, strict=True)) / max(
            1e-9, sum(weights)
        )
        count = float(len(items))
        strength = params.category_prior_strength
        shrunk = (count / (count + strength)) * raw_mean + (strength / (count + strength)) * global_prior
        result[category] = {"raw_mean": raw_mean, "shrunk_mean": shrunk, "recipe_count": int(count)}
    return result


def expected_category_rating(item: dict, baselines: dict[str, dict], global_prior: float) -> float:
    values = [float(baselines[name]["shrunk_mean"]) for name in recipe_categories(item) if name in baselines]
    return sum(values) / len(values) if values else global_prior


def source_adjustments(
    current: list[dict],
    global_prior: float,
    baselines: dict[str, dict],
    params: ModelParameters,
) -> dict[str, dict]:
    grouped: dict[str, list[tuple[dict, float]]] = defaultdict(list)
    for item in current:
        expected = expected_category_rating(item, baselines, global_prior)
        grouped[item.get("source", "")].append((item, expected))
    result: dict[str, dict] = {}
    for source, pairs in grouped.items():
        weights = [math.sqrt(max(1, int(item.get("rating_count", 1)))) for item, _ in pairs]
        ratings = [float(item.get("normalized_rating", 0.0)) for item, _ in pairs]
        residuals = [rating - expected for rating, (_, expected) in zip(ratings, pairs, strict=True)]
        raw_mean = sum(value * weight for value, weight in zip(ratings, weights, strict=True)) / max(1e-9, sum(weights))
        raw_residual = sum(value * weight for value, weight in zip(residuals, weights, strict=True)) / max(
            1e-9, sum(weights)
        )
        count = float(len(pairs))
        shrunk_residual = (count / (count + params.source_prior_strength)) * raw_residual
        bias = max(-params.max_source_bias, min(params.max_source_bias, shrunk_residual))
        result[source] = {
            "raw_mean": raw_mean,
            "raw_category_adjusted_bias": raw_residual,
            "shrunk_category_adjusted_bias": shrunk_residual,
            "bias": bias,
            "bias_capped": not math.isclose(shrunk_residual, bias, rel_tol=0.0, abs_tol=1e-12),
            "recipe_count": int(count),
        }
    return result
