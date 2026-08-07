# Plan 003 — Versioned `/api/v1` surface + an enforced ingress contract

**Status:** proposed 2026-08-03, not started. Not urgent — no production
incident is open. Spans two repos (usage-dashboard, switchboard).

**Tracking:** usage-dashboard WI-024 (ingress path list), switchboard WI-001
(contract drift). This plan supersedes the three-option sketch on WI-024.

> Goal: stop the external API surface from being a hand-maintained list that
> silently 404s, and stop the usage-dashboard ↔ switchboard contract from
> drifting unnoticed. Both are the same underlying problem — an inter-component
> contract that nothing enforces.

## Problem

### 1. The ingress path list is hand-maintained

The external ingress (`usage-dashboard-readings-ext`, `usage.hraedon.com`)
routes **named Prefix paths only**. The Pis point `SERVER_URL` at that host, so
any endpoint the app grows 404s for the whole fleet until a human remembers to
patch the ingress. It has happened three times:

| # | endpoint | when |
|---|----------|------|
| 1 | `/readings` | original |
| 2 | `/schedule` | remote backlight schedules (WI-006) |
| 3 | `/refresh` | on-demand refresh (WI-012), 2026-08-03 |

There is a latent fourth: `/history` is authenticated but **not** routed
externally, so the moment a client wants trends (WI-002) it 404s too.

It degrades quietly. The client catches the error and shows `refresh failed`,
which reads as a server or provider fault. Nothing points at routing.

Root cause: the live ingresses are hand-applied and deliberately not committed
(the repo keeps a `CHANGE-ME.example.com` template), so nothing ties "the app
grew an endpoint" to "the ingress must learn about it".

### 2. The enumeration is load-bearing, and that constrains the fix

`/dashboard` is **deliberately unauthenticated** — it is the phone-friendly view
for the private network. The path allowlist is the only thing keeping it off the
public internet. Current surface:

| route | auth | external |
|-------|------|----------|
| `/readings` | yes | yes |
| `/schedule` | yes | yes |
| `/refresh` | yes | yes |
| `/history` | yes | **no** (latent gap) |
| `/dashboard` | **no** | no |
| `/` | **no** | no |
| `/health` | **no** | no |

So this is not tedium to be deleted; it is a deny-by-default boundary. Any fix
must preserve it. A bare `/` prefix is wrong.

### 3. `/readings` is a contract, not a private channel — and it has drifted

switchboard consumes `/readings` as a `TruthSource` for providers with no native
usage endpoint. The contract is already broken:

- `src/switchboard/dashboard.py` reads `target.get("timestamp")`.
- usage-dashboard emits **`fetched_at`**. There is no `timestamp` key.
- Therefore `ts_epoch` is `None` → `stale` stays `True` → `CachedReading.ok` is
  `False` on **every successful fetch**.
- Downstream, `providers.py` only computes `usage_headroom` / `quota_resets_in`
  when `.ok`, and `reconcile.py` refuses overrides on a not-ok reading. The
  usage-aware failover the truth source exists to provide never fires.

Verified against the live endpoint 2026-08-03. **Not a production incident:**
switchboard is not deployed (no k8s deployment, no `deploy/` dir). That also
makes now the cheapest possible moment to fix the contract.

Why it was invisible: `tests/test_dashboard.py` builds its own fixture with a
`timestamp` key, so the suite validates switchboard's *assumed* contract and
never usage-dashboard's *actual* one.

> **Recurring pattern worth naming.** Three separate bugs found on 2026-08-03
> shared this shape: a test that is self-consistent but never touches reality.
> The z.ai window bug (fixture pinned a `nextResetTime` that had aged into the
> past, making an inverted range look valid); the agent-notes `repo_root` bug (a
> test *depended* on the buggy repointing); and this one. Where a test encodes a
> contract with something outside the repo, it should be built from a recorded
> real payload, not a hand-written dict.

## Decision: which side is canonical

**usage-dashboard is the canonical source of provider usage. switchboard
consumes it.** This is already how the code is built; the plan records *why*, so
it does not get re-litigated:

- usage-dashboard **polls providers directly**, so it sees *all* usage —
  Claude Code sessions, the CLI, anything that never passes through the proxy.
  switchboard only ever sees what it proxies.
- Inverting it would make the authority structurally blind to most usage, and
  would couple usage data to the availability of an in-request-path proxy.
- usage-dashboard already owns the provider credentials, the poll scheduling and
  backoff, and the persisted history.

switchboard remains free to hold its own *in-flight* observations (it already
does, in `token_budget.py`) and reconcile them against the canonical reading.
That is a cache, not a second source of truth.

## Status (updated 2026-08-07)

| WP | State |
|----|-------|
| WP-1 | **DONE** — `tests/fixtures/readings.json` recorded, contract test asserts every key `DashboardTruthSource` reads. |
| WP-2 | **DONE** — one `APIRouter` mounted at `/api/v1` and at the legacy root, so the two path sets cannot drift. |
| WP-3 | items 1+3 **DONE** (single `/api` Prefix in the template; guard is prefix-coverage-aware). Item 2 (commit real manifests) **awaiting owner decision**. |
| WP-4 | **Blocked** — `mpmusage02` is offsite; cannot confirm both units on a WP-2 client. |
| WP-5 | Not started. |

**Premises that moved since this plan was written (2026-08-03):**

1. **The switchboard motive is confirmed and sharpened** (owner, 2026-08-07):
   switchboard will route by usage, and usage history is to be **centralized in
   usage-dashboard so provider calls are not doubled**. That settles open
   question 3 in favour of usage-dashboard as the canonical store, and it is
   what makes the version prefix earn its keep — a second consumer on an
   independent release cycle. Note `DashboardTruthSource` is still
   single-provider and will need rework for multi-provider routing; that is
   switchboard-side work, not a change to this contract.
2. **`/history` is now externally routed.** This happened on 2026-08-07 as a
   side effect of syncing the live ingress to the app contract — precisely the
   "conscious decision, not a side effect" this plan's Risks section asked to
   avoid. It is bearer-authenticated and returns 401 unauthenticated, so it is
   consistent with its siblings, but it was not an explicit decision. Ratify or
   revert it deliberately.
3. **The guard that landed is not yet WP-3 item 3.** It asserts *every authed
   route is routed externally and no unauthed one is* — the enumerated-list
   contract. Item 3 as written asks for *no authenticated route defined outside
   `/api/`*, which is a different and stronger claim. Still to do.

**Open question 1 is now load-bearing, not theoretical.** This plan asks whether
exposure should be *declared per route* rather than *inferred from "is it
authenticated"*. The guard currently encodes `authed ⇒ external`, and a single
`/api` Prefix rule makes that inference automatic: every future authenticated
route is externally reachable the moment it exists. That is a default-**allow**
posture for external exposure, sitting inside a plan whose stated purpose is to
preserve deny-by-default. `/history` is the worked example — it was exposed
without anyone deciding to. There is no authed-but-internal-only route today
(an admin or debug endpoint would be the first), so this is cheap to settle now
and expensive later.

## Plan

Four work packages. WP-1 is independent and can land any time; WP-2→WP-4 are
ordered.

### WP-1 — Fix the switchboard contract (switchboard WI-001)

Small, isolated, and it validates the canonical direction before anything else
moves.

1. Read `fetched_at`. Only accept `timestamp` as a fallback if some other
   producer actually emits it — otherwise drop it, so the mismatch cannot
   silently reappear.
2. Replace the hand-rolled fixture in `tests/test_dashboard.py` with a
   **recorded real `/readings` payload** committed as a fixture file.
3. Add a contract test that fails if the recorded fixture lacks any key
   `DashboardTruthSource` reads.

**Acceptance:** with the recorded payload, `CachedReading.ok` is `True` for a
fresh reading and `False` for a stale one. Confirm the new test fails against
the current `timestamp` lookup before trusting it.

### WP-2 — Introduce `/api/v1/*` with aliases

Move the authenticated routes under a single versioned prefix:

| now | becomes |
|-----|---------|
| `/readings` | `/api/v1/readings` |
| `/refresh` | `/api/v1/refresh` |
| `/history` | `/api/v1/history` |
| `/schedule` | `/api/v1/schedule` |

The unauthenticated routes (`/`, `/dashboard`, `/health`) **stay where they
are** and are never placed under `/api`.

**The old paths must keep working as aliases.** The server and the Pi clients
roll on independent schedules (server via image rebuild + rollout; clients via
the 15-minute `update.sh` timer), so a flag-day rename breaks the fleet. Serve
both path sets from the same handlers.

**Acceptance:** every authed route answers on both paths; `/dashboard` answers
on neither `/api/v1/dashboard` nor any `/api` path.

### WP-3 — Route one prefix, commit the manifests, enforce in CI

1. Add `/api` as a single Prefix path on the external ingress. The enumeration
   problem disappears permanently: future endpoints under `/api/v1` are routed
   the moment they exist.
2. **Commit the real ingress manifests**, replacing the `CHANGE-ME` template.
   The hostname is not a secret — cert-manager issues a public Let's Encrypt
   cert for `usage.hraedon.com`, so it is already in Certificate Transparency
   logs. Committing it leaks nothing new and is consistent with the
   homelab-names-are-not-secret stance. (usage-dashboard is a public repo;
   the work-domain denylist is unaffected by any of this.)
3. Add a CI check asserting:
   - no authenticated route is defined outside `/api/`;
   - the committed external ingress routes exactly `/api` (plus the legacy
     aliases until WP-4 removes them);
   - no unauthenticated route is reachable under `/api/`.

**Acceptance:** add a dummy authed route outside `/api` and watch CI fail.
Do not trust the check until it has been seen failing.

Note the boundary gets *stronger*, not weaker: `/dashboard` stays private
**by construction** rather than by a list someone has to maintain.

### WP-4 — Retire the aliases (gated on the fleet)

Remove the legacy paths from the app and the ingress once **both** units are
confirmed on a client that uses `/api/v1`.

**Gate:** `mpmusage02` lives at work and updates on its own schedule whenever it
has network. Do not remove aliases until both units report a commit at or after
the WP-2 client release. `git -C ~/usage-dashboard rev-parse --short HEAD` on
each unit is the check.

### WP-5 (small, independent) — Make the failure loud

Have the client distinguish a `404` from other failures — "endpoint not routed"
rather than "refresh failed". Cheap, and it addresses the half of the bug that
let this recur three times before anyone noticed.

## Risks

- **Fleet split-brain during WP-2→WP-4.** Mitigated by aliases plus the WP-4
  gate. The failure mode if rushed is a Pi that 404s every poll and shows stale
  data — quiet, which is exactly the problem this plan exists to fix.
- **`mpmusage02` is offsite.** It may sit on an old client for a while. That is
  fine; it only blocks WP-4.
- **Breaking switchboard.** It reads `/readings`. Do WP-1 first, and keep the
  alias through WP-2/WP-3 so switchboard can move to `/api/v1/readings` on its
  own schedule.
- **`/history` is authed but currently unrouted.** Routing `/api` exposes it
  externally for the first time. It is bearer-authenticated, so this is
  consistent with the others — but it should be a conscious decision, not a
  side effect. Confirm before WP-3 lands.

## Open questions

- Should the exposure decision be **declared per route** (e.g. a decorator or a
  registry marking `external` / `internal-only`) rather than inferred from
  "is it authenticated"? That makes adding a route *force* the decision. It is
  the more robust version of WP-3's check, at the cost of some machinery.
- Is `/api/v1` versioning worth it beyond solving the ingress problem — i.e.
  are breaking changes to `/readings` actually anticipated? If not, `/api/`
  without the `v1` is simpler. The counter-argument is that switchboard is a
  second consumer with an independent release cycle, which is exactly when a
  version prefix earns its keep.
- Should the two repos share a typed contract artifact (a small package, or a
  versioned JSON schema) rather than a recorded fixture? There is precedent in
  the suite (`agent-suite-conformance`, cross-repo `lock_agreement`). A fixture
  is the cheap 80%; a shared schema is the durable answer if more consumers
  appear.
