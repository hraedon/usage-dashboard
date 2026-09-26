# Plan 006 — Browser Enrolment Pane

Prepared: 2026-09-26
Status: in implementation
Origin: the 2026-09-26 claude_work incident. The work account's stored refresh
token died (`invalid_grant` on `platform.claude.com/v1/oauth/token`); the
operator swapped the k8s Secret to no effect, because Plan 005 made
`/data/tokens.json` authoritative and the Secret can only seed an *empty*
entry. Recovery required `kubectl exec` into the single RWO pod to run
`usage-dashboard login claude --account work` — correct, but it means every
future credential death costs a terminal session against the cluster.

## Problem

Enrolment is the designed recovery path for every credential the dashboard
holds, yet it is reachable only through `kubectl exec` into the server pod.
The ceremonies are already in-pod by necessity (the RWO PVC holding the token
store is mounted exactly once), so the remaining friction is purely the
transport to the operator: SSH + kubectl + a terminal paste buffer.

## Goal

Serve a browser pane (`/login`, internal ingress only) that runs the same
enrolment ceremonies the CLI runs, to the extent each provider's flow allows:

* **Claude (personal + work)** — fully interactive in the browser. The official
  `claude auth login --claudeai` ceremony is driven on a PTY inside the server
  pod; the pane streams its transcript (URL + one-time code) and feeds the
  operator's paste-back line to its stdin.
* **Codex** — fully non-interactive for the operator beyond opening a URL and
  entering a code: the App Server device-code flow surfaces
  `verificationUrl`/`userCode` as structured fields in the pane.
* **Ollama / OpenCode Go** — *not* driveable in-pod: their flows require a
  real GUI browser (Playwright) that cannot live in the container, and the
  WorkOS/anti-bot reality of ollama.com rules out headless. The pane instead
  verifies + stores an operator-pasted session cookie (and the `wrk_…`
  workspace id for OpenCode) directly into the token store, removing the
  `kubectl patch secret` + rollout hop from the existing runbook.

## Design decisions

1. **The CLI stays the ceremony owner.** The pane spawns
   `python -m usage_dashboard.cli login claude …` as a subprocess rather than
   re-implementing the flow in HTTP. One code path per ceremony — the pane is
   a transport, not a second implementation that can drift. For Codex the
   service calls `cli.login_codex` directly with an injected `emit`/`present`
   pair (a small, backwards-compatible CLI refactor), because that flow has
   no terminal dependency to preserve.

2. **One enrolment job at a time.** Jobs are serialized behind a single
   active-job guard: the store is a single JSON file, the Claude ceremony
   mints a credential family that must have exactly one refresher, and the
   Codex flow needs exclusive `CODEX_HOME` ownership. Concurrent ceremonies
   would violate all three. Finished jobs are kept (bounded) for inspection.

3. **Claude runs on a PTY.** The official CLI's SSH/container story is
   "display a code the operator pastes back"; it expects a terminal. The
   service allocates a PTY, streams the transcript (ANSI-stripped,
   token-shaped runs redacted as defence in depth), and writes operator input
   to the master side. The transcript never echoes a pasted credential back.

4. **Codex enrolment pauses the runtime App Server.** "Never run two App
   Server processes against one CODEX_HOME" is a hard Plan 005 rule. The
   scheduler gains `pause_codex()`/`resume_codex()`: pause closes the
   persistent child (blocking until any in-flight fetch completes — the
   client's own lock guarantees this), enrolment runs its one-shot child,
   resume rebuilds the client from an injected factory. This replaces the
   runbook's `CODEX_MODE=disabled` + two rollouts with a sub-second hop, and
   the Codex tile degrades to one contained fetch failure during enrolment —
   the same shape as a poll during a restart, not a new failure mode.

5. **Credentials reload without a rollout.** The scheduler resolves tokens
   once at startup and holds them in memory, which is why enrolment
   previously required `kubectl rollout restart` to take effect. The
   scheduler gains `update_credentials(**attrs)` (a locked attribute swap;
   fetch tasks are rebuilt from the attributes every cycle), and main wires a
   `_reload_credentials()` closure that re-runs the Plan 005 resolution
   functions (store-authoritative, env-seeded) and applies the result. The
   pane triggers it after every successful enrolment/credential store, then
   forces a fetch. The Secret stays what it always was: a seed for empty
   entries, never a live credential channel.

6. **OpenCode workspace id moves store-side.** The `wrk_…` id joins the
   `auth` cookie as a token-store field (`opencode.workspace_id` metadata),
   resolved store-first with the env var as fallback, so a pane-submitted
   workspace survives without a Secret patch. Env-only deployments are
   unaffected.

7. **Exposure: internal-only, declared.** Every `/internal/v1/login/*` route
   carries `**INTERNAL_ONLY` and is mounted off `/api`, so the external
   ingress (`usage.hraedon.com`, whole-`/api` routing) cannot reach a surface
   that mints credentials. The `/login` page itself is an unauthenticated
   shell exactly like `/dashboard` (no data without the bearer key), also not
   routed externally. The Plan 003 contract test enforces both halves.

8. **Verification before store, unchanged.** Paste-submit runs the real
   fetcher (`fetch_ollama_usage` / `fetch_opencode_usage`) before anything is
   written, mirroring the CLI's `--verify` and `login_claude`'s
   `_validate_usage_access`: a credential that cannot do the dashboard's one
   job never displaces a working one.

## Non-goals

* No OAuth implementation of our own (Plan 005 hard rule, unchanged).
* No Playwright in the server image; ollama/opencode stay paste-based.
* No changes to `/readings`, `/dashboard`, client GUIs, or the reading schema.

## Acceptance criteria

* AC-1: `GET /login` serves the pane on the internal host; every
  `/internal/v1/login/*` route requires the bearer key and declares
  `internal-only`; the ingress contract test stays green with the new routes.
* AC-2: Starting a Claude enrolment streams the ceremony transcript to the
  pane; submitting input through the pane reaches the CLI's stdin; on exit 0
  the new credential is in the token store, the scheduler's in-memory
  credentials were reloaded, and a fetch runs without a pod restart.
* AC-3: Starting a Codex enrolment pauses the runtime App Server, surfaces
  verification URL + user code in the pane, and resumes the client on both
  success and failure.
* AC-4: An ollama/opencode paste that fails provider verification stores
  nothing; a successful paste stores credential (+ workspace id) and reloads.
* AC-5: Transcripts redact token-shaped material; pasted credentials are
  never echoed into any transcript or API response.
* AC-6: A second enrolment cannot start while one is active; finished jobs
  remain inspectable; a running Claude job can be cancelled.
* AC-7: Existing suite stays green; new unit tests cover the service
  lifecycle, PTY streaming/input, pause/resume, update_credentials, store
  metadata, and API auth/exposure.

## Test plan

`tests/test_enrolment.py` (service + API), additions to
`tests/test_scheduler.py` (update_credentials, pause/resume via a fake
client + factory) and `tests/test_token_store.py` (credential metadata),
`tests/test_api.py` (login routes auth + 501-without-service + page shell).
CI: `ci.yaml` (pytest/ruff/mypy) + `identifier-gate` + the Plan 003 contract
test against the real ingress manifest.
