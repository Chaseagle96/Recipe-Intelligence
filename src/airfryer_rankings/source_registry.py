from __future__ import annotations

import statistics
from copy import deepcopy
from pathlib import Path
from typing import Iterable

from .models import SourceConfig, now_iso
from .persistence import PersistenceValidationError, atomic_write_json, load_json_object
from .source_security import normalize_candidate_domain

SOURCE_REGISTRY_SCHEMA_VERSION = 1
SOURCE_GATE_VERSION = 2

DISCOVERED = "DISCOVERED"
CANDIDATE = "CANDIDATE"
QUARANTINED = "QUARANTINED"
QUALIFIED = "QUALIFIED"
PROMOTED = "PROMOTED"
ACTIVE = "ACTIVE"
DEGRADED = "DEGRADED"
SUSPENDED = "SUSPENDED"
REJECTED = "REJECTED"
BLOCKED = "BLOCKED"

EFFECTIVE_AUTO_STATES = {PROMOTED, ACTIVE, DEGRADED}
ALL_STATES = {
    DISCOVERED,
    CANDIDATE,
    QUARANTINED,
    QUALIFIED,
    PROMOTED,
    ACTIVE,
    DEGRADED,
    SUSPENDED,
    REJECTED,
    BLOCKED,
}


def empty_source_registry(vertical: str) -> dict:
    return {
        "schema_version": SOURCE_REGISTRY_SCHEMA_VERSION,
        "source_gate_version": SOURCE_GATE_VERSION,
        "vertical": vertical,
        "candidates": {},
        "manual_overrides": {},
        "audit": [],
    }


def _validate_source_registry_shape(payload: dict, target: Path) -> None:
    expected_containers = {
        "candidates": dict,
        "manual_overrides": dict,
        "audit": list,
    }
    for field, expected_type in expected_containers.items():
        value = payload.get(field)
        if value is not None and not isinstance(value, expected_type):
            raise PersistenceValidationError(
                f"Source registry field {field!r} must be {expected_type.__name__}: {target}"
            )

    for field in ("schema_version", "source_gate_version"):
        value = payload.get(field)
        if value is None:
            continue
        try:
            int(value)
        except (TypeError, ValueError) as exc:
            raise PersistenceValidationError(f"Source registry field {field!r} is invalid: {target}") from exc

    vertical = payload.get("vertical")
    if vertical is not None and not isinstance(vertical, str):
        raise PersistenceValidationError(f"Source registry field 'vertical' must be str: {target}")

    for domain, record in (payload.get("candidates") or {}).items():
        if not isinstance(record, dict):
            raise PersistenceValidationError(f"Source registry candidate {domain!r} must be an object: {target}")
    for domain, override in (payload.get("manual_overrides") or {}).items():
        if not isinstance(override, dict):
            raise PersistenceValidationError(f"Source registry manual override {domain!r} must be an object: {target}")
    for index, event in enumerate(payload.get("audit") or []):
        if not isinstance(event, dict):
            raise PersistenceValidationError(f"Source registry audit event {index} must be an object: {target}")


def load_source_registry(path: str | Path, vertical: str) -> dict:
    target = Path(path)
    if not target.exists():
        return empty_source_registry(vertical)

    payload = load_json_object(target)
    _validate_source_registry_shape(payload, target)
    payload.setdefault("schema_version", SOURCE_REGISTRY_SCHEMA_VERSION)
    payload.setdefault("source_gate_version", SOURCE_GATE_VERSION)
    payload.setdefault("vertical", vertical)
    payload.setdefault("candidates", {})
    payload.setdefault("manual_overrides", {})
    payload.setdefault("audit", [])
    return payload


def save_source_registry(path: str | Path, registry: dict) -> None:
    registry["schema_version"] = SOURCE_REGISTRY_SCHEMA_VERSION
    registry["source_gate_version"] = int(registry.get("source_gate_version") or SOURCE_GATE_VERSION)
    atomic_write_json(path, registry, default=str)


def _audit_event(
    registry: dict,
    *,
    domain: str,
    previous_state: str | None,
    new_state: str,
    reason: str,
    timestamp: str,
    metrics: dict | None = None,
    thresholds: dict | None = None,
    event: str | None = None,
) -> dict:
    payload = {
        "event": event or f"SOURCE_{new_state}",
        "domain": domain,
        "vertical": registry.get("vertical", ""),
        "previous_state": previous_state,
        "new_state": new_state,
        "reason": reason,
        "metrics": deepcopy(metrics or {}),
        "thresholds": deepcopy(thresholds or {}),
        "timestamp": timestamp,
        "source_gate_version": int(registry.get("source_gate_version") or SOURCE_GATE_VERSION),
    }
    registry.setdefault("audit", []).append(payload)
    return payload


def transition_source(
    registry: dict,
    domain: str,
    new_state: str,
    reason: str,
    *,
    timestamp: str | None = None,
    metrics: dict | None = None,
    thresholds: dict | None = None,
    event: str | None = None,
) -> dict:
    if new_state not in ALL_STATES:
        raise ValueError(f"Unknown source state: {new_state}")
    timestamp = timestamp or now_iso()
    normalized = normalize_candidate_domain(domain)
    if not normalized:
        raise ValueError(f"Invalid candidate domain: {domain!r}")
    candidates = registry.setdefault("candidates", {})
    record = candidates.setdefault(
        normalized,
        {
            "domain": normalized,
            "vertical": registry.get("vertical", ""),
            "origin": "discovered",
            "pinned": False,
            "status": DISCOVERED,
            "first_discovered_at": timestamp,
            "last_discovered_at": timestamp,
            "discovery_count": 0,
            "discovery_evidence": [],
            "qualification_attempts": 0,
            "consecutive_qualifying_attempts": 0,
            "consecutive_healthy_runs": 0,
            "consecutive_degraded_runs": 0,
            "quality_score": None,
            "promotion_eligible": False,
            "rediscovery_blocked": False,
            "crawl_config": {},
        },
    )
    previous = str(record.get("status") or DISCOVERED)
    record["status"] = new_state
    record["status_reason"] = reason
    record["updated_at"] = timestamp
    if metrics is not None:
        record["last_qualification_metrics"] = deepcopy(metrics)
        if metrics.get("quality_score") is not None:
            record["quality_score"] = float(metrics["quality_score"])
    if new_state == PROMOTED:
        record.setdefault("promoted_at", timestamp)
        record["promotion_eligible"] = True
        record["promotion_reason"] = reason
    elif new_state == ACTIVE:
        record.setdefault("activated_at", timestamp)
        record["suspension_reason"] = ""
    elif new_state == SUSPENDED:
        record["suspension_reason"] = reason
    elif new_state in {REJECTED, BLOCKED}:
        record["rejection_reason"] = reason
        record["promotion_eligible"] = False
    _audit_event(
        registry,
        domain=normalized,
        previous_state=previous,
        new_state=new_state,
        reason=reason,
        timestamp=timestamp,
        metrics=metrics,
        thresholds=thresholds,
        event=event,
    )
    return record


def record_candidate_discovery(
    registry: dict,
    *,
    domain: str,
    provider: str,
    query: str,
    discovery_url: str,
    timestamp: str,
    base_domains: set[str] | None = None,
) -> tuple[dict | None, bool]:
    normalized = normalize_candidate_domain(domain)
    if not normalized:
        return None, False
    if normalized in (base_domains or set()):
        return None, False
    override = registry.setdefault("manual_overrides", {}).get(normalized, {})
    candidates = registry.setdefault("candidates", {})
    existing = candidates.get(normalized)
    if bool(override.get("rediscovery_blocked")) or bool((existing or {}).get("rediscovery_blocked")):
        return existing, False

    is_new = existing is None
    if existing is None:
        existing = {
            "domain": normalized,
            "vertical": registry.get("vertical", ""),
            "origin": "discovered",
            "pinned": False,
            "status": DISCOVERED,
            "first_discovered_at": timestamp,
            "last_discovered_at": timestamp,
            "discovery_count": 0,
            "discovery_evidence": [],
            "qualification_attempts": 0,
            "consecutive_qualifying_attempts": 0,
            "consecutive_healthy_runs": 0,
            "consecutive_degraded_runs": 0,
            "quality_score": None,
            "promotion_eligible": False,
            "rediscovery_blocked": False,
            "crawl_config": {},
        }
        candidates[normalized] = existing
        _audit_event(
            registry,
            domain=normalized,
            previous_state=None,
            new_state=DISCOVERED,
            reason=f"discovered via {provider}",
            timestamp=timestamp,
            metrics={"provider": provider, "query": query, "url": discovery_url},
            event="SOURCE_DISCOVERED",
        )

    existing["last_discovered_at"] = timestamp
    existing["discovery_count"] = int(existing.get("discovery_count") or 0) + 1
    evidence = existing.setdefault("discovery_evidence", [])
    fingerprint = (provider, query, discovery_url)
    if not any((row.get("provider"), row.get("query"), row.get("url")) == fingerprint for row in evidence):
        evidence.append(
            {
                "provider": provider,
                "query": query,
                "url": discovery_url,
                "first_seen_at": timestamp,
            }
        )
    if existing.get("status") == DISCOVERED:
        transition_source(
            registry,
            normalized,
            CANDIDATE,
            "normalized and queued for source qualification",
            timestamp=timestamp,
            event="SOURCE_CANDIDATE",
        )
    return existing, is_new


def _source_config_from_record(record: dict, defaults: SourceConfig) -> SourceConfig:
    crawl = record.get("crawl_config") or {}
    delay_value = crawl.get("delay")
    if not isinstance(delay_value, (int, float, str)):
        delay_value = max(defaults.delay, 0.20)
    return SourceConfig(
        domain=str(record["domain"]),
        enabled=True,
        max_urls=int(crawl.get("max_urls") or defaults.max_urls),
        delay=float(delay_value),
        sitemap_urls=tuple(crawl.get("sitemap_urls") or ()),
        discovery_urls=tuple(crawl.get("discovery_urls") or ()),
        rating_selector=str(crawl.get("rating_selector") or ""),
        count_selector=str(crawl.get("count_selector") or ""),
        include_pattern=str(crawl.get("include_pattern") or defaults.include_pattern),
        allow_unmatched_discovery_links=bool(crawl.get("allow_unmatched_discovery_links", False)),
        origin="discovered",
        pinned=bool(record.get("pinned", False)),
    )


def effective_source_configs(base_sources: Iterable[SourceConfig], registry: dict) -> list[SourceConfig]:
    base = list(base_sources)
    base_domains = {cfg.domain for cfg in base}
    defaults = base[0] if base else SourceConfig(domain="example.invalid")
    output = list(base)
    overrides = registry.get("manual_overrides", {}) or {}
    for domain, record in sorted((registry.get("candidates", {}) or {}).items()):
        if domain in base_domains:
            continue
        override = overrides.get(domain, {}) if isinstance(overrides, dict) else {}
        if bool(override.get("blocked")):
            continue
        status = str(record.get("status") or "")
        manually_approved = str(override.get("decision") or "") == "approve"
        if status not in EFFECTIVE_AUTO_STATES and not manually_approved:
            continue
        output.append(_source_config_from_record(record, defaults))
    return output


def source_registry_summary(base_sources: Iterable[SourceConfig], registry: dict) -> dict:
    base = list(base_sources)
    candidates = list((registry.get("candidates", {}) or {}).values())
    status_counts: dict[str, int] = {}
    scores: list[float] = []
    for record in candidates:
        status = str(record.get("status") or "UNKNOWN")
        status_counts[status] = status_counts.get(status, 0) + 1
        score_value = record.get("quality_score")
        if isinstance(score_value, (int, float, str)):
            try:
                scores.append(float(score_value))
            except ValueError:
                pass
    effective = effective_source_configs(base, registry)
    auto_effective = [cfg for cfg in effective if cfg.origin == "discovered"]
    return {
        "source_gate_version": int(registry.get("source_gate_version") or SOURCE_GATE_VERSION),
        "manual_source_count": len(base),
        "auto_source_count": len(auto_effective),
        "effective_source_count": len(effective),
        "candidate_source_count": len(candidates),
        "candidate_status_counts": status_counts,
        "median_source_quality_score": statistics.median(scores) if scores else None,
    }


def apply_manual_override(
    registry: dict,
    domain: str,
    action: str,
    *,
    reason: str,
    timestamp: str | None = None,
) -> dict:
    timestamp = timestamp or now_iso()
    normalized = normalize_candidate_domain(domain)
    if not normalized:
        raise ValueError(f"Invalid domain: {domain!r}")
    action = action.strip().lower()
    overrides = registry.setdefault("manual_overrides", {})
    override = overrides.setdefault(normalized, {})
    override.update({"action": action, "reason": reason, "updated_at": timestamp})

    candidates = registry.setdefault("candidates", {})
    if normalized not in candidates:
        candidates[normalized] = {
            "domain": normalized,
            "vertical": registry.get("vertical", ""),
            "origin": "discovered",
            "pinned": False,
            "status": CANDIDATE,
            "first_discovered_at": timestamp,
            "last_discovered_at": timestamp,
            "discovery_count": 0,
            "discovery_evidence": [],
            "qualification_attempts": 0,
            "consecutive_qualifying_attempts": 0,
            "consecutive_healthy_runs": 0,
            "consecutive_degraded_runs": 0,
            "quality_score": None,
            "promotion_eligible": False,
            "rediscovery_blocked": False,
            "crawl_config": {},
        }
    record = candidates[normalized]

    if action == "approve":
        override.update({"decision": "approve", "blocked": False, "rediscovery_blocked": False})
        record["rediscovery_blocked"] = False
        return transition_source(
            registry,
            normalized,
            PROMOTED,
            f"manual approval: {reason}",
            timestamp=timestamp,
            event="SOURCE_PROMOTED",
        )
    if action == "reject":
        override.update({"decision": "reject", "rediscovery_blocked": True})
        record["rediscovery_blocked"] = True
        return transition_source(
            registry,
            normalized,
            REJECTED,
            f"manual rejection: {reason}",
            timestamp=timestamp,
            event="SOURCE_REJECTED",
        )
    if action == "block":
        override.update({"decision": "block", "blocked": True, "rediscovery_blocked": True})
        record["rediscovery_blocked"] = True
        return transition_source(
            registry,
            normalized,
            BLOCKED,
            f"manual block: {reason}",
            timestamp=timestamp,
            event="SOURCE_BLOCKED",
        )
    if action == "pin":
        override.update({"pinned": True})
        record["pinned"] = True
        if record.get("status") not in EFFECTIVE_AUTO_STATES:
            transition_source(
                registry,
                normalized,
                PROMOTED,
                f"manually pinned: {reason}",
                timestamp=timestamp,
                event="SOURCE_PROMOTED",
            )
        return record
    if action == "suspend":
        override.update({"decision": "suspend"})
        return transition_source(
            registry,
            normalized,
            SUSPENDED,
            f"manual suspension: {reason}",
            timestamp=timestamp,
            event="SOURCE_SUSPENDED",
        )
    if action == "restore":
        override.update({"decision": "restore", "blocked": False, "rediscovery_blocked": False})
        record["rediscovery_blocked"] = False
        return transition_source(
            registry,
            normalized,
            ACTIVE,
            f"manual restore: {reason}",
            timestamp=timestamp,
            event="SOURCE_RECOVERED",
        )
    raise ValueError(f"Unknown source override action: {action}")
