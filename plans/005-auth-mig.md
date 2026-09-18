Usage Dashboard Authentication Migration — Implementer Handoff

Repository: hraedon/usage-dashboard
Prepared: 2026-09-18
Purpose: Replace the dashboard's custom OpenAI OAuth/private-endpoint integration with the supported Codex App Server interface, and replace the custom Claude enrollment ceremony with the official Claude Code login while retaining the isolated Claude quota fetcher.

Executive summary

Implement two related authentication changes without altering the dashboard's public API or presentation:

Codex/OpenAI: make the official codex app-server process the sole owner of ChatGPT authentication and quota retrieval. The dashboard must no longer mint, refresh, store, or transmit OpenAI OAuth tokens and must no longer call https://chatgpt.com/backend-api/wham/usage directly.

Claude/Anthropic: use the official Claude Code /login flow to create a dedicated credential for each dashboard account, import that credential into the existing isolated token store, and then let the dashboard remain the sole refresher for that credential family. Continue calling GET https://api.anthropic.com/api/oauth/usage, because Anthropic still exposes no documented full-fidelity subscription-quota API or headless RPC replacement.

The desired steady state is:

Codex tile
  usage-dashboard -> codex app-server -> account/rateLimits/read
                                      -> Codex-managed OAuth and refresh

Claude tile
  official Claude Code /login (one-time enrollment)
      -> isolated dashboard token family
      -> usage-dashboard-owned refresh
      -> /api/oauth/usage (undocumented)

The current normalized Reading, scheduler behavior, SQLite history, /readings, /dashboard, and clients should not change.

Important correction and support boundary

Do not design around CLAUDE_CODE_OAUTH_REFRESH_TOKEN or CLAUDE_CODE_OAUTH_SCOPES. I previously described those as documented automation inputs; I could not substantiate that in the current official Claude Code documentation. The documented behavior is:

/login works in SSH sessions and containers using a displayed code that the operator pastes back into the terminal.

On Linux, Claude Code stores the resulting credential in ~/.claude/.credentials.json, or beneath the directory named by CLAUDE_CONFIG_DIR.

claude setup-token creates a long-lived inference token, but it cannot provide the user:profile access needed by the subscription-usage endpoint.

Therefore the Claude design below intentionally uses the official login ceremony but still has two unsupported implementation dependencies: reading Claude Code's credential file once during import, and calling /api/oauth/usage. Keep both dependencies isolated and clearly documented.

Current repository state

The following is true on main at preparation time:

src/usage_dashboard/server/fetch_codex.py

hardcodes the Codex CLI OAuth client ID;

refreshes tokens directly through auth.openai.com/oauth/token;

identifies requests as codex_cli_rs;

calls the internal /backend-api/wham/usage endpoint;

parses both session+weekly and weekly-only response shapes.

src/usage_dashboard/cli.py

implements independent PKCE flows for Claude and Codex;

prints token material for manual transfer into a Kubernetes Secret.

src/usage_dashboard/server/scheduler.py

owns access/refresh pairs for Claude, Claude work, and Codex;

refreshes on FetchAuthError and persists rotated pairs.

src/usage_dashboard/server/token_store.py

stores token pairs in /data/tokens.json with atomic replacement and mode 0600;

uses an in-process lock only.

k8s/server-deployment.yaml injects Claude and Codex tokens/client IDs/account ID from server-secrets.

Dockerfile.server contains only the Python application; neither official CLI is installed.

The server uses a single-replica Recreate deployment because the Longhorn PVC is RWO.

Relevant repository files:

fetch_codex.py

fetch_claude.py

scheduler.py

token_store.py

cli.py

Dockerfile.server

server-deployment.yaml

Required target architecture

1. Codex App Server adapter

Create a small adapter, preferably src/usage_dashboard/server/codex_app_server.py, which owns one long-lived codex app-server subprocess.

Required behavior:

Start the process with a dedicated persistent CODEX_HOME, defaulting to /data/codex.

Prefer the least-privileged supported invocation, currently expected to be equivalent to:

codex -s read-only -a never app-server

Verify the exact global-flag ordering against the pinned Codex version in the image. If those flags are not accepted, document the tested alternative.

Use the default stdio JSONL transport.

Immediately send one initialize request and then one initialized notification. Use honest client metadata such as:

{
  "method": "initialize",
  "id": 1,
  "params": {
    "clientInfo": {
      "name": "usage_dashboard",
      "title": "Usage Dashboard",
      "version": "0.1.0"
    }
  }
}

Do not enable experimentalApi; the account rate-limit methods are on the stable surface.

Serialize requests with a lock. The scheduler loop and the API-triggered fetch_now() path can overlap.

Assign monotonically increasing request IDs and read until the matching response arrives. Ignore or log safe metadata from unrelated notifications; never assume the next output line belongs to the last request.

Drain stderr continuously so the child cannot deadlock on a full pipe. Never copy credential-bearing output into application logs.

Apply a finite request timeout.

If the subprocess exits or the transport breaks, restart it at most once for that fetch. If the retry fails, raise a normal FetchError and let the existing scheduler backoff operate. Do not create an internal restart storm.

Provide close() and call it from FetchScheduler.stop() or the owning service lifecycle.

The adapter needs these operations:

account/read for authentication state and an actionable "login required" failure;

account/rateLimits/read for the actual tile data;

account/login/start with type: chatgptDeviceCode for the one-time enrollment command.

OpenAI documents that ChatGPT-managed mode makes Codex own the OAuth flow, token persistence, and refresh. It also documents account/rateLimits/read, including rateLimits, rateLimitsByLimitId, usedPercent, windowDurationMins, and resetsAt: Codex App Server documentation.

2. Codex parsing and Reading mapping

Refactor fetch_codex.py into a pure adapter/parser layer over the App Server response. It must contain no OpenAI OAuth endpoints, client IDs, originator spoofing, refresh-token code, or direct HTTP calls.

Mapping rules:

App Server field

Dashboard field

rateLimitsByLimitId["codex"], when present

preferred bucket

otherwise rateLimits

compatibility bucket

primary.usedPercent

session_percent

primary.resetsAt

session_resets_at

secondary.usedPercent

weekly_percent

secondary.resetsAt

weekly_resets_at

Preserve the current weekly-only behavior. If only one window exists, classify it by windowDurationMins, not by whether it arrived in the primary or secondary slot. Use a named threshold comfortably above the session window and below the weekly window.

Additional rules:

Treat timestamps as Unix seconds and normalize to naive UTC, matching current model conventions.

Tolerate absent/null windows.

Ignore credits, reset-credit details, and other buckets for now. Do not silently merge codex_other into the Codex tile.

If account/read reports no ChatGPT account/auth mode, raise an actionable auth error such as Codex login required; do not attempt custom refresh.

Preserve provider name, status, freshness, scheduler backoff, and database behavior.

3. Codex configuration and enrollment

Add explicit configuration:

CODEX_MODE=app_server|legacy|disabled during migration;

CODEX_HOME=/data/codex;

CODEX_BIN=/usr/local/bin/codex or equivalent.

Use legacy only as a deployment bridge. The final state must default to app_server when Codex is enabled and must remove the legacy implementation.

Replace usage-dashboard login codex with a wrapper around a one-shot App Server process using the same CODEX_HOME as runtime:

Start and initialize App Server.

Send:

{
  "method": "account/login/start",
  "id": 2,
  "params": { "type": "chatgptDeviceCode" }
}

Print only the returned verification URL and user code.

Wait for the matching account/login/completed notification.

Verify account/read reports ChatGPT-managed auth.

Verify account/rateLimits/read succeeds.

Exit without printing or copying any token.

For initial migration, deploy the new image in legacy mode, run the new enrollment command in the existing pod, then change the mode to app_server and roll the deployment. The legacy scheduler does not use CODEX_HOME, so this avoids competing App Server processes while keeping the current tile alive until cutover.

For later re-enrollment, do not run two App Server processes against the same CODEX_HOME. Temporarily set Codex to disabled, roll the pod, run enrollment, then restore app_server and roll again. Document this sequence.

4. Claude official-login bootstrap

Keep the current Claude fetcher and refresh-on-401 behavior, but delete the custom Claude authorization-code/PKCE ceremony from cli.py.

The replacement usage-dashboard login claude command should:

Require a target account selector, for example --account personal|work, mapping to token-store keys claude and claude_work.

Create a mode-0700 temporary directory and set it as CLAUDE_CONFIG_DIR for a child claude process.

Launch the official Claude Code login interactively with the caller's TTY attached. Current official documentation guarantees the copy/paste-code path for SSH sessions and containers. If the bundled version has a tested noninteractive command that begins the same official login, use it; otherwise launch claude, instruct the operator to complete /login, and exit the session afterward.

Read <CLAUDE_CONFIG_DIR>/.credentials.json after successful login. At implementation time, confirm the exact schema emitted by the pinned Claude Code version. Do not silently accept unknown structures.

Extract the access token, refresh token, optional client ID, expiry, and scopes when present.

Require user:profile when scope metadata is available. If scope metadata is absent, validate permission by calling the existing Claude usage fetcher before changing stored credentials.

Save the pair directly into /data/tokens.json under the selected account key. Do not print either token.

Delete the temporary Claude credential store after a successful import. This is essential: after import, usage-dashboard must be the only process capable of refreshing that credential family.

Leave the prior token-store entry untouched if login, parsing, scope validation, or the live verification request fails.

Anthropic documents the container/SSH login-code path, Linux credential location, CLAUDE_CONFIG_DIR, and the limitations of setup-token: Claude Code authentication.

The importer's credential-file parser is an intentionally quarantined compatibility layer. Put it in a small module with fixture-based tests and an error that names the installed Claude Code version when an unknown schema appears.

5. Claude runtime source of truth

After enrollment, runtime must load Claude credentials from TokenStore, not from Kubernetes environment variables.

The existing environment-seed algorithm is unsafe for the new flow: an old Secret value can overwrite a newly imported credential after restart. Change it as follows:

Existing deployments may perform a one-time import of current Secret values into an empty token store.

Once a token-store entry exists, it is authoritative.

Fresh credentials are replaced only by the official enrollment command.

Remove CLAUDE_TOKEN, CLAUDE_REFRESH_TOKEN, CLAUDE_CLIENT_ID, and work-account equivalents from the deployment after migration.

If Claude refresh requires a client ID, persist it as optional metadata alongside the pair. Prefer the value emitted by the official credential store. Test whether the refresh endpoint accepts an omitted client ID before retaining any hardcoded client identifier.

The Claude read and refresh operations remain unsupported interfaces. Preserve these invariants:

refresh only after 401, never after 403, 429, or a transport error;

one credential family has exactly one refresher;

persist a rotated refresh token before considering the refresh successful;

retry the usage request once after refresh;

redact tokens from logs and exceptions.

6. Token-store concurrency and durability

The enrollment commands run as a second process in the pod, while the current TokenStore lock is only thread-local. Fix this before allowing an enrollment command to update /data/tokens.json alongside the server.

Preferred implementation:

use an advisory filesystem lock next to the token file;

under that lock, reload the latest JSON, apply one mutation, write a mode-0600 temp file, fsync, and atomically replace the target;

optionally fsync the parent directory after replacement;

reject malformed on-disk content rather than overwriting it with an empty store;

keep existing entries and metadata not involved in the mutation;

reload before reads that need to observe a credential written by another process, or require a process restart after enrollment and document that requirement.

The operational procedure should still roll the deployment after enrollment so the scheduler starts with the newly imported credential.

Container and Kubernetes changes

Server image

Add the official Codex CLI and Claude Code CLI to Dockerfile.server.

Requirements:

Pin both versions. Do not install an unversioned latest during every image build.

Use an official distribution path and verify the downloaded artifact/checksum where the distribution supports it.

Keep build tooling out of the runtime image where practical.

Run codex --version and claude --version during the image build.

Record both versions in startup logs at INFO without logging configuration or credentials.

Confirm the image architecture matches the Kubernetes nodes.

OpenAI's current CLI documentation includes a standalone macOS/Linux installer, while App Server ships as a Codex CLI subcommand: Codex CLI.

Deployment

Add:

- name: CODEX_MODE
  value: "app_server"
- name: CODEX_HOME
  value: "/data/codex"
- name: CODEX_BIN
  value: "/usr/local/bin/codex"

Remove after successful migration:

CODEX_TOKEN

CODEX_REFRESH_TOKEN

CODEX_CLIENT_ID

CODEX_ACCOUNT_ID

CLAUDE_TOKEN

CLAUDE_REFRESH_TOKEN

CLAUDE_CLIENT_ID

the corresponding CLAUDE_WORK_* variables

Do not delete the Secret values during the first rollout. Stop injecting them, retain them for one rollback/soak period, and remove them only after the new image has survived token refreshes and pod restarts.

Add a short operator runbook to the README covering:

Codex enrollment and re-enrollment;

personal and work Claude enrollment;

the required rollout restart after enrollment;

how to distinguish expired/revoked auth from transport failure;

rollback to the prior image and legacy Secret values.

Suggested implementation sequence

The repository requires work to be represented by a plan entry or tracked work item. Add one before coding.

Phase A — safe migration bridge

Add pinned official CLIs to the server image.

Add the App Server protocol client and unit tests.

Add CODEX_MODE, retaining the old Codex path only under legacy.

Replace the Codex login CLI with App Server device-code enrollment.

Add the Claude official-login importer while retaining the current runtime tokens.

Harden TokenStore for cross-process mutation.

Update Kubernetes manifests and README.

Phase B — production enrollment and cutover

Deploy Phase A with CODEX_MODE=legacy.

Run Codex enrollment in the pod against /data/codex.

Run Claude personal/work enrollment only if replacing the existing dedicated token families now; otherwise leave the existing token-store entries in place and use the new flow at the next reauthentication.

Set CODEX_MODE=app_server, stop injecting legacy credentials, and roll the deployment.

Verify live readings, adaptive scheduling, manual refresh, and restart persistence.

Phase C — cleanup after soak

Remove legacy Codex HTTP/OAuth code and legacy mode.

Remove custom Claude PKCE code and obsolete CLI helpers/tests.

Remove obsolete Secret keys after rollback is no longer needed.

Remove dead constants, docs, and token-store seed-marker behavior that served only Secret-based enrollment.

File-level change map

File

Expected change

server/codex_app_server.py

New supervised JSONL client, login support, process lifecycle, and error mapping.

server/fetch_codex.py

Keep only App Server response parsing and Reading construction; delete direct HTTP/OAuth behavior.

server/scheduler.py

Accept a Codex fetch callable/client instead of Codex tokens; remove Codex refresh/retry ownership; close the client on shutdown. Keep Claude refresh behavior.

server/main.py

Load Claude pairs from TokenStore; construct the Codex client from CODEX_MODE, CODEX_HOME, and CODEX_BIN; stop resolving Codex tokens from the Secret.

server/token_store.py

Add cross-process-safe mutations and optional OAuth metadata if Claude refresh requires it.

cli.py

Replace both custom PKCE flows with the Codex App Server enrollment and Claude official-login/import commands. Delete obsolete OAuth/JWT helpers.

Dockerfile.server

Install pinned Codex and Claude Code executables and verify their versions.

k8s/server-deployment.yaml

Add App Server configuration and remove token env injection after cutover.

k8s/server-secret.yaml

Mark old keys deprecated during soak; remove them only in cleanup. Do not add new subscription tokens.

README.md

Replace token-copy instructions with terminal enrollment/re-enrollment and rollback runbooks; state the Anthropic support boundary plainly.

tests/test_fetchers.py

Replace direct Codex HTTP fixtures with App Server response fixtures; preserve weekly-only coverage.

tests/test_cli.py

Remove custom-PKCE expectations; add official-login import and device-code tests.

new focused test module

Exercise process framing, interleaved notifications, timeouts, restart, and cleanup using a fake executable.

The scheduler's configured-provider contract should remain deterministic. In app_server mode, Codex is configured because the operator explicitly enabled that mode; an absent/expired login produces an offline/auth-required reading rather than silently removing the tile. Claude and Claude-work remain configured only when their respective token-store entries exist.

Test requirements

Codex protocol tests

initialization occurs exactly once per child process;

requests before initialization are impossible through the public adapter;

notifications interleaved before a response do not break ID correlation;

concurrent callers serialize correctly;

timeout, malformed JSON, EOF, and child exit become FetchError;

one transport restart is attempted, then scheduler backoff takes over;

stderr is drained and token-like values are not logged;

close() terminates and reaps the child.

Codex parser tests

standard primary+secondary windows;

weekly-only window in the primary slot;

session-only window;

rateLimitsByLimitId["codex"] preferred over compatibility rateLimits;

compatibility response with only rateLimits;

unknown additional buckets ignored;

null/missing percentages and reset times;

malformed response raises a contained parse error.

Codex enrollment tests

prints verification URL/code but never tokens;

waits for the matching completion notification;

failure/cancellation leaves existing CODEX_HOME state recoverable;

verifies account/read and account/rateLimits/read before success.

Claude enrollment tests

personal and work targets map to separate token-store keys;

known credential schema imports correctly;

unknown schema fails closed and includes the CLI version in the error;

missing access/refresh token fails without modifying the existing entry;

missing user:profile fails when scopes are available;

a 403 during validation fails without replacing the existing entry;

successful import never prints tokens and removes the temporary credential file;

subsequent refresh rotation affects only the selected account.

Token-store tests

atomic write and 0600 mode;

cross-process serialization;

read-modify-write preserves unrelated provider entries;

malformed existing JSON is not silently replaced;

restart loads the newest rotated credential.

Regression suite

Run the repository's required checks:

.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src

Add focused integration tests using fake codex and claude executables placed earlier on PATH; CI must not require real accounts or network access.

Acceptance criteria

The work is complete only when all of the following are true:

No application code contains the OpenAI OAuth client ID, OpenAI token endpoint, codex_cli_rs originator, or /backend-api/wham/usage URL.

Codex readings come exclusively from account/rateLimits/read through a Codex-owned App Server process.

Codex auth survives a pod restart through persistent CODEX_HOME without Kubernetes token secrets.

usage-dashboard login codex completes device-code enrollment without exposing token material.

The project no longer implements Claude's initial authorization-code/PKCE flow.

usage-dashboard login claude --account personal|work uses the official Claude Code login, imports a dedicated credential, validates quota access, and removes the temporary official-client copy.

Claude token refresh remains isolated per account and persists rotated tokens.

The existing dashboard API, provider names, history, adaptive polling, display layout, and two-account Claude presentation remain unchanged.

A clean deployment can be enrolled entirely through terminal access to Kubernetes; no sacrificial VM or token copy/paste is required.

Tests, lint, and strict type checking pass.

Explicit non-goals

Do not add API token-usage summaries to the Codex tile; this change uses rate-limit windows only.

Do not expose Codex reset-credit consumption in the UI.

Do not combine additional App Server limit buckets into the existing tile without a separate design decision.

Do not replace the Claude usage endpoint with status-line scraping; it is incomplete and session-dependent.

Do not point the dashboard at a Claude Code credential store used for interactive development.

Do not broaden the public API or add a browser-based administration surface as part of this migration.

Risks and mitigations

Risk

Mitigation

App Server protocol changes

Pin Codex, isolate protocol code, fixture tests, log version, fail contained.

App Server child hangs or exits

Timeout, stderr drain, one restart, scheduler backoff, proper reap.

Claude credential schema changes

Tiny fail-closed importer, fixtures by CLI version, preserve old token on failure.

Two refreshers rotate one Claude family

Delete temporary Claude credential store after import; never reuse an interactive profile.

Enrollment races with token-store writes

Cross-process file lock and read-modify-write; rollout after enrollment.

Old Secret overwrites new Claude login

Make the PVC token store authoritative; remove env injection after migration.

RWO PVC complicates helper pods

Prefer enrollment through kubectl exec in the existing pod; do not mount the PVC from a second live pod.

Rollback loses credentials

Retain old Secret values and prior image for one soak period; do not delete on first rollout.

Source references

OpenAI Codex App Server — initialization, ChatGPT-managed auth, device-code login, account state, and rate-limit methods.

OpenAI Codex CLI — official CLI distribution and sign-in.

Anthropic Claude Code authentication — /login, SSH/container code paste, credential storage, CLAUDE_CONFIG_DIR, credential precedence, and setup-token limitations.

usage-dashboard repository — current implementation and deployment contract.

---

## Implementation note — Phase A (2026-09-18)

Phase A is implemented. Every protocol claim below was verified against real
binaries (codex-cli 0.155.1, Claude Code 2.1.276) rather than taken from the
documentation summary, and four things in the plan above needed correcting.

### Verified as written

- `account/read`, `account/rateLimits/read`, `account/login/start`
  (`{"type":"chatgptDeviceCode"}` → `{loginId, verificationUrl, userCode}`) and
  the `account/login/completed` notification all exist on the **default**
  surface — `experimentalApi` is not required, as the plan says.
- `codex -s read-only -a never app-server` is accepted in that flag order.
- `rateLimitsByLimitId["codex"]` must be preferred: a live account also carries
  a `base_model_inference` bucket that would otherwise pollute the tile.
- Claude's credential file is
  `claudeAiOauth.{accessToken, refreshToken, expiresAt, refreshTokenExpiresAt,
  scopes, subscriptionType, rateLimitTier}`, and a real `/login` does carry
  `user:profile`.

### Corrections

1. **`windowDurationMins` is MINUTES.** The pre-migration field was
   `limit_window_seconds` and the threshold constant was `100_000` seconds. A
   weekly window is `10_080` minutes, which is *less* than 100 000, so carrying
   that constant across classifies every weekly window as a session window. The
   new constant is `_WEEKLY_MIN_MINUTES = 1_440`. This was not hypothetical: the
   live account reports the weekly window in the `primary` slot with
   `secondary: null`, so it would have been wrong on the first fetch. Covered by
   a named regression test.
2. **Responses arrive out of order.** A live probe issued `account/read` (id 2)
   then `account/rateLimits/read` (id 3) and received id 3 first. The plan's
   insistence on id correlation is load-bearing, not defensive.
3. **`app-server` is `[experimental]`** at the subcommand level even though the
   `account/*` methods are not, so pinning the Codex version is required rather
   than hygiene.
4. **`account/login/cancel`** (takes `loginId`, answers `{"status":"canceled"}`)
   exists and is what the cancellation path uses.

### Deviations from the plan, and why

- **One-shot process, not a long-lived supervised child.** A full spawn →
  initialize → `account/read` → `account/rateLimits/read` cycle measures
  **~0.7 s** against a 300 s poll interval. One child per call removes the
  restart policy, the resident process and most of the supervision surface: the
  next poll *is* the retry, on the scheduler's existing backoff. The session
  layer (`AppServerSession`) is lifetime-agnostic and a `persistent=True`
  policy is implemented **and tested**, so moving to a long-lived child later is
  a constructor flag, not a rewrite. (Owner decision, 2026-09-18.)
- **No `legacy` CODEX_MODE.** `CODEX_MODE` is `app_server|disabled`. Keeping a
  legacy bridge would mean retaining the exact OpenAI OAuth code that acceptance
  criterion #1 says must not exist, so Phase A could not have satisfied its own
  criteria. The bridge bought continuity of the Codex tile for the few minutes
  between rollout and enrolment, which is not worth that on a personal
  dashboard. Cut over with `disabled` → enrol → `app_server`.
- **Claude env injection retained (deprecated) for one soak.** The token store
  is authoritative and the `claude-*` Secret keys now seed only an *empty*
  entry. Removing the injection immediately would give no benefit and would
  strand a deployment whose store was empty. Marked deprecated in both
  manifests; delete in Phase C.
- **Codex joins the re-auth detail map.** The plan asks for an
  "offline/auth-required reading". A bare offline reading put the guidance only
  in the pod log, so `CodexLoginRequired` now parks the tile with the actionable
  detail line (`_REAUTH_DETAIL`, the existing cookie-auth pattern), visible on
  the Pi and `/dashboard`.

### Found while implementing

`select()` on the child's stdout fd cannot see lines already buffered inside
Python's `TextIOWrapper`. When the App Server writes a notification and a
response together — which it does — `readline()` returns the notification and
the following `select()` reports "nothing to read" while the response sits in
memory, so the request times out holding its own answer. stdout is now pumped
by a reader thread into a queue. Caught by the interleaved-notification test.

### Evidence

- Live: `/readings` served `codex / current / weekly 93.0%` through the App
  Server, `POST /refresh` re-fetched, and an empty `CODEX_HOME` produced
  `offline` + `login required — run \`usage-dashboard login codex\``.
- GUI captured headless at 1280×720 with the Codex tile in the login-required
  state: no layout collision, other tiles unchanged.
- Both pinned artefacts' SHA256 checksums verified against real downloads.
- 739 tests pass; `ruff check .` and `mypy src` (strict) clean.

### Not done here

Phase B (production enrolment: device-code paste + rollout) is the operator's.
Phase C (delete the deprecated Secret keys) waits on the soak.
