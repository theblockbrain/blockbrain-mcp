"""Tests for the outbound-fetch guard."""

from __future__ import annotations

import ipaddress

import httpx
import pytest
import respx

from blockbrain_mcp.url_guard import (
    ResponseTooLargeError,
    UnsafeUrlError,
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


def _pinned(path: str) -> str:
    """The URL the client actually connects to once `example.com` is pinned."""
    return f"https://{PUBLIC_IP}{path}"


@pytest.mark.asyncio
async def test_fetch_returns_body(resolve_public) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(_pinned("/page"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(200, html="<p>hello</p>")
        )
        final_url, text = await _fetch("https://example.com/page")

    assert final_url == "https://example.com/page"
    assert "hello" in text


@pytest.mark.asyncio
async def test_connection_targets_the_validated_ip_not_the_hostname(resolve_public) -> None:
    """Regression test for the DNS-rebinding TOCTOU gap: the request must be sent
    directly to the IP address that was just validated (with `Host`/SNI set to the
    original hostname for correct routing and certificate checks), not to the
    hostname itself — otherwise the HTTP client would resolve it again,
    independently, at connect time.
    """
    with respx.mock(assert_all_called=True) as router:
        route = router.get(_pinned("/page"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(200, html="<p>hello</p>")
        )
        await _fetch("https://example.com/page")

    sent = route.calls.last.request
    assert sent.url.host == PUBLIC_IP
    assert sent.headers["host"] == "example.com"
    assert sent.extensions.get("sni_hostname") == "example.com"


PUBLIC_IPV6 = "2606:4700:4700::1111"


@pytest.mark.asyncio
async def test_ipv6_literal_host_gets_bracketed_host_header_and_no_sni() -> None:
    """An IP-literal URL must get a correctly bracketed `Host` header (with the
    port, when one is given) and no `sni_hostname` extension: TLS SNI's
    `server_name` (RFC 6066 §3) is defined for DNS names only, never for IP
    literals, and an unbracketed IPv6 address in `Host` is malformed.
    """
    with respx.mock(assert_all_called=True) as router:
        route = router.get(
            f"https://[{PUBLIC_IPV6}]:8443/page", headers={"host": f"[{PUBLIC_IPV6}]:8443"}
        ).mock(return_value=httpx.Response(200, html="<p>hello</p>"))
        await _fetch(f"https://[{PUBLIC_IPV6}]:8443/page")

    sent = route.calls.last.request
    assert sent.headers["host"] == f"[{PUBLIC_IPV6}]:8443"
    assert "sni_hostname" not in sent.extensions


@pytest.mark.asyncio
async def test_redirect_to_private_address_is_blocked(resolve_public) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(_pinned("/go"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(
                302, headers={"location": "https://169.254.169.254/latest/meta-data/"}
            )
        )
        with pytest.raises(UnsafeUrlError, match="non-public"):
            await _fetch("https://example.com/go")


@pytest.mark.asyncio
async def test_redirect_to_http_is_blocked(resolve_public) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(_pinned("/go"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(302, headers={"location": "http://example.com/plain"})
        )
        with pytest.raises(UnsafeUrlError, match="Refusing to fetch http"):
            await _fetch("https://example.com/go")


@pytest.mark.asyncio
async def test_allowed_redirect_is_followed(resolve_public) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(_pinned("/go"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(302, headers={"location": "/final"})
        )
        router.get(_pinned("/final"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(200, html="<p>arrived</p>")
        )
        final_url, text = await _fetch("https://example.com/go")

    assert final_url == "https://example.com/final"
    assert "arrived" in text


@pytest.mark.asyncio
async def test_redirect_loop_is_capped(resolve_public) -> None:
    with respx.mock() as router:
        router.get(_pinned("/loop"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(302, headers={"location": "https://example.com/loop"})
        )
        with pytest.raises(UnsafeUrlError, match="redirects"):
            await _fetch("https://example.com/loop", max_redirects=2)


@pytest.mark.asyncio
async def test_oversized_body_is_rejected(resolve_public) -> None:
    with respx.mock() as router:
        router.get(_pinned("/big"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(200, html="x" * 5_000)
        )
        with pytest.raises(ResponseTooLargeError):
            await _fetch("https://example.com/big", max_bytes=1_000)


@pytest.mark.asyncio
async def test_oversized_content_length_is_rejected_before_reading(resolve_public) -> None:
    with respx.mock() as router:
        router.get(_pinned("/big"), headers={"host": "example.com"}).mock(
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
        router.get(_pinned("/blob"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(
                200, headers={"content-type": "application/octet-stream"}, content=b"\x00\x01"
            )
        )
        with pytest.raises(UnsafeUrlError, match="octet-stream"):
            await _fetch("https://example.com/blob")


@pytest.mark.asyncio
async def test_http_error_status_raises(resolve_public) -> None:
    with respx.mock() as router:
        router.get(_pinned("/missing"), headers={"host": "example.com"}).mock(
            return_value=httpx.Response(404)
        )
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
