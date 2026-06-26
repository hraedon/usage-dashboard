# AGENTS.md

Conventions and quick reference for agents (and humans) working on usage-dashboard.

## What this is

A two-component system for monitoring AI usage across Claude, z.ai, Ollama, and umans providers:

- **Server** (k8s Deployment): Fetches usage data from all providers, normalizes into unified readings, stores in SQLite, serves via authenticated FastAPI endpoint. Also serves `/dashboard`, an unauthenticated mobile-friendly HTML view (private-network use; shows nothing beyond what the display shows — `/readings` keeps bearer auth)
- **Clients**: Both poll the server API and share colour/threshold + countdown logic (`client/format.py`). (1) **Touch GUI** (`usage-dashboard-gui`, `client/gui.py`) — the primary target: a fullscreen pygame app for a **Pi 4B + Touch Display 2** (720×1280), run under a minimal X server (`xinit`+`xrandr`), *not* bare KMS/DRM (which presents black on this panel). Optional scheduled backlight-sleep + tap-to-wake (`BACKLIGHT_SLEEP`/`UNIT_ID`, server `/schedule`) and tap-the-status-line `+`/`-` brightness control (`BRIGHTNESS_STEPS`, persisted to `BRIGHTNESS_STATE_FILE`; `client/brightness.py`). See `deploy/pi/` for the install + auto-update tooling. (2) **PNG renderer** (`usage-dashboard`, `client/main.py`) — the original Pi Zero / 240×320 ST7789 client. umans (no percentage quota) renders as a single text line via the generic `Reading.detail` field, color-coded on throttle

Key modules:
- `src/usage_dashboard/shared/models.py` — Normalized reading schema (Provider enum, Reading dataclass)
- `src/usage_dashboard/server/` — Fetchers (Claude, z.ai, Ollama, umans), SQLite DB, API, scheduler
- `src/usage_dashboard/client/` — HTTP fetcher with adaptive refresh, Pillow-based display renderer
- `k8s/` — Kubernetes manifests for deployment
- `docs/spec.md` — Full specification with acceptance criteria (AC-01 through AC-16)

## Build / test / lint

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src
```

## Hard rules

- **Spec acceptance criteria are the boundary.** Don't add features beyond the spec without a tracked breadcrumb or plan entry.

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
