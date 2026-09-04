"""Mask sensitive header values on *output*.

Redaction happens when a capture is rendered — ``curl`` export, ``list --json``
and ``show`` — never when it is captured or replayed. A webhook is usually
being debugged *because* its signature is failing, so the stored bytes stay
verbatim and :mod:`webhook_replay.replay` keeps sending the real values; only
what lands on your terminal, your clipboard or a pasted bug report is masked.

Detection is deny-list based on the header *name* only: an exact match against
:data:`SENSITIVE_HEADERS`, or a substring match against
:data:`SENSITIVE_NAME_RE` (``secret``, ``token``, ``signature``, ``api-key``).
Values are never inspected, so a masked header stays recognisable in the output.
"""
from __future__ import annotations

import re

from .storage import Header

#: Header names that always carry a credential, whatever the value looks like.
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "www-authenticate",
        "proxy-authenticate",
    }
)

#: Substring pattern for the long tail: ``X-Hub-Signature``, ``X-Api-Key``,
#: ``Stripe-Signature``, ``X-Auth-Token``, ``X-Shared-Secret``, ...
SENSITIVE_NAME_RE = re.compile(r"secret|token|signature|api[-_]?key", re.IGNORECASE)

#: What replaces a masked value.
PLACEHOLDER = "<redacted>"

# Auth schemes worth keeping in front of the placeholder: seeing
# ``Bearer <redacted>`` instead of a bare ``<redacted>`` tells you the scheme
# was right without disclosing the credential.
_SCHEMES = frozenset({"basic", "bearer", "digest", "hmac", "negotiate", "token"})


def is_sensitive(name: str) -> bool:
    """Return whether a header *name* should have its value masked."""
    lowered = name.strip().lower()
    return lowered in SENSITIVE_HEADERS or bool(SENSITIVE_NAME_RE.search(lowered))


def redact_value(value: str) -> str:
    """Mask a header value, keeping a recognised auth scheme prefix."""
    scheme, _, rest = value.partition(" ")
    if rest.strip() and scheme.lower() in _SCHEMES:
        return f"{scheme} {PLACEHOLDER}"
    return PLACEHOLDER


def redact_headers(
    headers: list[Header], *, show_secrets: bool = False
) -> list[Header]:
    """Return ``headers`` with sensitive values masked.

    Pass ``show_secrets=True`` to get the list back untouched — that is what the
    ``--show-secrets`` flag does.
    """
    if show_secrets:
        return list(headers)
    return [
        (key, redact_value(value) if is_sensitive(key) else value)
        for key, value in headers
    ]


def has_sensitive(headers: list[Header]) -> bool:
    """Return whether any header in ``headers`` would be masked."""
    return any(is_sensitive(key) for key, _ in headers)
