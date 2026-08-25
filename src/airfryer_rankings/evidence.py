from __future__ import annotations

import json
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import NUMBER_RE, SourceConfig

SCHEMA_ONLY_CONFIDENCE = 0.65


def jsonld_objects(soup: BeautifulSoup) -> Iterator[dict]:
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        stack = obj if isinstance(obj, list) else [obj]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                yield item
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
                main = item.get("mainEntity")
                if isinstance(main, (dict, list)):
                    stack.extend(main if isinstance(main, list) else [main])
            elif isinstance(item, list):
                stack.extend(item)


def _author_name(obj: dict) -> str:
    author = obj.get("author")
    if isinstance(author, str):
        return author.strip()
    if isinstance(author, dict):
        return str(author.get("name") or "").strip()
    if isinstance(author, list):
        names = []
        for x in author:
            if isinstance(x, str):
                names.append(x.strip())
            elif isinstance(x, dict) and x.get("name"):
                names.append(str(x["name"]).strip())
        return ", ".join(x for x in names if x)
    return ""


def _instruction_texts(obj: dict) -> tuple[str, ...]:
    raw = obj.get("recipeInstructions") or []
    if not isinstance(raw, list):
        raw = [raw]
    texts: list[str] = []
    for item in raw:
        if isinstance(item, str):
            texts.append(item.strip())
        elif isinstance(item, dict):
            text = item.get("text") or item.get("name")
            if text:
                texts.append(str(text).strip())
            steps = item.get("itemListElement")
            if isinstance(steps, list):
                for step in steps:
                    if isinstance(step, dict) and step.get("text"):
                        texts.append(str(step["text"]).strip())
    return tuple(x for x in texts if x)


def _image_url(obj: dict) -> str:
    image = obj.get("image")
    if isinstance(image, str):
        return image.strip()
    if isinstance(image, list) and image:
        first = image[0]
        if isinstance(first, str):
            return first.strip()
        if isinstance(first, dict):
            return str(first.get("url") or first.get("contentUrl") or "").strip()
    if isinstance(image, dict):
        return str(image.get("url") or image.get("contentUrl") or "").strip()
    return ""


def _parse_number(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = NUMBER_RE.search(str(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except Exception:
        return None


def _parse_histogram(agg: dict) -> dict[str, int]:
    raw = agg.get("ratingHistogram") or agg.get("ratingDistribution") or {}
    output: dict[str, int] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            star = _parse_number(key)
            count = _parse_number(value)
            if star is not None and count is not None and 0 <= star <= 5 and count >= 0:
                output[str(int(round(star)))] = int(count)
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            star = _parse_number(item.get("ratingValue") or item.get("star") or item.get("value"))
            count = _parse_number(item.get("count") or item.get("ratingCount") or item.get("reviewCount"))
            if star is not None and count is not None and 0 <= star <= 5 and count >= 0:
                output[str(int(round(star)))] = int(count)
    return output


def _element_number(element) -> float | None:
    if element is None:
        return None
    for attr in ("content", "value", "data-rating", "data-value", "aria-label"):
        if element.get(attr):
            value = _parse_number(element.get(attr))
            if value is not None:
                return value
    return _parse_number(element.get_text(" ", strip=True))


def visible_rating_evidence(soup: BeautifulSoup, cfg: SourceConfig) -> tuple[float | None, int | None]:
    rating = None
    count = None
    if cfg.rating_selector:
        try:
            rating = _element_number(soup.select_one(cfg.rating_selector))
        except Exception:
            rating = None
    if cfg.count_selector:
        try:
            value = _element_number(soup.select_one(cfg.count_selector))
            count = int(value) if value is not None else None
        except Exception:
            count = None

    if rating is None:
        for key in ("ratingValue", "rating"):
            element = soup.find(True, attrs={"itemprop": key})
            rating = _element_number(element)
            if rating is not None:
                break
    if count is None:
        for key in ("ratingCount", "reviewCount"):
            element = soup.find(True, attrs={"itemprop": key})
            value = _element_number(element)
            if value is not None:
                count = int(value)
                break
    return rating, count


def _evidence_score(
    schema_rating: float,
    schema_count: int,
    visible_rating: float | None,
    visible_count: int | None,
) -> tuple[float, str, str]:
    if visible_rating is None and visible_count is None:
        # Structured publisher metadata is useful but is only one evidence channel.
        # Keep it rankable while assigning the same confidence tier as visible-only
        # evidence; dual-channel agreement remains the strongest signal.
        return SCHEMA_ONLY_CONFIDENCE, "schema_only", "jsonld"
    rating_ok = visible_rating is None or abs(schema_rating - visible_rating) <= 0.08
    count_ok = visible_count is None or abs(schema_count - visible_count) <= max(5, int(schema_count * 0.08))
    if rating_ok and count_ok:
        return 1.0, "verified", "jsonld+visible"
    return 0.25, "conflict", "jsonld+visible"


def _canonical_url(soup: BeautifulSoup, fallback: str) -> str:
    tag = soup.find("link", rel=lambda value: value and "canonical" in str(value).lower())
    if tag and tag.get("href"):
        return urljoin(fallback, str(tag["href"]).strip())
    return fallback
