from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean, pstdev

from ..model_config import ModelParameters
from .scoring import score_current


def spearman(base: dict[str, int], other: dict[str, int], recipe_ids: list[str]) -> float | None:
    pairs = [
        (base[recipe_id], other[recipe_id]) for recipe_id in recipe_ids if recipe_id in base and recipe_id in other
    ]
    if len(pairs) < 2:
        return None
    xs = [left for left, _ in pairs]
    ys = [right for _, right in pairs]
    mean_x = mean(xs)
    mean_y = mean(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    denominator = math.sqrt(sum((x - mean_x) ** 2 for x in xs) * sum((y - mean_y) ** 2 for y in ys))
    return numerator / denominator if denominator else 1.0


def kendall(base: dict[str, int], other: dict[str, int], recipe_ids: list[str]) -> float | None:
    ids = [recipe_id for recipe_id in recipe_ids if recipe_id in base and recipe_id in other]
    if len(ids) < 2:
        return None
    concordant = 0
    discordant = 0
    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            product = (base[left] - base[right]) * (other[left] - other[right])
            if product > 0:
                concordant += 1
            elif product < 0:
                discordant += 1
    total = concordant + discordant
    return (concordant - discordant) / total if total else 1.0


def robustness_lab(
    current: list[dict],
    baseline: list[dict],
    calibration: dict[str, dict] | None,
    active: ModelParameters,
) -> tuple[dict[str, dict], dict]:
    base_positions = {row["recipe_id"]: row["rank"] for row in baseline}
    positions: dict[str, list[int]] = defaultdict(list)
    top10_hits: dict[str, int] = defaultdict(int)
    top50_hits: dict[str, int] = defaultdict(int)
    simulations: list[dict] = []

    for max_bias in (0.10, active.max_source_bias, 0.20):
        for evidence_scale in (0.10, active.evidence_penalty_scale, 0.30):
            for volume_multiplier in (0.80, 1.20):
                for uncertainty_cap in (0.20, 0.30):
                    params = active.with_overrides(
                        max_source_bias=max_bias,
                        evidence_penalty_scale=evidence_scale,
                        volume_prior_multiplier=volume_multiplier,
                        uncertainty_cap=uncertainty_cap,
                    )
                    simulated, _ = score_current(current, calibration, params)
                    simulated_positions = {row["recipe_id"]: row["rank"] for row in simulated}
                    for recipe_id, rank in simulated_positions.items():
                        positions[recipe_id].append(rank)
                        if rank <= 10:
                            top10_hits[recipe_id] += 1
                        if rank <= 50:
                            top50_hits[recipe_id] += 1
                    top200_ids = [row["recipe_id"] for row in baseline[:200]]
                    top100_ids = [row["recipe_id"] for row in baseline[:100]]
                    simulations.append(
                        {
                            "max_source_bias": max_bias,
                            "evidence_penalty_scale": evidence_scale,
                            "volume_prior_multiplier": volume_multiplier,
                            "uncertainty_cap": uncertainty_cap,
                            "spearman_top200": spearman(base_positions, simulated_positions, top200_ids),
                            "kendall_top100": kendall(base_positions, simulated_positions, top100_ids),
                            "top10_overlap": len(
                                set(top200_ids[:10]) & {rid for rid, rank in simulated_positions.items() if rank <= 10}
                            )
                            / 10.0,
                            "top50_overlap": len(
                                set(top200_ids[:50]) & {rid for rid, rank in simulated_positions.items() if rank <= 50}
                            )
                            / 50.0,
                        }
                    )

    simulation_count = len(simulations)
    by_recipe: dict[str, dict] = {}
    for recipe_id, values in positions.items():
        base_rank = base_positions.get(recipe_id, values[0])
        stddev = pstdev(values) if len(values) > 1 else 0.0
        scale = max(2.0, base_rank * 0.10 + 2.0)
        confidence = max(0.0, min(1.0, 1.0 / (1.0 + stddev / scale)))
        by_recipe[recipe_id] = {
            "rank_confidence": confidence,
            "rank_stddev": stddev,
            "rank_range_low": min(values),
            "rank_range_high": max(values),
            "top10_frequency": top10_hits[recipe_id] / max(1, simulation_count),
            "top50_frequency": top50_hits[recipe_id] / max(1, simulation_count),
            "simulation_count": simulation_count,
        }

    spearman_values = [row["spearman_top200"] for row in simulations if row["spearman_top200"] is not None]
    kendall_values = [row["kendall_top100"] for row in simulations if row["kendall_top100"] is not None]
    return by_recipe, {
        "simulation_count": simulation_count,
        "mean_spearman_top200": mean(spearman_values) if spearman_values else None,
        "min_spearman_top200": min(spearman_values) if spearman_values else None,
        "mean_kendall_top100": mean(kendall_values) if kendall_values else None,
        "min_kendall_top100": min(kendall_values) if kendall_values else None,
        "mean_top10_overlap": mean(row["top10_overlap"] for row in simulations) if simulations else None,
        "mean_top50_overlap": mean(row["top50_overlap"] for row in simulations) if simulations else None,
        "simulations": simulations,
    }
