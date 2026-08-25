from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import parse_dt

TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "share",
}


def normalize_discovered_url(url: str) -> str:
    """Canonicalize a discovered public URL without erasing semantic query state.

    Fragments and known tracking/share parameters are removed. Other query keys are
    preserved and deterministically sorted because some publishers legitimately use
    query parameters to identify content.
    """

    raw = str(url or "").strip()
    if not raw:
        return raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw.split("#", 1)[0]
    filtered = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        filtered.append((key, value))
    query = urlencode(sorted(filtered), doseq=True)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", query, ""))


def _earliest(left: str | None, right: str | None) -> str:
    values = [value for value in (left, right) if value]
    if not values:
        return ""
    dated = [(parse_dt(value), value) for value in values]
    valid = [(parsed, value) for parsed, value in dated if parsed is not None]
    if valid:
        return min(valid, key=lambda item: item[0])[1]
    return min(values)


def _latest(left: str | None, right: str | None) -> str:
    values = [value for value in (left, right) if value]
    if not values:
        return ""
    dated = [(parse_dt(value), value) for value in values]
    valid = [(parsed, value) for parsed, value in dated if parsed is not None]
    if valid:
        return max(valid, key=lambda item: item[0])[1]
    return max(values)


def _merge_catalog_entries(existing: dict[str, Any], incoming: dict[str, Any], normalized_url: str) -> dict[str, Any]:
    """Merge aliases conservatively, retaining the most useful crawl metadata."""

    existing_checked = parse_dt(str(existing.get("last_checked") or ""))
    incoming_checked = parse_dt(str(incoming.get("last_checked") or ""))
    preferred = (
        incoming if incoming_checked and (not existing_checked or incoming_checked >= existing_checked) else existing
    )
    other = existing if preferred is incoming else incoming
    merged = dict(other)
    merged.update(preferred)
    merged["url"] = normalized_url
    merged["first_discovered"] = _earliest(
        str(existing.get("first_discovered") or ""),
        str(incoming.get("first_discovered") or ""),
    )
    merged["last_discovered"] = _latest(
        str(existing.get("last_discovered") or ""),
        str(incoming.get("last_discovered") or ""),
    )
    merged["last_checked"] = _latest(
        str(existing.get("last_checked") or ""),
        str(incoming.get("last_checked") or ""),
    )
    for key in ("recipe_id", "etag", "last_modified", "page_hash", "dom_fingerprint", "schema_signature", "lastmod"):
        if not merged.get(key):
            merged[key] = existing.get(key) or incoming.get(key) or ""
    priority_order = {"contract_changed": 5, "modified": 4, "changed": 3, "new": 2, "stable": 1}
    priorities = [str(existing.get("priority") or ""), str(incoming.get("priority") or "")]
    merged["priority"] = max(priorities, key=lambda value: priority_order.get(value, 0))
    merged["missing_count"] = max(int(existing.get("missing_count") or 0), int(incoming.get("missing_count") or 0))
    return merged


def normalize_url_catalog(state: dict[str, Any]) -> dict[str, int]:
    """Coalesce tracking/share aliases already present in a persisted URL catalog."""

    raw_catalog = state.get("url_catalog", {}) or {}
    if not isinstance(raw_catalog, dict):
        return {"before": 0, "after": 0, "aliases_coalesced": 0, "urls_rewritten": 0}
    normalized_catalog: dict[str, dict[str, Any]] = {}
    rewritten = 0
    for key, raw_entry in raw_catalog.items():
        entry = dict(raw_entry) if isinstance(raw_entry, dict) else {"url": str(key)}
        original_url = str(entry.get("url") or key)
        normalized = normalize_discovered_url(original_url)
        if normalized != original_url or str(key) != normalized:
            rewritten += 1
        entry["url"] = normalized
        existing = normalized_catalog.get(normalized)
        normalized_catalog[normalized] = (
            _merge_catalog_entries(existing, entry, normalized) if existing is not None else entry
        )
    state["url_catalog"] = normalized_catalog
    before = len(raw_catalog)
    after = len(normalized_catalog)
    return {
        "before": before,
        "after": after,
        "aliases_coalesced": max(0, before - after),
        "urls_rewritten": rewritten,
    }
