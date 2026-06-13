# Plan 001 — Dedicated Claude OAuth login (don't share the interactive session)

**Status:** proposed 2026-06-13
**Tracking:** WI-009

## Problem

The Claude usage endpoint (`GET api.anthropic.com/api/oauth/usage`,
`anthropic-beta: oauth-2025-04-20`) requires the **`user:profile`** OAuth
scope. Two credential sources were tried:

- **Interactive-session tokens** (copied from a logged-in Claude Code /
  claude.ai session): these *do* carry `user:profile` — they passed auth and
  only hit rate limiting (429). But they are shared with the live session, and
  Claude appears to rotate refresh tokens on use, so if the dashboard refreshes
  an expired access token it rotates the refresh token out from under the
  interactive session and breaks that login. The user flagged this as
  unacceptable.
- **`claude setup-token` tokens** (`sk-ant-oat01-…`): dedicated and long-lived,
  but scoped for inference only. The usage endpoint returns
  `403 OAuth token does not meet scope requirement user:profile`. Verified
  2026-06-13. So setup-token cannot drive the tile.

Net: there is no copy-paste credential that is both dedicated and
usage-scoped. We must mint our own.

## Interim state (already shipped)

- Refresh is gated to **401 only** (expired). 403 is a plain `FetchError`
  (permanent scope failure) and never triggers refresh. 429 backs off honoring
  `Retry-After`. (commit on 2026-06-13)
- The shared **`claude-refresh-token` secret key was cleared** so the dashboard
  cannot rotate/break the interactive session. Consequence: the claude tile
  runs read-only off the access token until it expires (hours), then goes
  offline (401, no refresh) until this plan lands. Acceptable, reversible.

## The fix: a dedicated PKCE OAuth login

Mirror what `steipete/CodexBar` does (`ClaudeLoginFlow` / `ClaudeOAuth*`): run
the Authorization-Code-with-PKCE flow against Claude's OAuth, requesting
`user:profile` (plus `user:inference`, `org:create_api_key` as Claude Code
does), to obtain an **independent** access+refresh token pair that belongs to
the dashboard alone. Refreshing that pair never touches the interactive
session.

### Work items

1. **`usage-dashboard login claude` CLI command** (run once on a workstation
   with a browser):
   - Generate PKCE verifier/challenge (S256).
   - Open the authorize URL (client_id = the public Claude Code client id;
     confirm current value before building — do not hardcode from memory).
   - Receive the code via a localhost loopback redirect, or accept a
     manual code paste for headless use.
   - Exchange code → access + refresh tokens at the token endpoint.
   - Print the two tokens for the operator to load into the Secret (do not
     write them to disk from this command).
2. **Scheduler/fetcher:** no change to the read path; it already uses
   access-token + refresh-on-401. Re-populate `claude-token` and
   `claude-refresh-token` from step 1's dedicated pair, and the existing
   refresh path is now safe because the pair is independent.
3. **Token persistence across refresh:** today a rotated refresh token lives
   only in the scheduler's memory and is lost on restart (then a stale refresh
   token in the Secret fails once → 401 → offline until re-login). Decide:
   persist the rotated pair back (to the PVC or by patching the Secret via the
   k8s API) so restarts survive a rotation. CodexBar persists; we probably
   should too.
4. **Docs:** README "Secrets" + a short "Claude login" section.

### Open questions

- Confirm the live Claude OAuth client_id, authorize/token URLs, and exact
  scope strings against a current source (CodexBar's `ClaudeOAuth*` or Claude
  Code itself) — these are not stable enough to trust from memory.
- Loopback redirect vs. manual code paste for the first login. Manual paste is
  simplest for a one-off and needs no open port.
- Whether to persist refreshed tokens by patching the Secret (needs RBAC for
  the pod's ServiceAccount) or by writing to the existing PVC alongside the DB.
