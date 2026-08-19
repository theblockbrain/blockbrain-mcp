"""
Structured JSON logging via structlog.

Two things every log line gets:

  • a `request_id` bound by `CorrelationIdMiddleware`, taken from the incoming
    W3C `traceparent` or `X-Request-Id` when present, so a tool call can be
    traced across this server and the caller.
  • a redaction pass that drops secrets, masks e-mail local parts, and trims
    free-text fields before anything reaches stdout.

⭐ CUSTOMIZE HERE — add your own field names to `_SECRET_KEYS` or `_TEXT_KEYS`
   when you log something new.
"""

from __future__ import annotations

import logging
import re
import sys
import uuid
from collections.abc import MutableMapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import get_settings

REDACTED = "[redacted]"

_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "brave_api_key",
        "cookie",
        "client_secret",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "set-cookie",
        "token",
        "x-subscription-token",
    }
)
_EMAIL_KEYS = frozenset({"email", "mail", "preferred_username", "upn", "user_email"})
_URL_KEYS = frozenset({"url", "location", "endpoint", "final_url"})
_TEXT_KEYS = frozenset({"query", "topic"})
_TEXT_PREVIEW_CHARS = 200

_TRACEPARENT = re.compile(r"^\d{2}-([0-9a-f]{32})-[0-9a-f]{16}-\d{2}$")


def safe_url(url: str) -> str:
    """Drop the query string and any inline credentials before logging a URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    if not parts.scheme:
        return REDACTED
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def mask_email(email: str) -> str:
    """Keep the domain and the first character, which is enough to support a user."""
    local, _, domain = email.partition("@")
    if not domain or not local:
        return REDACTED
    return f"{local[0]}***@{domain}"


def _redact_value(key: str, value: Any) -> Any:
    if key in _SECRET_KEYS:
        return REDACTED
    if key in _EMAIL_KEYS and isinstance(value, str):
        return mask_email(value)
    if key in _URL_KEYS and isinstance(value, str):
        return safe_url(value)
    if key in _TEXT_KEYS and isinstance(value, str) and len(value) > _TEXT_PREVIEW_CHARS:
        return f"{value[:_TEXT_PREVIEW_CHARS]}..."
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return {key: _redact_value(key.lower(), value) for key, value in event_dict.items()}


def new_request_id(traceparent: str | None, request_id: str | None) -> str:
    match = _TRACEPARENT.match((traceparent or "").strip())
    if match:
        return match.group(1)
    candidate = (request_id or "").strip()
    if candidate and len(candidate) <= 128:
        return candidate
    return uuid.uuid4().hex


class CorrelationIdMiddleware:
    """Bind a `request_id` for the duration of a request and echo it back."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        request_id = new_request_id(headers.get("traceparent"), headers.get("x-request-id"))

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(
                    (b"x-request-id", request_id.encode("latin-1"))
                )
            await send(message)

        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            await self.app(scope, receive, send_with_header)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")


def configure_logging() -> None:
    level = getattr(logging, get_settings().mcp_log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
