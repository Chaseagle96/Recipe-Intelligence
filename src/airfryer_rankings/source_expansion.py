from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import requests
import yaml
from bs4 import BeautifulSoup

from .dedupe import DEDUPE_THRESHOLD, duplicate_similarity
from .discovery import discover_source_urls
from .evidence import (
    _author_name,
    _canonical_url,
    _evidence_score,
    _image_url,
    _instruction_texts,
    _parse_number,
    jsonld_objects,
    visible_rating_evidence,
)
from .extract import extract_recipe_from_html
from .http import get, iter_sitemap_records, make_session, robots_and_sitemaps
from .models import (
    UA,
    SourceConfig,
    fingerprint_image_url,
    instruction_simhash,
    load_sources,
    normalize_text,
    now_iso,
    parse_dt,
)
from .persistence import atomic_write_json, exclusive_lock
from .source_registry import (
    ACTIVE,
    CANDIDATE,
    DEGRADED,
    PROMOTED,
    QUALIFIED,
    QUARANTINED,
    REJECTED,
    SOURCE_GATE_VERSION,
    SUSPENDED,
    effective_source_configs,
    load_source_registry,
    record_candidate_discovery,
    save_source_registry,
    source_registry_summary,
    transition_source,
)
from .source_security import (
    UnsafeNetworkTarget,
    candidate_domain_from_url,
    is_non_publisher_domain,
    safe_get,
)
from .storage import load_state, write_run_records
from .verticals import VerticalDefinition, load_verticals


@dataclass(frozen=True)
class DiscoveryHit:
    url: str
    provider: str
    query: str = ""
    title: str = ""


@dataclass
class SampledPage:
    url: str
    fetched: bool = False
    final_url: str = ""
    http_status: int | None = None
    error: str = ""
    is_recipe: bool = False
    vertical_relevant: bool = False
    title: str = ""
    ingredients: tuple[str, ...] = ()
    instructions: tuple[str, ...] = ()
    author: str = ""
    publisher: str = ""
    date_published: str = ""
    date_modified: str = ""
    has_yield: bool = False
    has_time: bool = False
    field_completeness: float = 0.0
    ranking_extractable: bool = False
    evidence_status: str = "missing"
    evidence_confidence: float = 0.0
    has_rating: bool = False
    canonical_url: str = ""
    canonical_external: bool = False
    editorial_link: bool = False
    trap_url: bool = False
    recipe_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerticalContext:
    slug: str
    name: str
    base_sources_path: Path
    state_path: Path
    registry_path: Path
    output_dir: Path
    events_dir: Path
    include_pattern: str
    allow_unmatched_discovery_links: bool
    query_terms: list[str]
    proteins: list[str]
    cuisines: list[str]
    meal_types: list[str]
    categories: list[str]
    ingredients: list[str]
    base_sources: list[SourceConfig] = field(default_factory=list)
    state: dict = field(default_factory=dict)
    registry: dict = field(default_factory=dict)
    new_candidate_domains: set[str] = field(default_factory=set)
    provider_status: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, float] = field(default_factory=dict)
    audit_start: int = 0


class SearchProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def search(self, query: str, limit: int) -> list[DiscoveryHit]: ...


class BraveSearchProvider:
    name = "brave_search"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = (api_key or os.getenv("BRAVE_SEARCH_API_KEY") or "").strip()

    def available(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str, limit: int) -> list[DiscoveryHit]:
        if not self.available():
            return []
        params: dict[str, str | int] = {
            "q": query,
            "count": min(20, max(1, limit)),
            "safesearch": "moderate",
        }
        response = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        results = ((payload or {}).get("web") or {}).get("results") or []
        return [
            DiscoveryHit(
                url=str(item.get("url") or ""),
                provider=self.name,
                query=query,
                title=str(item.get("title") or ""),
            )
            for item in results[:limit]
            if item.get("url")
        ]


class GoogleCustomSearchProvider:
    name = "google_cse"

    def __init__(self, api_key: str | None = None, cx: str | None = None) -> None:
        self.api_key = (api_key or os.getenv("GOOGLE_CSE_API_KEY") or "").strip()
        self.cx = (cx or os.getenv("GOOGLE_CSE_ID") or "").strip()

    def available(self) -> bool:
        return bool(self.api_key and self.cx)

    def search(self, query: str, limit: int) -> list[DiscoveryHit]:
        if not self.available():
            return []
        output: list[DiscoveryHit] = []
        start = 1
        while len(output) < limit and start <= 91:
            count = min(10, limit - len(output))
            params: dict[str, str | int] = {
                "key": self.api_key,
                "cx": self.cx,
                "q": query,
                "num": count,
                "start": start,
            }
            response = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params=params,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            items = (payload or {}).get("items") or []
            if not items:
                break
            output.extend(
                DiscoveryHit(
                    url=str(item.get("link") or ""),
                    provider=self.name,
                    query=query,
                    title=str(item.get("title") or ""),
                )
                for item in items
                if item.get("link")
            )
            start += len(items)
        return output[:limit]


def _read_mapping(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    raw = target.read_text(encoding="utf-8")
    if target.suffix.lower() in {".yaml", ".yml"}:
        payload = yaml.safe_load(raw) or {}
    else:
        payload = json.loads(raw)
    return payload if isinstance(payload, dict) else {}


def load_source_discovery_config(path: str | Path) -> dict[str, Any]:
    payload = _read_mapping(path)
    if int(payload.get("source_gate_version") or 0) != SOURCE_GATE_VERSION:
        raise ValueError(
            f"source_gate_version must be {SOURCE_GATE_VERSION}; got {payload.get('source_gate_version')!r}"
        )
    if not isinstance(payload.get("verticals"), dict) or not payload["verticals"]:
        raise ValueError("source discovery config must define at least one vertical")
    return payload


def _context_from_config(definition: VerticalDefinition, item: dict[str, Any]) -> VerticalContext:
    return VerticalContext(
        slug=definition.id,
        name=definition.name,
        base_sources_path=definition.source_config_path,
        state_path=definition.state_path,
        registry_path=definition.registry_path,
        output_dir=definition.output_root,
        events_dir=definition.events_root,
        include_pattern=definition.include_pattern,
        allow_unmatched_discovery_links=definition.allow_unmatched_discovery_links,
        query_terms=[str(value) for value in item.get("query_terms", [])],
        proteins=[str(value) for value in item.get("proteins", [])],
        cuisines=[str(value) for value in item.get("cuisines", [])],
        meal_types=[str(value) for value in item.get("meal_types", [])],
        categories=[str(value) for value in item.get("categories", [])],
        ingredients=[str(value) for value in item.get("ingredients", [])],
    )


def _load_contexts(
    config: dict[str, Any],
    definitions: dict[str, VerticalDefinition],
) -> list[VerticalContext]:
    output: list[VerticalContext] = []
    for slug, item in config["verticals"].items():
        if not isinstance(item, dict):
            continue
        context = _context_from_config(definitions[str(slug)], item)
        context.base_sources = load_sources(context.base_sources_path, include_discovered=False)
        context.state = load_state(context.state_path)
        context.registry = load_source_registry(context.registry_path, context.slug)
        context.audit_start = len(context.registry.get("audit", []))
        output.append(context)
    return output


def build_query_family(context: VerticalContext, max_queries: int) -> list[str]:
    base_terms = context.query_terms or [context.name.lower() + " recipes"]
    queries: list[str] = []
    for term in base_terms:
        queries.extend((f"best {term}", term))
    dimensions = [context.proteins, context.meal_types, context.categories, context.cuisines, context.ingredients]
    for values in dimensions:
        for value in values:
            for term in base_terms[:2]:
                queries.append(f"{term} {value} recipe")
    # Stable de-duplication means repeated scheduled runs use a reproducible query
    # family while different dimensions still rotate into the bounded prefix.
    return list(dict.fromkeys(query.strip() for query in queries if query.strip()))[:max_queries]


def _mode_budget(config: dict[str, Any], mode: str) -> dict[str, Any]:
    budgets = config.get("budgets", {}) or {}
    selected = budgets.get(mode) or budgets.get("daily") or {}
    return dict(selected)


def _providers(config: dict[str, Any]) -> list[SearchProvider]:
    configured = config.get("providers", {}) or {}
    output: list[SearchProvider] = []
    if bool((configured.get("brave_search") or {}).get("enabled", True)):
        output.append(BraveSearchProvider())
    if bool((configured.get("google_cse") or {}).get("enabled", True)):
        output.append(GoogleCustomSearchProvider())
    return output


def _blocked_domains(config: dict[str, Any]) -> list[str]:
    return [str(value) for value in (config.get("blocked_domain_suffixes") or [])]


def _deadline_reached(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _record_hit(context: VerticalContext, hit: DiscoveryHit, config: dict[str, Any], run_at: str) -> bool:
    domain = candidate_domain_from_url(hit.url)
    if not domain or is_non_publisher_domain(domain, _blocked_domains(config)):
        return False
    base_domains = {cfg.domain for cfg in context.base_sources}
    _, is_new = record_candidate_discovery(
        context.registry,
        domain=domain,
        provider=hit.provider,
        query=hit.query,
        discovery_url=hit.url,
        timestamp=run_at,
        base_domains=base_domains,
    )
    if is_new:
        context.new_candidate_domains.add(domain)
    return is_new


def _discover_search_candidates(
    context: VerticalContext,
    config: dict[str, Any],
    budget: dict[str, Any],
    providers: list[SearchProvider],
    run_at: str,
    deadline: float | None = None,
) -> None:
    max_queries = int(budget.get("max_search_queries_per_run", 0))
    if max_queries <= 0:
        return
    max_domains = int(budget.get("max_candidate_domains_per_run", 20))
    per_query = int(budget.get("search_results_per_query", 10))
    queries = build_query_family(context, max_queries)
    for provider in providers:
        if len(context.new_candidate_domains) >= max_domains or _deadline_reached(deadline):
            break
        if not provider.available():
            context.provider_status.append({"provider": provider.name, "status": "unavailable_missing_configuration"})
            continue
        provider_hits = 0
        try:
            for query in queries:
                if len(context.new_candidate_domains) >= max_domains or _deadline_reached(deadline):
                    break
                hits = provider.search(query, per_query)
                provider_hits += len(hits)
                for hit in hits:
                    _record_hit(context, hit, config, run_at)
                    if len(context.new_candidate_domains) >= max_domains:
                        break
            status = "deadline_exceeded" if _deadline_reached(deadline) else "ok"
            context.provider_status.append({"provider": provider.name, "status": status, "hits": provider_hits})
        except Exception as exc:
            context.provider_status.append(
                {"provider": provider.name, "status": f"error:{type(exc).__name__}", "detail": str(exc)[:200]}
            )


def _discovery_pages_for_outbound(context: VerticalContext, max_pages: int) -> list[tuple[SourceConfig, str]]:
    pages: list[tuple[SourceConfig, str]] = []
    seen: set[str] = set()
    catalog = context.state.get("url_catalog", {}) or {}
    by_source: dict[str, list[str]] = {}
    for entry in catalog.values():
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or "")
        url = str(entry.get("url") or "")
        if source and url:
            by_source.setdefault(source, []).append(url)
    for cfg in context.base_sources:
        candidate_pages = list(cfg.discovery_urls)
        if by_source.get(cfg.domain):
            candidate_pages.append(sorted(by_source[cfg.domain])[0])
        for url in candidate_pages:
            if url in seen:
                continue
            seen.add(url)
            pages.append((cfg, url))
            if len(pages) >= max_pages:
                return pages
    return pages


def _discover_outbound_candidates(
    context: VerticalContext,
    config: dict[str, Any],
    budget: dict[str, Any],
    run_at: str,
    deadline: float | None = None,
) -> None:
    max_pages = int(budget.get("max_outbound_pages_per_run", 0))
    if max_pages <= 0:
        return
    max_domains = int(budget.get("max_candidate_domains_per_run", 20))
    include_re = re.compile(context.include_pattern, re.I)
    base_domains = {cfg.domain for cfg in context.base_sources}
    session = make_session()
    fetched = 0
    hits = 0
    for cfg, page_url in _discovery_pages_for_outbound(context, max_pages):
        if len(context.new_candidate_domains) >= max_domains or _deadline_reached(deadline):
            break
        try:
            response = get(session, page_url, 20)
            fetched += 1
            soup = BeautifulSoup(response.text, "lxml")
        except Exception:
            continue
        for anchor in soup.find_all("a", href=True):
            href = urljoin(page_url, str(anchor.get("href") or "").strip())
            text = anchor.get_text(" ", strip=True)
            if not include_re.search(href + " " + text):
                continue
            domain = candidate_domain_from_url(href)
            if not domain or domain in base_domains or domain == cfg.domain:
                continue
            if is_non_publisher_domain(domain, _blocked_domains(config)):
                continue
            hits += 1
            _record_hit(
                context,
                DiscoveryHit(url=href, provider="trusted_outbound_link", query=page_url, title=text),
                config,
                run_at,
            )
            if len(context.new_candidate_domains) >= max_domains:
                break
    status = "deadline_exceeded" if _deadline_reached(deadline) else "ok"
    context.provider_status.append(
        {"provider": "trusted_outbound_link", "status": status, "pages_fetched": fetched, "hits": hits}
    )


def _load_seed_hits(seed_file: str | Path | None, contexts: list[VerticalContext]) -> dict[str, list[DiscoveryHit]]:
    output: dict[str, list[DiscoveryHit]] = {context.slug: [] for context in contexts}
    if not seed_file:
        return output
    payload = _read_mapping(seed_file)
    rows = payload.get("candidates", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return output
    for row in rows:
        if not isinstance(row, dict):
            continue
        vertical = str(row.get("vertical") or "")
        url = str(row.get("url") or "")
        if vertical not in output or not url:
            continue
        output[vertical].append(
            DiscoveryHit(
                url=url,
                provider=str(row.get("provider") or "bootstrap_seed"),
                query=str(row.get("query") or ""),
                title=str(row.get("title") or ""),
            )
        )
    return output


def _cross_seed(contexts: list[VerticalContext], config: dict[str, Any], run_at: str) -> None:
    for target in contexts:
        target_base = {cfg.domain for cfg in target.base_sources}
        for peer in contexts:
            if peer.slug == target.slug:
                continue
            for domain, record in (peer.registry.get("candidates", {}) or {}).items():
                if domain in target_base or is_non_publisher_domain(domain, _blocked_domains(config)):
                    continue
                evidence = record.get("discovery_evidence") or []
                url = "https://" + domain + "/"
                if evidence and isinstance(evidence[0], dict) and evidence[0].get("url"):
                    url = str(evidence[0]["url"])
                record_candidate_discovery(
                    target.registry,
                    domain=domain,
                    provider="cross_vertical",
                    query=f"candidate observed in {peer.slug}",
                    discovery_url=url,
                    timestamp=run_at,
                    base_domains=target_base,
                )


def _same_source_domain(url: str, domain: str) -> bool:
    host = candidate_domain_from_url(url)
    return bool(host and (host == domain or host.endswith("." + domain)))


def _looks_trap_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return True
    query_pairs = [part for part in parsed.query.split("&") if part]
    path = parsed.path.lower()
    if len(query_pairs) > 4:
        return True
    if any(token in path for token in ("/calendar/", "/search/", "/feed/", "/page/999", "/wp-json/")):
        return True
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) > 10:
        return True
    return any(segments.count(segment) >= 4 for segment in set(segments))


def _select_evenly(urls: list[str], count: int) -> list[str]:
    urls = list(dict.fromkeys(urls))
    if len(urls) <= count:
        return urls
    if count <= 1:
        return [urls[0]]
    indices = [round(position * (len(urls) - 1) / (count - 1)) for position in range(count)]
    return [urls[index] for index in dict.fromkeys(indices)]


def _recipe_jsonld(soup: BeautifulSoup) -> dict[str, Any] | None:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for obj in jsonld_objects(soup):
        typ = obj.get("@type")
        types = typ if isinstance(typ, list) else [typ]
        if not any(str(value).lower() == "recipe" for value in types if value is not None):
            continue
        ingredients = obj.get("recipeIngredient") or []
        instructions = _instruction_texts(obj)
        score = (
            int(bool(obj.get("name")))
            + int(isinstance(ingredients, list) and len(ingredients) >= 3)
            + int(len(instructions) >= 2)
        )
        candidates.append((score, obj))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def analyze_recipe_page(
    html: str,
    *,
    url: str,
    final_url: str,
    domain: str,
    include_pattern: str,
    response_headers: dict[str, Any] | None = None,
) -> SampledPage:
    page = SampledPage(url=url, fetched=True, final_url=final_url, http_status=200, trap_url=_looks_trap_url(url))
    soup = BeautifulSoup(html, "lxml")
    obj = _recipe_jsonld(soup)
    include_re = re.compile(include_pattern, re.I)
    canonical = _canonical_url(soup, final_url)
    page.canonical_url = canonical
    page.canonical_external = not _same_source_domain(canonical, domain)
    page.editorial_link = any(
        re.search(
            r"(?:about|editorial|test[- ]?kitchen|recipe[- ]?development)",
            anchor.get_text(" ", strip=True) + " " + str(anchor.get("href") or ""),
            re.I,
        )
        for anchor in soup.find_all("a", href=True)
    )
    if obj is None:
        extracted, _ = extract_recipe_from_html(
            html, final_url, domain, SourceConfig(domain=domain), response_headers or {}
        )
        page.ranking_extractable = extracted is not None
        page.title = str((soup.title.string if soup.title else "") or "").strip()
        page.vertical_relevant = bool(include_re.search(final_url + " " + page.title))
        return page

    page.is_recipe = True
    page.title = str(obj.get("name") or (soup.title.string if soup.title else "") or final_url).strip()
    raw_ingredients = obj.get("recipeIngredient") or []
    page.ingredients = (
        tuple(str(value).strip() for value in raw_ingredients) if isinstance(raw_ingredients, list) else ()
    )
    page.instructions = _instruction_texts(obj)
    page.author = _author_name(obj)
    publisher = obj.get("publisher")
    if isinstance(publisher, dict):
        page.publisher = str(publisher.get("name") or "").strip()
    elif isinstance(publisher, str):
        page.publisher = publisher.strip()
    page.date_published = str(obj.get("datePublished") or "")
    page.date_modified = str(obj.get("dateModified") or "")
    page.has_yield = bool(obj.get("recipeYield"))
    page.has_time = bool(obj.get("cookTime") or obj.get("prepTime") or obj.get("totalTime"))
    completeness_fields = (
        bool(page.title),
        len(page.ingredients) >= 3,
        len(page.instructions) >= 2,
        page.has_yield,
        page.has_time,
        bool(page.author or page.publisher),
    )
    page.field_completeness = sum(int(value) for value in completeness_fields) / len(completeness_fields)
    page.vertical_relevant = bool(include_re.search(final_url + " " + page.title))

    cfg = SourceConfig(domain=domain)
    visible_rating, visible_count = visible_rating_evidence(soup, cfg)
    aggregate = obj.get("aggregateRating") or {}
    if isinstance(aggregate, dict):
        schema_rating = _parse_number(aggregate.get("ratingValue"))
        count_value = _parse_number(aggregate.get("ratingCount") or aggregate.get("reviewCount"))
        best = _parse_number(aggregate.get("bestRating")) or 5.0
        if schema_rating is not None and count_value is not None and count_value > 0 and best > 0:
            normalized = max(0.0, min(5.0, float(schema_rating) / float(best) * 5.0))
            visible_normalized = visible_rating
            if visible_rating is not None and visible_rating > 5.05:
                visible_normalized = visible_rating / float(best) * 5.0
            confidence, status, _ = _evidence_score(normalized, int(count_value), visible_normalized, visible_count)
            page.has_rating = True
            page.evidence_confidence = confidence
            page.evidence_status = status

    extracted, _ = extract_recipe_from_html(html, final_url, domain, cfg, response_headers or {})
    page.ranking_extractable = extracted is not None
    image = _image_url(obj)
    page.recipe_payload = {
        "title": page.title,
        "source": domain,
        "url": final_url,
        "canonical_url": canonical,
        "ingredients": list(page.ingredients),
        "instructions": list(page.instructions),
        "author": page.author,
        "instruction_simhash": instruction_simhash(page.instructions),
        "image_fingerprint": fingerprint_image_url(image),
        "image_perceptual_hash": "",
    }
    return page


def _candidate_urls(
    context: VerticalContext,
    record: dict[str, Any],
    budget: dict[str, Any],
) -> tuple[SourceConfig, Any, list[str], str, list[str]]:
    domain = str(record["domain"])
    evidence = record.get("discovery_evidence") or []
    discovery_urls = [
        str(item.get("url"))
        for item in evidence
        if isinstance(item, dict) and item.get("url") and _same_source_domain(str(item.get("url")), domain)
    ]
    cfg = SourceConfig(
        domain=domain,
        max_urls=int(budget.get("auto_source_max_urls", 250)),
        delay=float(budget.get("auto_source_delay", 0.25)),
        discovery_urls=tuple(discovery_urls[:5]),
        include_pattern=context.include_pattern,
        allow_unmatched_discovery_links=False,
        origin="discovered",
        pinned=bool(record.get("pinned", False)),
    )
    session = make_session()
    parser, sitemaps, _, robots_status = robots_and_sitemaps(session, cfg)
    if robots_status != "ok":
        return cfg, parser, [], robots_status, sitemaps

    include_re = re.compile(context.include_pattern, re.I)
    urls: list[str] = []
    scan_cap = int(budget.get("candidate_url_scan_cap", 200))
    for discovery_url in discovery_urls:
        if include_re.search(discovery_url):
            urls.append(discovery_url)
    seen_sitemaps: set[str] = set()
    for sitemap in sitemaps:
        for item in iter_sitemap_records(
            session,
            sitemap,
            seen=seen_sitemaps,
            max_docs=int(budget.get("max_candidate_sitemap_docs", 40)),
        ):
            url = str(item.get("url") or "")
            if not url or not _same_source_domain(url, domain) or not include_re.search(url):
                continue
            try:
                if not parser.can_fetch(UA, url):
                    continue
            except Exception:
                continue
            urls.append(url)
            if len(urls) >= scan_cap:
                break
        if len(urls) >= scan_cap:
            break

    if len(urls) < int(budget.get("max_candidate_pages_per_domain", 10)):
        entry_points = discovery_urls + [f"https://{domain}/"]
        for entry in entry_points[:3]:
            try:
                if not parser.can_fetch(UA, entry):
                    continue
                response = safe_get(session, entry, 20)
                soup = BeautifulSoup(response.text, "lxml")
            except Exception:
                continue
            for anchor in soup.find_all("a", href=True):
                href = urljoin(entry, str(anchor.get("href") or "").strip()).split("#", 1)[0]
                text = anchor.get_text(" ", strip=True)
                if _same_source_domain(href, domain) and include_re.search(href + " " + text):
                    urls.append(href)
                    if len(urls) >= scan_cap:
                        break
    return cfg, parser, list(dict.fromkeys(urls)), robots_status, sitemaps


def _freshness_score(pages: list[SampledPage], run_at: str) -> float:
    now = parse_dt(run_at) or datetime.now(timezone.utc)
    dated = 0
    recent = 0
    for page in pages:
        stamp = parse_dt(page.date_modified) or parse_dt(page.date_published)
        if not stamp:
            continue
        dated += 1
        if (now - stamp).days <= 730:
            recent += 1
    if not dated:
        return 50.0
    return 40.0 + 60.0 * (recent / dated)


def _novelty_ratio(pages: list[SampledPage], existing_recipes: list[dict[str, Any]]) -> float:
    sampled = [page.recipe_payload for page in pages if page.recipe_payload]
    if not sampled:
        return 0.0
    if not existing_recipes:
        return 1.0
    novel = 0
    for recipe in sampled:
        max_similarity = 0.0
        title_tokens = set(normalize_text(recipe.get("title", "")).split())
        plausible = []
        for existing in existing_recipes:
            existing_tokens = set(normalize_text(existing.get("title", "")).split())
            if title_tokens and existing_tokens and title_tokens & existing_tokens:
                plausible.append(existing)
        for existing in plausible[:500]:
            max_similarity = max(max_similarity, duplicate_similarity(recipe, existing))
            if max_similarity >= DEDUPE_THRESHOLD:
                break
        if max_similarity < DEDUPE_THRESHOLD:
            novel += 1
    return novel / len(sampled)


def _within_source_duplicate_ratio(pages: list[SampledPage]) -> float:
    recipes = [page.recipe_payload for page in pages if page.recipe_payload]
    if len(recipes) < 2:
        return 0.0
    duplicate_members: set[int] = set()
    for left in range(len(recipes)):
        for right in range(left + 1, len(recipes)):
            if duplicate_similarity(recipes[left], recipes[right]) >= DEDUPE_THRESHOLD:
                duplicate_members.update({left, right})
    return len(duplicate_members) / len(recipes)


def qualification_metrics(
    pages: list[SampledPage],
    *,
    candidate_url_count: int,
    robots_status: str,
    run_at: str,
    existing_recipes: list[dict[str, Any]],
) -> dict[str, Any]:
    sampled = len(pages)
    fetched_pages = [page for page in pages if page.fetched]
    recipe_pages = [page for page in fetched_pages if page.is_recipe]
    relevant_pages = [page for page in recipe_pages if page.vertical_relevant]
    rated_pages = [page for page in recipe_pages if page.has_rating]
    conflicts = [page for page in rated_pages if page.evidence_status == "conflict"]
    canonical_external = [page for page in recipe_pages if page.canonical_external]
    ranking_extractable = [page for page in recipe_pages if page.ranking_extractable]
    trap_pages = [page for page in pages if page.trap_url]
    editorial = [page for page in recipe_pages if page.author or page.publisher or page.editorial_link]
    substantive = [page for page in recipe_pages if len(page.ingredients) >= 3 and len(page.instructions) >= 2]
    completeness = [page.field_completeness for page in recipe_pages]
    evidence_confidences = [page.evidence_confidence for page in rated_pages]

    rating_signatures = [(round(page.evidence_confidence, 2), page.evidence_status) for page in rated_pages]
    suspicious_uniform_ratings = bool(
        len(rating_signatures) >= 5
        and len(set(rating_signatures)) == 1
        and all(page.evidence_status == "schema_only" for page in rated_pages)
    )

    metrics = {
        "pages_sampled": sampled,
        "pages_fetched": len(fetched_pages),
        "recipes_recognized": len(recipe_pages),
        "recipes_extracted": len(ranking_extractable),
        "qualifying_vertical_recipe_count": candidate_url_count,
        "sample_recipe_yield": len(recipe_pages) / sampled if sampled else 0.0,
        "fetch_success_rate": len(fetched_pages) / sampled if sampled else 0.0,
        "recipe_structure_rate": len(recipe_pages) / len(fetched_pages) if fetched_pages else 0.0,
        "vertical_relevance_ratio": len(relevant_pages) / len(recipe_pages) if recipe_pages else 0.0,
        "extraction_success_rate": len(ranking_extractable) / len(recipe_pages) if recipe_pages else 0.0,
        "substantive_recipe_ratio": len(substantive) / len(recipe_pages) if recipe_pages else 0.0,
        "field_completeness": statistics.mean(completeness) if completeness else 0.0,
        "editorial_provenance_ratio": len(editorial) / len(recipe_pages) if recipe_pages else 0.0,
        "rating_coverage_ratio": len(rated_pages) / len(recipe_pages) if recipe_pages else 0.0,
        "rating_conflict_ratio": len(conflicts) / len(rated_pages) if rated_pages else 0.0,
        "mean_rating_evidence_confidence": statistics.mean(evidence_confidences) if evidence_confidences else None,
        "external_canonical_ratio": len(canonical_external) / len(recipe_pages) if recipe_pages else 0.0,
        "trap_url_ratio": len(trap_pages) / sampled if sampled else 0.0,
        "within_source_duplicate_ratio": _within_source_duplicate_ratio(recipe_pages),
        "novelty_ratio": _novelty_ratio(recipe_pages, existing_recipes),
        "freshness_score": _freshness_score(recipe_pages, run_at),
        "robots_status": robots_status,
        "suspicious_uniform_rating_evidence": suspicious_uniform_ratings,
    }
    evidence_pages = [page for page in recipe_pages if page.has_rating or page.ranking_extractable]
    metrics["ranking_evidence_pages"] = len(evidence_pages)
    metrics["ranking_evidence_coverage_ratio"] = len(evidence_pages) / len(recipe_pages) if recipe_pages else 0.0
    metrics["ranking_row_yield"] = len(ranking_extractable) / len(recipe_pages) if recipe_pages else 0.0
    metrics["extraction_success_rate"] = len(ranking_extractable) / len(evidence_pages) if evidence_pages else None
    return metrics


def score_source_quality(metrics: dict[str, Any], policy: dict[str, Any]) -> tuple[float, dict[str, float]]:
    weights = policy.get("weights", {}) or {}
    relevance = 100.0 * min(
        1.0,
        0.65 * float(metrics.get("vertical_relevance_ratio") or 0.0)
        + 0.35
        * min(
            1.0,
            float(metrics.get("qualifying_vertical_recipe_count") or 0)
            / max(1.0, float(policy.get("target_vertical_recipe_count") or 20)),
        ),
    )
    conditional_extraction = metrics.get("extraction_success_rate")
    if conditional_extraction is None:
        extraction_unit = (
            0.50 * float(metrics.get("recipe_structure_rate") or 0.0)
            + 0.30 * float(metrics.get("field_completeness") or 0.0)
            + 0.20 * float(metrics.get("substantive_recipe_ratio") or 0.0)
        )
    else:
        extraction_unit = (
            0.35 * float(metrics.get("recipe_structure_rate") or 0.0)
            + 0.25 * float(metrics.get("field_completeness") or 0.0)
            + 0.20 * float(metrics.get("substantive_recipe_ratio") or 0.0)
            + 0.20 * float(conditional_extraction)
        )
    extraction = 100.0 * min(1.0, extraction_unit)
    editorial = 100.0 * min(1.0, float(metrics.get("editorial_provenance_ratio") or 0.0))
    crawl = 100.0 * min(1.0, float(metrics.get("fetch_success_rate") or 0.0))
    rated = float(metrics.get("rating_coverage_ratio") or 0.0)
    conflicts = float(metrics.get("rating_conflict_ratio") or 0.0)
    # Missing ratings are neutral, not disqualifying. When ratings do exist,
    # consistency with the visible/structured evidence framework determines trust.
    rating_integrity = 70.0 if rated == 0 else max(0.0, 100.0 * (1.0 - conflicts))
    if bool(metrics.get("suspicious_uniform_rating_evidence")):
        rating_integrity = max(0.0, rating_integrity - 25.0)
    novelty = 100.0 * min(1.0, float(metrics.get("novelty_ratio") or 0.0))
    freshness = max(0.0, min(100.0, float(metrics.get("freshness_score") or 50.0)))
    general = 100.0 * max(
        0.0,
        min(
            1.0,
            0.55 * (1.0 - float(metrics.get("within_source_duplicate_ratio") or 0.0))
            + 0.30 * float(metrics.get("substantive_recipe_ratio") or 0.0)
            + 0.15 * (1.0 - float(metrics.get("trap_url_ratio") or 0.0)),
        ),
    )
    components = {
        "vertical_relevance": relevance,
        "extraction_reliability": extraction,
        "editorial_provenance": editorial,
        "crawl_stability": crawl,
        "rating_integrity": rating_integrity,
        "unique_contribution": novelty,
        "freshness": freshness,
        "general_quality": general,
    }
    total_weight = sum(float(weights.get(name, 0.0)) for name in components)
    if total_weight <= 0:
        raise ValueError("source quality weights must sum to a positive value")
    score = sum(components[name] * float(weights.get(name, 0.0)) for name in components) / total_weight
    return round(score, 3), {name: round(value, 3) for name, value in components.items()}


def hard_gate_failures(metrics: dict[str, Any], policy: dict[str, Any]) -> tuple[list[str], list[str]]:
    hard = policy.get("hard_gates", {}) or {}
    permanent: list[str] = []
    temporary: list[str] = []
    if str(metrics.get("robots_status") or "").startswith("unavailable"):
        temporary.append("robots_unavailable")
    if int(metrics.get("pages_sampled") or 0) < int(hard.get("min_pages_sampled", 5)):
        temporary.append("insufficient_sample")
    if int(metrics.get("pages_fetched") or 0) < int(hard.get("min_pages_fetched", 4)):
        temporary.append("insufficient_fetches")
    if int(metrics.get("qualifying_vertical_recipe_count") or 0) < int(hard.get("min_vertical_recipe_count", 5)):
        temporary.append("insufficient_vertical_recipe_body")
    if float(metrics.get("fetch_success_rate") or 0.0) < float(hard.get("min_fetch_success_rate", 0.70)):
        temporary.append("low_fetch_success")
    if float(metrics.get("recipe_structure_rate") or 0.0) < float(hard.get("min_recipe_structure_rate", 0.55)):
        permanent.append("low_recipe_structure_yield")
    if float(metrics.get("vertical_relevance_ratio") or 0.0) < float(hard.get("min_vertical_relevance_ratio", 0.40)):
        permanent.append("low_vertical_relevance")
    if float(metrics.get("substantive_recipe_ratio") or 0.0) < float(hard.get("min_substantive_recipe_ratio", 0.55)):
        permanent.append("thin_recipe_content")
    if float(metrics.get("external_canonical_ratio") or 0.0) > float(hard.get("max_external_canonical_ratio", 0.25)):
        permanent.append("external_canonical_or_mirror_behavior")
    if float(metrics.get("within_source_duplicate_ratio") or 0.0) > float(
        hard.get("max_within_source_duplicate_ratio", 0.65)
    ):
        permanent.append("extreme_internal_duplicate_content")
    if float(metrics.get("trap_url_ratio") or 0.0) > float(hard.get("max_trap_url_ratio", 0.30)):
        permanent.append("crawler_trap_risk")
    if float(metrics.get("rating_conflict_ratio") or 0.0) > float(hard.get("max_rating_conflict_ratio", 0.50)):
        permanent.append("rating_evidence_conflicts")
    evidence_pages = int(metrics.get("ranking_evidence_pages") or 0)
    extraction_success = metrics.get("extraction_success_rate")
    if (
        evidence_pages >= int(hard.get("min_ranking_evidence_pages_for_extraction_gate", 3))
        and extraction_success is not None
        and float(extraction_success) < float(hard.get("min_extraction_success_rate", 0.60))
    ):
        permanent.append("ranking_extractor_incompatible")
    return permanent, temporary


def qualify_candidate(
    context: VerticalContext,
    record: dict[str, Any],
    config: dict[str, Any],
    budget: dict[str, Any],
    run_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    policy = config.get("quality_gate", {}) or {}
    domain = str(record["domain"])
    try:
        cfg, parser, candidate_urls, robots_status, sitemaps = _candidate_urls(context, record, budget)
    except UnsafeNetworkTarget as exc:
        return (
            {
                "domain": domain,
                "quality_score": 0.0,
                "hard_failures": ["unsafe_network_target"],
                "temporary_failures": [],
                "error": str(exc),
                "pages_sampled": 0,
                "pages_fetched": 0,
                "qualifying_vertical_recipe_count": 0,
            },
            {},
        )
    if robots_status != "ok":
        metrics = {
            "domain": domain,
            "quality_score": 0.0,
            "hard_failures": [],
            "temporary_failures": ["robots_unavailable"],
            "robots_status": robots_status,
            "pages_sampled": 0,
            "pages_fetched": 0,
            "qualifying_vertical_recipe_count": 0,
        }
        return metrics, {}

    sample_count = int(budget.get("max_candidate_pages_per_domain", 10))
    sample_urls = _select_evenly(candidate_urls, sample_count)
    pages: list[SampledPage] = []
    session = make_session()
    for url in sample_urls:
        page = SampledPage(url=url, trap_url=_looks_trap_url(url))
        try:
            if not parser.can_fetch(UA, url):
                page.error = "robots_denied"
                pages.append(page)
                continue
        except Exception:
            page.error = "robots_check_failed"
            pages.append(page)
            continue
        try:
            response = safe_get(session, url, 25)
            page = analyze_recipe_page(
                response.text,
                url=url,
                final_url=str(response.url or url),
                domain=domain,
                include_pattern=context.include_pattern,
                response_headers=dict(response.headers),
            )
            page.http_status = response.status_code
        except Exception as exc:
            page.error = type(exc).__name__
        pages.append(page)
        delay = float(budget.get("candidate_page_delay", 0.10))
        if delay > 0:
            time.sleep(delay)

    existing_recipes = [
        dict(value) for value in (context.state.get("recipes", {}) or {}).values() if isinstance(value, dict)
    ]
    metrics = qualification_metrics(
        pages,
        candidate_url_count=len(candidate_urls),
        robots_status=robots_status,
        run_at=run_at,
        existing_recipes=existing_recipes,
    )
    score, components = score_source_quality(metrics, policy)
    metrics.update({"domain": domain, "quality_score": score, "quality_components": components})
    permanent, temporary = hard_gate_failures(metrics, policy)
    metrics["hard_failures"] = permanent
    metrics["temporary_failures"] = temporary
    crawl_config = {
        "max_urls": int(budget.get("auto_source_max_urls", 250)),
        "delay": float(budget.get("auto_source_delay", 0.25)),
        "sitemap_urls": list(dict.fromkeys(sitemaps))[:10],
        "discovery_urls": list(dict.fromkeys(sample_urls))[:5],
        "include_pattern": context.include_pattern,
        "allow_unmatched_discovery_links": False,
    }
    return metrics, crawl_config


def _promotion_thresholds(config: dict[str, Any]) -> dict[str, Any]:
    policy = config.get("quality_gate", {}) or {}
    return {
        "qualification_score": float(policy.get("qualification_score", 74.0)),
        "promotion_min_qualifying_attempts": int(policy.get("promotion_min_qualifying_attempts", 2)),
        "deep_fast_track_score": float(policy.get("deep_fast_track_score", 88.0)),
        "deep_fast_track_min_pages": int(policy.get("deep_fast_track_min_pages", 10)),
        "source_gate_version": SOURCE_GATE_VERSION,
    }


def _apply_qualification_result(
    context: VerticalContext,
    record: dict[str, Any],
    metrics: dict[str, Any],
    crawl_config: dict[str, Any],
    config: dict[str, Any],
    mode: str,
    run_at: str,
) -> str:
    domain = str(record["domain"])
    thresholds = _promotion_thresholds(config)
    policy = config.get("quality_gate", {}) or {}
    previous_status = str(record.get("status") or CANDIDATE)
    record["qualification_attempts"] = int(record.get("qualification_attempts") or 0) + 1
    record["last_evaluated_at"] = run_at
    record["quality_score"] = float(metrics.get("quality_score") or 0.0)
    record["last_qualification_metrics"] = metrics
    if crawl_config:
        record["crawl_config"] = crawl_config

    permanent = list(metrics.get("hard_failures") or [])
    temporary = list(metrics.get("temporary_failures") or [])
    if permanent:
        record["consecutive_qualifying_attempts"] = 0
        run_datetime = parse_dt(run_at) or datetime.now(timezone.utc)
        cooldown_days = int(policy.get("automatic_rejection_cooldown_days", 30))
        record["rejection_cooldown_until"] = (run_datetime + timedelta(days=cooldown_days)).isoformat()
        transition_source(
            context.registry,
            domain,
            REJECTED,
            "hard source gate failed: " + ", ".join(permanent),
            timestamp=run_at,
            metrics=metrics,
            thresholds=thresholds,
            event="SOURCE_REJECTED",
        )
        return REJECTED
    if temporary:
        record["consecutive_qualifying_attempts"] = 0
        transition_source(
            context.registry,
            domain,
            QUARANTINED,
            "qualification incomplete: " + ", ".join(temporary),
            timestamp=run_at,
            metrics=metrics,
            thresholds=thresholds,
            event="SOURCE_QUARANTINED",
        )
        return QUARANTINED

    score = float(metrics.get("quality_score") or 0.0)
    qualification_score = float(thresholds["qualification_score"])
    if score < qualification_score:
        record["consecutive_qualifying_attempts"] = 0
        transition_source(
            context.registry,
            domain,
            QUARANTINED,
            f"quality score {score:.1f} below qualification threshold {qualification_score:.1f}",
            timestamp=run_at,
            metrics=metrics,
            thresholds=thresholds,
            event="SOURCE_QUARANTINED",
        )
        return QUARANTINED

    if previous_status == SUSPENDED:
        recovery = int(record.get("recovery_qualifying_attempts") or 0) + 1
        record["recovery_qualifying_attempts"] = recovery
        required = int(policy.get("suspended_recovery_qualifying_attempts", 2))
        if recovery >= required:
            record["recovery_qualifying_attempts"] = 0
            transition_source(
                context.registry,
                domain,
                ACTIVE,
                f"recovered after {recovery} healthy requalification attempts",
                timestamp=run_at,
                metrics=metrics,
                thresholds=thresholds,
                event="SOURCE_RECOVERED",
            )
            return ACTIVE
        # Remain suspended while proving recovery. The evaluation is still auditable.
        context.registry.setdefault("audit", []).append(
            {
                "event": "SOURCE_EVALUATED",
                "domain": domain,
                "vertical": context.slug,
                "previous_state": SUSPENDED,
                "new_state": SUSPENDED,
                "reason": f"healthy recovery evaluation {recovery}/{required}",
                "metrics": metrics,
                "thresholds": thresholds,
                "timestamp": run_at,
                "source_gate_version": SOURCE_GATE_VERSION,
            }
        )
        return SUSPENDED

    consecutive = int(record.get("consecutive_qualifying_attempts") or 0) + 1
    record["consecutive_qualifying_attempts"] = consecutive
    transition_source(
        context.registry,
        domain,
        QUALIFIED,
        f"source quality gate passed at {score:.1f}",
        timestamp=run_at,
        metrics=metrics,
        thresholds=thresholds,
        event="SOURCE_EVALUATED",
    )
    fast_track = bool(
        mode == "deep"
        and score >= float(thresholds["deep_fast_track_score"])
        and int(metrics.get("pages_fetched") or 0) >= int(thresholds["deep_fast_track_min_pages"])
        and float(metrics.get("fetch_success_rate") or 0.0) >= 0.90
        and float(metrics.get("recipe_structure_rate") or 0.0) >= 0.80
        and float(metrics.get("vertical_relevance_ratio") or 0.0) >= 0.65
        and float(metrics.get("rating_conflict_ratio") or 0.0) == 0.0
    )
    enough_runs = consecutive >= int(thresholds["promotion_min_qualifying_attempts"])
    if fast_track or enough_runs:
        reason = (
            "deep high-confidence fast-track" if fast_track else f"passed {consecutive} independent qualification runs"
        )
        transition_source(
            context.registry,
            domain,
            PROMOTED,
            reason,
            timestamp=run_at,
            metrics=metrics,
            thresholds=thresholds,
            event="SOURCE_PROMOTED",
        )
        return PROMOTED

    transition_source(
        context.registry,
        domain,
        QUARANTINED,
        f"qualified once; awaiting confirmation ({consecutive}/{thresholds['promotion_min_qualifying_attempts']})",
        timestamp=run_at,
        metrics=metrics,
        thresholds=thresholds,
        event="SOURCE_QUARANTINED",
    )
    return QUARANTINED


def _recent_source_statuses(state: dict[str, Any], domain: str, limit: int = 8) -> list[str]:
    statuses: list[str] = []
    for run in reversed(state.get("source_history", []) or []):
        for row in run.get("coverage", []) or []:
            if row.get("source") != domain or row.get("status") == "not_checked_this_run":
                continue
            statuses.append(str(row.get("status") or "ok"))
            break
        if len(statuses) >= limit:
            break
    return statuses


def update_promoted_source_lifecycle(context: VerticalContext, config: dict[str, Any], run_at: str) -> None:
    lifecycle = config.get("lifecycle", {}) or {}
    degrade_after = int(lifecycle.get("degrade_after_consecutive_failures", 3))
    suspend_after = int(lifecycle.get("suspend_after_consecutive_failures", 5))
    recover_after = int(lifecycle.get("recover_after_consecutive_successes", 2))
    for domain, record in (context.registry.get("candidates", {}) or {}).items():
        status = str(record.get("status") or "")
        if status not in {PROMOTED, ACTIVE, DEGRADED}:
            continue
        statuses = _recent_source_statuses(context.state, domain, max(suspend_after, recover_after) + 2)
        if not statuses:
            continue
        consecutive_bad = 0
        consecutive_good = 0
        for item in statuses:
            if item == "ok":
                if consecutive_bad:
                    break
                consecutive_good += 1
            else:
                if consecutive_good:
                    break
                consecutive_bad += 1
        record["consecutive_healthy_runs"] = consecutive_good
        record["consecutive_degraded_runs"] = consecutive_bad
        if bool(record.get("pinned")):
            if consecutive_bad >= degrade_after:
                context.counters["pinned_source_warnings"] = context.counters.get("pinned_source_warnings", 0.0) + 1.0
            continue
        if status == PROMOTED and consecutive_good >= 1:
            transition_source(
                context.registry,
                domain,
                ACTIVE,
                "first healthy production crawl after promotion",
                timestamp=run_at,
                event="SOURCE_ACTIVE",
            )
        elif status == ACTIVE and consecutive_bad >= degrade_after:
            transition_source(
                context.registry,
                domain,
                DEGRADED,
                f"{consecutive_bad} consecutive degraded production checks",
                timestamp=run_at,
                event="SOURCE_DEGRADED",
            )
        elif status == DEGRADED and consecutive_bad >= suspend_after:
            transition_source(
                context.registry,
                domain,
                SUSPENDED,
                f"{consecutive_bad} consecutive degraded production checks",
                timestamp=run_at,
                event="SOURCE_SUSPENDED",
            )
        elif status == DEGRADED and consecutive_good >= recover_after:
            transition_source(
                context.registry,
                domain,
                ACTIVE,
                f"{consecutive_good} consecutive healthy production checks",
                timestamp=run_at,
                event="SOURCE_RECOVERED",
            )


def _evaluation_queue(
    context: VerticalContext, config: dict[str, Any], budget: dict[str, Any], mode: str, run_at: str
) -> list[dict[str, Any]]:
    limit = int(budget.get("max_candidate_domains_evaluated", budget.get("max_candidate_domains_per_run", 20)))
    now = parse_dt(run_at) or datetime.now(timezone.utc)
    eligible: list[dict[str, Any]] = []
    for record in (context.registry.get("candidates", {}) or {}).values():
        status = str(record.get("status") or "")
        if bool(record.get("rediscovery_blocked")):
            continue
        if status in {PROMOTED, ACTIVE, DEGRADED}:
            continue
        if status == SUSPENDED and mode != "deep":
            continue
        if status == REJECTED:
            cooldown = parse_dt(record.get("rejection_cooldown_until"))
            if cooldown and cooldown > now:
                continue
        eligible.append(record)
    priority = {SUSPENDED: 5, QUALIFIED: 4, QUARANTINED: 3, CANDIDATE: 2, REJECTED: 1}
    eligible.sort(
        key=lambda row: (
            priority.get(str(row.get("status") or ""), 0),
            int(row.get("discovery_count") or 0),
            str(row.get("last_discovered_at") or ""),
        ),
        reverse=True,
    )
    return eligible[:limit]


def _promoted_catalog_discovery(
    context: VerticalContext,
    record: dict[str, Any],
    mode: str,
    budget: dict[str, Any],
    run_at: str,
) -> dict[str, Any] | None:
    configs = effective_source_configs(context.base_sources, context.registry)
    cfg = next((item for item in configs if item.domain == record.get("domain")), None)
    if not cfg:
        return None
    try:
        return discover_source_urls(
            cfg,
            context.state,
            "deep" if mode == "deep" else "daily",
            run_at,
            global_max_urls=int(budget.get("promotion_catalog_discovery_cap", 250)),
        )
    except Exception as exc:
        return {
            "source": cfg.domain,
            "status": f"promotion_discovery_error:{type(exc).__name__}",
            "new_urls": 0,
            "discovered_urls": 0,
        }


def _metrics_for_context(
    context: VerticalContext,
    evaluated: list[dict[str, Any]],
    run_at: str,
) -> dict[str, Any]:
    summary = source_registry_summary(context.base_sources, context.registry)
    status_counts = summary.get("candidate_status_counts", {}) or {}
    scores = [float(row.get("quality_score") or 0.0) for row in evaluated if row.get("quality_score") is not None]
    pages_fetched = sum(int(row.get("pages_fetched") or 0) for row in evaluated)
    recipes_recognized = sum(int(row.get("recipes_recognized") or 0) for row in evaluated)
    promoted_this_run = sum(
        1
        for event in context.registry.get("audit", [])[context.audit_start :]
        if event.get("event") == "SOURCE_PROMOTED"
    )
    rejected_this_run = sum(
        1
        for event in context.registry.get("audit", [])[context.audit_start :]
        if event.get("event") == "SOURCE_REJECTED"
    )
    quarantined_this_run = sum(
        1
        for event in context.registry.get("audit", [])[context.audit_start :]
        if event.get("event") == "SOURCE_QUARANTINED"
    )
    return {
        "generated_at": run_at,
        "vertical": context.slug,
        "source_gate_version": SOURCE_GATE_VERSION,
        "candidate_domains_discovered": sum(
            int(record.get("discovery_count") or 0)
            for record in (context.registry.get("candidates", {}) or {}).values()
        ),
        "candidate_domains_new": len(context.new_candidate_domains),
        "candidate_domains_evaluated": len(evaluated),
        "candidate_domains_rejected": rejected_this_run,
        "candidate_domains_quarantined": quarantined_this_run,
        "candidate_domains_promoted": promoted_this_run,
        "candidate_domains_active": int(status_counts.get(ACTIVE, 0)),
        "candidate_domains_degraded": int(status_counts.get(DEGRADED, 0)),
        "candidate_domains_suspended": int(status_counts.get(SUSPENDED, 0)),
        "promotion_rate": promoted_this_run / len(evaluated) if evaluated else 0.0,
        "median_source_quality_score": statistics.median(scores)
        if scores
        else summary.get("median_source_quality_score"),
        "qualification_pages_fetched": pages_fetched,
        "qualification_extraction_success_rate": recipes_recognized / pages_fetched if pages_fetched else 0.0,
        "effective_source_count": summary["effective_source_count"],
        "manual_source_count": summary["manual_source_count"],
        "auto_source_count": summary["auto_source_count"],
        "catalog_url_count": len(context.state.get("url_catalog", {}) or {}),
        "provider_status": context.provider_status,
        "pinned_source_warnings": int(context.counters.get("pinned_source_warnings", 0.0)),
        "evaluations": evaluated,
    }


def _run_source_expansion(
    config_path: str | Path,
    *,
    mode: str,
    seed_file: str | Path | None = None,
    dry_run: bool = False,
    run_at: str | None = None,
) -> dict[str, Any]:
    if mode not in {"daily", "deep", "smoke"}:
        raise ValueError("source expansion mode must be daily, deep, or smoke")
    run_at = run_at or now_iso()
    config_path = Path(config_path).resolve()
    config = load_source_discovery_config(config_path)
    contexts = _load_contexts(config, load_verticals(config_path))
    budget = _mode_budget(config, mode)
    search_providers = _providers(config)
    seeds = _load_seed_hits(seed_file, contexts)
    deadline: float | None = None

    if mode != "smoke":
        max_seconds = max(0.0, float(budget.get("max_discovery_seconds", 900)))
        deadline = time.monotonic() + max_seconds
        for context in contexts:
            update_promoted_source_lifecycle(context, config, run_at)
            for hit in seeds.get(context.slug, []):
                _record_hit(context, hit, config, run_at)
            if not _deadline_reached(deadline):
                _discover_search_candidates(context, config, budget, search_providers, run_at, deadline=deadline)
            if not _deadline_reached(deadline):
                _discover_outbound_candidates(context, config, budget, run_at, deadline=deadline)
        _cross_seed(contexts, config, run_at)

    aggregate: dict[str, Any] = {
        "generated_at": run_at,
        "mode": mode,
        "source_gate_version": SOURCE_GATE_VERSION,
        "verticals": {},
    }
    for context in contexts:
        evaluated: list[dict[str, Any]] = []
        if mode != "smoke":
            max_promotions = int(budget.get("max_new_promotions_per_run", 5))
            promotions = 0
            for record in _evaluation_queue(context, config, budget, mode, run_at):
                if _deadline_reached(deadline):
                    break
                metrics, crawl_config = qualify_candidate(context, record, config, budget, run_at)
                evaluated.append(metrics)
                outcome = _apply_qualification_result(context, record, metrics, crawl_config, config, mode, run_at)
                if outcome in {PROMOTED, ACTIVE} and str(record.get("status")) in {PROMOTED, ACTIVE}:
                    if outcome == PROMOTED:
                        promotions += 1
                    metrics["production_activation"] = (
                        "effective allowlist immediately; URL catalog expansion on the next vertical daily/deep discovery run"
                    )
                if promotions >= max_promotions:
                    break

        metrics_payload = _metrics_for_context(context, evaluated, run_at)
        aggregate["verticals"][context.slug] = metrics_payload
        if not dry_run:
            save_source_registry(context.registry_path, context.registry)
            context.output_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(context.output_dir / "source_expansion.json", metrics_payload, default=str)
            new_audit = context.registry.get("audit", [])[context.audit_start :]
            write_run_records(context.events_dir, new_audit, run_at)

    if not dry_run:
        aggregate_path = Path(str(config.get("aggregate_output_path") or "output/source_expansion_all.json"))
        if not aggregate_path.is_absolute():
            aggregate_path = config_path.parent.parent / aggregate_path
        atomic_write_json(aggregate_path, aggregate, default=str)
    return aggregate


def run_source_expansion(
    config_path: str | Path,
    *,
    mode: str,
    seed_file: str | Path | None = None,
    dry_run: bool = False,
    run_at: str | None = None,
) -> dict[str, Any]:
    lock_target = Path(config_path).resolve().with_suffix(".source-expansion")
    with exclusive_lock(lock_target):
        return _run_source_expansion(
            config_path,
            mode=mode,
            seed_file=seed_file,
            dry_run=dry_run,
            run_at=run_at,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover, qualify, and govern Recipe Intelligence source publishers")
    parser.add_argument("--config", default="config/source_discovery.yaml")
    parser.add_argument("--mode", choices=("daily", "deep", "smoke"), default="daily")
    parser.add_argument("--seed-file", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run_source_expansion(
        args.config,
        mode=args.mode,
        seed_file=args.seed_file,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
