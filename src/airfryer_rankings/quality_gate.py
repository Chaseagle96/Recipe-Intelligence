from __future__ import annotations

import csv
import json
from pathlib import Path

import yaml


class PublishGateError(RuntimeError):
    pass


DEFAULT_QUALITY_GATE_POLICY = {
    "corpus_retention_fail_below": 0.80,
    "top50_overlap_warn_below": 0.50,
    "top50_overlap_fail_below": 0.35,
    "evidence_conflict_warn_above": 0.10,
    "evidence_conflict_fail_above": 0.20,
    "crawl_success_warn_below": 0.90,
    "crawl_success_fail_below": 0.70,
    "extract_success_warn_below": 0.90,
    "extract_success_fail_below": 0.70,
    "ranking_eligible_warn_below": 0.90,
    "dom_structure_change_warn_fraction": 0.10,
    "dom_structure_change_fail_fraction": 0.50,
    "dom_structure_change_warn_min": 5,
    "dom_structure_change_fail_min": 25,
    "anomaly_warn_fraction": 0.03,
    "anomaly_warn_min": 20,
    "stale_sources_24h_warn_above": 0,
    "http_429_warn_above": 3,
    "dedupe_spike_minimum": 20,
    "dedupe_spike_multiplier": 10,
    "dedupe_spike_offset": 5,
}


def load_quality_gate_policy(path: str | Path = "config/slo.yaml") -> dict:
    target = Path(path)
    if not target.exists():
        return dict(DEFAULT_QUALITY_GATE_POLICY)
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    raw = payload.get("quality_gate") or {}
    policy = dict(DEFAULT_QUALITY_GATE_POLICY)
    for key, default in DEFAULT_QUALITY_GATE_POLICY.items():
        value = raw.get(key, default)
        policy[key] = int(value) if isinstance(default, int) else float(value)
    return policy


def load_previous_serving_snapshot(output_dir: str | Path = "output") -> tuple[dict, list[dict]]:
    root = Path(output_dir)
    summary: dict = {}
    rankings: list[dict] = []
    summary_path = root / "summary.json"
    leaderboard_path = root / "leaderboard.csv"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            summary = {}
    if leaderboard_path.exists():
        try:
            with leaderboard_path.open(newline="", encoding="utf-8") as handle:
                rankings = list(csv.DictReader(handle))
        except Exception:
            rankings = []
    return summary, rankings


def evaluate_publish_gate(
    previous_summary: dict,
    previous_rankings: list[dict],
    ranked: list[dict],
    metrics: dict,
    *,
    mode: str,
    model_version: int,
    deduplicated_count: int,
    model_semver: str | None = None,
    policy: dict | None = None,
) -> dict:
    thresholds = dict(DEFAULT_QUALITY_GATE_POLICY)
    if policy:
        thresholds.update(policy)

    failures: list[str] = []
    warnings: list[str] = []
    details: dict = {}
    previous_count = int(previous_summary.get("ranked_recipes") or 0)
    current_count = len(ranked)
    details["previous_ranked_recipes"] = previous_count
    details["current_ranked_recipes"] = current_count
    if current_count == 0:
        failures.append("leaderboard is empty")
    elif previous_count >= 100:
        ratio = current_count / previous_count
        details["ranked_recipe_retention"] = ratio
        if ratio < float(thresholds["corpus_retention_fail_below"]):
            failures.append(f"ranked recipe count retained only {ratio:.1%} of previous production output")
    if previous_count >= 50 and current_count < 50:
        failures.append("Top 50 cannot be produced from current ranking")

    conflict_rate = metrics.get("evidence_conflict_rate")
    if conflict_rate is not None and float(conflict_rate) > float(thresholds["evidence_conflict_fail_above"]):
        failures.append(f"evidence conflict rate is catastrophically high at {float(conflict_rate):.1%}")
    elif conflict_rate is not None and float(conflict_rate) > float(thresholds["evidence_conflict_warn_above"]):
        warnings.append(f"evidence conflict rate elevated at {float(conflict_rate):.1%}")

    previous_model = int(previous_summary.get("model_version") or model_version)
    previous_semver = str(previous_summary.get("model_semver") or "").strip()
    current_semver = str(model_semver or "").strip()
    model_identity_changed = previous_model != model_version
    if current_semver:
        model_identity_changed = model_identity_changed or previous_semver != current_semver
    details["previous_model_version"] = previous_model
    details["current_model_version"] = model_version
    details["previous_model_semver"] = previous_semver or None
    details["current_model_semver"] = current_semver or None
    details["model_identity_changed"] = model_identity_changed

    previous_top50 = [str(row.get("recipe_id") or "") for row in previous_rankings[:50] if row.get("recipe_id")]
    current_top50 = [str(row.get("recipe_id") or "") for row in ranked[:50]]
    if previous_top50 and current_top50:
        overlap = len(set(previous_top50) & set(current_top50)) / min(len(previous_top50), len(current_top50))
        details["top50_overlap_previous"] = overlap
        fail_below = float(thresholds["top50_overlap_fail_below"])
        warn_below = float(thresholds["top50_overlap_warn_below"])
        if not model_identity_changed and overlap < fail_below:
            failures.append(f"Top-50 overlap collapsed to {overlap:.1%} without a model-version change")
        elif overlap < fail_below:
            warnings.append(f"model identity changed and Top-50 overlap is only {overlap:.1%}")
        elif not model_identity_changed and overlap < warn_below:
            warnings.append(f"Top-50 overlap fell to {overlap:.1%} without a model-version change")

    previous_deduplicated = int(previous_summary.get("deduplicated_count") or 0)
    details["previous_deduplicated_count"] = previous_deduplicated
    details["current_deduplicated_count"] = int(deduplicated_count)
    spike_threshold = max(
        int(thresholds["dedupe_spike_minimum"]),
        previous_deduplicated * int(thresholds["dedupe_spike_multiplier"]) + int(thresholds["dedupe_spike_offset"]),
    )
    if int(deduplicated_count) > spike_threshold:
        failures.append(
            f"deduplication count spiked to {deduplicated_count} from {previous_deduplicated}; threshold is {spike_threshold}"
        )

    crawl_success = metrics.get("crawl_success_rate")
    if crawl_success is not None:
        value = float(crawl_success)
        details["crawl_success_rate"] = value
        if value < float(thresholds["crawl_success_fail_below"]):
            failures.append(f"crawl success rate fell to {value:.1%}")
        elif value < float(thresholds["crawl_success_warn_below"]):
            warnings.append(f"crawl success rate is degraded at {value:.1%}")

    extract_success = metrics.get("extract_success_rate")
    if extract_success is not None:
        value = float(extract_success)
        details["extract_success_rate"] = value
        if value < float(thresholds["extract_success_fail_below"]):
            failures.append(f"extraction success rate fell to {value:.1%}")
        elif value < float(thresholds["extract_success_warn_below"]):
            warnings.append(f"extraction success rate is degraded at {value:.1%}")

    ranking_eligible = metrics.get("ranking_eligible_rate")
    if ranking_eligible is not None:
        value = float(ranking_eligible)
        details["ranking_eligible_rate"] = value
        if value < float(thresholds["ranking_eligible_warn_below"]):
            warnings.append(f"ranking eligibility rate is degraded at {value:.1%}")

    crawl_targets = max(1, int(metrics.get("crawl_targets") or 0))
    raw_dom_breaks = metrics.get("dom_structure_breaks")
    dom_breaks = int(raw_dom_breaks if raw_dom_breaks is not None else metrics.get("dom_structure_changes") or 0)
    dom_variances = int(metrics.get("dom_structure_variances") or 0)
    details["dom_structure_changes"] = dom_breaks
    details["dom_structure_breaks"] = dom_breaks
    details["dom_structure_variances"] = dom_variances
    details["crawl_targets"] = crawl_targets
    dom_warn = max(
        int(thresholds["dom_structure_change_warn_min"]),
        int(crawl_targets * float(thresholds["dom_structure_change_warn_fraction"])),
    )
    dom_fail = max(
        int(thresholds["dom_structure_change_fail_min"]),
        int(crawl_targets * float(thresholds["dom_structure_change_fail_fraction"])),
    )
    if dom_breaks >= dom_fail:
        failures.append(f"DOM structure changes broke extraction on {dom_breaks} across {crawl_targets} crawl targets")
    elif dom_breaks >= dom_warn:
        warnings.append(f"DOM structure changes broke extraction on {dom_breaks} across {crawl_targets} crawl targets")
    if dom_variances:
        warnings.append(f"tolerated layout variance on {dom_variances} crawl targets with recipe extraction preserved")

    anomaly_count = int(metrics.get("anomalies") or 0)
    details["anomalies"] = anomaly_count
    anomaly_warn = max(
        int(thresholds["anomaly_warn_min"]),
        int(max(1, current_count) * float(thresholds["anomaly_warn_fraction"])),
    )
    if anomaly_count >= anomaly_warn:
        warnings.append(f"anomaly volume is elevated at {anomaly_count}; review operational diagnostics")

    stale_24h = int(metrics.get("sources_stale_24h") or 0)
    details["sources_stale_24h"] = stale_24h
    if stale_24h > int(thresholds["stale_sources_24h_warn_above"]):
        warnings.append(f"{stale_24h} configured sources have not been checked successfully within 24 hours")

    if int(metrics.get("legacy_evidence_pending") or 0) > 0 and mode not in {"backfill", "smoke"}:
        warnings.append("legacy evidence remains pending outside explicit backfill mode")
    if int(metrics.get("http_429") or 0) > int(thresholds["http_429_warn_above"]):
        warnings.append("publisher throttling is elevated; crawl cadence may need reduction")

    return {
        "passed": not failures,
        "mode": mode,
        "model_version": model_version,
        "model_semver": current_semver or None,
        "failures": failures,
        "warnings": warnings,
        "metrics": details,
        "policy": thresholds,
    }


def write_quality_gate(path: str | Path, result: dict) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(target)


def assert_publishable(result: dict) -> None:
    if result.get("passed"):
        return
    failures = "; ".join(str(value) for value in result.get("failures", [])) or "unknown publication gate failure"
    raise PublishGateError(failures)
