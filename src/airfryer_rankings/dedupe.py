from __future__ import annotations

import hashlib
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Iterable

from .models import GENERIC_TITLE_TOKENS, normalize_ingredient, normalize_text

# duplicate_similarity hard-caps pairs that fail the strong evidence gate at 0.79.
# The production threshold is therefore aligned immediately above that gate instead
# of the former 0.88 cutoff, which benchmark data showed sacrificed substantial recall.
DEDUPE_THRESHOLD = 0.80


def _tokens(value: str) -> set[str]:
    return {x for x in normalize_text(value).split() if len(x) > 2}


def _match_token(token: str) -> str:
    token = token.strip().lower()
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _normalized_title_for_match(value: str) -> str:
    tokens = []
    for token in normalize_text(value).split():
        token = _match_token(token)
        if token in GENERIC_TITLE_TOKENS or len(token) <= 2:
            continue
        tokens.append(token)
    return " ".join(tokens)


def _title_similarity(a: dict, b: dict) -> float:
    left = _normalized_title_for_match(a.get("title", ""))
    right = _normalized_title_for_match(b.get("title", ""))
    if not left or not right:
        left = normalize_text(a.get("title", ""))
        right = normalize_text(b.get("title", ""))
    return SequenceMatcher(None, left, right).ratio()


def _ingredient_set(recipe: dict) -> set[str]:
    tokens: set[str] = set()
    for ingredient in recipe.get("ingredients", []) or []:
        for token in normalize_ingredient(ingredient).split():
            token = _match_token(token)
            if len(token) > 2 and token not in {"fresh", "kosher", "ground", "large", "small", "medium", "optional"}:
                tokens.add(token)
    return tokens


def _instruction_set(recipe: dict) -> set[str]:
    text = " ".join(recipe.get("instructions", []) or [])
    return _tokens(text)


def _jaccard(a: set[str], b: set[str]) -> float | None:
    if not a or not b:
        return None
    return len(a & b) / max(1, len(a | b))


def _hex_hamming_similarity(left: str, right: str) -> float | None:
    if not left or not right:
        return None
    try:
        bits = max(len(left), len(right)) * 4
        xor = int(left, 16) ^ int(right, 16)
        return 1.0 - (xor.bit_count() / max(1, bits))
    except Exception:
        return None


def duplicate_similarity(a: dict, b: dict) -> float:
    ca = a.get("canonical_url") or a.get("url")
    cb = b.get("canonical_url") or b.get("url")
    if ca and cb and ca == cb:
        return 1.0
    title = _title_similarity(a, b)
    ingredient = _jaccard(_ingredient_set(a), _ingredient_set(b))
    instruction = _jaccard(_instruction_set(a), _instruction_set(b))
    instruction_simhash = _hex_hamming_similarity(a.get("instruction_simhash", ""), b.get("instruction_simhash", ""))
    author_match = bool(
        normalize_text(a.get("author", ""))
        and normalize_text(a.get("author", "")) == normalize_text(b.get("author", ""))
    )
    perceptual_image = _hex_hamming_similarity(a.get("image_perceptual_hash", ""), b.get("image_perceptual_hash", ""))
    image_url_match = bool(a.get("image_fingerprint") and a.get("image_fingerprint") == b.get("image_fingerprint"))

    parts: list[tuple[float, float]] = [(title, 0.38)]
    if ingredient is not None:
        parts.append((ingredient, 0.35))
    if instruction is not None:
        parts.append((instruction, 0.08))
    if instruction_simhash is not None:
        parts.append((instruction_simhash, 0.07))
    if author_match:
        parts.append((1.0, 0.04))
    if perceptual_image is not None:
        parts.append((perceptual_image, 0.08))
    elif image_url_match:
        parts.append((1.0, 0.03))
    total_weight = sum(w for _, w in parts)
    score = sum(v * w for v, w in parts) / total_weight

    strong_image = perceptual_image is not None and perceptual_image >= 0.90
    strong_instruction = instruction_simhash is not None and instruction_simhash >= 0.88
    if ingredient is not None and not (title >= 0.88 and ingredient >= 0.62) and not (title >= 0.94 and strong_image):
        return min(score, DEDUPE_THRESHOLD - 0.01)
    if ingredient is None and not (
        title >= 0.97 and (author_match or image_url_match or strong_image or strong_instruction)
    ):
        return min(score, DEDUPE_THRESHOLD - 0.01)
    return score


def _candidate_block_keys(recipe: dict) -> set[str]:
    keys: set[str] = set()
    canonical = recipe.get("canonical_url") or recipe.get("url")
    if canonical:
        keys.add("canonical:" + canonical)
    title_tokens = [x for x in _normalized_title_for_match(recipe.get("title", "")).split() if len(x) > 2]
    if title_tokens:
        keys.add("title:" + "|".join(sorted(title_tokens[:4])[:2]))
        if len(title_tokens) == 1:
            keys.add("title1:" + title_tokens[0])
    ingredient_tokens = sorted(_ingredient_set(recipe))
    if ingredient_tokens:
        keys.add("ing:" + "|".join(ingredient_tokens[:3]))
    if recipe.get("image_perceptual_hash"):
        keys.add("phash:" + str(recipe["image_perceptual_hash"])[:6])
    if not keys:
        keys.add("id:" + str(recipe.get("recipe_id", "")))
    return keys


def candidate_duplicate_pairs(
    recipes: Iterable[dict], low: float = 0.72, high: float = 0.90, limit: int = 100
) -> list[tuple[dict, dict, float]]:
    recipes = [dict(x) for x in recipes]
    blocks: dict[str, list[int]] = defaultdict(list)
    for idx, recipe in enumerate(recipes):
        for key in _candidate_block_keys(recipe):
            blocks[key].append(idx)
    seen = set()
    output = []
    for block in blocks.values():
        if len(block) < 2 or len(block) > 100:
            continue
        for pos, left in enumerate(block):
            for right in block[pos + 1 :]:
                pair = (min(left, right), max(left, right))
                if pair in seen:
                    continue
                seen.add(pair)
                score = duplicate_similarity(recipes[left], recipes[right])
                if low <= score <= high:
                    output.append((recipes[left], recipes[right], score))
                    if len(output) >= limit:
                        return output
    return output


def dedupe_current(recipes: Iterable[dict], detailed: bool = False):
    recipes = [dict(x) for x in recipes]
    n = len(recipes)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    blocks: dict[str, list[int]] = defaultdict(list)
    for idx, recipe in enumerate(recipes):
        for key in _candidate_block_keys(recipe):
            blocks[key].append(idx)

    seen_pairs: set[tuple[int, int]] = set()
    pair_confidence: dict[tuple[int, int], float] = {}
    for block in blocks.values():
        if len(block) < 2:
            continue
        if len(block) > 300:
            block = sorted(block, key=lambda i: int(recipes[i].get("rating_count", 0)), reverse=True)[:300]
        for pos, left in enumerate(block):
            for right in block[pos + 1 :]:
                pair = (min(left, right), max(left, right))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                confidence = duplicate_similarity(recipes[left], recipes[right])
                if confidence >= DEDUPE_THRESHOLD:
                    pair_confidence[pair] = confidence
                    union(left, right)

    grouped_indices: dict[int, list[int]] = defaultdict(list)
    for idx in range(n):
        grouped_indices[find(idx)].append(idx)

    output: list[dict] = []
    duplicate_rows: list[dict] = []
    deduped = 0
    for indices in grouped_indices.values():
        group = [recipes[i] for i in indices]
        representative = max(
            group, key=lambda x: (int(x.get("rating_count", 0)), float(x.get("evidence_confidence", 0)))
        )
        confidence = 0.0
        if len(indices) > 1:
            for pos, left in enumerate(indices):
                for right in indices[pos + 1 :]:
                    confidence = max(
                        confidence,
                        pair_confidence.get(
                            (min(left, right), max(left, right)), duplicate_similarity(recipes[left], recipes[right])
                        ),
                    )
        item = dict(representative)
        sources = sorted({x.get("source", "") for x in group if x.get("source")})
        urls = sorted({x.get("canonical_url") or x.get("url", "") for x in group if x.get("url")})
        evidence = [
            {
                "source": x.get("source", ""),
                "url": x.get("canonical_url") or x.get("url", ""),
                "rating": x.get("normalized_rating"),
                "rating_count": x.get("rating_count"),
                "evidence_confidence": x.get("evidence_confidence"),
            }
            for x in sorted(group, key=lambda x: int(x.get("rating_count", 0)), reverse=True)
        ]
        group_payload = "|".join(sorted(x.get("recipe_id", "") for x in group))
        group_id = hashlib.sha256(group_payload.encode("utf-8")).hexdigest()[:16]
        item["combined_sources"] = " | ".join(sources)
        item["combined_urls"] = " | ".join(urls)
        item["source_evidence"] = evidence
        item["duplicate_group_id"] = group_id if len(group) > 1 else ""
        item["duplicate_confidence"] = confidence if len(group) > 1 else 0.0
        output.append(item)
        if len(group) > 1:
            deduped += len(group) - 1
            for member in group:
                duplicate_rows.append(
                    {
                        "duplicate_group_id": group_id,
                        "confidence": confidence,
                        "representative_recipe_id": representative.get("recipe_id"),
                        "recipe_id": member.get("recipe_id"),
                        "title": member.get("title"),
                        "source": member.get("source"),
                        "url": member.get("canonical_url") or member.get("url"),
                        "rating": member.get("normalized_rating"),
                        "rating_count": member.get("rating_count"),
                    }
                )
    if detailed:
        return output, deduped, duplicate_rows
    return output, deduped
