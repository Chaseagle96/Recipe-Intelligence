from __future__ import annotations

import hashlib
import json
from typing import Any

from bs4 import BeautifulSoup

STRUCTURAL_ATTRIBUTES = ("itemprop", "itemtype", "itemscope", "type", "rel")
DOM_FINGERPRINT_VERSION = 2
SCHEMA_SIGNATURE_VERSION = 2


def _jsonld_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _jsonld_shape(value[key])
            for key in sorted(value)
            if key
            in {
                "@type",
                "aggregateRating",
                "ratingValue",
                "ratingCount",
                "reviewCount",
                "bestRating",
                "recipeIngredient",
                "recipeInstructions",
                "author",
                "image",
                "name",
            }
        }
    if isinstance(value, list):
        shapes = {}
        for item in value:
            shape = _jsonld_shape(item)
            shapes[_canonical_shape(shape)] = shape
        return [shapes[key] for key in sorted(shapes)]
    return type(value).__name__


def _canonical_shape(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def dom_structure_fingerprint(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    tokens: list[str] = []
    for tag in soup.find_all(True)[:2500]:
        attrs = []
        for name in STRUCTURAL_ATTRIBUTES:
            if name not in tag.attrs:
                continue
            value = tag.attrs.get(name)
            if isinstance(value, list):
                value = "|".join(sorted(str(item) for item in value))
            attrs.append(f"{name}={value}")
        if attrs:
            tokens.append(f"{tag.name}[{';'.join(attrs)}]")
    payload = "\n".join(tokens)
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()[:24]


def schema_signature(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    shapes: list[Any] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        shapes.append(_jsonld_shape(payload))
    canonical = json.dumps(sorted({_canonical_shape(shape) for shape in shapes}), separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24] if shapes else ""


def rating_evidence_signature(html: str) -> dict[str, int | bool]:
    soup = BeautifulSoup(html, "lxml")
    rating_values = len(soup.select('[itemprop="ratingValue"]'))
    rating_counts = len(soup.select('[itemprop="ratingCount"], [itemprop="reviewCount"]'))
    jsonld_scripts = len(soup.find_all("script", attrs={"type": "application/ld+json"}))
    return {
        "visible_rating_value_nodes": rating_values,
        "visible_rating_count_nodes": rating_counts,
        "jsonld_scripts": jsonld_scripts,
        "has_visible_pair": bool(rating_values and rating_counts),
    }


def structure_metadata(html: str) -> dict:
    return {
        "dom_fingerprint": dom_structure_fingerprint(html),
        "dom_fingerprint_version": DOM_FINGERPRINT_VERSION,
        "schema_signature": schema_signature(html),
        "schema_signature_version": SCHEMA_SIGNATURE_VERSION,
        "rating_evidence_signature": rating_evidence_signature(html),
    }
