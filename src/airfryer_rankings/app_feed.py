from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import parse_dt
from .runtime import vertical_name, vertical_slug

APP_FEED_SCHEMA_VERSION = 1
DEFAULT_PAGE_SIZE = 100


def _categories(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split("|") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _ingredients(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(part) for part in value if str(part).strip()]


def _recipe_payload(row: dict) -> dict:
    return {
        "recipe_id": str(row.get("recipe_id") or ""),
        "vertical_id": vertical_slug(),
        "vertical_name": vertical_name(),
        "title": str(row.get("title") or ""),
        "source": str(row.get("source") or ""),
        "combined_sources": str(row.get("combined_sources") or row.get("source") or ""),
        "url": str(row.get("url") or ""),
        "canonical_url": str(row.get("canonical_url") or row.get("url") or ""),
        "image_url": str(row.get("image_url") or ""),
        "author": str(row.get("author") or ""),
        "categories": _categories(row.get("categories")),
        "ingredients": _ingredients(row.get("ingredients")),
        "has_instructions": bool(row.get("has_instructions", bool(row.get("instructions")))),
        "instruction_count": int(row.get("instruction_count") or len(row.get("instructions") or [])),
        "rank": int(row.get("rank") or 0),
        "rating": float(row.get("rating", row.get("normalized_rating")) or 0.0),
        "rating_count": int(row.get("rating_count") or 0),
        "hierarchical_score": float(row.get("hierarchical_score") or 0.0),
        "evidence_confidence": float(row.get("evidence_confidence") or 0.0),
        "evidence_grade": str(row.get("evidence_grade") or ""),
        "evidence_status": str(row.get("evidence_status") or ""),
        "rank_confidence": float(row.get("rank_confidence") or 0.0),
        "rank_range_low": row.get("rank_range_low"),
        "rank_range_high": row.get("rank_range_high"),
        "rank_provenance": str(row.get("rank_provenance") or ""),
        "last_seen_at": str(row.get("last_seen_at") or row.get("retrieved_at") or ""),
        "duplicate_group_id": str(row.get("duplicate_group_id") or ""),
        "duplicate_confidence": float(row.get("duplicate_confidence") or 0.0),
    }


def build_app_recipe(row: dict) -> dict:
    """Project a ranked recipe into the public mobile-serving contract.

    Publisher instruction prose is intentionally not republished. The clean state
    may retain it for research/dedupe purposes, but mobile clients receive only a
    factual availability/count signal plus the canonical source URL.
    """
    payload = _recipe_payload(row)
    payload.update(
        {
            "is_ranked": True,
            "discover_eligible": True,
            "explore_eligible": True,
            "serveability": "discover",
            "status_reasons": [],
            "duplicate_representative_recipe_id": payload["recipe_id"],
        }
    )
    return payload


def _duplicate_membership(duplicate_groups: list[dict] | None) -> dict[str, dict]:
    membership: dict[str, dict] = {}
    for row in duplicate_groups or []:
        recipe_id = str(row.get("recipe_id") or "")
        if not recipe_id:
            continue
        membership[recipe_id] = {
            "duplicate_group_id": str(row.get("duplicate_group_id") or ""),
            "duplicate_confidence": float(row.get("confidence") or 0.0),
            "representative_recipe_id": str(row.get("representative_recipe_id") or ""),
        }
    return membership


def _is_fresh(item: dict, as_of: datetime, stale_days: int) -> bool:
    observed = parse_dt(item.get("last_seen_at") or item.get("retrieved_at"))
    return bool(observed and observed >= as_of - timedelta(days=stale_days))


def build_corpus_recipe(
    item: dict,
    ranked_by_id: dict[str, dict],
    duplicate_membership: dict[str, dict],
    *,
    as_of: datetime,
    stale_days: int,
) -> dict:
    """Project one normalized state recipe into the complete mobile corpus.

    The corpus is intentionally broader than the leaderboard. Status metadata lets
    clients distinguish globally ranked Discover candidates from exploratory,
    stale/archive, duplicate-alias, or otherwise suppressed records without losing
    the underlying Recipe Intelligence work.
    """
    recipe_id = str(item.get("recipe_id") or "")
    ranked_row = ranked_by_id.get(recipe_id)
    source = ranked_row or item
    payload = _recipe_payload(source)

    # Ranked rows carry scoring metadata, while clean-state rows carry the richest
    # content fields. Fill content from state without overwriting ranked statistics.
    payload["canonical_url"] = str(item.get("canonical_url") or item.get("url") or payload["canonical_url"])
    payload["image_url"] = str(item.get("image_url") or payload["image_url"])
    payload["author"] = str(item.get("author") or payload["author"])
    payload["ingredients"] = _ingredients(item.get("ingredients")) or payload["ingredients"]
    payload["has_instructions"] = bool(item.get("instructions"))
    payload["instruction_count"] = len(item.get("instructions") or [])
    payload["last_seen_at"] = str(item.get("last_seen_at") or item.get("retrieved_at") or payload["last_seen_at"])

    duplicate = duplicate_membership.get(recipe_id, {})
    if duplicate:
        payload["duplicate_group_id"] = duplicate["duplicate_group_id"]
        payload["duplicate_confidence"] = duplicate["duplicate_confidence"]
    representative_id = str(duplicate.get("representative_recipe_id") or recipe_id)
    is_duplicate_alias = bool(duplicate and representative_id and representative_id != recipe_id)

    is_ranked = ranked_row is not None
    fresh = _is_fresh(item, as_of, stale_days)
    evidence_confidence = float(item.get("evidence_confidence", payload["evidence_confidence"]) or 0.0)
    evidence_status = str(item.get("evidence_status") or payload["evidence_status"])
    rating_count = int(item.get("rating_count", payload["rating_count"]) or 0)
    title_ok = bool(str(item.get("title") or payload["title"]).strip())
    url_ok = bool(payload["canonical_url"].strip())

    reasons: list[str] = []
    if not fresh:
        reasons.append("stale")
    if rating_count <= 0:
        reasons.append("no_rating_evidence")
    if evidence_confidence < 0.60:
        reasons.append("low_evidence")
    if evidence_status == "conflict":
        reasons.append("evidence_conflict")
    if not title_ok:
        reasons.append("missing_title")
    if not url_ok:
        reasons.append("missing_source_url")
    if is_duplicate_alias:
        reasons.append("duplicate_alias")

    explore_eligible = bool(title_ok and url_ok and fresh and evidence_status != "conflict" and not is_duplicate_alias)
    if is_ranked:
        serveability = "discover"
    elif is_duplicate_alias or evidence_status == "conflict" or not title_ok or not url_ok:
        serveability = "suppressed"
    elif not fresh:
        serveability = "archive"
    else:
        serveability = "explore"

    payload.update(
        {
            "is_ranked": is_ranked,
            "discover_eligible": is_ranked,
            "explore_eligible": explore_eligible or is_ranked,
            "serveability": serveability,
            "status_reasons": reasons,
            "duplicate_representative_recipe_id": representative_id,
        }
    )
    return payload


def _write_pages(root: Path, dirname: str, generated_at: str, rows: list[dict], page_size: int) -> list[dict]:
    target_dir = root / dirname
    target_dir.mkdir(parents=True, exist_ok=True)
    for stale in target_dir.glob("*.json"):
        stale.unlink()

    pages: list[dict] = []
    for index, start in enumerate(range(0, len(rows), page_size), 1):
        page_rows = rows[start : start + page_size]
        filename = f"{index:04d}.json"
        payload = {
            "schema_version": APP_FEED_SCHEMA_VERSION,
            "generated_at": generated_at,
            "vertical_id": vertical_slug(),
            "vertical_name": vertical_name(),
            "page": index,
            "recipes": page_rows,
        }
        (target_dir / filename).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        pages.append({"index": index, "path": f"{dirname}/{filename}", "count": len(page_rows)})
    return pages


def write_app_feed(
    docs_dir: str | Path,
    generated_at: str,
    ranked: list[dict],
    source_count: int,
    *,
    corpus: list[dict] | None = None,
    duplicate_groups: list[dict] | None = None,
    stale_days: int = 14,
    catalog_url_count: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> str:
    if page_size <= 0:
        raise ValueError("page_size must be positive")

    root = Path(docs_dir) / "api"
    root.mkdir(parents=True, exist_ok=True)

    projected_ranked = [build_app_recipe(row) for row in ranked]
    ranked_pages = _write_pages(root, "recipes", generated_at, projected_ranked, page_size)

    ranked_by_id = {str(row.get("recipe_id") or ""): row for row in ranked if row.get("recipe_id")}
    duplicate_map = _duplicate_membership(duplicate_groups)
    as_of = parse_dt(generated_at) or datetime.now(timezone.utc)
    corpus_source = corpus if corpus is not None else ranked
    projected_corpus = [
        build_corpus_recipe(
            dict(item),
            ranked_by_id,
            duplicate_map,
            as_of=as_of,
            stale_days=stale_days,
        )
        for item in corpus_source
        if str(item.get("recipe_id") or "")
    ]
    serveability_order = {"discover": 0, "explore": 1, "archive": 2, "suppressed": 3}
    projected_corpus.sort(
        key=lambda row: (
            serveability_order.get(str(row.get("serveability")), 9),
            int(row.get("rank") or 10**9),
            -float(row.get("evidence_confidence") or 0.0),
            -int(row.get("rating_count") or 0),
            str(row.get("title") or "").lower(),
        )
    )
    corpus_pages = _write_pages(root, "corpus", generated_at, projected_corpus, page_size)
    status_counts = Counter(str(row.get("serveability") or "unknown") for row in projected_corpus)

    manifest = {
        "schema_version": APP_FEED_SCHEMA_VERSION,
        "generated_at": generated_at,
        "vertical": {
            "id": vertical_slug(),
            "name": vertical_name(),
            "source_count": int(source_count),
        },
        # recipe_count/pages remain the backwards-compatible default Discover feed.
        "recipe_count": len(projected_ranked),
        "ranked_recipe_count": len(projected_ranked),
        "page_size": page_size,
        "pages": ranked_pages,
        # corpus_* exposes every normalized recipe record retained by this vertical.
        "corpus_recipe_count": len(projected_corpus),
        "corpus_pages": corpus_pages,
        "corpus_status_counts": dict(sorted(status_counts.items())),
        "catalog_url_count": int(catalog_url_count) if catalog_url_count is not None else None,
        "content_policy": {
            "ingredients": "factual structured ingredient lines",
            "instructions": "publisher prose not republished; open canonical_url for full directions",
            "corpus": "all normalized recipe records are published with serveability metadata; default Discover remains ranking-gated",
        },
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return str(manifest_path)
