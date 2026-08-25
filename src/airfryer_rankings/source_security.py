from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from requests.cookies import extract_cookies_to_jar

from .models import HEADERS

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_PRIVATE_HOST_SUFFIXES = (".internal", ".local", ".localhost", ".home", ".lan")
_BLOCKED_EXACT_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}
_METADATA_IPS = {
    "169.254.169.254",
    "100.100.100.200",
}

# These hosts are useful infrastructure, social/search/shopping services, link
# shorteners/affiliate routers, aggregators, or generic publication platforms rather
# than independent recipe publishers. They are rejected during candidate discovery
# but are not part of the lower-level SSRF policy.
NON_PUBLISHER_SUFFIXES = (
    "amazon.com",
    "amazonaws.com",
    "amzn.to",
    "amzlink.to",
    "apple.com",
    "barnesandnoble.com",
    "bestbuy.com",
    "bing.com",
    "bit.ly",
    "cloudfront.net",
    "doubleclick.net",
    "duckduckgo.com",
    "ebay.com",
    "etsy.com",
    "facebook.com",
    "fb.com",
    "flipboard.com",
    "geni.us",
    "google.com",
    "googleapis.com",
    "googlesyndication.com",
    "instagram.com",
    "instacart.com",
    "linkedin.com",
    "linktr.ee",
    "lnkd.in",
    "pinterest.com",
    "reddit.com",
    "rstyle.me",
    "shopify.com",
    "shopstyle.com",
    "t.co",
    "target.com",
    "tiktok.com",
    "tinyurl.com",
    "twitter.com",
    "walmart.com",
    "wayfair.com",
    "x.com",
    "youtube.com",
    "yummly.com",
)


class UnsafeNetworkTarget(ValueError):
    """Raised when an untrusted discovery URL could reach a non-public network target."""


class ResponseTooLarge(requests.RequestException):
    """Raised before an external response can exceed its configured memory budget."""


Resolver = Callable[..., list[tuple]]


def _materialize_bounded(response: requests.Response, max_bytes: int) -> None:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    try:
        declared_size = int(response.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        declared_size = 0
    if declared_size > max_bytes:
        response.close()
        raise ResponseTooLarge(
            f"response declared {declared_size} bytes; limit is {max_bytes}",
            response=response,
        )
    content = response.raw.read(max_bytes + 1, decode_content=True)
    if len(content) > max_bytes:
        response.close()
        raise ResponseTooLarge(f"response exceeded {max_bytes} bytes", response=response)
    response._content = content
    response._content_consumed = True


def normalize_candidate_domain(value: str) -> str | None:
    """Normalize a discovered hostname without collapsing meaningful subdomains.

    Only a leading ``www.`` alias is collapsed. ``recipes.example.com`` therefore
    remains distinct from ``example.com`` until a maintainer or future registrable-
    domain policy explicitly chooses otherwise.
    """

    raw = str(value or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if len(host) > 253 or any(not label or len(label) > 63 for label in host.split(".")):
        return None
    if "." not in host:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    return host


def candidate_domain_from_url(url: str) -> str | None:
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    return normalize_candidate_domain(parsed.hostname or "")


def is_non_publisher_domain(domain: str, extra_blocked: Iterable[str] = ()) -> bool:
    normalized = normalize_candidate_domain(domain)
    if not normalized:
        return True
    blocked = tuple(str(value).lower().lstrip(".") for value in extra_blocked if str(value).strip())
    for suffix in (*NON_PUBLISHER_SUFFIXES, *blocked):
        if normalized == suffix or normalized.endswith("." + suffix):
            return True
    labels = normalized.split(".")
    infrastructure_tokens = {
        "account",
        "accounts",
        "ads",
        "analytics",
        "assets",
        "auth",
        "cdn",
        "id",
        "images",
        "img",
        "login",
        "media",
        "oauth",
        "sso",
        "static",
    }
    return bool(labels and labels[0] in infrastructure_tokens)


def _public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if str(ip) in _METADATA_IPS:
        return False
    return bool(ip.is_global)


def resolve_public_addresses(host: str, resolver: Resolver = socket.getaddrinfo) -> tuple[str, ...]:
    normalized = normalize_candidate_domain(host)
    if not normalized:
        raise UnsafeNetworkTarget(f"invalid hostname: {host!r}")
    if normalized in _BLOCKED_EXACT_HOSTS or normalized.endswith(_PRIVATE_HOST_SUFFIXES):
        raise UnsafeNetworkTarget(f"private hostname is not allowed: {normalized}")
    try:
        answers = resolver(normalized, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeNetworkTarget(f"hostname did not resolve publicly: {normalized}") from exc
    addresses = sorted({str(answer[4][0]) for answer in answers if len(answer) >= 5 and answer[4]})
    if not addresses:
        raise UnsafeNetworkTarget(f"hostname has no address records: {normalized}")
    unsafe = [address for address in addresses if not _public_ip(address)]
    if unsafe:
        raise UnsafeNetworkTarget(f"hostname resolves to non-public address(es): {normalized}: {unsafe}")
    return tuple(addresses)


def _normalize_public_url(url: str) -> str:
    raw = str(url or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise UnsafeNetworkTarget("malformed URL") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeNetworkTarget(f"unsupported URL scheme: {scheme or '<missing>'}")
    if parsed.username or parsed.password:
        raise UnsafeNetworkTarget("URLs containing credentials are not allowed")
    host = parsed.hostname or ""
    normalized = normalize_candidate_domain(host)
    if not normalized:
        raise UnsafeNetworkTarget(f"invalid public hostname: {host!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeNetworkTarget("malformed URL port") from exc
    if port is not None and port not in {80, 443}:
        raise UnsafeNetworkTarget(f"non-standard network port is not allowed: {port}")
    netloc = normalized
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


def validate_public_url(url: str, resolver: Resolver = socket.getaddrinfo) -> str:
    normalized_url = _normalize_public_url(url)
    host = urlsplit(normalized_url).hostname or ""
    resolve_public_addresses(host, resolver=resolver)
    return normalized_url


def _host_header(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port
    default_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != default_port:
        return f"{host}:{port}"
    return host


def _ip_netloc(address: str, port: int | None) -> str:
    ip = ipaddress.ip_address(address)
    host = f"[{ip}]" if ip.version == 6 else str(ip)
    if port is not None:
        return f"{host}:{port}"
    return host


class _PinnedIPAdapter(HTTPAdapter):
    """Connect to one validated IP while preserving the publisher HTTP/TLS identity."""

    def __init__(self, connect_ip: str, server_hostname: str, host_header: str, *, max_retries: Any = 0) -> None:
        self._connect_ip = connect_ip
        self._server_hostname = server_hostname
        self._host_header = host_header
        super().__init__(max_retries=max_retries)

    def build_connection_pool_key_attributes(
        self,
        request: requests.PreparedRequest,
        verify: Any,
        cert: Any = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        host_params["host"] = self._connect_ip
        if host_params.get("scheme") == "https":
            pool_kwargs["server_hostname"] = self._server_hostname
            pool_kwargs["assert_hostname"] = self._server_hostname
        return host_params, pool_kwargs

    def add_headers(self, request: requests.PreparedRequest, **kwargs: Any) -> None:
        request.headers["Host"] = self._host_header

    def request_url(self, request: requests.PreparedRequest, proxies: dict[str, str] | None) -> str:
        target = super().request_url(request, proxies)
        if "://" not in target:
            return target
        parsed = urlsplit(target)
        return urlunsplit(
            (
                parsed.scheme,
                _ip_netloc(self._connect_ip, parsed.port),
                parsed.path or "/",
                parsed.query,
                "",
            )
        )


def _send_pinned(
    session: requests.Session,
    url: str,
    connect_ip: str,
    timeout: int,
    headers: dict[str, str],
    *,
    max_bytes: int,
) -> requests.Response:
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    prepared = session.prepare_request(requests.Request("GET", url, headers=headers))
    original_adapter = session.get_adapter(url)
    adapter = _PinnedIPAdapter(
        connect_ip,
        hostname,
        _host_header(url),
        max_retries=getattr(original_adapter, "max_retries", 0),
    )
    settings = session.merge_environment_settings(prepared.url or url, {}, False, session.verify, session.cert)
    try:
        response = adapter.send(
            prepared,
            stream=True,
            timeout=timeout,
            verify=settings["verify"],
            cert=settings["cert"],
            proxies=settings["proxies"],
        )
        extract_cookies_to_jar(session.cookies, prepared, response.raw)
        if response.status_code in _REDIRECT_STATUSES:
            response.close()
            response._content = b""
            response._content_consumed = True
        else:
            _materialize_bounded(response, max_bytes)
        return response
    finally:
        adapter.close()


def safe_get(
    session: requests.Session,
    url: str,
    timeout: int = 20,
    headers: dict | None = None,
    *,
    max_redirects: int = 5,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    resolver: Resolver = socket.getaddrinfo,
) -> requests.Response:
    """GET untrusted public-web content without re-resolving an approved target.

    Each redirect destination is normalized and resolved once. Every returned DNS
    address must be public, and the transport connects directly to one of those
    validated IPs while preserving the original hostname for HTTP Host and HTTPS
    SNI/certificate verification. This closes the DNS-rebinding gap between target
    validation and the actual connection.
    """

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    merged = dict(HEADERS)
    if headers:
        merged.update(headers)
    current = _normalize_public_url(url)
    for redirect_number in range(max_redirects + 1):
        parsed = urlsplit(current)
        hostname = parsed.hostname or ""
        addresses = resolve_public_addresses(hostname, resolver=resolver)
        response: requests.Response | None = None
        last_error: requests.RequestException | None = None
        for address in addresses:
            try:
                response = _send_pinned(session, current, address, timeout, merged, max_bytes=max_bytes)
                break
            except requests.RequestException as exc:
                last_error = exc
        if response is None:
            if last_error is not None:
                raise last_error
            raise UnsafeNetworkTarget(f"no validated connection target available for {hostname}")
        if response.status_code in _REDIRECT_STATUSES:
            if redirect_number >= max_redirects:
                raise requests.TooManyRedirects(f"more than {max_redirects} redirects for {url}")
            location = str(response.headers.get("Location") or "").strip()
            if not location:
                response.raise_for_status()
                return response
            current = _normalize_public_url(urljoin(current, location))
            continue
        if response.status_code != 304:
            response.raise_for_status()
        return response
    raise requests.TooManyRedirects(f"more than {max_redirects} redirects for {url}")
