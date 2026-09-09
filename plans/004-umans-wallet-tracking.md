# Plan 004 — Umans wallet tracking (Pi corner line)

**Status:** implemented 2026-09-09 (branch `feat/umans-wallet`); verified
live against the real API — server `/readings` carries the wallet reading
(`detail: "$15.94"`, no promo tail: the live API exposes no promo field).
The promo tail was exercised against the stub server, whose frame captures at
1280×720 and 720×1280 show `Umans: $15.94, promo: $7.14` right-aligned in the
status band; the web `/dashboard` capsule was checked the same way. Read the
promo half as stub-verified, not live-verified. Work-item
backfill pending: the agent-notes store is down (regista migrations 45–50
pending across several project schemas; CLI refuses all operations as of
2026-09-09) — this plan is the tracked entry per AGENTS.md until the store
returns.

> Goal: show the Umans wallet balance (and promo credit when the API exposes
> one) as a small corner line on the Pi units, of the form
> `Umans: $16.32, promo: $7.14`.

## Problem

Umans Code is pay-per-token from a prepaid wallet; the balance is the real
spend limit. The owner wants that number glanceable on the dashboard panels —
not as a provider tile (the wallet has no session/weekly quota windows), but
as a corner line.

The umans provider was removed entirely in e871c8c (PR #22) and WI-021 was
closed "do NOT re-implement". That closure covered the retired trailing-24h
usage line; this plan is a new, owner-requested feature (the wallet spend
view) and explicitly supersedes that note. Tracked here while the agent-notes
store is down (regista migrations 45–50 pending, CLI bricked 2026-09-09);
file a WI referencing this plan when it returns.

## API (live-verified 2026-09-09 with the rotated key)

- `GET https://app.umans.ai/api/v1/wallet/summary`, Bearer = the gateway key
  (same `umans-api-key` secret as before). Amounts are **cents** (floats).
- Response: `balance.balanceCents` + `funded` + `asOf`, `spend`
  (24h/7d/30d cents), `usage30d`, per-model `breakdown`.
- The ledger trails real time ~1 min (`asOf` labels freshness).
- **No promo field exists in the documented shape or the live response
  today.** Promo/bonus credit is a documented wallet concept ("no paid
  top-up, no active promo grant"), so the fetcher parses plausible promo
  fields tolerantly (`promoCents` / `promo_cents` in `balance` or at top
  level) and the display shows `, promo: $X` only when one is present.
- `404 wallet_not_found` = archived-plan key: a documented steady state, not
  an error — render `no wallet` as the line instead of failing the provider.
- `401 invalid_key` = auth failure → normal FetchAuthError path (stale →
  offline); personal wallets reject `scope=organization` (403) — never send
  a scope param.
- `usage-history` endpoint exists (bucketed spend) but is out of scope here.

## Design decisions

- **Model the wallet as a `Provider.UMANS` quota-less reading** whose
  `detail` carries the formatted money text. This reuses the entire
  scheduler/backoff/stale/offline/DB/`/readings` path — the exact pattern the
  retired umans provider used (and z.ai's weekly-token line still uses).
  No schema change (WI-002's one-row-per-provider gripe is not worsened: the
  readings table already carries arbitrary `detail`).
- **No tile.** `_PROVIDER_ORDER` in the layout deliberately omits UMANS, so
  the grid is unchanged; `build_main_layout` derives `wallet_text` from the
  umans reading and the GUI renders it **right-aligned in the status bar**
  (the bottom corner, left of the refresh button), shrinking the status
  text's fit region so the two can never collide.
- **Format lives server-side**: `detail = "$16.32"` or
  `"$16.32, promo: $7.14"`; the layout prefixes `Umans: `. The client never
  reformats money.
- **Pi + web.** The Pi shows the corner line in the status band; the web
  `/dashboard` shows the same string as a small capsule in the header's
  top-right corner (a capsule, not a card — the wallet is a balance, not a
  quota provider). The show/hide + wording rule lives in
  `shared/format.umans_wallet_line` so the two surfaces cannot drift the way
  WI-020/WI-030 did; `test_render_parity.py` locks it. Follow-ups (not in
  this slice): tap-through wallet detail, low-balance tint.
- **Polling**: the normal idle ladder applies; the balance is cheap to fetch
  and changes only when the ledger moves. Old `umans` rows in the prod DB
  (pre-#22) parse again once the enum member returns; the first wallet fetch
  overwrites them within one poll, so the transient is invisible.
- **Config**: `UMANS_API_KEY` env (secret key `umans-api-key`, already
  present in the cluster secret; the owner rotated the value 2026-09-09).
  Absent → provider unconfigured → absent from `/readings` (WI-003 rule).

## Acceptance

1. `fetch_umans_wallet` parses the live shape (fixture recorded from the
   2026-09-09 response): balance-only detail; promo appended when a promo
   field is present; 404 wallet_not_found → current "no wallet" reading;
   401 → FetchAuthError; 429 → FetchRateLimitError (Retry-After honored);
   malformed → FetchError.
2. Scheduler: umans configured iff key set; `configured_providers` stays in
   enum order.
3. Layout: no UMANS tile at any audited size; `wallet_text` None without the
   reading, prefixed line with it.
4. GUI: headless render exercises the corner line at all audit sizes; status
   text and wallet line never overlap (fit regions partition the band).
5. `pytest -q`, `ruff check .`, `mypy src` clean.
6. Runtime verify per `.claude/skills/verify` (stub server + live-server
   frame capture) before review.
