"""
Outbound-fetch guard for tools that follow caller-supplied URLs.

Every hop is checked before a request goes out: scheme, optional domain
allow-list, and the resolved IP addresses. The connection is then pinned to
the address that was just validated (see `check_url_target`) rather than
letting the HTTP client re-resolve the host, which would reopen a DNS
rebinding gap between the check and the connect. Redirects are followed
manually so each new location goes through the same checks, and the body is
read with a byte cap so a large or endless response cannot fill the model's
context.

⭐ CUSTOMIZE HERE — set `READ_URL_ALLOWED_DOMAINS` to pin the tool to a fixed
   set of hosts, which is the strongest option when you know them up front.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

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


def _bracketed(ip: IpAddress) -> str:
    return f"[{ip}]" if isinstance(ip, ipaddress.IPv6Address) else str(ip)


async def check_url_target(url: str, allowed_domains: list[str] | None = None) -> tuple[str, str]:
    """Validate a URL and every address its host resolves to.

    A literal IP host is validated directly. All resolved addresses must be
    public, so a name with one public and one internal record is still rejected.

    Returns `(host, pinned_address)`, where `pinned_address` is one of the
    just-validated addresses, formatted for use in a URL (IPv6 bracketed).
    The caller must connect to `pinned_address` rather than re-resolving
    `host`, otherwise a second, independent DNS lookup at connect time could
    return a different — and unvalidated — address (DNS rebinding).
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
    return host, _bracketed(addresses[0])


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


async def fetch_document(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    user_agent: str,
    allowed_domains: list[str] | None = None,
    max_redirects: int = 5,
) -> tuple[str, str]:
    """Fetch a document over https and return `(final_url, text)`.

    Redirects are resolved one hop at a time because httpx only validates the
    URL it is handed, so an open redirect would otherwise walk straight into the
    internal network.

    Each request connects to the specific IP address that was just validated
    (via a rewritten request URL, an explicit `Host` header, and TLS SNI set to
    the original hostname) instead of letting httpx resolve the hostname again
    at connect time. Without this, a host that resolves to a public address on
    the validation lookup could resolve to an internal one on the connection's
    own, independent lookup moments later (DNS rebinding), walking straight
    past the check above. Pin `READ_URL_ALLOWED_DOMAINS` for extra safety when
    the target hosts are known.
    """
    current = url
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": user_agent},
    ) as client:
        for _ in range(max_redirects + 1):
            host, pinned_address = await check_url_target(current, allowed_domains)

            parts = urlsplit(current)
            netloc = f"{pinned_address}:{parts.port}" if parts.port else pinned_address
            pinned_url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

            request = client.build_request(
                "GET",
                pinned_url,
                headers={"Host": host},
                extensions={"sni_hostname": host},
            )
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
