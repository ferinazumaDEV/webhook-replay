# webhook-replay

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)

**Capture a webhook once, then re-fire it at your local app as many times as you need — without re-triggering the real event upstream.**

Debugging a webhook integration usually means going back to Stripe / GitHub / Shopify and manually re-sending the event every time you tweak your handler. `webhook-replay` breaks that loop: run a local endpoint that records every incoming request (headers, body, timestamp), then replay any captured request into your app on demand — with filtering, `curl` export, and payload diffing along the way.

Zero runtime dependencies. Pure Python standard library.

---

## Features

- **Capture everything** — accepts any method on any path, storing full headers, raw body, source address and timestamp in a local SQLite file.
- **Replay to your app** — re-send a captured request (or the last _N_, or the same one _N_ times) to any base URL. Non-2xx responses are reported, not swallowed.
- **Readable CLI** — colorized `list` / `show` with pretty-printed JSON bodies, or `--json` for scripting.
- **`curl` export** — turn any captured request into a copy-pasteable `curl` command.
- **Diff payloads** — compare two captured bodies; JSON is canonicalized first, so key-order noise disappears and only real changes show.
- **Filters** — by method, path substring, or age (`--since 10m`).
- **Secrets masked on output** — `Authorization`, `Cookie`, signature and API-key headers are printed as `<redacted>` in `show`, `list --json` and `curl` export, so a pasted command is safe. `--show-secrets` opts out; replay always sends the real values.
- **Bounded by default** — a body-size cap (`413` above it), a retention cap that evicts the oldest captures, and a read timeout, so a stray sender cannot fill your disk or pin a thread.
- **Nothing to install but Python** — no framework, no broker, no external service.

---

## Install

```bash
git clone https://github.com/ferinazumaDEV/webhook-replay
cd webhook-replay
pip install .
```

Or run it straight from a checkout without installing:

```bash
python -m webhook_replay --help
```

Requires Python 3.9+.

---

## Usage

### 1. Start the capture server

```console
$ webhook-replay serve --port 8973
webhook-replay listening on http://127.0.0.1:8973
  storing captures in /home/you/.webhook-replay/captures.db
  limits: body 1048576 bytes, retain 1000 newest, read timeout 30s
  point your webhook here, then Ctrl-C to stop
22:42:34 #1 POST /github/webhook (34 bytes)
22:42:34 #2 POST /stripe/webhook (27 bytes)
22:42:34 #3 GET /health (0 bytes)
```

Point your provider's webhook (or a tunnel like ngrok/cloudflared) at this endpoint. Every request is logged live and persisted.

### 2. List what came in

```console
$ webhook-replay list
    #3 22:41:43  GET      /health
        0 bytes  -  from 127.0.0.1
    #2 22:41:43  POST     /stripe/webhook?livemode=false
        74 bytes  application/json  from 127.0.0.1
    #1 22:41:43  POST     /stripe/webhook?livemode=false
        74 bytes  application/json  from 127.0.0.1

3 request(s).
```

Filter it: `webhook-replay list --method POST --path stripe --since 10m`, or add `--json` to pipe it elsewhere.

### 3. Inspect one request

```console
$ webhook-replay show 1
POST /stripe/webhook?livemode=false
#1  2026-08-21T22:41:43.423+00:00  from 127.0.0.1

Headers
  Content-Type: application/json
  Stripe-Signature: <redacted>
  ...
  (some header values are masked; re-run with --show-secrets to see them)

Body
  {
    "id": "evt_1",
    "type": "invoice.paid",
    "amount": 4200,
    "currency": "usd"
  }
```

### 4. Replay it into your app

```console
$ webhook-replay replay 1 --to http://127.0.0.1:8972 --show-response
#1 POST http://127.0.0.1:8972/stripe/webhook?livemode=false -> 200 OK 3ms
  {"handled": true}
```

Your local handler receives a byte-for-byte copy of the original request (one caveat, repeated header names, is noted under [How it works](#how-it-works)):

```
[my-app] received POST /stripe/webhook?livemode=false -> {"id": "evt_1", "type": "invoice.paid", "amount": 4200, "currency": "usd"}
```

Replay the two most recent, twice each, in one shot:

```console
$ webhook-replay replay --last 2 --to http://127.0.0.1:8972 --times 2
#2 (x1) POST http://127.0.0.1:8972/stripe/webhook?livemode=false -> 200 OK 3ms
#2 (x2) POST http://127.0.0.1:8972/stripe/webhook?livemode=false -> 200 OK 1ms
#3 (x1) GET  http://127.0.0.1:8972/health -> 501 Unsupported method ('GET') 1ms
#3 (x2) GET  http://127.0.0.1:8972/health -> 501 Unsupported method ('GET') 1ms
```

You can override the method (`--method POST`) or inject extra headers (`--header 'X-Debug: 1'`) on replay.

### 5. Export a request as curl

```console
$ webhook-replay curl 2 --base https://api.myapp.local
curl -X POST 'https://api.myapp.local/stripe/webhook?livemode=false' \
  -H 'Content-Type: application/json' \
  -H 'Stripe-Signature: <redacted>' \
  --data-binary '{"id": "evt_2", "type": "invoice.paid", "amount": 9900, "currency": "eur"}'
```

Signature, `Authorization`, `Cookie` and API-key headers are masked so the command is safe to paste into an issue or a chat. Add `--show-secrets` when you need a command that actually authenticates.

### 6. Diff two payloads

```console
$ webhook-replay diff 1 2
--- #1 (POST /stripe/webhook?livemode=false)
+++ #2 (POST /stripe/webhook?livemode=false)
@@ -1,6 +1,6 @@
 {
-  "amount": 4200,
-  "currency": "usd",
-  "id": "evt_1",
+  "amount": 9900,
+  "currency": "eur",
+  "id": "evt_2",
   "type": "invoice.paid"
 }
```

Both bodies are canonicalized as sorted-key JSON before diffing, so reordered keys don't show up as changes — only real value differences do.

---

## Command reference

| Command | What it does |
| --- | --- |
| `serve` | Start the local capture endpoint (`--host`, `--port`, `--status`, `--response`, `--max-body`, `--max-captures`, `--read-timeout`). |
| `list` | List captured requests (`--method`, `--path`, `--since`, `--limit`, `--json`, `--show-secrets`). |
| `show <id>` | Show one request in full (`--raw` writes just the body to stdout, `--show-secrets` unmasks headers). |
| `replay <id...>` | Replay request(s) to `--to <url>` (`--last N`, `--times N`, `--method`, `--header`, `--show-response`). |
| `curl <id>` | Print a request as a `curl` command (`--base <url>`, `--show-secrets`). |
| `diff <a> <b>` | Diff two captured bodies. |
| `prune` | Delete old captures (`--older-than 7d`, `--keep N`). |
| `clear` | Delete all captured requests (`-y` to skip the prompt). |

Global: `--db <path>` to use an alternate store, `--no-color` to disable ANSI colors (also honored via `NO_COLOR`).

---

## Security

**The capture store holds real credentials in clear text.** Every request is saved exactly as it arrived — full headers and full body — in a local SQLite file (`~/.webhook-replay/captures.db` by default). Webhook traffic routinely carries `Authorization` headers, session cookies, signing signatures and API keys, and all of it lands in that file unencrypted. That is deliberate: a replay is only faithful, and a signature only verifies, if the stored bytes are the original ones.

Because the store is not sanitised, the *output* is:

- `show`, `list --json` and `curl` mask the values of `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie` and any header whose name contains `secret`, `token`, `signature`, `hmac`, `api-key` / `api_key`, or a `sig` segment (`X-Shopify-Hmac-Sha256`, `Paypal-Transmission-Sig`). They print as `<redacted>`, keeping a recognisable scheme prefix where there is one (`Bearer <redacted>`).
- `--show-secrets` turns masking off for a single command, when you genuinely need the value.
- `replay` is never masked. It forwards the captured headers verbatim, which is the whole point of the tool.
- Masking is name-based, not value-based, and it covers the output paths only. It is a guard against pasting a secret into an issue or a screen share — not a guarantee that no secret can appear anywhere.

Handling the store:

- **Do not commit it and do not share it.** `.gitignore` already excludes `*.db`, `*.sqlite3` and `captures.db`, but a store kept outside the repo is safer still.
- **Delete it when you are done.** `webhook-replay clear -y` empties it; `webhook-replay prune --older-than 7d` drops everything older than a week; `rm ~/.webhook-replay/captures.db` removes the file outright.
- **Keep the server local.** It binds `127.0.0.1` by default and answers *every* method on *every* path with a canned success. Exposing it (`--host 0.0.0.0`, or a tunnel left running) turns it into an open, unauthenticated sink for anything on the network.
- **The defaults are bounded, not zero.** `serve` refuses bodies over 1 MiB with `413`, retains the 1000 newest captures, and drops a connection that stalls for 30 seconds. Tune them with `--max-body`, `--max-captures` and `--read-timeout`; `0` disables any of the three.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

---

## How it works

- **Capture** — a threaded `http.server` handler is registered for every HTTP method. It reads the body — `Content-Length` bytes, or a `Transfer-Encoding: chunked` stream decoded on the way in — snapshots the headers in order, and writes a row to SQLite. A request that carries both `Content-Length` and `Transfer-Encoding`, or a malformed length, is refused with `400` rather than guessed at. It then answers with a configurable canned response (default `200 {"received": true}`) so the sender is satisfied, plus an `X-Webhook-Replay-Id` header echoing the stored id. Three bounds apply before anything is stored: an oversized body is refused with `413` without being buffered (for a chunked body, as soon as the declared chunks pass the cap), a connection that stalls mid-body hits the socket timeout and is dropped, and once the retention cap is reached each new capture evicts the oldest row. Every response closes the connection, so one connection serves exactly one request.
- **Store** — one SQLite table, one short-lived connection per operation (with a busy timeout), which keeps it safe under the server's per-request threads. Bodies are stored as `BLOB`, so binary payloads round-trip exactly.
- **Replay** — the stored method, path (including query string) and raw body are rebuilt into a `urllib` request against your target base URL. Hop-by-hop headers that describe the *original* connection (`Host`, `Content-Length`, `Connection`, `Accept-Encoding`) are dropped and recomputed; everything else — including signature headers — is forwarded verbatim, with one limitation: repeated header names are collapsed to the last value, because `urllib` keeps a single value per header name. A non-2xx reply is captured and reported rather than raised.
- **Redact** — masking lives in one module and runs only where a capture is *rendered*: `curl` export, `list --json` and `show`. Nothing filters the capture path or the replay path, so what is stored and what is re-sent stay byte-for-byte original.

Everything is standard library: `http.server`, `sqlite3`, `urllib`, `argparse`, `difflib`, `json`, `shlex`.

---

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

The test suite (153 tests) is self-contained: it spins real capture and receiver servers on OS-assigned free ports and exercises capture (including chunked bodies and malformed framing), persistence, concurrent writes, the size / retention / timeout limits, replay (success, non-2xx, connection error, stalled target, method/header overrides, verbatim forwarding of sensitive headers), every CLI command, output redaction, `curl` export and JSON diffing end-to-end. No network access or external services required.

```console
$ pytest
........................................................................ [ 47%]
........................................................................ [ 94%]
.........                                                                [100%]
153 passed in 22.55s
```

---

## Part of a family of small developer tools

`webhook-replay` is one of a family of small, dependency-light developer tools I build and maintain in the open — each a focused, standalone utility meant to do one job well. If this one was useful, these siblings might be too:

- [The GEO Handbook](https://github.com/ferinazumaDEV/generative-engine-optimization-handbook) — the open reference on getting content cited by AI answer engines (ChatGPT, Perplexity, Google AI Overviews, Gemini, Copilot).
- [politeclient](https://github.com/ferinazumaDEV/politeclient) — a polite, bulletproof HTTP client for Python: retries with backoff, per-host rate-limiting, caching and pagination.
- [scaffld](https://github.com/ferinazumaDEV/scaffld) — scaffold fully-wired Python projects (tests, CI, pre-commit, license) from templates, with a TUI.
- [typedout](https://github.com/ferinazumaDEV/typedout) — reliable structured output from any LLM: schema-validated JSON with tolerant repair and retries.
- Hub & writing: [zentimes.es](https://zentimes.es).

By [ferinazumaDEV](https://github.com/ferinazumaDEV).

---

## License

MIT — see [LICENSE](LICENSE).

---

_Built by Fernando Aporta Franco ([@ferinazumaDEV](https://github.com/ferinazumaDEV))._
