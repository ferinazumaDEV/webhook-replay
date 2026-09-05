"""Command-line interface for webhook-replay."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone

from . import __version__, _color
from .diff import diff_records
from .export import to_curl
from .redact import has_sensitive, redact_headers
from .replay import replay
from .server import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_CAPTURES,
    DEFAULT_READ_TIMEOUT,
    create_server,
)
from .storage import DEFAULT_DB, Header, Storage, WebhookRecord

_SINCE_RE = re.compile(r"^(\d+)([smhd])$", re.IGNORECASE)
_SINCE_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmg]?i?b?)$", re.IGNORECASE)
_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1000, "kb": 1000, "ki": 1024, "kib": 1024,
    "m": 1000 ** 2, "mb": 1000 ** 2, "mi": 1024 ** 2, "mib": 1024 ** 2,
    "g": 1000 ** 3, "gb": 1000 ** 3, "gi": 1024 ** 3, "gib": 1024 ** 3,
}

_REDACTION_HINT = (
    "some header values are masked; re-run with --show-secrets to see them"
)


def parse_since(value: str | None) -> str | None:
    """Turn ``10m`` / ``2h`` / ``1d`` or an ISO timestamp into a UTC ISO cutoff.

    Units are case-insensitive (``7D`` is seven days; there is no month unit,
    so ``10M`` is ten minutes). A naive ISO timestamp is taken as UTC. Anything
    else raises :class:`argparse.ArgumentTypeError`: the store compares
    timestamps as strings, so an unparsed value such as ``yesterday`` would
    otherwise become a cutoff that matches -- or deletes -- every capture.
    """
    if not value:
        return None
    text = value.strip()
    match = _SINCE_RE.match(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        cutoff = datetime.now(timezone.utc) - timedelta(**{_SINCE_UNITS[unit]: amount})
        return cutoff.isoformat(timespec="milliseconds")
    if text.endswith(("Z", "z")):
        # datetime.fromisoformat only accepts a trailing Z from Python 3.11 on.
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected an age like 7d, 12h, 10m or an ISO timestamp, got {value!r}"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def parse_positive_int(value: str) -> int:
    """An integer of at least 1, for counts such as ``--times``."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {number}")
    return number


def parse_keep(value: str) -> int:
    """``--keep N``: at least 1, because 0 would silently keep everything."""
    try:
        return parse_positive_int(value)
    except argparse.ArgumentTypeError:
        raise argparse.ArgumentTypeError(
            "--keep must be >= 1; use `clear` to delete everything"
        ) from None


def parse_size(value: str) -> int:
    """Turn ``1048576`` / ``512KB`` / ``1MiB`` into a byte count."""
    match = _SIZE_RE.match(str(value).strip())
    if not match:
        raise argparse.ArgumentTypeError(
            f"size must be bytes or a value like 512KB / 1MiB, got {value!r}"
        )
    amount, unit = float(match.group(1)), match.group(2).lower()
    if unit not in _SIZE_UNITS:
        raise argparse.ArgumentTypeError(f"unknown size unit in {value!r}")
    return int(amount * _SIZE_UNITS[unit])


def parse_header(raw: str) -> Header:
    if ":" not in raw:
        raise argparse.ArgumentTypeError(
            f"header must be in 'Name: value' form, got {raw!r}"
        )
    key, value = raw.split(":", 1)
    return key.strip(), value.strip()


def _short_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M:%S")
    except ValueError:
        return iso


def _record_to_dict(record: WebhookRecord, *, show_secrets: bool = False) -> dict:
    headers = redact_headers(record.headers, show_secrets=show_secrets)
    return {
        "id": record.id,
        "method": record.method,
        "path": record.path,
        "headers": [list(h) for h in headers],
        "body": record.body_text() if record.body_text() is not None else None,
        "body_bytes": len(record.body),
        "remote_addr": record.remote_addr,
        "received_at": record.received_at,
        "redacted": not show_secrets and has_sensitive(record.headers),
    }


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_serve(args: argparse.Namespace, store: Storage) -> int:
    server = create_server(
        store,
        host=args.host,
        port=args.port,
        response_status=args.status,
        response_body=args.response.encode("utf-8"),
        max_body_bytes=args.max_body,
        max_captures=args.max_captures,
        read_timeout=args.read_timeout,
    )
    host, port = server.server_address
    banner = f"webhook-replay listening on http://{host}:{port}"
    body_cap = f"{args.max_body} bytes" if args.max_body else "unlimited"
    keep_cap = f"{args.max_captures} newest" if args.max_captures else "unlimited"
    read_cap = f"{args.read_timeout:g}s" if args.read_timeout else "none"
    print(_color.paint(banner, "bold", "green"))
    print(_color.paint(f"  storing captures in {store.path}", "dim"))
    print(
        _color.paint(
            f"  limits: body {body_cap}, retain {keep_cap}, read timeout {read_cap}",
            "dim",
        )
    )
    print(_color.paint("  point your webhook here, then Ctrl-C to stop", "dim"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        print(_color.paint(f"stopped. {store.count()} request(s) captured.", "dim"))
    finally:
        server.server_close()
    return 0


def cmd_list(args: argparse.Namespace, store: Storage) -> int:
    records = store.list(
        method=args.method,
        path_contains=args.path,
        since=args.since,
        limit=args.limit,
    )
    if args.json:
        payload = [
            _record_to_dict(r, show_secrets=args.show_secrets) for r in records
        ]
        # Keep stdout pure JSON so it stays pipeable; the hint goes to stderr.
        print(json.dumps(payload, indent=2), flush=True)
        if any(item["redacted"] for item in payload):
            print(_color.paint(_REDACTION_HINT, "dim"), file=sys.stderr)
        return 0
    if not records:
        print(_color.paint("no captured requests match.", "dim"))
        return 0

    for record in records:
        ctype = record.content_type or "-"
        line = (
            f"{_color.paint('#' + str(record.id), 'bold'):>6} "
            f"{_color.paint(_short_time(record.received_at), 'gray')}  "
            f"{_color.method(record.method):<8} {record.path}"
        )
        print(line)
        print(
            _color.paint(
                f"        {len(record.body)} bytes  {ctype}  from {record.remote_addr}",
                "dim",
            )
        )
    print(_color.paint(f"\n{len(records)} request(s).", "dim"))
    return 0


def cmd_show(args: argparse.Namespace, store: Storage) -> int:
    record = store.get(args.id)
    if record is None:
        print(_color.paint(f"no request with id {args.id}", "red"), file=sys.stderr)
        return 1
    if args.raw:
        sys.stdout.buffer.write(record.body)
        return 0

    print(f"{_color.method(record.method)} {record.path}")
    print(_color.paint(f"#{record.id}  {record.received_at}  from {record.remote_addr}", "dim"))
    print()
    print(_color.paint("Headers", "bold"))
    for key, value in redact_headers(record.headers, show_secrets=args.show_secrets):
        print(f"  {_color.paint(key, 'cyan')}: {value}")
    if not args.show_secrets and has_sensitive(record.headers):
        print(_color.paint(f"  ({_REDACTION_HINT})", "dim"))
    print()
    print(_color.paint("Body", "bold"))
    text = record.body_text()
    if text is None:
        print(_color.paint(f"  <{len(record.body)} bytes of binary data>", "dim"))
        return 0
    if not text:
        print(_color.paint("  <empty>", "dim"))
        return 0
    # Pretty-print JSON bodies.
    try:
        parsed = json.loads(text)
        text = json.dumps(parsed, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        pass
    for line in text.splitlines():
        print(f"  {line}")
    return 0


def _resolve_targets(args: argparse.Namespace, store: Storage) -> list[WebhookRecord]:
    if args.last:
        return list(reversed(store.latest(args.last)))
    records = []
    for request_id in args.ids:
        record = store.get(request_id)
        if record is None:
            print(_color.paint(f"skipping unknown id {request_id}", "yellow"), file=sys.stderr)
            continue
        records.append(record)
    return records


def cmd_replay(args: argparse.Namespace, store: Storage) -> int:
    if args.ids and args.last:
        args.parser.error("pass request id(s) or --last N, not both")
    if not args.ids and not args.last:
        args.parser.error("pass request id(s) or --last N")
    targets = _resolve_targets(args, store)
    if not targets:
        print(_color.paint("nothing to replay.", "yellow"), file=sys.stderr)
        return 1

    failures = 0
    for record in targets:
        for attempt in range(args.times):
            result = replay(
                record,
                args.to,
                timeout=args.timeout,
                method=args.method,
                extra_headers=args.header or None,
            )
            suffix = f" (x{attempt + 1})" if args.times > 1 else ""
            if result.error is not None:
                failures += 1
                print(
                    f"{_color.paint('#' + str(record.id), 'bold')}{suffix} "
                    f"{_color.method(result.method)} {result.url} "
                    f"{_color.paint('ERROR: ' + result.error, 'red')}"
                )
                continue
            status_style = "green" if result.ok else "red"
            print(
                f"{_color.paint('#' + str(record.id), 'bold')}{suffix} "
                f"{_color.method(result.method)} {result.url} "
                f"-> {_color.paint(str(result.status) + ' ' + result.reason, status_style)} "
                f"{_color.paint(f'{result.elapsed_ms:.0f}ms', 'dim')}"
            )
            if args.show_response and result.body:
                print(_color.paint("  " + result.body_text(limit=2000).replace("\n", "\n  "), "dim"))
    return 1 if failures else 0


def cmd_curl(args: argparse.Namespace, store: Storage) -> int:
    record = store.get(args.id)
    if record is None:
        print(_color.paint(f"no request with id {args.id}", "red"), file=sys.stderr)
        return 1
    print(to_curl(record, base_url=args.base, show_secrets=args.show_secrets), flush=True)
    if not args.show_secrets and has_sensitive(record.headers):
        print(_color.paint(_REDACTION_HINT, "dim"), file=sys.stderr)
    return 0


def cmd_diff(args: argparse.Namespace, store: Storage) -> int:
    a = store.get(args.id_a)
    b = store.get(args.id_b)
    missing = [str(i) for i, r in ((args.id_a, a), (args.id_b, b)) if r is None]
    if missing:
        print(_color.paint(f"no request with id {', '.join(missing)}", "red"), file=sys.stderr)
        return 1
    print(diff_records(a, b))
    return 0


def cmd_clear(args: argparse.Namespace, store: Storage) -> int:
    if not args.yes:
        count = store.count()
        answer = input(f"delete all {count} captured request(s)? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("aborted.")
            return 0
    removed = store.clear()
    print(_color.paint(f"cleared {removed} request(s).", "dim"))
    return 0


def cmd_prune(args: argparse.Namespace, store: Storage) -> int:
    if not args.older_than and args.keep is None:
        print(
            _color.paint("pass --older-than and/or --keep.", "red"), file=sys.stderr
        )
        return 2

    removed = 0
    if args.older_than:
        removed += store.prune_older_than(args.older_than)
    if args.keep is not None:
        removed += store.prune_to(args.keep)
    print(
        _color.paint(
            f"pruned {removed} request(s); {store.count()} left.", "dim"
        )
    )
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def _add_show_secrets(parser: argparse.ArgumentParser) -> None:
    """Add the opt-in that turns masking of sensitive headers back off."""
    parser.add_argument(
        "--show-secrets",
        action="store_true",
        help="print sensitive header values in full instead of masking them",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webhook-replay",
        description="Capture, inspect and re-fire webhooks locally.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--db", default=str(DEFAULT_DB), help=f"SQLite store path (default: {DEFAULT_DB})"
    )
    parser.add_argument("--no-color", action="store_true", help="disable colored output")

    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="start the local capture server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("-p", "--port", type=int, default=8000)
    p_serve.add_argument("--status", type=int, default=200, help="status code to reply with")
    p_serve.add_argument("--response", default='{"received": true}', help="response body")
    p_serve.add_argument(
        "--max-body", type=parse_size, default=DEFAULT_MAX_BODY_BYTES, metavar="SIZE",
        help=(
            "reject bodies larger than SIZE with 413, e.g. 512KB or 2MiB "
            f"(default: {DEFAULT_MAX_BODY_BYTES}; 0 disables)"
        ),
    )
    p_serve.add_argument(
        "--max-captures", type=int, default=DEFAULT_MAX_CAPTURES, metavar="N",
        help=(
            "keep only the N newest captures, discarding older ones "
            f"(default: {DEFAULT_MAX_CAPTURES}; 0 disables)"
        ),
    )
    p_serve.add_argument(
        "--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT, metavar="SECONDS",
        help=(
            "drop a connection that stalls mid-request "
            f"(default: {DEFAULT_READ_TIMEOUT:g}; 0 disables)"
        ),
    )
    p_serve.set_defaults(func=cmd_serve)

    p_list = sub.add_parser("list", help="list captured requests")
    p_list.add_argument("--method", help="filter by HTTP method")
    p_list.add_argument("--path", help="filter by substring of the path")
    p_list.add_argument(
        "--since", type=parse_since, metavar="AGE",
        help="only newer than e.g. 10m, 2h, 1d (case-insensitive), or an ISO time",
    )
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--json", action="store_true", help="output as JSON")
    _add_show_secrets(p_list)
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="show one request in full")
    p_show.add_argument("id", type=int)
    p_show.add_argument("--raw", action="store_true", help="write only the raw body to stdout")
    _add_show_secrets(p_show)
    p_show.set_defaults(func=cmd_show)

    p_replay = sub.add_parser("replay", help="re-send captured request(s) to a URL")
    p_replay.add_argument("ids", type=int, nargs="*", help="request id(s) to replay")
    p_replay.add_argument("--to", required=True, help="base URL of your local app")
    p_replay.add_argument("--last", type=int, metavar="N", help="replay the N most recent instead")
    p_replay.add_argument(
        "--times", type=parse_positive_int, default=1, metavar="N",
        help="send each request N times (N >= 1)",
    )
    p_replay.add_argument("--timeout", type=float, default=10.0)
    p_replay.add_argument("--method", help="override the HTTP method")
    p_replay.add_argument(
        "--header", type=parse_header, action="append", metavar="'K: V'",
        help="add/override a header (repeatable)",
    )
    p_replay.add_argument("--show-response", action="store_true", help="print each response body")
    p_replay.set_defaults(func=cmd_replay, parser=p_replay)

    p_curl = sub.add_parser("curl", help="export a request as a curl command")
    p_curl.add_argument("id", type=int)
    p_curl.add_argument("--base", default="http://localhost:8000", help="base URL for the curl call")
    _add_show_secrets(p_curl)
    p_curl.set_defaults(func=cmd_curl)

    p_diff = sub.add_parser("diff", help="diff the bodies of two requests")
    p_diff.add_argument("id_a", type=int)
    p_diff.add_argument("id_b", type=int)
    p_diff.set_defaults(func=cmd_diff)

    p_clear = sub.add_parser("clear", help="delete all captured requests")
    p_clear.add_argument("-y", "--yes", action="store_true", help="do not prompt")
    p_clear.set_defaults(func=cmd_clear)

    p_prune = sub.add_parser("prune", help="delete old captures, keep the rest")
    p_prune.add_argument(
        "--older-than", type=parse_since, metavar="AGE",
        help=(
            "delete captures older than e.g. 7d, 12h, 10m (case-insensitive), "
            "or an ISO timestamp"
        ),
    )
    p_prune.add_argument(
        "--keep", type=parse_keep, metavar="N",
        help="delete all but the N newest captures (N >= 1; use `clear` to delete all)",
    )
    p_prune.set_defaults(func=cmd_prune)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.no_color:
        _color.set_enabled(False)
    store = Storage(args.db)
    return args.func(args, store)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
