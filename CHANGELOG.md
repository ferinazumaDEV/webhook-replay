# Changelog

Notable changes, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
