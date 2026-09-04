# Security Policy

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting on this repository:
<https://github.com/ferinazumaDEV/webhook-replay/security/advisories/new>. If that form is not
available to you, open an issue at
<https://github.com/ferinazumaDEV/webhook-replay/issues> saying only that you have a security
report and asking for a private channel — do not put the details in a public issue.

Please include what you can: affected version, the steps to reproduce, and what an attacker gets
out of it. Expect a first reply within a few days; this is a small, single-maintainer project, so
there is no formal SLA and no bug bounty.

## Supported versions

Only the latest release on `main` is supported. Fixes land there; there are no maintained
backport branches.

| Version | Supported |
| --- | --- |
| 0.1.x | yes |
| older | no |

## Before you report: what is by design

`webhook-replay` stores every captured request — full headers and full body — in clear text in a
local SQLite file (`~/.webhook-replay/captures.db` by default). That file will contain real
credentials: `Authorization` headers, session cookies, signing signatures, API keys. It is not
encrypted, and the tool never sanitises what it stores, because a replay is only faithful and a
signature only verifies if the stored bytes are the original ones. Do not share that file and do
not commit it to a repository. Delete it when you are done: `webhook-replay clear -y`, or
`webhook-replay prune --older-than 7d`, or `rm ~/.webhook-replay/captures.db`.

Because the store is not sanitised, the output is: `show`, `list --json` and `curl` mask sensitive
header values as `<redacted>` unless you pass `--show-secrets`. That masking is a guard against
pasting a secret into an issue or a screen share, not a security boundary — anyone who can read
the store can read everything in it.

The capture server binds `127.0.0.1` by default and answers every method on every path with a
canned success. Exposing it (`--host 0.0.0.0`, or a public tunnel left running) makes it an open,
unauthenticated sink; that is a deployment choice, not a vulnerability in the tool.
