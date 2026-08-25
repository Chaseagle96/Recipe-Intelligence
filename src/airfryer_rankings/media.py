from __future__ import annotations

from dataclasses import asdict
from io import BytesIO
from typing import Iterable, cast

from PIL import Image, UnidentifiedImageError

from .dedupe import candidate_duplicate_pairs
from .http import get, make_session
from .models import RecipeRow


def perceptual_hash_bytes(payload: bytes, size: int = 8) -> str:
    with Image.open(BytesIO(payload)) as opened:
        grayscale = opened.convert("L").resize((size, size))
        # Pillow's generic pixel typing includes RGB tuples/floats, but mode L
        # guarantees scalar 8-bit grayscale values at runtime.
        pixels = list(cast(Iterable[int], grayscale.get_flattened_data()))
    average = sum(pixels) / max(1, len(pixels))
    value = 0
    for idx, pixel in enumerate(pixels):
        if pixel >= average:
            value |= 1 << idx
    return f"{value:0{size * size // 4}x}"


def fetch_perceptual_hash(url: str, max_bytes: int = 2_000_000) -> str:
    if not url:
        return ""
    session = make_session()
    try:
        response = get(session, url, 15, headers={"Accept": "image/*"}, max_bytes=max_bytes)
        return perceptual_hash_bytes(response.content)
    except (OSError, UnidentifiedImageError, Exception):
        return ""
    finally:
        session.close()


def enrich_ambiguous_perceptual_hashes(rows: Iterable[RecipeRow], state: dict, max_fetches: int = 20) -> dict:
    rows = list(rows)
    row_map = {row.recipe_id: row for row in rows}
    current_ids = set(row_map)
    candidates = [asdict(row) for row in rows]
    candidates.extend(dict(recipe) for rid, recipe in state.get("recipes", {}).items() if rid not in current_ids)
    ambiguous = candidate_duplicate_pairs(candidates, low=0.72, high=0.90, limit=100)

    ids_to_enrich: list[str] = []
    for left, right, _ in ambiguous:
        for item in (left, right):
            rid = str(item.get("recipe_id") or "")
            if rid and not item.get("image_perceptual_hash") and item.get("image_url") and rid not in ids_to_enrich:
                ids_to_enrich.append(rid)

    fetched = 0
    enriched = 0
    for rid in ids_to_enrich:
        if fetched >= max_fetches:
            break
        if rid in row_map:
            target = row_map[rid]
            image_url = target.image_url
        else:
            target = state.get("recipes", {}).get(rid)
            image_url = target.get("image_url", "") if target else ""
        if not image_url:
            continue
        fetched += 1
        fingerprint = fetch_perceptual_hash(image_url)
        if not fingerprint:
            continue
        enriched += 1
        if rid in row_map:
            row_map[rid].image_perceptual_hash = fingerprint
        elif isinstance(target, dict):
            target["image_perceptual_hash"] = fingerprint

    return {
        "ambiguous_pairs": len(ambiguous),
        "image_hash_fetches": fetched,
        "image_hash_enriched": enriched,
        "image_hash_fetch_cap": max_fetches,
    }
