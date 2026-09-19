# AGENTS.md

Conventions and quick reference for agents (and humans) working on usage-dashboard.

## What this is

A two-component system for monitoring AI usage across Claude, z.ai, Ollama, Codex, and OpenCode Go providers:

- **Server** (k8s Deployment): Fetches usage data from all providers, normalizes into unified readings, stores in SQLite, serves via authenticated FastAPI endpoint. Also serves `/dashboard`, an unauthenticated mobile-friendly HTML view (private-network use; shows nothing beyond what the display shows — `/readings` keeps bearer auth)
- **Clients**: The touch GUI polls the server API and uses colour/threshold + countdown logic from `client/format.py`. **Touch GUI** (`usage-dashboard-gui`, `client/gui.py`) — the primary target: a fullscreen pygame app for a **Pi 4B + Touch Display 2** (720×1280), run under a minimal X server (`xinit`+`xrandr`), *not* bare KMS/DRM (which presents black on this panel). Optional scheduled backlight-sleep + tap-to-wake (`BACKLIGHT_SLEEP`/`UNIT_ID`, server `/schedule`) and a tap-the-status-line overlay: unit diagnostics on the left (`client/diagnostics.py` — hostname/IPs, server, running commit, updater health read from the two status files `deploy/pi/update.sh` writes under the state dir) + brightness `+`/`-` on the right (`BRIGHTNESS_STEPS`, persisted to `BRIGHTNESS_STATE_FILE`; `client/brightness.py`). See `deploy/pi/` for the install + auto-update tooling. Off-peak windows (`shared/offpeak.py`) drive display hints: the z.ai tile title tints green off-peak / orange during peak hours, with a countdown to the next boundary

Key modules:
- `src/usage_dashboard/shared/models.py` — Normalized reading schema (Provider enum, Reading dataclass)
- `src/usage_dashboard/server/` — Fetchers (Claude, z.ai, Ollama, Codex, OpenCode Go), SQLite DB, API, scheduler
- `src/usage_dashboard/server/codex_app_server.py` — client for the official `codex app-server` (JSONL over stdio). **Protocol facts in its docstring were verified against a real binary, not docs** — keep it that way. One short-lived child per fetch by default (~0.7s vs a 300s poll); `AppServerSession` is lifetime-agnostic and a tested `persistent=True` policy keeps a long-lived child one flag away. Never bound a read with `select()` here: the child writes notifications and responses together, and `select()` cannot see lines already in Python's buffer
- `src/usage_dashboard/claude_credentials.py` — quarantined parser for Claude Code's `.credentials.json`. Fails closed and names the installed CLI version; it is the only place that knows that (undocumented) schema
- `src/usage_dashboard/client/` — HTTP fetcher with adaptive refresh, pygame touch GUI
- `src/usage_dashboard/deploy/` — `redeploy.py`: opt-in self-redeploy of the Pi's installer-managed components (units/scripts) from the pulled checkout (`AUTO_REDEPLOY=1`); driven by `deploy/pi/update.sh` via the root `usage-dashboard-redeploy` helper. Content-addressed + atomic-write + unit-verify + GUI rollback
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
- `usage-dashboard login claude --account personal|work` — runs the official
  Claude Code login in a throwaway `CLAUDE_CONFIG_DIR`, imports the credential
  into the token store, then **deletes** that directory. The deletion is
  load-bearing: a refresh-token family must have exactly one refresher.

`/data/tokens.json` is authoritative for Claude; the `claude-*` Secret keys seed
only an *empty* entry (deprecated, deleted in Phase C). The token store takes an
advisory file lock because enrolment mutates it from a second process, and it
fails closed on corrupt content rather than replacing it.

Claude usage remains an **unsupported** dependency (undocumented
`GET /api/oauth/usage` plus that credential file); Codex usage is supported.

## Hard rules

- **Spec acceptance criteria are the boundary.** Don't add features beyond the spec without a tracked breadcrumb or plan entry.
- **Don't reintroduce an OpenAI OAuth surface.** A test asserts no runtime string in `src/` contains the Codex client id, `auth.openai.com`, `codex_cli_rs` or `backend-api/wham/usage`.

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
