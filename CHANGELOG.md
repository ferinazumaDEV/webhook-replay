# Changelog

Notable changes, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Deprecated

- **Python below 3.11 is compatibility, not support.** The floor becomes **3.11 in 0.2.0** (3.10 reaches
  end of life in October 2026). Nothing changes within 0.1.x; CI keeps running on every version the
  package still declares.

### Fixed

- **A `Content-Length` with more than 4300 digits no longer kills the handler.** `int()` refuses
  strings that long with a `ValueError` that escaped as a traceback on stderr and a connection
  closed without any response (also through `Expect: 100-continue`). Leading zeros are now dropped
  before conversion, so `000…01` of any length is the one byte the grammar says it is, and a numeral
  of more than 20 significant digits is a `400 malformed Content-Length`. That is what RFC 9110 §8.6
  asks for: anticipate very large decimal numerals and prevent integer-conversion errors.
- **A body shorter than its `Content-Length` is no longer stored as a capture.** When the sender
  closed early, the partial body was saved as if complete and answered `200`. RFC 9112 §6.3 says
  such a message MUST be treated as incomplete; `serve` now answers `400 body truncated: N of M bytes
  received` and stores nothing. A sender that stalls instead still gets the `408`.
- **`--max-body`, `--since` and `--header` fail as usage errors instead of tracebacks.** A size
  past float range (`--max-body 1` followed by 400 zeros) raised `OverflowError` from `int(inf)`; an
  age past `timedelta`'s range (`--since 99999999999d`) or an ISO timestamp with no UTC form
  (`0001-01-01T00:00:00+14:00`) raised `OverflowError` from the date arithmetic; and a header with
  an empty name or a line break in its value (`--header ':x'`, `--header $'X-A: v\nInjected: y'`)
  raised `ValueError` from `http.client` in the middle of the replay. All three are now
  `ArgumentTypeError`s with a message. Sizes are parsed exactly (no float rounding) and capped at
  2⁶³−1; header names must be RFC 9110 tokens.

### Security

- **Redaction covers more credential-bearing header names.** `X-Auth-Key` (Cloudflare),
  `Ocp-Apim-Subscription-Key` (Azure), `X-Forwarded-Authorization`, `X-Password`, `X-Credential`,
  `X-Access-Key`, `X-Private-Key`, `X-Shared-Key`, `X-Client-Key` and `X-Session-Key` were printed in
  clear by `show`, `list --json` and `curl`. The name rule now also matches `password`, `passwd`,
  `credential`, `authorization`, an `auth` segment, and the `*-key` compounds that name a
  credential; `Idempotency-Key`, `X-Request-Id`, `X-Session-Id` and `X-Author` stay readable.

All of the above were found by `tests/test_adversarial.py`, a new suite that feeds `serve`, the
CLI parsers and the redaction rule the inputs a hostile or merely broken sender produces. Every
test in it failed against 0.1.2 before the fix was written.

## [0.1.2] — 2026-09-13

### Fixed

- **The capture store is created owner-only.** `captures.db` holds every
  captured header and body in clear text — real credentials — and was created
  with whatever the umask allowed (typically `0644`: readable by every user on
  the machine). On POSIX it is now created with mode `0600`, and the mode is set
  *at creation*, before SQLite opens the file, so there is no instant in which
  it is wider. A store that already exists keeps the mode it has: opening is not
  a `chmod`. On Windows the mode is not applied; the store relies on the
  profile's ACLs, as before.
- **`--db` pointing at a file that is not a SQLite database is an error, not a
  traceback.** A text file, a database from another tool, or a store that was
  cut short (an interrupted copy, a full disk) now exits `2` with one line naming
  the path and the reason, the way argparse reports a bad argument. (#14)

### Added

- Tests for the store as a file: a text file and a truncated database raise
  `sqlite3.DatabaseError` at `Storage()` and are a controlled error at the CLI;
  a zero-byte file is an empty store; a new store is `0600` even with `umask 0`;
  an existing store keeps its mode.

## [0.1.1] — 2026-09-06

**Upgrade if you are on 0.1.0.** That release drops webhooks under a burst.

### Fixed

- **The listen backlog was 5, so bursts were lost.** `socketserver` defaults
  `request_queue_size` to 5: the kernel holds five pending connections and
  refuses or resets everything past that *before any handler thread sees it*.
  The server was already threaded and could have served them all — it never got
  the chance to accept them.

  Measured on a 2-core machine, six rounds each, counting failed connections:

  | burst | `backlog=5` | `backlog=SOMAXCONN` |
  |---:|---:|---:|
  | 20 | 6 | 0 |
  | 60 | 173 | 0 |
  | 150 | 649 | 0 |
  | 300 | 1448 | 0 |

  Twenty simultaneous senders already lost data, and a fan-out or a provider
  working through a retry backlog is exactly that. For a tool whose whole job
  is not losing captures, this was the failure that mattered.

  Fixed with a `CaptureServer` subclass using
  `request_queue_size = socket.SOMAXCONN` — as deep a queue as the OS permits
  rather than a guessed number; the kernel caps it where its own maximum is
  smaller.

### Added

- Two regression tests, both checked to fail against the old value: the
  contract asserted directly, and a 50-sender burst released from a
  `threading.Barrier`. The barrier matters — the pre-existing concurrency test
  starts threads one at a time, and that stagger hid the bug locally while it
  still failed on CI.
- CI on every push and pull request across Python 3.9–3.13, running the suite
  the README documents (`pytest`) **and** as a module (`python -m pytest`),
  because the two resolve imports differently and had diverged.
- `SECURITY.md`, including the thing worth saying out loud: the capture store
  holds real credentials in clear text, by design, because a replay is only
  faithful if the stored bytes are the original ones.
- This changelog.

## [0.1.0] — 2026-09-05

First tagged release and first publication to PyPI.

Carries the burst bug fixed in 0.1.1.

[0.1.1]: https://github.com/ferinazumaDEV/webhook-replay/releases/tag/v0.1.1
[0.1.0]: https://github.com/ferinazumaDEV/webhook-replay/releases/tag/v0.1.0
