from __future__ import annotations

import hashlib

from bs4 import BeautifulSoup

from .evidence import (
    _author_name,
    _canonical_url,
    _evidence_score,
    _image_url,
    _instruction_texts,
    _parse_histogram,
    _parse_number,
    recipe_objects,
    visible_rating_evidence,
)
from .models import (
    RecipeRow,
    SourceConfig,
    categorize_recipe,
    fingerprint_image_url,
    ingredient_signature,
    instruction_signature,
    instruction_simhash,
    now_iso,
)
from .structure import structure_metadata


def extract_recipe_from_html(
    html: str,
    url: str,
    domain: str,
    cfg: SourceConfig | None = None,
    response_headers: dict | None = None,
) -> tuple[RecipeRow | None, dict]:
    cfg = cfg or SourceConfig(domain=domain)
    response_headers = response_headers or {}
    soup = BeautifulSoup(html, "lxml")
    canonical = _canonical_url(soup, url)
    visible_rating, visible_count = visible_rating_evidence(soup, cfg)
    page_hash = hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest()[:24]
    structural = structure_metadata(html)
    parse_meta = {"issues": [], "page_hash": page_hash, "recipe_recognized": False, **structural}
    candidates: list[RecipeRow] = []

    for obj in recipe_objects(soup):
        # Recognizing a Recipe schema is a successful structural extraction even
        # when the publisher does not expose public aggregate rating evidence.
        # Ranking eligibility is tracked separately by whether a RecipeRow can
        # be produced with verified rating/count evidence.
        parse_meta["recipe_recognized"] = True
        aggregate = obj.get("aggregateRating") or {}
        if not isinstance(aggregate, dict):
            continue
        rating = _parse_number(aggregate.get("ratingValue"))
        count_value = _parse_number(aggregate.get("ratingCount") or aggregate.get("reviewCount"))
        best = _parse_number(aggregate.get("bestRating")) or 5.0
        if rating is None or count_value is None:
            continue
        count = int(count_value)
        if count <= 0 or best <= 0 or rating < 0 or rating > best * 1.05:
            parse_meta["issues"].append("malformed_rating_scale")
            continue

        normalized = max(0.0, min(5.0, rating / best * 5.0))
        visible_normalized = None
        if visible_rating is not None:
            visible_normalized = visible_rating if visible_rating <= 5.05 else visible_rating / best * 5.0
        extraction_method = str(obj.get("__extraction_method") or "jsonld")
        confidence, evidence_status, method = _evidence_score(
            normalized,
            count,
            visible_normalized,
            visible_count,
            extraction_method,
        )
        histogram = _parse_histogram(aggregate)
        if evidence_status == "conflict":
            parse_meta["issues"].append("rating_evidence_conflict")

        ingredients_raw = obj.get("recipeIngredient") or []
        ingredients = (
            tuple(str(value).strip() for value in ingredients_raw) if isinstance(ingredients_raw, list) else ()
        )
        instructions = _instruction_texts(obj)
        image = _image_url(obj)
        title = str(obj.get("name") or (soup.title.string if soup.title else "") or url).strip()
        recipe_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        candidates.append(
            RecipeRow(
                recipe_id=recipe_id,
                title=title,
                source=domain,
                url=url,
                rating=float(rating),
                rating_count=count,
                best_rating=float(best),
                normalized_rating=normalized,
                retrieved_at=now_iso(),
                author=_author_name(obj),
                ingredient_signature=ingredient_signature(ingredients),
                canonical_url=canonical,
                ingredients=ingredients,
                instruction_signature=instruction_signature(instructions),
                instruction_simhash=instruction_simhash(instructions),
                instructions=instructions,
                image_url=image,
                image_fingerprint=fingerprint_image_url(image),
                extraction_method=method,
                evidence_confidence=confidence,
                evidence_status=evidence_status,
                page_hash=page_hash,
                dom_fingerprint=str(structural["dom_fingerprint"]),
                schema_signature=str(structural["schema_signature"]),
                rating_evidence_signature=dict(structural["rating_evidence_signature"]),
                etag=str(response_headers.get("ETag") or response_headers.get("etag") or ""),
                last_modified=str(response_headers.get("Last-Modified") or response_headers.get("last-modified") or ""),
                schema_rating=normalized,
                schema_rating_count=count,
                visible_rating=visible_normalized,
                visible_rating_count=visible_count,
                rating_histogram=histogram,
                categories=categorize_recipe(title, ingredients),
            )
        )

    if candidates:
        return max(candidates, key=lambda row: (row.evidence_confidence, row.rating_count)), parse_meta

    if visible_rating is not None and visible_count and 0 <= visible_rating <= 5.05:
        # The visible-evidence fallback itself proves that the page was
        # structurally extractable as a recipe/rating document.
        parse_meta["recipe_recognized"] = True
        title = str((soup.title.string if soup.title else "") or url).strip()
        recipe_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        return (
            RecipeRow(
                recipe_id=recipe_id,
                title=title,
                source=domain,
                url=url,
                rating=float(visible_rating),
                rating_count=int(visible_count),
                best_rating=5.0,
                normalized_rating=float(visible_rating),
                retrieved_at=now_iso(),
                canonical_url=canonical,
                extraction_method="visible_microdata",
                evidence_confidence=0.65,
                evidence_status="visible_only",
                page_hash=page_hash,
                dom_fingerprint=str(structural["dom_fingerprint"]),
                schema_signature=str(structural["schema_signature"]),
                rating_evidence_signature=dict(structural["rating_evidence_signature"]),
                etag=str(response_headers.get("ETag") or response_headers.get("etag") or ""),
                last_modified=str(response_headers.get("Last-Modified") or response_headers.get("last-modified") or ""),
                visible_rating=float(visible_rating),
                visible_rating_count=int(visible_count),
                categories=categorize_recipe(title),
            ),
            parse_meta,
        )
    return None, parse_meta
