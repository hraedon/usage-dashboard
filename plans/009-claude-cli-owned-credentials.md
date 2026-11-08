# Plan 009 — Claude credentials owned by the CLI (the Codex pattern)

Status: implemented 2026-11-08 (branch `feat/plan-009-claude-cli-credentials-20261108`),
after the refresh-client_id outage (fix: PR 57). WP-1 passed with a different
trigger than proposed; see *WP-1 result* and *Deviations* below.

## Problem

The dashboard is still a hand-rolled OAuth client for Claude — the only
provider where it mints nothing but *refreshes* by itself. Plan 005 gave
Codex's login/tokens/refresh to the official App Server and Plan 007 gave
Antigravity's to the official `agy` CLI; both have had zero auth incidents.
Claude's compromise — login via the CLI in a throwaway profile, import the
pair into the token store, delete the profile "so the family keeps exactly
one refresher" — made the *dashboard* that one refresher, speaking Anthropic's
undocumented OAuth dialect.

That dialect is a moving target and it moved: on 2026-11-07 the token
endpoint started (or came to visibly) reject a `refresh_token` grant without
`client_id` (400 `invalid_request_error "Invalid request format"`; the CLI's
own payload always sends one). Combined with Plan 005 Phase C deleting the
`CLAUDE_CLIENT_ID` env that had silently supplied one, every credential died
at its 8-hour access-token expiry. Two re-logins bought 8 hours each. The
fix PR repairs the refresh, but the structural lesson stands: **when
Anthropic changes their OAuth contract, we should absorb it by bumping the
pinned CLI in the image, not by re-reverse-engineering the protocol** —
exactly what Plan 001's binary analysis, and this incident, both cost.

## Goal

Move Claude to the Codex ownership model, in the narrow form:

- **Credential lifecycle** (login, token storage, refresh): the official
  Claude Code CLI, against a persistent `CLAUDE_CONFIG_DIR` on the PVC.
- **Usage read**: unchanged — the dashboard's own `GET
  api.anthropic.com/api/oauth/usage` with tokens read from the CLI's
  `.credentials.json` (the existing quarantined parser), because Claude Code
  has **no non-interactive usage command** (checked 2.1.276: no subcommand;
  usage lives in the REPL or that endpoint), and screen-scraping a REPL is
  the delta-grammar cost this project has already declined elsewhere.

Exactly one refresher per credential family: the CLI. The dashboard never
POSTs to the token endpoint again.

## WP-1 result (2026-11-08, Claude Code 2.1.276, in the server pod)

Run against copies of the live personal credential in throwaway
`CLAUDE_CONFIG_DIR`s; hashes compared, values never printed. The one rotation
the spike caused was written straight back to the token store, verified with a
live usage read, and adopted by a server restart (tile stayed current).

| Question | `claude auth status --json` | `claude -p /usage --output-format json` |
|---|---|---|
| Refreshes an expired token? | **No.** File untouched (same mtime/inode) and still reports `loggedIn: true` | **Yes.** New access *and* refresh token, `expiresAt` +8 h, adds `refreshTokenExpiresAt`/`rateLimitTier` |
| Cost | ~0.15 s, local only | ~1 s, `num_turns: 0`, `total_cost_usd: 0`, `local_command: "usage"` |
| Write safety | n/a | Atomic: new inode (temp + rename), so torn reads are not a real risk |
| Signed out | n/a | Exits 0 in ~40 ms with `is_error: false` and a session-cost summary, **not** usage, so the output is never the verdict; the re-read file is |

Footprint per `-p /usage` run (3-run measurements): ~4.3 MB once (bundled
skills) into the config dir; ~3 kB/run of session transcript under `projects/`
(removed by `--no-session-persistence`); ~6 kB/run of cache under `HOME`
(removed by a throwaway `HOME` per run). `--bare` is unusable (it disables
OAuth). `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` breaks `/usage`.

Concurrency: the CLI keeps a `.claude.json.lock` for its own config, but the
fetcher does not rely on it. Every fetch holds the profile's `cli_home` file
lock across read → trigger → re-read → usage call, which also excludes an
enrolment swap.

Side finding, deliberately not acted on: `-p /usage` prints the usage too
("Current session: 10% used · resets …"), as prose. The read stays on
`GET /api/oauth/usage` (structured JSON, existing parser); the prose is a
known fallback if that endpoint closes to non-CLI callers.

## Deviations from the proposal

- **Trigger:** `claude -p /usage --output-format json --no-session-persistence`,
  not `auth status`. It runs when the token is within 5 min of expiry
  (`REFRESH_SKEW_SECONDS`) or after a 401. If the CLI declines to refresh a
  token that is still valid, the fetch proceeds with it. Expired and not
  refreshed, or a 401 the CLI cannot fix, means a re-login reading.
- **Migration:** no forced re-login. WP-1 showed the CLI accepts and then
  refreshes a minimal file (pair, expiry, scopes, plan), so the one-off
  `scripts/migrate_claude_store_to_profile.py` moves the live pair into the
  profile right *after* the rollout. It needs the new image's modules, and
  with the `Recreate` strategy no old server remains to refresh the same
  family. It is a script, not server code: `src/` never writes the CLI's
  credential format. An unmigrated account stays configured, reading "login
  required" rather than vanishing. The store entry is pruned only once the
  profile's refresh token differs from it, which proves the CLI has refreshed,
  so the last good copy is never thrown away early.
- **Review hardening (pre-merge):** a trigger that times out or misbehaves is
  transient, not a re-login. Only a clean run that leaves the token expired,
  or the CLI reporting an auth error, is one. A failed trigger never discards
  a token that has not expired yet. A wedged CLI gets SIGTERM and 5 s of
  grace before SIGKILL, so it can finish a rotation's file write. Startup
  settles an interrupted enrolment swap before judging a profile absent. The
  login child gets the same env allowlist as the trigger.
- **Shared code:** Antigravity's staging/lock helpers moved to
  `server/cli_home.py` and serve both CLIs.
- **Guard:** a test pins that no runtime string in `src/` names the Claude
  Code client id, `oauth/token`, `grant_type` or `refresh_token` (the token
  store's generic pair keys excepted). `refresh_claude_token` and
  `CLAUDE_CODE_CLIENT_ID` are deleted.

## Non-goals

- Changing the usage *read* path or its parser (stays; unsupported-endpoint
  risk narrows to reads only).
- Touching Codex, Antigravity, z.ai, ollama or opencode credentials.
- Any interactive-CLI invocation per poll beyond the WP-1 refresh trigger.

## Work packages

### WP-1 — Spike: `claude auth status --json` as the refresh trigger (gate)

The design hinges on a cheap, non-interactive CLI invocation that refreshes
an expired/near-expiry access token and rewrites `.credentials.json`.
`claude auth status --json` exists in 2.1.276 and is the candidate. Verify
against a real credential in the server pod (staging `CLAUDE_CONFIG_DIR`):

1. With an hours-from-expiry token: does invocation rewrite the file with a
   fresh `accessToken`/`expiresAt`? (mtime + parsed fields, values unprinted.)
2. Output shape and exit codes: signed-out vs. healthy; JSON fields worth
   surfacing on the tile.
3. Cost: wall time, any quota consumption (expected none), behavior under
   the pod's egress.
4. Concurrency: does the CLI take a lock we must respect, or must the
   scheduler serialize refresh-trigger invocations behind its own lock?

**Decision gate:** if `auth status` does not refresh, the trigger falls back
to the dashboard performing a CLI-shaped refresh itself (client_id always
sent — the fix PR's payload, already verified working). The plan then shrinks
to WP-2 + WP-3's reader half, and that fallback is documented as permanent.
Record either way; do not proceed on assumption.

### WP-2 — Persistent `CLAUDE_CONFIG_DIR`; enrolment without import

- `/data/claude-personal` and (when used) `/data/claude-work` on the PVC;
  `0700`, same layout discipline as `CODEX_HOME`.
- `login claude` and the `/login` pane run the official login directly
  against that directory — no throwaway profile, no import, no delete. The
  deletion invariant inverts: the profile MUST stay; it is the credential.
- The token store's `claude`/`claude_work` entries stop being written by
  enrolment; the pane's post-enrolment reload reads the file instead.
- Migration: an existing store credential has no backing CLI profile. Accept
  a one-time re-login per account (the pane already makes this cheap) rather
  than inventing a file-format writer. A fresh-enrolment prompt on the tile
  covers it.

### WP-3 — Fetcher reads the file; refresh via the CLI

- `fetch_claude` (or a thin sibling) reads `access_token`/`expires_at` from
  `.credentials.json` each poll (cheap; the parser is fail-closed and
  version-naming). Absent/unreadable file = auth failure, re-login prompt.
- At/near `expires_at` (say T-15min), invoke the WP-1 trigger under the
  scheduler's lock, then re-read. Refresh-on-401 stays as a belt-and-braces
  path for clock skew — but it goes *through the same trigger*, not through
  our own POST.
- The `claude_*` constructor plumbing (tokens, refresh tokens, client ids)
  and `_do_refresh`'s direct `refresh_claude_token` call are deleted.
  `refresh_claude_token` itself is deleted or reduced to the WP-1 fallback.

### WP-4 — Retire the OAuth machinery; docs and guards

- Token store keeps cookies/workspace ids only; claude OAuth fields
  (`seed_marker` remnants included) are pruned on read, with a one-time
  migration note.
- AGENTS.md / README enrolment sections rewritten to the ownership model;
  the "Claude usage remains an unsupported dependency" caveat is updated to
  name the *read* as the remaining unsupported surface.
- A test pins that no runtime string in `src/` posts to a Claude token
  endpoint (mirroring the Codex rule), unless WP-1's fallback is in force —
  then it pins that the fallback payload carries `client_id`.

### WP-5 — Verification drill

In the pod, with a freshly enrolled staging credential: run the poller
across a simulated expiry (rewind `expires_at` in a copy, or wait out a real
8h window once), and evidence: usage readings continue, the file's
`expiresAt` advances, the readings DB shows no gap, and no POST to the token
endpoint appears in logs (fallback case: one, shaped like the CLI's). Then
repeat the Phase-C style audit: pod restart mid-life keeps the tile alive
with zero re-login.

## Risks

- `.credentials.json` remains undocumented; a CLI bump can change its shape.
  The parser already fails closed and names the version; CLI bumps become a
  deliberate, tested image change instead of an ambient hazard.
- Torn reads: WP-3 reads `.credentials.json` while the CLI may be rewriting
  it (refresh trigger, or an enrolment). WP-1 should note whether the CLI
  writes atomically (temp + rename); either way the reader retries a parse
  failure once before reporting an auth failure, so a mid-write read does not
  enter the auth-failure backoff ladder.
- Two writers to the file (CLI refresh during a concurrent enrolment) —
  mitigated by the enrolment pause/resume bracket the pane already has for
  Codex, extended to the claude trigger.
- If Anthropic closes the usage endpoint to non-CLI callers, this plan's
  read half breaks and the delta-grammar cost arrives on its own schedule;
  that is the documented worst case, not a reason to pre-pay it.
