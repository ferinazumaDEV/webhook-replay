"""Diff the bodies of two captured requests.

When both bodies parse as JSON they are pretty-printed with sorted keys first,
so the diff shows semantic changes instead of key-ordering noise. Otherwise a
plain text diff is produced.
"""
from __future__ import annotations

import difflib
import json

from . import _color
from .storage import WebhookRecord


def _normalize(body: bytes) -> tuple[str, bool]:
    """Return (text, is_json). JSON is canonicalized for a stable diff."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return f"<{len(body)} bytes of binary data>", False
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text, False
    return json.dumps(parsed, indent=2, sort_keys=True, ensure_ascii=False), True


def diff_records(a: WebhookRecord, b: WebhookRecord) -> str:
    """Produce a colorized unified diff between two records' bodies."""
    text_a, json_a = _normalize(a.body)
    text_b, json_b = _normalize(b.body)

    lines = difflib.unified_diff(
        text_a.splitlines(),
        text_b.splitlines(),
        fromfile=f"#{a.id} ({a.method} {a.path})",
        tofile=f"#{b.id} ({b.method} {b.path})",
        lineterm="",
    )

    out: list[str] = []
    for line in lines:
        if line.startswith("+++") or line.startswith("---"):
            out.append(_color.paint(line, "bold"))
        elif line.startswith("@@"):
            out.append(_color.paint(line, "cyan"))
        elif line.startswith("+"):
            out.append(_color.paint(line, "green"))
        elif line.startswith("-"):
            out.append(_color.paint(line, "red"))
        else:
            out.append(line)

    if not out:
        note = "bodies are identical"
        if json_a and json_b:
            note += " (after JSON normalization)"
        return _color.paint(note, "dim")
    return "\n".join(out)
