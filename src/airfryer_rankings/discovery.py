from __future__ import annotations

import re
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .http import get_for_source, iter_sitemap_records, make_session, robots_and_sitemaps
from .models import UA, SourceConfig
from .url_normalization import normalize_discovered_url


def _catalog_update(
    state: dict, cfg: SourceConfig, url: str, run_at: str, *, lastmod: str = "", method: str = "sitemap"
) -> bool:
    catalog = state.setdefault("url_catalog", {})
    normalized_url = normalize_discovered_url(url)
    existing = catalog.get(normalized_url, {})
    is_new = not bool(existing)
    changed_lastmod = bool(lastmod and existing.get("lastmod") and lastmod != existing.get("lastmod"))
    entry = dict(existing)
    entry.update(
        {
            "url": normalized_url,
            "source": cfg.domain,
            "lastmod": lastmod or existing.get("lastmod", ""),
            "first_discovered": existing.get("first_discovered", run_at),
            "last_discovered": run_at,
            "discovery_method": method,
        }
    )
    if changed_lastmod:
        entry["priority"] = "modified"
    elif is_new:
        entry["priority"] = "new"
    catalog[normalized_url] = entry
    return is_new


def _same_domain(url: str, domain: str) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host == domain or host.endswith("." + domain)


def _compile_include_pattern(cfg: SourceConfig) -> re.Pattern[str]:
    try:
        return re.compile(cfg.include_pattern, re.I)
    except re.error as exc:
        raise ValueError(f"Invalid include_pattern for {cfg.domain}: {cfg.include_pattern!r}") from exc


def _looks_recipe_link(
    url: str,
    text: str,
    domain: str,
    include_re: re.Pattern[str],
    *,
    allow_unmatched: bool = True,
) -> bool:
    if not _same_domain(url, domain):
        return False
    parsed = urlparse(url)
    path = parsed.path.lower()
    if path in ("", "/") or any(
        x in path for x in ("/category/", "/tag/", "/author/", "/about", "/contact", "/privacy")
    ):
        return False
    if include_re.search(url + " " + text):
        return True
    if not allow_unmatched:
        return False
    # Backward-compatible mode for trusted vertical pages whose recipe slugs omit the cooking method.
    return path.count("/") >= 2 and not path.endswith((".jpg", ".jpeg", ".png", ".webp", ".pdf"))


def _discovery_get(session, url: str, cfg: SourceConfig, timeout: int = 25):
    return get_for_source(session, url, cfg, timeout)


def _sitemap_records(
    session,
    sitemap: str,
    cfg: SourceConfig,
    seen_sitemaps: set[str],
    max_docs: int,
    diagnostics: dict,
):
    return iter_sitemap_records(
        session,
        sitemap,
        seen=seen_sitemaps,
        max_docs=max_docs,
        diagnostics=diagnostics,
    )


def discover_source_urls(
    cfg: SourceConfig,
    state: dict,
    mode: str,
    run_at: str,
    global_max_urls: int | None = None,
) -> dict:
    started = time.monotonic()
    session = make_session()
    parser, sitemaps, _, robots_status = robots_and_sitemaps(session, cfg)
    include_re = _compile_include_pattern(cfg)
    seen_sitemaps: set[str] = set()
    diagnostics: dict = {"attempted": 0, "succeeded": 0, "errors": []}
    max_docs = 300 if mode == "deep" else 120
    matched = 0
    newly_discovered = 0
    discovery_page_links = 0
    match_cap = (
        20000
        if mode == "deep" and global_max_urls is None
        else max(2000, (global_max_urls or cfg.max_urls) * (12 if mode == "deep" else 6))
    )

    for sitemap in sitemaps:
        for record in _sitemap_records(session, sitemap, cfg, seen_sitemaps, max_docs, diagnostics):
            url = normalize_discovered_url(record["url"])
            if not _same_domain(url, cfg.domain) or not include_re.search(url):
                continue
            try:
                if not parser.can_fetch(UA, url):
                    continue
            except Exception:
                pass
            newly_discovered += int(
                _catalog_update(state, cfg, url, run_at, lastmod=record.get("lastmod", ""), method="sitemap")
            )
            matched += 1
            if matched >= match_cap:
                break
        if matched >= match_cap:
            break

    for discovery_url in cfg.discovery_urls:
        try:
            if not parser.can_fetch(UA, discovery_url):
                continue
        except Exception:
            pass
        diagnostics["attempted"] += 1
        try:
            response = _discovery_get(session, discovery_url, cfg, 25)
            soup = BeautifulSoup(response.text, "lxml")
            diagnostics["succeeded"] += 1
        except Exception as exc:
            diagnostics["errors"].append(f"{discovery_url}:{type(exc).__name__}")
            continue
        for anchor in soup.find_all("a", href=True):
            href = normalize_discovered_url(urljoin(discovery_url, str(anchor.get("href") or "").strip()))
            text = anchor.get_text(" ", strip=True)
            if not _looks_recipe_link(
                href,
                text,
                cfg.domain,
                include_re,
                allow_unmatched=cfg.allow_unmatched_discovery_links,
            ):
                continue
            newly_discovered += int(_catalog_update(state, cfg, href, run_at, method="category"))
            discovery_page_links += 1

    status = robots_status
    if status == "ok" and diagnostics["errors"]:
        status = "degraded"
    return {
        "source": cfg.domain,
        "discovered_urls": matched + discovery_page_links,
        "new_urls": newly_discovered,
        "sitemap_docs": len(seen_sitemaps),
        "robots_status": robots_status,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "status": status,
        "discovery_errors": diagnostics["errors"],
    }
