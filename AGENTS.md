# AGENTS.md

Conventions and quick reference for agents (and humans) working on usage-dashboard.

## What this is

A two-component system for monitoring AI usage across Claude, z.ai, Ollama, Codex, OpenCode Go, and Google Antigravity (Gemini / Gemini 4 Argon) providers:

- **Server** (k8s Deployment): Fetches usage data from all providers, normalizes into unified readings, stores in SQLite, serves via authenticated FastAPI endpoint. Also serves `/dashboard`, an unauthenticated mobile-friendly HTML view (private-network use; shows nothing beyond what the display shows — `/readings` keeps bearer auth)
- **Clients**: The touch GUI polls the server API and uses colour/threshold + countdown logic from `client/format.py`. **Touch GUI** (`usage-dashboard-gui`, `client/gui.py`) — the primary target: a fullscreen pygame app for a **Pi 4B + Touch Display 2** (720×1280), run under a minimal X server (`xinit`+`xrandr`), *not* bare KMS/DRM (which presents black on this panel). Optional scheduled backlight-sleep + tap-to-wake (`BACKLIGHT_SLEEP`/`UNIT_ID`, server `/schedule`) and a tap-the-status-line overlay: unit diagnostics on the left (`client/diagnostics.py` — hostname/IPs, server, running commit, updater health read from the two status files `deploy/pi/update.sh` writes under the state dir) + brightness `+`/`-` on the right (`BRIGHTNESS_STEPS`, persisted to `BRIGHTNESS_STATE_FILE`; `client/brightness.py`). See `deploy/pi/` for the install + auto-update tooling. Off-peak windows (`shared/offpeak.py`) drive display hints: the z.ai tile title tints green off-peak / orange during peak hours, with a countdown to the next boundary

Key modules:
- `src/usage_dashboard/server/capacity.py` — Read-only account-capacity projection
  for `/api/v1/agent/capacity`; see `docs/agent-api.md`. It evaluates cached
  observations only. The optional `AGENT_API_KEY` cannot refresh providers or
  access operator endpoints. `usage-dashboard capacity` is the thin JSON client.
- `src/usage_dashboard/shared/models.py` — Normalized reading schema (Provider enum, Reading dataclass)
- `src/usage_dashboard/server/` — Fetchers (Claude, z.ai, Ollama, Codex, OpenCode Go, Antigravity), SQLite DB, API, scheduler
- `src/usage_dashboard/server/codex_app_server.py` — client for the official `codex app-server` (JSONL over stdio). **Protocol facts in its docstring were verified against a real binary, not docs** — keep it that way. **One long-lived child** (`persistent=True`) — not one per fetch. Latency would allow per-fetch (~0.7s vs a 300s poll), but each *start* leaks ~29 kB of uncheckpointed SQLite WAL plus a `.tmp/git-*` dir into `CODEX_HOME`: ~8.3 MB/day, which fills the 1 GiB PVC — and the readings DB with it — in ~4 months. Growth is per-start, not per-request. Resident cost is ~92 MB RSS in production (plugins disabled). Plugins are disabled in the default args because an authenticated App Server otherwise downloads ~50 MB of plugin catalog and Office templates this client can never reach (55.9 MB → 3.1 MB). Never bound a read with `select()` here: the child writes notifications and responses together, and `select()` cannot see lines already in Python's buffer
- `src/usage_dashboard/server/fetch_antigravity.py` — Google Antigravity via the official `agy` CLI (Plan 007): a **per-fetch** subprocess running the documented zero-turn `agy -p /usage --output-format json`. Facts in its docstring were **verified against agy 1.4.12, not docs**. Load-bearing flags: `--log-file /dev/null` (otherwise a new ~34 kB log per run, never pruned per run: ~10 MB/day; a fixed path *appends*), `AGY_CLI_DISABLE_AUTO_UPDATE=true` (`1` is silently ignored), `--disable-slash-commands=false` plus a `num_turns == 0` check (a saved setting can turn `/usage` into a model prompt). The child gets a minimal env allowlist, never the pod's secrets. Signed out, the CLI blocks 60 s on a paste prompt, so the fetch kills it on the prompt and reports an auth failure. Untouched buckets report a sliding "now + window" reset, which is dropped so the idle ladder can widen
- `src/usage_dashboard/claude_credentials.py` — quarantined parser for Claude Code's `.credentials.json`. Fails closed and names the installed CLI version; it is the only place that knows that (undocumented) schema
- `src/usage_dashboard/server/claude_profile.py` — Claude on the CLI ownership model (Plan 009). Each account has a persistent `CLAUDE_CONFIG_DIR` (`/data/claude-personal`, `/data/claude-work`); the fetcher reads the access token from the CLI's file per poll and **never refreshes**. At/near expiry or on a 401 it runs the zero-turn `claude -p /usage --output-format json --no-session-persistence` once (the CLI refreshes, rotates, rewrites atomically) and re-reads. Facts in its docstring were **verified against Claude Code 2.1.276 in the pod, not docs**: `claude auth status` does NOT refresh; `--bare` disables OAuth; `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` breaks `/usage`; a signed-out CLI also "succeeds", so the trigger's verdict is the re-read file, never its output. Throwaway `HOME` per run (~6 kB cache otherwise accumulates); `num_turns == 0` enforced
- `src/usage_dashboard/server/cli_home.py` — shared one-owner lock + staging swap for CLI state dirs (Antigravity `HOME`, Claude `CLAUDE_CONFIG_DIR`)
- `src/usage_dashboard/client/` — HTTP fetcher with adaptive refresh, pygame touch GUI
- `src/usage_dashboard/deploy/` — `redeploy.py`: opt-in self-redeploy of the Pi's installer-managed components (units/scripts) from the pulled checkout (`AUTO_REDEPLOY=1`); driven by `deploy/pi/update.sh` via the root `usage-dashboard-redeploy` helper. Content-addressed + atomic-write + unit-verify + GUI rollback
- **Pi cellular fallback (Plan 010)** — three modules, one arrangement; read `deploy/pi/README.md` §8 before touching any of them:
  - `src/usage_dashboard/client/cellular_worker.py` — the unprivileged readings worker (own system user). Polls the dashboard over the normal route with bounded timeouts; falls back to the metered link only after two unanswered probes, never on an HTTP error (401/429 are "reachable", surfaced, not retried over cellular). Connects to **pinned IP literals** with the hostname carried as SNI + `Host` (httpx `sni_hostname`; proven over a real handshake in `tests/test_cellular_tls.py`), `trust_env=False`, redirects not followed. One pool per route. Fails closed: every provisioning fact is a required setting and there is no default data budget — `CELLULAR_BUDGET_MB` must be set, counted from the modem interface's own sysfs counters, and the count survives a restart and a clock that is behind at boot (the month only ever rolls forward). Cellular reads only while the GUI's "awake" heartbeat is fresh. **The cellular route is selected only around a cellular read** (`route` → GET → `unroute`): held, the worker's own recovery probe would be answered over cellular and report a recovery that did not happen. **A failed `route` is always followed by `down`** — the worker cannot tell a refusal from a helper killed half-way through installing a route, and a rule left behind would carry that same recovery probe, uncounted. A refresh tap may bring a read forward, never past the minimum gap or a wait that follows a failed or refused read. At start it asks for `firewall` (the deny policy) before `down`. Tests pin all of this
  - `src/usage_dashboard/deploy/cellular_link.py` — the privileged half: a root-owned wrapper (`/usr/local/bin/usage-dashboard-cellular-link`; sudoers grants exactly `firewall|route|unroute|down`, no args) runs this module. It keeps **its own deadline below the worker's**, because killing sudo does not kill what sudo started. `route` loads and **verifies the nftables deny policy before bringing the link up**, refuses any NetworkManager profile that is not already isolated (no default route / carrier routes / DNS / static routes, no autoconnect, metered, bound to the modem interface), refuses while **another profile would auto-connect on that interface** (NetworkManager invents one for any Ethernet device it has none for — found live on the first unit), refuses if the modem's own subnet would shadow a system resolver or another default gateway, installs a route in its own table and a `uidrange` rule for the worker only, then proves with `ip route get … uid` that the worker is selected and that root and an ordinary user are not. **A refusal at any point unwinds to no rule, no route, no link.** The policy is re-checked against a stateless kernel listing every time (a table someone trimmed is reloaded), and `usage-dashboard-cellular-firewall.service` loads it at boot, before `network-pre.target`. Every command goes through an injected runner. **The strings it verifies in `nft list`, and the shapes it parses out of `nmcli`, were read off real tools, not assumed**: nftables lists a set by value (`192.0.2.9` before `192.0.2.10`) and collapses a one-element set to a bare address; NetworkManager 1.52 prints `connection.type` as `802-3-ethernet` and `connection.multi-connect` as `0`, rejects an indexed name such as `IP4.ADDRESS[1]` in `-g` mode, and joins a multi-valued setting on one line with ` | `. Each of those would have made the helper refuse every link. **`tests/test_cellular_link_kernel.py` rehearses the whole thing on a real kernel** (root, opt-in: `UD_CELLULAR_KERNEL_TEST=1`) with real packets — run it after touching the policy or the routing
  - `src/usage_dashboard/client/readings_cache.py` + `cache_view.py` — the only channel between worker and GUI: an atomically renamed `readings.json` the worker alone writes (last successful payload from either route, never cleared) and two files the GUI alone writes (`request/awake`, `request/refresh`). The GUI draws the newer of its own readings and the cache, so fallback and "preserved payload with its age after a restart" are one rule
- `k8s/` — Kubernetes manifests for deployment
- `docs/spec.md` — Full specification with acceptance criteria (AC-01 through AC-16)

## Build / test / lint

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src
```

## Provider authentication (Plan 005)

The dashboard implements **no OAuth flow of its own**. Enrolment happens inside
the server pod, via each vendor's official CLI (both pinned into the image):

- `usage-dashboard login codex` — device-code login against the official App
  Server. Codex owns the tokens under `CODEX_HOME` (`/data/codex`). Nothing here
  mints, stores or refreshes an OpenAI token. Never run two App Server processes
  against one `CODEX_HOME`.
- `usage-dashboard login claude --account personal|work` — Plan 009: runs the
  official Claude Code login in a **staging** sibling of the account's
  persistent `CLAUDE_CONFIG_DIR`, verifies scope + a live usage read, then swaps
  it into place (`cli_home.install_home`). The profile **stays** — it is the
  credential, and the pod's CLI is the family's one refresher. (Plan 005 did the
  opposite: imported into the token store and deleted the profile, making the
  dashboard the refresher; that broke on 2026-11-07 when the token endpoint
  began requiring `client_id`.) Never copy a desktop Claude credential in.

`/data/tokens.json` holds only scraped cookies and the OpenCode workspace id. A
leftover pre-009 Claude entry is never used. Without a profile it keeps the
account configured ("login required"). With one, startup deletes it only after
the CLI has rotated the refresh token, never before, since until then it is the
last good copy (`scripts/migrate_claude_store_to_profile.py` was the one-off
upgrade, run after the rollout).
The token store takes an advisory file lock because enrolment mutates it from a
second process, and it fails closed on corrupt content rather than replacing it.

## Browser enrolment pane (Plan 006)

`/login` (internal ingress only) drives the same ceremonies over HTTP:
`/internal/v1/login/*` is INTERNAL_ONLY and bearer-gated — it mints
credentials, so it is deliberately not under the externally-routed `/api`.
Claude runs the CLI on a PTY and streams the transcript (token-shaped output
redacted); Codex surfaces the device-code URL/code and pauses the runtime App
Server for the ceremony (`scheduler.pause_codex`/`resume_codex` — one
CODEX_HOME owner, always); ollama/opencode verify-then-store a pasted session
cookie (plus the OpenCode `wrk_…` workspace id, now store-side metadata).
After any successful enrolment the scheduler adopts the new credential
in-memory (`update_credentials`) and fetches — no rollout restart needed.
`kubectl exec` remains the fallback path.

Antigravity (Plan 007) is the Claude interaction on the Codex ownership model:
`login antigravity` runs the official `agy` sign-in (Google URL → paste the
code within its **fixed 60 s** window) inside a **staging** `HOME` beside
`/data/antigravity`. The same run answers `/usage`, so it doubles as the
verification, and only then is the staging dir renamed into place (file lock
shared with the fetcher). A failed, timed-out or cancelled ceremony is deleted
and never displaces a working login. The pane lifts the Google link into
`verification_url` (an anchored Google-OAuth match, so ceremony output can't
steer the button elsewhere) and shows a countdown. Enabled by `ANTIGRAVITY_MODE=cli`; never copy a desktop token in.

Claude usage remains an **unsupported** dependency (undocumented
`GET /api/oauth/usage` plus reading that credential file); Codex usage is
supported. Claude token *refresh* is no longer ours: the CLI owns it (Plan 009).

## Hard rules

- **Spec acceptance criteria are the boundary.** Don't add features beyond the spec without a tracked breadcrumb or plan entry.
- **Don't reintroduce an OpenAI OAuth surface.** A test asserts no runtime string in `src/` contains the Codex client id, `auth.openai.com`, `codex_cli_rs` or `backend-api/wham/usage`.
- **Don't reintroduce a Claude token refresh.** The official CLI is the only refresher (Plan 009). A test asserts no runtime string in `src/` contains the Claude Code client id, `oauth/token`, `grant_type` or `refresh_token` (token store excepted). Nothing in `src/` writes `.credentials.json` either.
- **Don't widen the display credential.** `DISPLAY_API_KEY` (Plan 010) is accepted by the readings route alone; it is the token a Pi carries across a metered, firewalled link. `tests/test_display_api.py` walks every authenticated route in the OpenAPI schema and requires 401 on all but `/readings`.
- **Keep the cellular pieces out of `deploy.redeploy.COMPONENTS`.** The worker's unit, the boot-time firewall unit, the wrapper and the sudoers rule change root-owned state; they are installed by hand (`deploy/pi/cellular-install.sh`) and never auto-redeployed. A test pins it. (The helper's *logic* is ordinary Python in the package and updates with the rest — so a change there is a change to root-executed code; the kernel rehearsal is the check that earns it.)

## Work items / breadcrumbs

Tracking lives in the **agent-notes** DB (the canonical store), not the
`breadcrumbs/` dir (which is legacy scaffold, empty, and safe to delete). Use the
CLI, resolving the project by path:

```bash
agent-notes orient --path .                 # open work items, recent changes, memories
agent-notes work-item find --path .         # list/search work items
agent-notes work-item file  --path . --title "…" --type bug --severity medium
agent-notes work-item get   WI-XXX --path . --with-body
agent-notes work-item close WI-XXX --path .
```

Don't add features beyond the spec without a tracked work item or `plans/` entry.
