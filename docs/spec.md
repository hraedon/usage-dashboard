# Specification: Usage Dashboard

**Spec Level:** 2
**Desired Level:** 3
**Date:** 2026-06-11 (original elicitation)
**Extensions active:** None

---

## 0. Status — implementation has moved beyond this spec

This section was added 2026-06-22. Everything below it is the **original
2026-06-11 elicitation**, kept as the record of intent; it is no longer an
accurate description of the shipped system. The MVP (FR-01–FR-14, all three
original providers) was delivered, then extended well past the original scope.

**Shipped beyond this spec** (see `README.md` for current behaviour, `plans/`
for the planned pieces):

- **umans** as a 4th provider — quota-less, rendered via `Reading.detail`, with
  a `throttle` severity (none/low/rate_limited/boxed) that colours its line and
  shows a penalty-box countdown. *(No spec/plan entry — added directly.)*
- **Second Claude account** (work login) — merged into the Claude tile as a
  muted second set of bars. *(No spec/plan entry.)*
- **Dedicated Claude OAuth login** replacing the `~/.claude/.credentials.json`
  assumption — see `plans/001`. Resolves the §14 token-longevity assumption.
- **Cookie-based Ollama auth** (human-in-the-loop Playwright CLI), not
  email/password — resolves the §13 open question; §6/§14 still say
  email/password.
- **Primary display is a Pi 4B + Touch Display 2** (720×1280, pygame under a
  minimal X server), not the Pi Zero / ST7789 240×320 of §9/§14 (now the legacy
  PNG client). Adds per-model breakdown, local-timezone display, and
  **scheduled backlight sleep + tap-to-wake** with server-served per-unit
  schedules — see `plans/002` and the `/schedule` endpoint.
- **Responsive web `/dashboard`** — listed *out of scope* in §3, but built.

**Reading schema** now also carries `detail`, `models`, and `throttle` (the §6
schema shows only the original fields).

**Not built:** history/trends (Phase 2). Storage is **append-only** — the
`readings` table has an autoincrement `id` and each fetch appends a row; a
prune job honors `RETENTION_DAYS` (default 7). The data model no longer
precludes historical analysis. Trends UI is still Phase 2. The
`login claude` browser flow is also currently broken
(agent-notes WI-013); the workaround is a manual login elsewhere.

**Resolved open questions (§13):** z.ai `unit` mappings and the Claude
`seven_day` reset timestamp are both resolved in the implementation.

> Governance note: per `AGENTS.md`, features beyond the spec are supposed to land
> with a breadcrumb or plan entry. Several above (umans, 2nd Claude account,
> model breakdown, web dashboard) did not — hence this reconciliation. A full
> rewrite to a current Level-3 spec is the cleaner long-term fix if desired.

---

## 1. Problem Statement

**Problem:** There is no single view of consumption across multiple AI providers (Claude, z.ai, Ollama). Usage data is scattered across different dashboards, APIs, and workarounds with no unified glanceable summary.

**User/Operator:** A developer at a desk with a small dedicated display (e-ink or LCD) mounted in their workspace. The display shows current AI provider usage at a glance. The data collection server runs in their Kubernetes cluster.

**Success condition:** A small screen on the desk showing current session and weekly usage percentages with reset times for all three AI providers, updating automatically, readable from a few feet away.

---

## 2. Glossary

| Term | Definition |
|------|-----------|
| Session window | A rolling time window (typically 5 hours) after which short-term usage resets. Maps to `five_hour` (Claude), `TIME_LIMIT unit=5` (z.ai), and `session_percent` (Ollama). |
| Weekly window | A 7-day rolling window after which usage resets. Maps to `seven_day` (Claude), `TOKENS_LIMIT unit=6` (z.ai), and `weekly_percent` (Ollama). |
| Stale reading | A reading from a previous fetch cycle that could not be updated because the provider fetch failed. Displayed with a visual indicator. |
| Adaptive refresh | Client behavior: polls at 60-second intervals when usage values are changing between consecutive readings; returns to the default 5-minute interval after 5 consecutive unchanged readings. |
| Provider | One of the three AI services: Claude, z.ai, or Ollama. |
| Reading | A normalized snapshot of usage data for a single provider at a point in time. |
| Offline | A provider state displayed when the last 24 consecutive fetch attempts have failed. |

---

## 3. Scope

**In scope:**
- Fetching usage data from Claude (OAuth API), z.ai (REST API), and Ollama (scraped from web)
- Normalizing all provider data into a unified schema
- Serving readings via an authenticated API
- Rendering readings on a small display with horizontal progress bars, color thresholds, and reset timers
- Adaptive refresh behavior on the display client
- Graceful degradation when providers are unreachable

**Out of scope:**
- Historical usage graphs or trend analysis (nice-to-have, deferred)
- Multi-user support or multi-tenancy
- Mobile app or responsive web dashboard
- Push notifications or alerts
- Organization-level spend tracking (Claude Admin API)
- Management of the Kubernetes cluster itself

---

## 4. MVP Definition

The minimum version of this system that would be genuinely useful. Declared by the human during elicitation.

**MVP is:** A small display showing current session and weekly usage percentages with reset times for all three AI providers, updating every 5 minutes, with color-coded thresholds.

**MVP functional requirements:** FR-01, FR-02, FR-03, FR-04, FR-05, FR-06, FR-07, FR-08, FR-09, FR-10, FR-11, FR-12, FR-13, FR-14

**Rationale:** The snapshot view with color thresholds is the core value — it answers "how much usage do I have left?" at a glance. Historical trends and analysis add value but are not required for the dashboard to be useful.

**Note to implementing agent:** This reflects value priority as declared by the human, not implementation order. Some non-MVP requirements may be architecturally load-bearing and must be built before MVP features can function. Surface any such conflicts before writing code.

---

## 5. Functional Requirements

Numbered list. Each requirement is a complete, testable behavior. Format: Given [precondition], when [event/input], the system [does X].

MVP items are marked **[MVP]**.

- FR-01 **[MVP]**: Given a configured Claude OAuth access token, when the server fetches Claude usage, the system calls `GET https://api.anthropic.com/api/oauth/usage` with `Authorization: Bearer <token>` and `anthropic-beta: oauth-2025-04-20` header, and extracts `five_hour` and `seven_day` utilization percentages and reset times.

- FR-02 **[MVP]**: Given a configured z.ai API key, when the server fetches z.ai usage, the system calls `GET https://api.z.ai/api/monitor/usage/quota/limit` with `Authorization: Bearer <key>` and `Accept: application/json`, and extracts session and weekly window percentages and reset times from the `limits` array.

- FR-03 **[MVP]**: Given configured Ollama credentials, when the server fetches Ollama usage, the system authenticates to ollama.com, fetches the settings page, parses session and weekly usage percentages and reset times from the HTML.

- FR-04 **[MVP]**: Given a successful fetch from any provider, when the server normalizes the data, the system produces a unified reading containing: provider name, session percentage, session reset time, weekly percentage, weekly reset time, and fetch timestamp.

- FR-05 **[MVP]**: Given a normalized reading, when the server persists it, the system stores it in a local database (SQLite) with the provider name, all reading fields, and the fetch timestamp.

- FR-06 **[MVP]**: Given a valid API key in the request, when a client calls the server's readings endpoint, the system returns the most recent reading for each provider as JSON.

- FR-07 **[MVP]**: Given a configured server URL and API key, when the client starts, the system begins periodically fetching readings from the server API.

- FR-08 **[MVP]**: Given current readings from the server, when the client renders the display, the system shows a horizontal tile per provider containing: provider name, session usage as a horizontal progress bar with percentage, session reset countdown, weekly usage as a horizontal progress bar with percentage, and weekly reset countdown.

- FR-09 **[MVP]**: Given a reading where session or weekly usage is between 75% and 85%, when rendered, the system displays that bar in a visually distinct warning color (orange). Given usage at or above 85%, the system displays that bar in a critical color (red).

- FR-10 **[MVP]**: Given a reading where a provider's weekly window resets within the next 3 days, when rendered, the system visually highlights the reset countdown for that provider.

- FR-11 **[MVP]**: Given consecutive readings from the client's fetch cycle, when the usage value for any provider has changed compared to the previous reading, the system switches to a 60-second refresh interval. When 5 consecutive readings show no usage change across all providers, the system returns to the default 5-minute refresh interval.

- FR-12 **[MVP]**: Given a provider fetch that fails after the server has at least one previous reading for that provider, when the server serves readings, the system includes the last known reading with a `stale: true` flag and the original fetch timestamp.

- FR-13 **[MVP]**: Given 24 consecutive failed fetch attempts for a provider, when the server serves readings, the system marks that provider's status as `offline` instead of returning a stale reading.

- FR-14 **[MVP]**: Given a configured API key on the server, when any request arrives without a matching `Authorization: Bearer <key>` header, the system responds with HTTP 401 and no usage data.

---

## 6. Data

**Inputs:**

| Name | Format | Source | Validation |
|------|--------|--------|-----------|
| Claude OAuth token | JSON from `~/.claude/.credentials.json` | K8s secret | Must contain `accessToken`; must not be expired |
| z.ai API key | String | K8s secret | Must be non-empty |
| Ollama credentials | Email + password | K8s secret | Must be non-empty; used to authenticate to ollama.com |
| Client-server auth key | String | Config | Must be non-empty; shared between server and client |

**Outputs:**

| Name | Format | Destination |
|------|--------|------------|
| Readings API response | JSON | HTTP response to display client |
| Display rendering | Pixel buffer to LCD/e-ink | Physical display via SPI |

**Persisted state:**

| Description | Location | Retention |
|-------------|----------|-----------|
| Provider readings | SQLite file in server container | Latest reading per provider; configurable retention (assumed 7 days) |
| Ollama session cookie | In-memory or SQLite | Refreshed on expiry; not persisted across server restarts |

**Normalized reading schema:**

```json
{
  "provider": "claude",
  "status": "current",
  "session_percent": 62,
  "session_resets_at": "2026-06-11T19:00:00Z",
  "weekly_percent": 44,
  "weekly_resets_at": "2026-06-18T12:00:00Z",
  "fetched_at": "2026-06-11T14:32:00Z",
  "stale": false
}
```

`status` values: `current` | `stale` | `offline`

---

## 7. Business Rules

- BR-01: Provider credentials must never appear in logs, error messages, or API responses.
- BR-02: All credentials must be sourced from environment variables or Kubernetes secrets — never hardcoded.
- BR-03: No usage data leaves the cluster except the client's authenticated API request over the network to the server.
- BR-04: The server must not expose any endpoint that reveals credential values.
- BR-05: When a provider fetch returns an error, the server must continue serving readings for other providers without degradation.

---

## 8. Error and Failure Handling

| Failure | Trigger | Response | Notification |
|---------|---------|----------|-------------|
| Provider fetch fails (network error, timeout, 429, 5xx) | HTTP error during fetch cycle | Serve last known reading with `stale: true` | Log warning server-side |
| Provider auth fails (expired token, invalid credentials) | 401/403 from provider | Same as fetch failure; do not retry until next cycle | Log error server-side |
| Ollama session cookie expires during fetch | Redirect to login page or auth error | Re-authenticate with stored credentials; retry fetch once | Log info server-side |
| 24 consecutive failures for a provider | Failure counter reaches 24 | Mark provider `offline`; continue attempting fetches | Log warning server-side |
| Client cannot reach server | Network error on client fetch | Display last rendered screen; retry on next cycle | None (display is unattended) |
| Server starts with no prior readings | Empty database on first boot | Attempt all provider fetches immediately; return partial data if some succeed | None |

---

## 9. Non-Functional Requirements

Values derived from domain-language answers.

- **Display update latency**: Client renders new data within 2 seconds of receiving an API response — derived from: "I want to glance at it" (implied: the display should reflect current state without perceptible delay).
- **Server fetch reliability**: The server continues operating and serving cached data if one or more providers are down — derived from: "it just needs to show me where I stand" (implied: a partial dashboard is better than no dashboard).
- **Operability**: Both components deployed as container images; all configuration via environment variables — derived from: "live in kubernetes" and "no dependencies outside this application."
- **Display readability**: Text and bars readable from 3-4 feet away on a 240x320 or similar small display — derived from: "hang in my cubicle" (implied: viewed at desk distance).
- **Self-sufficiency**: No external agents, cron jobs on personal machines, or manual cookie extraction required after initial deployment — derived from: "I ideally want no dependencies outside this application."

---

## 10. High-Coupling Decisions

| Decision | Status | Notes |
|----------|--------|-------|
| Unified reading data model | Decided | Normalized schema with provider name, session/weekly percentages, reset times, status, stale flag. Shared between server and client. |
| Auth model | Decided | Single shared API key (Bearer token). Server validates on every request. Client sends on every fetch. Stored as env var / k8s secret. |
| State persistence (server) | Decided | SQLite with a mounted volume. Survives container restarts. Simple, single-file, no external database dependency. |
| Ollama scraping approach | Deferred with flexibility | Attempt HTTP-based login first (POST to ollama.com auth endpoint, extract session cookie, fetch settings). If ollama.com requires JavaScript rendering, fall back to headless browser (Playwright) in the server container. |
| Display rendering approach | Deferred with flexibility | Python with Pillow to compose frames, pushed to display via SPI (luma.lcd or equivalent ST7789 driver). Driver choice is swappable with ~1 file change. |
| Repo structure | Decided | Single monorepo producing two container images: server and client. Shared data model module. |

---

## 11. Acceptance Criteria and Test Plan

**Testable items:**

- AC-01 [FR-01]: Given a valid Claude OAuth token, when the server fetches Claude usage, then the response contains `session_percent` (mapped from `five_hour`) and `weekly_percent` (mapped from `seven_day`) as numeric values, and both `session_resets_at` and `weekly_resets_at` as ISO 8601 timestamps.
- AC-02 [FR-02]: Given a valid z.ai API key, when the server fetches z.ai usage, then the response contains session and weekly percentages and a session reset timestamp extracted from `nextResetTime`.
- AC-03 [FR-03]: Given valid Ollama credentials, when the server fetches Ollama usage, then the response contains `session_percent` and `weekly_percent` as numeric values and reset timestamps.
- AC-04 [FR-04]: Given raw data from any provider, when normalized, the output matches the unified reading schema (provider, session_percent, session_resets_at, weekly_percent, weekly_resets_at, fetched_at, stale, status).
- AC-05 [FR-05]: Given a normalized reading, when persisted, then querying the database by provider name returns the most recent reading with all fields intact.
- AC-06 [FR-06]: Given a request with a valid API key, when the readings endpoint is called, then the response is JSON containing the most recent reading for each configured provider with HTTP 200.
- AC-07 [FR-14]: Given a request with no Authorization header or an incorrect API key, when the readings endpoint is called, then the response is HTTP 401 with no usage data in the body.
- AC-08 [FR-07]: Given a configured server URL and API key, when the client starts, then it fetches readings from the server within the first refresh interval.
- AC-09 [FR-08]: Given readings from the server, when rendered, the display shows one tile per provider with provider name, session bar + percentage, session reset countdown, weekly bar + percentage, and weekly reset countdown, laid out horizontally.
- AC-10 [FR-09]: Given a reading with usage at 80%, when rendered, the bar color is orange (warning). Given a reading with usage at 90%, when rendered, the bar color is red (critical).
- AC-11 [FR-10]: Given a reading where `weekly_resets_at` is within 3 days, when rendered, the reset countdown is visually highlighted.
- AC-12 [FR-11]: Given that usage changed between two consecutive readings, when the client schedules the next fetch, then the interval is 60 seconds. Given 5 consecutive readings with no usage change, when the client schedules the next fetch, then the interval is 5 minutes.
- AC-13 [FR-12]: Given a provider fetch failure where a previous reading exists, when the server responds, the reading includes `stale: true` and the original `fetched_at` timestamp.
- AC-14 [FR-13]: Given 24 consecutive failures for a provider, when the server responds, the reading has `status: "offline"` and no percentage values.
- AC-15 [FR-01, FR-02, FR-03, FR-05, FR-06]: Given the server container starts, when startup completes, then the server begins fetching from all configured providers within the first minute.
- AC-16 [FR-07, FR-08]: Given the client container starts, when startup completes, then the client begins fetching from the server within the first refresh interval.

**Untestable items:**

| Item | Reason untestable |
|------|------------------|
| Readability from 3-4 feet | Requires human judgment; depends on physical display and mounting |
| Ollama scraping correctness across site redesigns | External dependency; site structure may change without notice |
| e-ink display ghosting | Hardware-specific; varies by display model and refresh strategy |

---

## 12. Work Decomposition

### Value Phases — owned by the human

- **Phase 1 (MVP):** FR-01 through FR-14 — A small display showing current session and weekly usage with color thresholds and adaptive refresh for all three providers. This is the minimum useful version.

- **Phase 2 (Nice-to-have):** Historical usage storage, trend graphs, sparklines. Adds temporal context to the snapshot view.

### Implementation Phasing — owned by the implementing agent

The agent is responsible for determining build sequence based on architectural dependencies.

**Known prerequisites identified during spec:**
- FR-08 (display rendering) requires FR-04 (normalized schema) to exist first — the renderer needs a stable data contract
- FR-11 (adaptive refresh) requires FR-07 (client fetch loop) and FR-04 (normalized schema) to compare readings
- FR-03 (Ollama scraping) may require a headless browser dependency in the server image — flagged during elicitation, not yet validated

**Dependency hints** *(intent-level only — not a build plan):*
- FR-04 and FR-05 (normalize + persist) are likely prerequisites for FR-06 through FR-13
- FR-01, FR-02, FR-03 are likely independent of each other and can be built in parallel
- FR-08, FR-09, FR-10 (rendering + thresholds) likely require FR-04 to be complete first
- FR-11 (adaptive refresh) likely requires FR-07 and FR-08 to be complete first
- FR-12, FR-13 (degradation) likely require FR-05 and FR-06 to be complete first
- FR-14 (auth) is likely independent and can be built early

**Limitation:** Dependency hints reflect intent and logical inference from the spec, not verified implementation constraints. Implementing agents must derive actual build order from the codebase.

---

## 13. Open Questions

| Question | Category | Owner |
|----------|----------|-------|
| ~~Can ollama.com be logged into via HTTP POST, or does it require a browser?~~ **Resolved 2026-06-14:** No HTTP login — ollama.com uses WorkOS AuthKit (JS-driven form + anti-bot device-fingerprint signal); WorkOS's password API needs ollama's server-side key. Chosen: a local human-in-the-loop Playwright CLI (`usage-dashboard login ollama`) that extracts the session cookie, not unattended server-side refresh. | Resolved | — |
| What are the exact mappings for z.ai `unit` values (3, 5, 6) to session/weekly windows? | Needs research | Implementing agent |
| Does the Claude OAuth API return `seven_day` reset timestamps, or only percentages? | Needs research | Implementing agent |

---

## 14. Assumptions

- **Ollama login is HTTP-based:** Assumed that ollama.com accepts standard form-based authentication that can be performed with HTTP requests. If not, a headless browser (Playwright) will be included in the server image as a fallback.
- **z.ai session window maps to `TIME_LIMIT unit=5`:** The `TIME_LIMIT` entry with `unit: 5` and a `nextResetTime` field is assumed to represent the session (5-hour) window. The `TOKENS_LIMIT` entries represent token caps within their respective windows. This will be validated at implementation time.
- **Claude OAuth token can be long-lived enough for server use:** Assumed that the OAuth token from `~/.claude/.credentials.json` can be used server-side with reasonable longevity. If it expires frequently, a refresh mechanism or Web API (cookie-based) fallback will be needed.
- **Pi Zero 2 W is the target display device:** Assumed based on the user's hardware research. The ST7789 LCD driver at 240x320 resolution is the baseline. Rendering approach should be adaptable to other SPI-driven displays.
- **Single API key is sufficient:** Assumed that one shared Bearer token between server and client is enough for the auth model. No per-user or per-device differentiation needed.
- **SQLite retention of 7 days:** Assumed that keeping readings for 7 days is sufficient. Older readings are pruned. This supports future Phase 2 (history/trends) without excessive storage.

---

## 15. Handoff State

**Decisions made:**
- **Two-component architecture (server + client):** Server runs in k8s as a Deployment, scrapes providers, serves API. Client runs on Pi Zero, polls server, renders display. Chosen for separation of concerns and because the scraping complexity (headless browser, credential management) belongs server-side.
- **Single monorepo:** One codebase producing two container images. Shared data model. Simpler than maintaining two repos for a small project.
- **SQLite for persistence:** File-based, no external database dependency, survives container restarts with a volume mount. Adequate for the read/write pattern (one write per provider per fetch cycle, one read per API request).
- **Bearer token auth:** Simplest auth model that meets the requirement of preventing casual snooping. One key, shared between client and server, stored as a k8s secret.

**Pending / deferred:**
- **Ollama scraping method:** HTTP-based login preferred; headless browser fallback. Deferred because the answer requires testing against the live site. Impact if wrong: adding Playwright to the server image increases image size by ~400MB and adds complexity.
- **z.ai field mapping:** Exact interpretation of `unit` values. Deferred because it requires empirical testing against the API. Impact if wrong: wrong data displayed; easily fixable by adjusting the mapping.
- **Claude OAuth token longevity:** Whether the token needs a refresh mechanism. Deferred because it depends on the user's Claude account and token lifecycle. Impact if wrong: server needs a token refresh flow or Web API fallback.

**Intent signals:**
- *"hang in my cubicle"* — The display is semi-public (coworkers can see it). Auth prevents snooping, but the display itself does not need to hide which providers are used.
- *"no dependencies outside this application"* — Zero-touch operation after deployment. No cron jobs on personal machines, no manual cookie extraction, no external agents. The system must be self-contained.
- *"I'd prefer to be able to power via USB-C"* — Hardware should be USB-C powered. The Pi Zero 2 W supports this. Not an architectural constraint but a deployment preference.
- *"every five minutes would be fine"* — Timeliness is valued but not urgency. The dashboard is ambient information, not an alerting system.
- *"historical usage and trend analysis would be nice to have"* — Phase 2 interest. The data model and storage should not preclude historical analysis, but it must not complicate the MVP build.

---

## 16. Delta to Next Level

What would be required to reach Level 3:

- **Resolve Ollama scraping method:** Test ollama.com auth flow to confirm HTTP-based login works or identify that headless browser is required. This eliminates the largest "needs research" item.
- **Validate z.ai field mappings:** Test the z.ai API response to confirm `unit` value meanings and map them to session/weekly windows with certainty.
- **Confirm Claude OAuth response schema:** Test the Claude usage API to confirm exactly what fields are returned (reset timestamps, model-specific windows, etc.).
- **Specify display pixel layout:** Define exact pixel positions, font sizes, and bar dimensions for the target resolution (240x320). This is implementable without this detail but would remove all rendering ambiguity.
