from __future__ import annotations

import gzip
from io import BytesIO
from typing import Any, Iterator
from urllib.robotparser import RobotFileParser

import requests
from lxml import etree as ET
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import SourceConfig
from .source_security import DEFAULT_MAX_RESPONSE_BYTES, safe_get

MAX_SITEMAP_BYTES = 50 * 1024 * 1024


def make_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get(
    session: requests.Session,
    url: str,
    timeout: int = 20,
    headers: dict | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> requests.Response:
    return safe_get(session, url, timeout, headers, max_bytes=max_bytes)


def get_for_source(
    session: requests.Session,
    url: str,
    cfg: SourceConfig,
    timeout: int = 20,
    headers: dict | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> requests.Response:
    return get(session, url, timeout, headers, max_bytes=max_bytes)


def _robots_policy_parser(robots_url: str, *, allow: bool) -> RobotFileParser:
    parser = RobotFileParser()
    parser.set_url(robots_url)
    directive = "Allow: /" if allow else "Disallow: /"
    parser.parse(["User-agent: *", directive])
    return parser


def _robots_http_status(exc: requests.HTTPError) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return int(status) if isinstance(status, int) else None


def robots_and_sitemaps(session: requests.Session, cfg: SourceConfig) -> tuple[RobotFileParser, list[str], str, str]:
    """Fetch REP policy with RFC 9309 error semantics.

    A 4xx robots response is "Unavailable" and may be treated as unrestricted.
    A 5xx response or network failure is "Unreachable" and must be treated as
    complete disallow. Unreachable sources intentionally return no sitemap
    fallbacks so discovery does not make secondary requests while access is
    disallowed.
    """
    robots_url = f"https://{cfg.domain}/robots.txt"
    try:
        robots_text = get_for_source(session, robots_url, cfg, 15).text
    except requests.HTTPError as exc:
        status = _robots_http_status(exc)
        if status is not None and 400 <= status <= 499:
            parser = _robots_policy_parser(robots_url, allow=True)
            sitemaps = list(cfg.sitemap_urls) or [f"https://{cfg.domain}/sitemap.xml"]
            return parser, list(dict.fromkeys(sitemaps)), "", "ok"
        detail = f"http_{status}" if status is not None else type(exc).__name__
        return _robots_policy_parser(robots_url, allow=False), [], "", f"unreachable:{detail}"
    except Exception as exc:
        return _robots_policy_parser(robots_url, allow=False), [], "", f"unreachable:{type(exc).__name__}"

    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.parse(robots_text.splitlines())
    except Exception as exc:
        return _robots_policy_parser(robots_url, allow=False), [], robots_text, f"parse_error:{type(exc).__name__}"

    sitemaps = list(cfg.sitemap_urls)
    for line in robots_text.splitlines():
        if line.lower().startswith("sitemap:"):
            value = line.split(":", 1)[1].strip()
            if value:
                sitemaps.append(value)
    if not sitemaps:
        sitemaps = [f"https://{cfg.domain}/sitemap.xml"]
    return parser, list(dict.fromkeys(sitemaps)), robots_text, "ok"


def _xml_bytes(response: requests.Response, url: str, max_bytes: int = MAX_SITEMAP_BYTES) -> bytes:
    content = response.content
    if url.lower().endswith(".gz") or content[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=BytesIO(content)) as compressed:
            content = compressed.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError(f"sitemap exceeds {max_bytes} decompressed bytes")
    return content


def iter_sitemap_records(
    session: requests.Session,
    sitemap_url: str,
    seen: set[str] | None = None,
    max_docs: int = 150,
    diagnostics: dict[str, Any] | None = None,
) -> Iterator[dict]:
    seen = seen if seen is not None else set()
    if sitemap_url in seen or len(seen) >= max_docs:
        return
    seen.add(sitemap_url)
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics["attempted"] = int(diagnostics.get("attempted") or 0) + 1
    errors = diagnostics.setdefault("errors", [])
    try:
        response = get(session, sitemap_url, 30)
        parser = ET.XMLParser(resolve_entities=False, no_network=True, recover=False, huge_tree=False)
        root = ET.fromstring(_xml_bytes(response, sitemap_url), parser=parser)
    except Exception as exc:
        errors.append(f"{sitemap_url}:{type(exc).__name__}")
        return
    diagnostics["succeeded"] = int(diagnostics.get("succeeded") or 0) + 1

    tag = root.tag.lower()
    if tag.endswith("sitemapindex"):
        for child in list(root):
            loc = ""
            for elem in list(child):
                if elem.tag.lower().endswith("loc") and elem.text:
                    loc = elem.text.strip()
                    break
            if loc:
                yield from iter_sitemap_records(session, loc, seen, max_docs=max_docs, diagnostics=diagnostics)
    else:
        for child in list(root):
            loc = ""
            lastmod = ""
            for elem in list(child):
                low = elem.tag.lower()
                if low.endswith("loc") and elem.text:
                    loc = elem.text.strip()
                elif low.endswith("lastmod") and elem.text:
                    lastmod = elem.text.strip()
            if loc:
                yield {"url": loc, "lastmod": lastmod, "sitemap": sitemap_url}
