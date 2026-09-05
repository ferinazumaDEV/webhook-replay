from __future__ import annotations

import pytest

from webhook_replay.redact import (
    has_sensitive,
    is_sensitive,
    redact_headers,
    redact_value,
)


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "authorization",
        "Proxy-Authorization",
        "Cookie",
        "Set-Cookie",
        "X-Hub-Signature-256",
        "Stripe-Signature",
        "X-Api-Key",
        "X-API_KEY",
        "x-apikey",
        "X-Auth-Token",
        "X-Shared-Secret",
        "X-Shopify-Hmac-Sha256",
        "Paypal-Transmission-Sig",
        "X-Webhook-Hmac",
        "Sig",
    ],
)
def test_sensitive_names_are_detected(name):
    assert is_sensitive(name)


@pytest.mark.parametrize(
    "name",
    [
        "Content-Type",
        "User-Agent",
        "Host",
        "X-Request-Id",
        "Accept",
        "X-Forwarded-For",
        "X-Design-Id",  # contains "sig", but not as a segment
        "X-Signal-Strength",
    ],
)
def test_ordinary_names_are_left_alone(name):
    assert not is_sensitive(name)


def test_redact_value_keeps_a_known_scheme():
    assert redact_value("Bearer EXAMPLE-VALUE") == "Bearer <redacted>"
    assert redact_value("Basic dXNlcjpwYXNz") == "Basic <redacted>"


def test_redact_value_masks_everything_else_whole():
    assert redact_value("t=1699,v1=abc123") == "<redacted>"
    assert redact_value("session=deadbeef") == "<redacted>"
    # An unknown first word is not a scheme, so nothing leaks through.
    assert redact_value("EXAMPLE-VALUE def") == "<redacted>"


def test_redact_headers_preserves_order_and_names():
    headers = [
        ("Content-Type", "application/json"),
        ("Authorization", "Bearer tok"),
        ("X-Request-Id", "req_1"),
    ]
    assert redact_headers(headers) == [
        ("Content-Type", "application/json"),
        ("Authorization", "Bearer <redacted>"),
        ("X-Request-Id", "req_1"),
    ]


def test_redact_headers_show_secrets_is_a_passthrough():
    headers = [("Authorization", "Bearer tok")]
    assert redact_headers(headers, show_secrets=True) == headers


def test_redact_headers_does_not_mutate_the_input():
    headers = [("Authorization", "Bearer tok")]
    redact_headers(headers)
    assert headers == [("Authorization", "Bearer tok")]


def test_has_sensitive():
    assert has_sensitive([("Cookie", "a=b")])
    assert not has_sensitive([("Content-Type", "text/plain")])
    assert not has_sensitive([])
