"""webhook-replay: capture, inspect and re-fire webhooks locally.

A zero-dependency developer tool for debugging webhook integrations without
re-triggering the real upstream event.
"""
from __future__ import annotations

__version__ = "0.1.1"

from .diff import diff_records
from .export import to_curl
from .redact import is_sensitive, redact_headers
from .replay import ReplayResult, replay
from .server import create_server, make_handler
from .storage import Storage, WebhookRecord

__all__ = [
    "__version__",
    "Storage",
    "WebhookRecord",
    "create_server",
    "make_handler",
    "replay",
    "ReplayResult",
    "to_curl",
    "diff_records",
    "redact_headers",
    "is_sensitive",
]
