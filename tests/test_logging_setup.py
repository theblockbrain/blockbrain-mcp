"""Tests for log redaction and request correlation."""

from __future__ import annotations

import pytest
import structlog

from blockbrain_mcp.logging_setup import (
    REDACTED,
    CorrelationIdMiddleware,
    mask_email,
    new_request_id,
    redact_processor,
    safe_url,
)


def _redact(**event) -> dict:
    return dict(redact_processor(None, "info", event))


@pytest.mark.parametrize(
    "key",
    ["authorization", "token", "access_token", "api_key", "brave_api_key", "cookie", "password"],
)
def test_secret_fields_are_dropped(key: str) -> None:
    assert _redact(**{key: "super-secret"})[key] == REDACTED


def test_secret_fields_are_matched_case_insensitively() -> None:
    assert _redact(Authorization="Bearer abc")["Authorization"] == REDACTED


def test_email_keeps_only_the_domain_and_first_character() -> None:
    assert _redact(email="alice@example.com")["email"] == "a***@example.com"


def test_url_query_string_is_dropped() -> None:
    event = _redact(url="https://example.com/search?token=abc&q=secret")
    assert event["url"] == "https://example.com/search"


def test_url_credentials_are_dropped() -> None:
    assert safe_url("https://user:pw@example.com/a") == "https://example.com/a"


def test_url_port_is_kept() -> None:
    assert safe_url("https://example.com:8443/a?b=c") == "https://example.com:8443/a"


def test_non_url_value_is_redacted_rather_than_logged() -> None:
    assert safe_url("not a url") == REDACTED


def test_mask_email_without_domain_is_redacted() -> None:
    assert mask_email("not-an-email") == REDACTED


def test_free_text_is_truncated() -> None:
    event = _redact(query="x" * 500)
    assert len(event["query"]) == 203
    assert event["query"].endswith("...")


def test_ordinary_fields_pass_through() -> None:
    assert _redact(user_id="u-1", event="read_url.request")["user_id"] == "u-1"


# ---------------------------------------------------------------------------
# Correlation id
# ---------------------------------------------------------------------------


def test_traceparent_trace_id_is_reused() -> None:
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert new_request_id(traceparent, None) == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_malformed_traceparent_falls_back_to_the_request_id_header() -> None:
    assert new_request_id("garbage", "req-7") == "req-7"


def test_absent_headers_produce_a_fresh_id() -> None:
    first, second = new_request_id(None, None), new_request_id(None, None)
    assert first != second
    assert len(first) == 32


def test_overlong_request_id_header_is_replaced() -> None:
    assert new_request_id(None, "x" * 500) != "x" * 500


def _start_response_app(seen: dict):
    async def app(scope, receive, send):
        seen.update(structlog.contextvars.get_contextvars())
        await send({"type": "http.response.start", "status": 200, "headers": []})

    return app


def _collector(messages: list[dict]):
    async def send(message: dict) -> None:
        messages.append(message)

    return send


@pytest.mark.asyncio
async def test_middleware_binds_and_echoes_the_request_id() -> None:
    seen: dict = {}
    messages: list[dict] = []

    scope = {"type": "http", "headers": [(b"x-request-id", b"req-42")]}
    await CorrelationIdMiddleware(_start_response_app(seen))(scope, None, _collector(messages))

    assert seen["request_id"] == "req-42"
    assert (b"x-request-id", b"req-42") in messages[0]["headers"]


@pytest.mark.asyncio
async def test_middleware_prefers_the_traceparent_trace_id() -> None:
    seen: dict = {}
    traceparent = b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    scope = {"type": "http", "headers": [(b"traceparent", traceparent), (b"x-request-id", b"r")]}
    await CorrelationIdMiddleware(_start_response_app(seen))(scope, None, _collector([]))

    assert seen["request_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.mark.asyncio
async def test_middleware_unbinds_after_the_request() -> None:
    scope = {"type": "http", "headers": []}
    await CorrelationIdMiddleware(_start_response_app({}))(scope, None, _collector([]))

    assert "request_id" not in structlog.contextvars.get_contextvars()
