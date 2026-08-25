from __future__ import annotations

from collections.abc import Callable

import pytest
import requests

import airfryer_rankings.source_security as source_security
from airfryer_rankings.models import UA
from airfryer_rankings.source_security import UnsafeNetworkTarget, safe_get


def _resolver_for(addresses: list[str]) -> Callable[..., list[tuple]]:
    def resolve(host: str, port, type=0):
        return [(2, 1, 6, "", (address, 0)) for address in addresses]

    return resolve


def _response(status: int, url: str, *, location: str | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = url
    response._content = b"ok"
    if location is not None:
        response.headers["Location"] = location
    return response


def test_https_adapter_connects_to_validated_ip_with_original_tls_identity() -> None:
    prepared = requests.Request("GET", "https://example.com/recipe").prepare()
    adapter = source_security._PinnedIPAdapter(
        "93.184.216.34",
        "example.com",
        "example.com",
    )
    try:
        host_params, pool_kwargs = adapter.build_connection_pool_key_attributes(prepared, True)
        adapter.add_headers(prepared)
    finally:
        adapter.close()

    assert host_params["host"] == "93.184.216.34"
    assert host_params["scheme"] == "https"
    assert pool_kwargs["server_hostname"] == "example.com"
    assert pool_kwargs["assert_hostname"] == "example.com"
    assert prepared.headers["Host"] == "example.com"


def test_http_proxy_request_target_uses_validated_ip_not_hostname() -> None:
    prepared = requests.Request("GET", "http://example.com:80/recipe?q=1").prepare()
    adapter = source_security._PinnedIPAdapter(
        "93.184.216.34",
        "example.com",
        "example.com",
    )
    try:
        target = adapter.request_url(prepared, {"http": "http://proxy.example:8080"})
        adapter.add_headers(prepared)
    finally:
        adapter.close()

    assert target == "http://93.184.216.34:80/recipe?q=1"
    assert "example.com" not in target
    assert prepared.headers["Host"] == "example.com"


def test_safe_get_uses_single_dns_snapshot_for_actual_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    resolutions = 0
    connected: list[str] = []

    def rebinding_resolver(host: str, port, type=0):
        nonlocal resolutions
        resolutions += 1
        address = "93.184.216.34" if resolutions == 1 else "127.0.0.1"
        return [(2, 1, 6, "", (address, 0))]

    def send_pinned(session, url: str, connect_ip: str, timeout: int, headers: dict[str, str], *, max_bytes: int):
        connected.append(connect_ip)
        assert headers["User-Agent"] == UA
        assert max_bytes == source_security.DEFAULT_MAX_RESPONSE_BYTES
        return _response(200, url)

    monkeypatch.setattr(source_security, "_send_pinned", send_pinned)

    response = safe_get(requests.Session(), "https://example.com/recipe", resolver=rebinding_resolver)

    assert response.status_code == 200
    assert resolutions == 1
    assert connected == ["93.184.216.34"]


def test_mixed_public_private_dns_answers_are_rejected_before_send(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = False

    def send_pinned(*args, **kwargs):
        nonlocal sent
        sent = True
        return _response(200, "https://example.com/")

    monkeypatch.setattr(source_security, "_send_pinned", send_pinned)

    with pytest.raises(UnsafeNetworkTarget, match="non-public"):
        safe_get(
            requests.Session(),
            "https://example.com/recipe",
            resolver=_resolver_for(["93.184.216.34", "127.0.0.1"]),
        )

    assert not sent


def test_redirect_to_private_target_is_rejected_before_second_request(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str]] = []

    def resolver(host: str, port, type=0):
        address = "93.184.216.34" if host == "example.com" else "127.0.0.1"
        return [(2, 1, 6, "", (address, 0))]

    def send_pinned(session, url: str, connect_ip: str, timeout: int, headers: dict[str, str], *, max_bytes: int):
        sent.append((url, connect_ip))
        return _response(302, url, location="https://private.example/recipe")

    monkeypatch.setattr(source_security, "_send_pinned", send_pinned)

    with pytest.raises(UnsafeNetworkTarget, match="non-public"):
        safe_get(requests.Session(), "https://example.com/recipe", resolver=resolver)

    assert sent == [("https://example.com/recipe", "93.184.216.34")]


def test_safe_get_tries_next_validated_address_after_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def send_pinned(session, url: str, connect_ip: str, timeout: int, headers: dict[str, str], *, max_bytes: int):
        attempts.append(connect_ip)
        if len(attempts) == 1:
            raise requests.ConnectionError("simulated first-address failure")
        return _response(200, url)

    monkeypatch.setattr(source_security, "_send_pinned", send_pinned)

    response = safe_get(
        requests.Session(),
        "https://example.com/recipe",
        resolver=_resolver_for(["93.184.216.34", "93.184.216.35"]),
    )

    assert response.status_code == 200
    assert attempts == ["93.184.216.34", "93.184.216.35"]


def test_ipv6_proxy_target_is_bracketed() -> None:
    prepared = requests.Request("GET", "http://example.com/recipe").prepare()
    adapter = source_security._PinnedIPAdapter(
        "2606:2800:220:1:248:1893:25c8:1946",
        "example.com",
        "example.com",
    )
    try:
        target = adapter.request_url(prepared, {"http": "http://proxy.example:8080"})
    finally:
        adapter.close()

    assert target == "http://[2606:2800:220:1:248:1893:25c8:1946]/recipe"


class _RawBody:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.reads = 0
        self.closed = False

    def read(self, amount: int, *, decode_content: bool) -> bytes:
        self.reads += 1
        assert decode_content is True
        return self.payload[:amount]

    def close(self) -> None:
        self.closed = True


def _streaming_response(payload: bytes, content_length: str | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.raw = _RawBody(payload)
    response._content = False
    response._content_consumed = False
    if content_length is not None:
        response.headers["Content-Length"] = content_length
    return response


def test_bounded_materialization_rejects_declared_and_streamed_overflow() -> None:
    declared = _streaming_response(b"small", "11")
    with pytest.raises(source_security.ResponseTooLarge, match="declared"):
        source_security._materialize_bounded(declared, 10)
    assert declared.raw.reads == 0
    assert declared.raw.closed is True

    streamed = _streaming_response(b"x" * 11, "invalid")
    with pytest.raises(source_security.ResponseTooLarge, match="exceeded"):
        source_security._materialize_bounded(streamed, 10)
    assert streamed.raw.reads == 1
    assert streamed.raw.closed is True


def test_bounded_materialization_preserves_content_at_limit() -> None:
    response = _streaming_response(b"x" * 10)
    source_security._materialize_bounded(response, 10)
    assert response.content == b"x" * 10
