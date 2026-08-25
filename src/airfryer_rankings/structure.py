from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from typing import Any

from bs4 import BeautifulSoup

DOM_FINGERPRINT_VERSION = 3
SCHEMA_SIGNATURE_VERSION = 3

_SCHEMA_FIELDS = {
    "@type",
    "aggregateRating",
    "ratingValue",
    "ratingCount",
    "bestRating",
    "recipeIngredient",
    "recipeInstructions",
    "recipeYield",
    "prepTime",
    "cookTime",
    "totalTime",
    "author",
    "publisher",
    "image",
    "name",
}
_PROPERTY_ALIASES = {
    "rating": "ratingValue",
    "reviewCount": "ratingCount",
}
_SEMANTIC_PROPERTIES = {
    "aggregateRating",
    "author",
    "bestRating",
    "cookTime",
    "dateModified",
    "datePublished",
    "image",
    "name",
    "prepTime",
    "publisher",
    "ratingCount",
    "ratingValue",
    "recipeIngredient",
    "recipeInstructions",
    "recipeYield",
    "totalTime",
}
_SEMANTIC_TYPES = {"aggregaterating", "organization", "person", "recipe", "review"}


def _canonical_key(key: Any) -> str:
    text = str(key)
    return _PROPERTY_ALIASES.get(text, text)


def _type_name(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    return text.rsplit("/", 1)[-1].rsplit("#", 1)[-1].lower()


def _bucket_count(value: int) -> int:
    if value <= 0:
        return 0
    if value == 1:
        return 1
    if value <= 3:
        return 2
    if value <= 7:
        return 4
    return 8


def _attribute_tokens(value: Any) -> list[str]:
    if isinstance(value, str):
        return value.split()
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _iter_json_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_dicts(child)


def _jsonld_shape(value: Any) -> Any:
    if isinstance(value, dict):
        shaped: dict[str, Any] = {}
        for raw_key in sorted(value):
            key = _canonical_key(raw_key)
            if key not in _SCHEMA_FIELDS or key in shaped:
                continue
            shaped[key] = _jsonld_shape(value[raw_key])
        return shaped
    if isinstance(value, list):
        shapes = {}
        for item in value:
            shape = _jsonld_shape(item)
            shapes[_canonical_shape(shape)] = shape
        return [shapes[key] for key in sorted(shapes)]
    return type(value).__name__


def _canonical_shape(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _semantic_dom_contract(html: str) -> dict[str, dict[str, int]]:
    """Return layout-independent recipe signals instead of raw HTML topology."""
    soup = BeautifulSoup(html, "lxml")
    properties: Counter[str] = Counter()
    types: Counter[str] = Counter()
    for tag in soup.find_all(True):
        for prop in _attribute_tokens(tag.attrs.get("itemprop", ())):
            canonical = _canonical_key(prop)
            if canonical in _SEMANTIC_PROPERTIES:
                properties[canonical] += 1
        for itemtype in _attribute_tokens(tag.attrs.get("itemtype", ())):
            name = _type_name(itemtype)
            if name in _SEMANTIC_TYPES:
                types[name] += 1

    jsonld_properties: Counter[str] = Counter()
    jsonld_types: Counter[str] = Counter()
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        for obj in _iter_json_dicts(payload):
            raw_type = obj.get("@type")
            raw_types = raw_type if isinstance(raw_type, list) else [raw_type]
            for value in raw_types:
                name = _type_name(value)
                if name in _SEMANTIC_TYPES:
                    jsonld_types[name] += 1
            for raw_key in obj:
                key = _canonical_key(raw_key)
                if key in _SCHEMA_FIELDS:
                    jsonld_properties[key] += 1

    def buckets(counter: Counter[str]) -> dict[str, int]:
        return {key: _bucket_count(counter[key]) for key in sorted(counter) if counter[key]}

    return {
        "itemprops": buckets(properties),
        "itemtypes": buckets(types),
        "jsonld_props": buckets(jsonld_properties),
        "jsonld_types": buckets(jsonld_types),
    }


def dom_structure_fingerprint(html: str) -> str:
    payload = _canonical_shape(_semantic_dom_contract(html))
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
    contract = _semantic_dom_contract(html)
    return {
        "dom_fingerprint": hashlib.sha256(_canonical_shape(contract).encode("utf-8")).hexdigest()[:24],
        "dom_fingerprint_version": DOM_FINGERPRINT_VERSION,
        "dom_structure_contract": contract,
        "schema_signature": schema_signature(html),
        "schema_signature_version": SCHEMA_SIGNATURE_VERSION,
        "rating_evidence_signature": rating_evidence_signature(html),
    }
