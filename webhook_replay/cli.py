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
from .replay import replay
from .server import create_server
from .storage import DEFAULT_DB, Header, Storage, WebhookRecord

_SINCE_RE = re.compile(r"^(\d+)([smhd])$")
_SINCE_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_since(value: str | None) -> str | None:
    """Turn ``10m`` / ``2h`` / ``1d`` into an ISO cutoff; pass ISO through."""
    if not value:
        return None
    match = _SINCE_RE.match(value.strip())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        cutoff = datetime.now(timezone.utc) - timedelta(**{_SINCE_UNITS[unit]: amount})
        return cutoff.isoformat(timespec="milliseconds")
    return value  # assume the user passed an ISO timestamp


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


def _record_to_dict(record: WebhookRecord) -> dict:
    return {
        "id": record.id,
        "method": record.method,
        "path": record.path,
        "headers": [list(h) for h in record.headers],
        "body": record.body_text() if record.body_text() is not None else None,
        "body_bytes": len(record.body),
        "remote_addr": record.remote_addr,
        "received_at": record.received_at,
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
    )
    host, port = server.server_address
    banner = f"webhook-replay listening on http://{host}:{port}"
    print(_color.paint(banner, "bold", "green"))
    print(_color.paint(f"  storing captures in {store.path}", "dim"))
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
        since=parse_since(args.since),
        limit=args.limit,
    )
    if args.json:
        print(json.dumps([_record_to_dict(r) for r in records], indent=2))
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
    for key, value in record.headers:
        print(f"  {_color.paint(key, 'cyan')}: {value}")
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
    print(to_curl(record, base_url=args.base))
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


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
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
    p_serve.set_defaults(func=cmd_serve)

    p_list = sub.add_parser("list", help="list captured requests")
    p_list.add_argument("--method", help="filter by HTTP method")
    p_list.add_argument("--path", help="filter by substring of the path")
    p_list.add_argument("--since", help="only newer than e.g. 10m, 2h, 1d, or an ISO time")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--json", action="store_true", help="output as JSON")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="show one request in full")
    p_show.add_argument("id", type=int)
    p_show.add_argument("--raw", action="store_true", help="write only the raw body to stdout")
    p_show.set_defaults(func=cmd_show)

    p_replay = sub.add_parser("replay", help="re-send captured request(s) to a URL")
    p_replay.add_argument("ids", type=int, nargs="*", help="request id(s) to replay")
    p_replay.add_argument("--to", required=True, help="base URL of your local app")
    p_replay.add_argument("--last", type=int, metavar="N", help="replay the N most recent instead")
    p_replay.add_argument("--times", type=int, default=1, help="send each request N times")
    p_replay.add_argument("--timeout", type=float, default=10.0)
    p_replay.add_argument("--method", help="override the HTTP method")
    p_replay.add_argument(
        "--header", type=parse_header, action="append", metavar="'K: V'",
        help="add/override a header (repeatable)",
    )
    p_replay.add_argument("--show-response", action="store_true", help="print each response body")
    p_replay.set_defaults(func=cmd_replay)

    p_curl = sub.add_parser("curl", help="export a request as a curl command")
    p_curl.add_argument("id", type=int)
    p_curl.add_argument("--base", default="http://localhost:8000", help="base URL for the curl call")
    p_curl.set_defaults(func=cmd_curl)

    p_diff = sub.add_parser("diff", help="diff the bodies of two requests")
    p_diff.add_argument("id_a", type=int)
    p_diff.add_argument("id_b", type=int)
    p_diff.set_defaults(func=cmd_diff)

    p_clear = sub.add_parser("clear", help="delete all captured requests")
    p_clear.add_argument("-y", "--yes", action="store_true", help="do not prompt")
    p_clear.set_defaults(func=cmd_clear)

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
