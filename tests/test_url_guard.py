"""Tests for the outbound-fetch guard."""

from __future__ import annotations

import ipaddress

import httpcore
import httpx
import pytest
import respx

from blockbrain_mcp.url_guard import (
    ResponseTooLargeError,
    UnsafeUrlError,
    _PinnedResolutionBackend,
    _PinnedTransport,
    check_url,
    check_url_target,
    fetch_document,
    is_public_ip,
)

PUBLIC_IP = "93.184.216.34"


@pytest.fixture()
def resolve_public(monkeypatch: pytest.MonkeyPatch):
    """Resolve every host to one public address unless a test overrides it."""

    def _install(addresses: list[str]) -> None:
        async def _fake(host: str, port: int):
            return [ipaddress.ip_address(a) for a in addresses]

        monkeypatch.setattr("blockbrain_mcp.url_guard._resolve", _fake)

    _install([PUBLIC_IP])
    return _install


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "::1",
        "fd00::1",
        "fe80::1",
        "::ffff:169.254.169.254",
        "::ffff:127.0.0.1",
        "2002:a9fe:a9fe::1",
    ],
)
def test_non_public_addresses_are_rejected(address: str) -> None:
    assert not is_public_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", [PUBLIC_IP, "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_accepted(address: str) -> None:
    assert is_public_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "example.com/no-scheme",
    ],
)
def test_non_https_schemes_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        check_url(url)


def test_inline_credentials_are_rejected() -> None:
    with pytest.raises(UnsafeUrlError, match="credentials"):
        check_url("https://user:pass@example.com/")


def test_allowlist_accepts_domain_and_subdomains() -> None:
    assert check_url("https://example.com/a", ["example.com"]) == "example.com"
    assert check_url("https://docs.example.com/a", ["example.com"]) == "docs.example.com"


def test_allowlist_rejects_other_hosts() -> None:
    with pytest.raises(UnsafeUrlError, match="ALLOWED_DOMAINS"):
        check_url("https://evil.com/a", ["example.com"])


def test_allowlist_rejects_suffix_lookalike_host() -> None:
    with pytest.raises(UnsafeUrlError, match="ALLOWED_DOMAINS"):
        check_url("https://notexample.com/a", ["example.com"])


@pytest.mark.asyncio
async def test_literal_private_ip_host_is_rejected_without_dns() -> None:
    with pytest.raises(UnsafeUrlError, match="non-public"):
        await check_url_target("https://169.254.169.254/latest/meta-data/")


@pytest.mark.asyncio
async def test_host_resolving_to_private_address_is_rejected(resolve_public) -> None:
    resolve_public(["10.1.2.3"])
    with pytest.raises(UnsafeUrlError, match="non-public"):
        await check_url_target("https://internal.example.com/")


@pytest.mark.asyncio
async def test_any_private_address_in_the_answer_rejects_the_host(resolve_public) -> None:
    resolve_public([PUBLIC_IP, "127.0.0.1"])
    with pytest.raises(UnsafeUrlError, match="non-public"):
        await check_url_target("https://split.example.com/")


@pytest.mark.asyncio
async def test_public_host_passes(resolve_public) -> None:
    await check_url_target("https://example.com/")


@pytest.mark.asyncio
async def test_check_url_target_returns_every_validated_address(resolve_public) -> None:
    """Every resolved address is returned, not collapsed to a single pick — the
    caller (the pinning network backend) needs the full list to fail over from
    one dead or unroutable address to the next, the way a plain hostname
    connection would."""
    resolve_public([PUBLIC_IP, "8.8.8.8"])
    host, addresses = await check_url_target("https://example.com/")
    assert host == "example.com"
    assert addresses == [PUBLIC_IP, "8.8.8.8"]


@pytest.mark.asyncio
async def test_fetch_returns_body(resolve_public) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get("https://example.com/page").mock(
            return_value=httpx.Response(200, html="<p>hello</p>")
        )
        final_url, text = await _fetch("https://example.com/page")

    assert final_url == "https://example.com/page"
    assert "hello" in text


@pytest.mark.asyncio
async def test_idn_hostname_is_encoded_like_a_plain_fetch(resolve_public) -> None:
    """Regression test: the request URL keeps the caller's hostname (only the
    TCP destination is pinned), so httpx's normal IDNA encoding of a Unicode
    hostname still runs — this used to raise `UnicodeEncodeError` when the
    pinned IP was substituted into the request URL before encoding happened.
    """
    with respx.mock(assert_all_called=True) as router:
        router.get("https://xn--mller-kva.de/p").mock(
            return_value=httpx.Response(200, html="<p>hallo</p>")
        )
        final_url, text = await _fetch("https://müller.de/p")

    assert final_url == "https://müller.de/p"
    assert "hallo" in text


@pytest.mark.asyncio
async def test_redirect_to_private_address_is_blocked(resolve_public) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get("https://example.com/go").mock(
            return_value=httpx.Response(
                302, headers={"location": "https://169.254.169.254/latest/meta-data/"}
            )
        )
        with pytest.raises(UnsafeUrlError, match="non-public"):
            await _fetch("https://example.com/go")


@pytest.mark.asyncio
async def test_redirect_to_http_is_blocked(resolve_public) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get("https://example.com/go").mock(
            return_value=httpx.Response(302, headers={"location": "http://example.com/plain"})
        )
        with pytest.raises(UnsafeUrlError, match="Refusing to fetch http"):
            await _fetch("https://example.com/go")


@pytest.mark.asyncio
async def test_allowed_redirect_is_followed(resolve_public) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get("https://example.com/go").mock(
            return_value=httpx.Response(302, headers={"location": "/final"})
        )
        router.get("https://example.com/final").mock(
            return_value=httpx.Response(200, html="<p>arrived</p>")
        )
        final_url, text = await _fetch("https://example.com/go")

    assert final_url == "https://example.com/final"
    assert "arrived" in text


@pytest.mark.asyncio
async def test_redirect_loop_is_capped(resolve_public) -> None:
    with respx.mock() as router:
        router.get("https://example.com/loop").mock(
            return_value=httpx.Response(302, headers={"location": "https://example.com/loop"})
        )
        with pytest.raises(UnsafeUrlError, match="redirects"):
            await _fetch("https://example.com/loop", max_redirects=2)


@pytest.mark.asyncio
async def test_oversized_body_is_rejected(resolve_public) -> None:
    with respx.mock() as router:
        router.get("https://example.com/big").mock(
            return_value=httpx.Response(200, html="x" * 5_000)
        )
        with pytest.raises(ResponseTooLargeError):
            await _fetch("https://example.com/big", max_bytes=1_000)


@pytest.mark.asyncio
async def test_oversized_content_length_is_rejected_before_reading(resolve_public) -> None:
    with respx.mock() as router:
        router.get("https://example.com/big").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html", "content-length": "999999"},
                content=b"<p>small in practice</p>",
            )
        )
        with pytest.raises(ResponseTooLargeError):
            await _fetch("https://example.com/big", max_bytes=1_000)


@pytest.mark.asyncio
async def test_non_document_content_type_is_rejected(resolve_public) -> None:
    with respx.mock() as router:
        router.get("https://example.com/blob").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "application/octet-stream"}, content=b"\x00\x01"
            )
        )
        with pytest.raises(UnsafeUrlError, match="octet-stream"):
            await _fetch("https://example.com/blob")


@pytest.mark.asyncio
async def test_http_error_status_raises(resolve_public) -> None:
    with respx.mock() as router:
        router.get("https://example.com/missing").mock(return_value=httpx.Response(404))
        with pytest.raises(httpx.HTTPStatusError):
            await _fetch("https://example.com/missing")


async def _fetch(url: str, *, max_bytes: int = 1_000_000, max_redirects: int = 5):
    return await fetch_document(
        url,
        max_bytes=max_bytes,
        timeout=5.0,
        user_agent="test-agent",
        max_redirects=max_redirects,
    )


# --- DNS-pinning transport internals -----------------------------------------
#
# `respx` replaces the whole transport, so it can't see which address the
# pinning backend actually dials. The tests below exercise `_PinnedTransport`
# and `_PinnedResolutionBackend` directly, against a fake network backend that
# never touches a real socket, to verify the properties that matter for the
# DNS-rebinding fix: the *hostname* — not the pinned IP — decides the
# connection-pool key and the TLS SNI, and every validated address is tried,
# not just the first.


class _FakeStream:
    """In-memory stand-in for `httpcore.AsyncNetworkStream`.

    Records the TLS `server_hostname` it was asked to present and replays a
    canned HTTP/1.1 response byte-for-byte — no real socket or TLS involved.
    """

    def __init__(self, response: bytes) -> None:
        self._chunks = [response]
        self.tls_server_hostname: str | None = None

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls_server_hostname = server_hostname
        return self

    def get_extra_info(self, info: str):
        return None


class _FakeBackend:
    """Records every `connect_tcp` call and hands back a scripted response."""

    def __init__(self, response: bytes) -> None:
        self._response = response
        self.connect_calls: list[tuple[str, int]] = []
        self.streams: list[_FakeStream] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connect_calls.append((host, port))
        stream = _FakeStream(self._response)
        self.streams.append(stream)
        return stream

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        pass


_RAW_HTTP_RESPONSE = (
    b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Type: text/plain\r\n\r\nhello"
)


@pytest.mark.asyncio
async def test_different_hostnames_pinned_to_the_same_ip_get_isolated_connections() -> None:
    """Regression test for the connection-reuse gap: two different hostnames
    that both validate to the same IP must not share a pooled connection (and
    therefore its already-negotiated TLS identity). httpcore's pool keys on
    the request's *hostname* origin, which pinning leaves untouched — so even
    though both hops dial the same address, each opens (and TLS-negotiates)
    its own connection with its own hostname's SNI.
    """
    fake = _FakeBackend(_RAW_HTTP_RESPONSE)
    transport = _PinnedTransport(delegate=fake)

    async with httpx.AsyncClient(transport=transport) as client:
        for host in ("a.example.com", "b.example.com"):
            transport.pin(host, 443, [PUBLIC_IP])
            request = client.build_request("GET", f"https://{host}/")
            resp = await client.send(request)
            assert resp.status_code == 200

    assert fake.connect_calls == [(PUBLIC_IP, 443), (PUBLIC_IP, 443)]
    assert [s.tls_server_hostname for s in fake.streams] == ["a.example.com", "b.example.com"]


@pytest.mark.asyncio
async def test_pinned_backend_falls_back_to_the_next_validated_address() -> None:
    """A dead first address must not sink the whole fetch: the backend tries
    every pinned address in order, the way a plain hostname connection would
    via the OS resolver's own address list."""

    class _FlakyDelegate:
        def __init__(self) -> None:
            self.attempts: list[str] = []

        async def connect_tcp(
            self, host, port, timeout=None, local_address=None, socket_options=None
        ):
            self.attempts.append(host)
            if host == "203.0.113.1":
                raise httpcore.ConnectError("unreachable")
            return _FakeStream(_RAW_HTTP_RESPONSE)

    delegate = _FlakyDelegate()
    backend = _PinnedResolutionBackend(delegate)
    backend.pin("example.com", 443, ["203.0.113.1", "203.0.113.2"])

    stream = await backend.connect_tcp("example.com", 443)

    assert delegate.attempts == ["203.0.113.1", "203.0.113.2"]
    assert isinstance(stream, _FakeStream)


@pytest.mark.asyncio
async def test_pinned_backend_refuses_to_connect_without_a_pin() -> None:
    backend = _PinnedResolutionBackend(_FakeBackend(_RAW_HTTP_RESPONSE))
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("example.com", 443)


@pytest.mark.asyncio
async def test_fetch_document_pins_the_validated_addresses(resolve_public) -> None:
    """`fetch_document`'s own `transport.pin(...)` call -- the one line the
    whole DNS-rebinding fix rests on -- is invisible to every other test in
    this file, since `respx` replaces the transport wholesale and never sees
    it run. Inject a real `_PinnedTransport` wired to a fake backend (via the
    `transport` seam) so this exact call path executes.
    """
    fake = _FakeBackend(_RAW_HTTP_RESPONSE)
    transport = _PinnedTransport(delegate=fake)

    final_url, text = await fetch_document(
        "https://example.com/page",
        max_bytes=1_000_000,
        timeout=5.0,
        user_agent="test-agent",
        transport=transport,
    )

    assert final_url == "https://example.com/page"
    assert text == "hello"
    assert fake.connect_calls == [(PUBLIC_IP, 443)]
    assert fake.streams[0].tls_server_hostname == "example.com"


@pytest.mark.asyncio
async def test_pinned_backend_bounds_total_connect_time_across_addresses() -> None:
    """A shared deadline across pinned addresses, not a fresh timeout per
    address -- otherwise N pinned addresses could take up to N times the
    caller's timeout, unlike a plain hostname connection's happy-eyeballs.
    """
    seen_timeouts: list[float | None] = []

    class _SlowDelegate:
        async def connect_tcp(
            self, host, port, timeout=None, local_address=None, socket_options=None
        ):
            seen_timeouts.append(timeout)
            raise httpcore.ConnectError("unreachable")

        async def sleep(self, seconds: float) -> None:
            pass

    backend = _PinnedResolutionBackend(_SlowDelegate())
    backend.pin("example.com", 443, ["203.0.113.1", "203.0.113.2", "203.0.113.3"])

    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("example.com", 443, timeout=1.0)

    assert len(seen_timeouts) == 3
    assert all(t is not None and t <= 1.0 for t in seen_timeouts)
    # Each later attempt gets what's left of the shared 1.0s budget, not a
    # fresh 1.0s of its own -- the clock only moves forward between attempts.
    assert seen_timeouts[0] >= seen_timeouts[-1]
