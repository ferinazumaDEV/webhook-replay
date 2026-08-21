"""Tiny ANSI color helper (stdlib only).

Coloring is disabled automatically when stdout is not a TTY or when the
``NO_COLOR`` environment variable is set (https://no-color.org/).
"""
from __future__ import annotations

import os
import sys

_ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

_CODES = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "gray": "90",
}

# HTTP method -> color, for quick visual scanning of a request list.
_METHOD_COLORS = {
    "GET": "green",
    "POST": "yellow",
    "PUT": "blue",
    "PATCH": "magenta",
    "DELETE": "red",
    "HEAD": "gray",
    "OPTIONS": "gray",
}


def set_enabled(value: bool) -> None:
    """Force-enable or force-disable coloring (used by ``--no-color``)."""
    global _ENABLED
    _ENABLED = bool(value)


def enabled() -> bool:
    return _ENABLED


def paint(text: str, *styles: str) -> str:
    """Wrap ``text`` in the given ANSI styles, unless coloring is disabled."""
    if not _ENABLED or not styles:
        return text
    seq = "".join(f"\x1b[{_CODES[s]}m" for s in styles if s in _CODES)
    if not seq:
        return text
    return f"{seq}{text}\x1b[0m"


def method(name: str) -> str:
    """Colorize an HTTP method name."""
    return paint(name, _METHOD_COLORS.get(name.upper(), "cyan"), "bold")
