"""
Outbound-fetch guard for tools that follow caller-supplied URLs.

Every hop is checked before a request goes out: scheme, optional domain
allow-list, and the resolved IP addresses. The connection is then pinned to
one of the addresses that was just validated (see `check_url_target`) via a
custom `httpcore` network backend, rather than letting the HTTP client
re-resolve the host, which would reopen a DNS rebinding gap between the
check and the connect. The hostname itself stays in the request the whole
time, so httpx's own connection-pool keying, TLS SNI, IDNA encoding, and
`Host` header generation are untouched and still correct. Redirects are
followed manually so each new location goes through the same checks, and the
body is read with a byte cap so a large or endless response cannot fill the
model's context.

`_PinnedTransport` connects directly and ignores `trust_env`, so an
`HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY` in the environment is not honoured —
fetches go straight to the pinned address rather than through the proxy.
That preserves the pinning guarantee (a proxy would do its own DNS lookup,
defeating it), but it is a silent behaviour change for a deployment that
relies on an egress proxy: such fetches now go direct instead of failing
loudly.

⭐ CUSTOMIZE HERE — set `READ_URL_ALLOWED_DOMAINS` to pin the tool to a fixed
   set of hosts, which is the strongest option when you know them up front.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlsplit

import httpcore
import httpx

_ALLOWED_SCHEMES = frozenset({"https"})
_ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/xml")

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UnsafeUrlError(ValueError):
    """Raised when a URL is rejected before or during a fetch."""


class ResponseTooLargeError(UnsafeUrlError):
    """Raised when a response body exceeds the configured byte cap."""


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses smuggled inside an IPv6 one, which route to the v4 target."""
    embedded: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped:
        embedded.append(ip.ipv4_mapped)
    if ip.sixtofour:
        embedded.append(ip.sixtofour)
    if ip.teredo:
        embedded.extend(ip.teredo)
    return embedded


def is_public_ip(ip: IpAddress) -> bool:
    """True when the address is publicly routable.

    `is_global` is the stdlib's own registry check. It already excludes loopback,
    RFC 1918, link-local, carrier-grade NAT, multicast, and reserved ranges.
    """
    candidates: list[IpAddress] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        candidates.extend(_embedded_ipv4(ip))
    return all(candidate.is_global for candidate in candidates)


def _host_matches_allowlist(host: str, allowed_domains: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in (d.lower().lstrip(".") for d in allowed_domains)
    )


def check_url(url: str, allowed_domains: list[str] | None = None) -> str:
    """Validate scheme and host without touching the network. Returns the host."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            f"Refusing to fetch {parts.scheme or '(no scheme)'}:// URL. "
            f"Allowed schemes: {', '.join(sorted(_ALLOWED_SCHEMES))}."
        )

    host = parts.hostname
    if not host:
        raise UnsafeUrlError("URL has no host.")

    if parts.username or parts.password:
        raise UnsafeUrlError("Refusing to fetch a URL carrying inline credentials.")

    if allowed_domains and not _host_matches_allowlist(host, allowed_domains):
        raise UnsafeUrlError(f"Host {host} is not on READ_URL_ALLOWED_DOMAINS.")

    return host


async def _resolve(host: str, port: int) -> list[IpAddress]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve host {host}.") from exc
    return [ipaddress.ip_address(info[4][0]) for info in infos]


async def check_url_target(
    url: str, allowed_domains: list[str] | None = None
) -> tuple[str, list[str]]:
    """Validate a URL and every address its host resolves to.

    A literal IP host is validated directly. All resolved addresses must be
    public, so a name with one public and one internal record is still rejected.

    Returns `(host, addresses)`, the just-validated addresses in resolution
    order. The caller must connect directly to one of `addresses` rather than
    letting the HTTP client re-resolve `host`, otherwise a second, independent
    DNS lookup at connect time could return a different — and unvalidated —
    address (DNS rebinding).
    """
    host = check_url(url, allowed_domains)
    parts = urlsplit(url)

    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None

    addresses = [literal] if literal else await _resolve(host, parts.port or 443)
    unsafe = [str(ip) for ip in addresses if not is_public_ip(ip)]
    if unsafe:
        raise UnsafeUrlError(
            f"Host {host} resolves to a non-public address ({', '.join(unsafe)}). "
            "Fetching internal or reserved ranges is not allowed."
        )
    return host, [str(ip) for ip in addresses]


def _check_content_type(resp: httpx.Response) -> None:
    """Refuse non-document media. A response with no Content-Type is let through."""
    content_type = resp.headers.get("content-type", "")
    media_type = content_type.split(";")[0].strip().lower()
    if media_type and not media_type.startswith(_ALLOWED_CONTENT_TYPES):
        raise UnsafeUrlError(f"Refusing to read {media_type} content as a document.")


async def _read_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    declared = resp.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise ResponseTooLargeError(f"Response exceeds the {max_bytes} byte cap.")

    body = bytearray()
    async for chunk in resp.aiter_bytes():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise ResponseTooLargeError(f"Response exceeds the {max_bytes} byte cap.")
    return bytes(body)


class _PinnedResolutionBackend(httpcore.AsyncNetworkBackend):
    """Dials pre-validated addresses instead of letting the resolver run again.

    `fetch_document` pins the addresses `check_url_target` just validated for
    a `(host, port)` before sending each request; `connect_tcp` then tries
    those addresses, in order, instead of resolving `host` itself. The
    hostname stays in the request throughout — only the TCP destination is
    overridden — so the connection pool's key, TLS SNI, IDNA encoding, and
    the `Host` header are all still derived by httpx/httpcore the normal way.
    Trying every validated address (rather than pinning to a single one)
    preserves the failover a plain hostname connection would otherwise get.
    """

    def __init__(self, delegate: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._delegate = delegate or httpcore.AnyIOBackend()
        self._pinned: dict[tuple[str, int], list[str]] = {}

    def pin(self, host: str, port: int, addresses: list[str]) -> None:
        self._pinned[(host, port)] = addresses

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = self._pinned.get((host, port))
        if not addresses:
            raise httpcore.ConnectError(f"No validated address pinned for {host}:{port}.")

        # `timeout` bounds the whole connect, not each address: with several
        # pinned addresses, an unbounded per-attempt timeout could add up to
        # N times the caller's timeout. This only bounds the *total* time,
        # matching a plain hostname connection's overall budget -- unlike
        # anyio's happy-eyeballs, attempts here are sequential rather than
        # concurrent, so a first address that hangs until its share of the
        # deadline still starves the rest of their fair share.
        deadline = None if timeout is None else time.monotonic() + timeout
        last_exc: Exception | None = None
        for address in addresses:
            remaining = timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    last_exc = last_exc or httpcore.ConnectTimeout(
                        f"Timed out connecting to {host}:{port}."
                    )
                    break
            try:
                return await self._delegate.connect_tcp(
                    address,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options=None
    ) -> httpcore.AsyncNetworkStream:
        return await self._delegate.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """`AsyncHTTPTransport` whose network backend dials validated addresses.

    httpx's `AsyncHTTPTransport` doesn't expose a `network_backend`
    constructor parameter, so `super().__init__()` is left to build the
    `httpcore.AsyncConnectionPool` with httpx's usual defaults (connection
    limits, http1/http2, retries, ...) and only the pool's network backend is
    swapped afterwards, rather than reconstructing the pool by hand here.
    """

    def __init__(self, *, delegate: httpcore.AsyncNetworkBackend | None = None) -> None:
        super().__init__()
        self._pinning = _PinnedResolutionBackend(delegate)
        self._pool._network_backend = self._pinning

    def pin(self, host: str, port: int, addresses: list[str]) -> None:
        self._pinning.pin(host, port, addresses)


async def fetch_document(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    user_agent: str,
    allowed_domains: list[str] | None = None,
    max_redirects: int = 5,
    transport: _PinnedTransport | None = None,
) -> tuple[str, str]:
    """Fetch a document over https and return `(final_url, text)`.

    Redirects are resolved one hop at a time because httpx only validates the
    URL it is handed, so an open redirect would otherwise walk straight into the
    internal network.

    Each request is sent, unmodified, for its original hostname, but the
    transport (`_PinnedTransport`) connects to the specific address(es) that
    were just validated instead of letting httpx resolve the hostname again
    at connect time. Without this, a host that resolves to a public address
    on the validation lookup could resolve to an internal one on the
    connection's own, independent lookup moments later (DNS rebinding),
    walking straight past the check above. Pin `READ_URL_ALLOWED_DOMAINS` for
    extra safety when the target hosts are known.

    `transport` is a test seam: callers that mock at the HTTP layer (e.g.
    `respx`, which replaces the transport wholesale) never exercise this
    function's own `transport.pin(...)` call below, so tests that need to
    verify pinning end to end can supply a `_PinnedTransport` wired to a fake
    network backend instead.
    """
    current = url
    transport = transport or _PinnedTransport()
    async with httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": user_agent},
    ) as client:
        for _ in range(max_redirects + 1):
            _host, addresses = await check_url_target(current, allowed_domains)

            request = client.build_request("GET", current)
            transport.pin(request.url.raw_host.decode("ascii"), request.url.port or 443, addresses)

            resp = await client.send(request, stream=True)
            try:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise UnsafeUrlError("Redirect response carried no Location header.")
                    current = str(httpx.URL(current).join(location))
                    continue

                resp.raise_for_status()
                _check_content_type(resp)
                body = await _read_capped(resp, max_bytes)
            finally:
                await resp.aclose()

            return current, body.decode(resp.charset_encoding or "utf-8", errors="replace")

    raise UnsafeUrlError(f"Exceeded {max_redirects} redirects.")
