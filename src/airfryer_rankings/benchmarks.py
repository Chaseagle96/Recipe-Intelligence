from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .dedupe import DEDUPE_THRESHOLD, candidate_duplicate_pairs, duplicate_similarity
from .models import instruction_simhash


def _prepare(recipe: dict) -> dict:
    item = dict(recipe)
    instructions = item.get("instructions") or []
    if instructions and not item.get("instruction_simhash"):
        item["instruction_simhash"] = instruction_simhash(instructions)
    return item


def _classification_metrics(rows: list[dict], threshold: float) -> dict:
    true_positive = false_positive = true_negative = false_negative = 0
    for row in rows:
        expected = bool(row["expected_duplicate"])
        predicted = float(row["similarity"]) >= threshold
        if expected and predicted:
            true_positive += 1
        elif expected:
            false_negative += 1
        elif predicted:
            false_positive += 1
        else:
            true_negative += 1
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else None
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "threshold": threshold,
        "true_positives": true_positive,
        "false_positives": false_positive,
        "true_negatives": true_negative,
        "false_negatives": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _threshold_curve(rows: list[dict]) -> list[dict]:
    return [_classification_metrics(rows, round(value / 100, 2)) for value in range(70, 96)]


def _group_metrics(rows: list[dict], threshold: float) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("kind") or "unspecified")].append(row)
    result = []
    for kind, values in sorted(grouped.items()):
        metrics = _classification_metrics(values, threshold)
        result.append({"kind": kind, "pairs": len(values), **metrics})
    return result


def evaluate_dedupe_benchmark(path: str | Path, threshold: float = DEDUPE_THRESHOLD) -> tuple[dict, list[dict]]:
    target = Path(path)
    if not target.exists():
        return {
            "benchmark_pairs": 0,
            "precision": None,
            "recall": None,
            "f1": None,
            "threshold": threshold,
            "threshold_curve": [],
            "by_kind": [],
        }, []
    payload = json.loads(target.read_text(encoding="utf-8"))
    pairs = payload.get("pairs", payload if isinstance(payload, list) else [])
    results = []
    for index, pair in enumerate(pairs, 1):
        left = _prepare(pair.get("left", {}))
        right = _prepare(pair.get("right", {}))
        expected = bool(pair.get("duplicate"))
        similarity = duplicate_similarity(left, right)
        predicted = similarity >= threshold
        outcome = "TP" if expected and predicted else "FN" if expected else "FP" if predicted else "TN"
        results.append(
            {
                "pair_id": pair.get("id", index),
                "kind": pair.get("kind", "unspecified"),
                "difficulty": pair.get("difficulty", "unspecified"),
                "expected_duplicate": expected,
                "predicted_duplicate": predicted,
                "similarity": similarity,
                "outcome": outcome,
                "left_title": left.get("title", ""),
                "right_title": right.get("title", ""),
                "note": pair.get("note", ""),
            }
        )
    metrics = _classification_metrics(results, threshold)
    positive_scores = [float(row["similarity"]) for row in results if row["expected_duplicate"]]
    negative_scores = [float(row["similarity"]) for row in results if not row["expected_duplicate"]]
    summary = {
        "benchmark_version": payload.get("version", 1) if isinstance(payload, dict) else 1,
        "benchmark_pairs": len(results),
        **metrics,
        "positive_score_min": min(positive_scores) if positive_scores else None,
        "positive_score_mean": sum(positive_scores) / len(positive_scores) if positive_scores else None,
        "negative_score_max": max(negative_scores) if negative_scores else None,
        "negative_score_mean": sum(negative_scores) / len(negative_scores) if negative_scores else None,
        "threshold_curve": _threshold_curve(results),
        "by_kind": _group_metrics(results, threshold),
        "target_adjudicated_pairs": int(payload.get("target_adjudicated_pairs", 500))
        if isinstance(payload, dict)
        else 500,
    }
    return summary, results


def build_dedupe_label_queue(
    recipes: list[dict],
    *,
    low: float = 0.72,
    high: float = 0.92,
    limit: int = 250,
) -> list[dict]:
    queue = []
    for left, right, similarity in candidate_duplicate_pairs(recipes, low=low, high=high, limit=limit):
        queue.append(
            {
                "pair_id": f"candidate-{left.get('recipe_id', '')[:8]}-{right.get('recipe_id', '')[:8]}",
                "similarity": similarity,
                "distance_from_production_threshold": abs(similarity - DEDUPE_THRESHOLD),
                "left_recipe_id": left.get("recipe_id"),
                "left_title": left.get("title"),
                "left_source": left.get("source"),
                "left_url": left.get("canonical_url") or left.get("url"),
                "right_recipe_id": right.get("recipe_id"),
                "right_title": right.get("title"),
                "right_source": right.get("source"),
                "right_url": right.get("canonical_url") or right.get("url"),
                "adjudicated_duplicate": "",
                "adjudicator": "",
                "adjudication_note": "",
            }
        )
    queue.sort(
        key=lambda row: (
            float(row.get("distance_from_production_threshold") or 0.0),
            -float(row.get("similarity") or 0.0),
        )
    )
    return queue
