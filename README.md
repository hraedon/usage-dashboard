# usage-dashboard

A two-component system for monitoring AI usage across [Claude](https://claude.ai), [z.ai](https://z.ai), [Ollama](https://ollama.com), and [umans](https://umans.ai). A server fetches usage data from all configured providers, normalizes it into a unified format, stores it in SQLite, and serves it via an authenticated API. A client polls the server and renders usage as color-coded progress bars on a 240x320 ST7789 LCD display (designed for a Pi Zero); umans (whose plan has no percentage quotas) renders as a single text line of requests and tokens in the current window. The server also serves a mobile-friendly HTML view at `/dashboard`.

## Architecture

```
┌─────────────────┐         ┌─────────────────┐
│   AI Providers  │         │    Pi Zero       │
│  Claude / z.ai  │         │  ST7789 LCD      │
│  Ollama / umans │         │  240×320 px      │
└────────┬────────┘         └────────▲─────────┘
         │                           │
         ▼                           │
┌─────────────────┐    HTTP API      │
│  Server (k8s)   │◄────────────────┘
│  FastAPI+SQLite │  Bearer auth
└────────┬────────┘
         │ /dashboard (HTML, no auth,
         ▼  private networks)
      📱 phone
```

## Server

Fetches usage from all configured providers on an **adaptive per-provider
schedule** and exposes a `/readings` endpoint. Each provider is polled
independently: a 5-minute floor that widens through 5 → 10 → 15 → 30 minutes
while a reading is unchanged (cutting baseline usage when idle) and snaps back
to 5 minutes the moment it moves. Failures back off exponentially (capped at
1 hour, `FAILURE_BACKOFF_CAP`); a `429` honours the server's `Retry-After`.
Runs as a Kubernetes Deployment with a Longhorn-backed PVC for persistent
SQLite storage.

### API

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /readings` | Bearer token | Returns latest reading per provider as JSON |
| `GET /dashboard` | None | Mobile-friendly HTML view of the same readings (intended for private networks; exposes usage stats only, never credentials) |
| `GET /health` | None | Health check |

### Reading format

```json
{
  "provider": "claude",
  "status": "current",
  "session_percent": 62,
  "session_resets_at": "2026-06-11T19:00:00Z",
  "weekly_percent": 44,
  "weekly_resets_at": "2026-06-18T12:00:00Z",
  "fetched_at": "2026-06-11T14:32:00Z",
  "stale": false,
  "detail": null
}
```

Status values: `current` | `stale` | `offline`

`detail` is an optional pre-formatted text line for providers that don't fit
the percentage model; umans uses it (e.g. `"req 161  tok 63.9M"`).

## Clients

Both clients poll the server API with adaptive refresh (60s when values change,
5min when stable). They share the colour/threshold and countdown logic
(`client/format.py`), so they never disagree on what "85% is red" means.

- **PNG renderer** (`usage-dashboard`, `client/main.py`) — the original Pi Zero
  client. Renders a 240×320 image and writes it to `/tmp/dashboard.png` for an
  SPI display (or headless use).
- **Touch GUI** (`usage-dashboard-gui`, `client/gui.py`) — fullscreen pygame app
  for a **Raspberry Pi 4B + official Touch Display 2** (5", 720×1280). Renders
  straight on the panel via KMS/DRM (no desktop); provider tiles with
  session/weekly bars and reset countdowns; tap a tile for a detail view.
  Resolution-independent (works portrait or landscape, and in a dev window).
  Prep a Pi with one command — `./deploy/pi/install.sh` sets up a venv, the
  systemd service, display rotation, and a git-based auto-update timer; see
  [`deploy/pi/README.md`](deploy/pi/README.md).

## Deploy

### Kubernetes

```bash
# Apply manifests
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/server-pvc.yaml
kubectl apply -f k8s/server-secret.yaml
kubectl apply -f k8s/server-deployment.yaml
kubectl apply -f k8s/server-service.yaml

# Populate secrets (edit server-secret.yaml with real values first)
kubectl apply -f k8s/server-secret.yaml
```

Images are built and pushed to `ghcr.io/hraedon/usage-dashboard-server` and `ghcr.io/hraedon/usage-dashboard-client` via GitHub Actions on push to `main`.

### Secrets

| Key | Required | Description |
|-----|----------|-------------|
| `api-key` | Yes | Shared Bearer token for server-client auth |
| `claude-token` | No | Claude OAuth access token |
| `claude-refresh-token` | No | Claude OAuth refresh token |
| `claude-client-id` | No | Claude OAuth client ID |
| `zai-api-key` | No | z.ai API key |
| `ollama-cookie` | No | ollama.com browser session cookie (`name=value`; see below) |
| `umans-api-key` | No | umans API key |
| `ollama-email` | No | Unused — see *Ollama login* (kept only as a placeholder) |
| `ollama-password` | No | Unused — see *Ollama login* (kept only as a placeholder) |

ollama.com has no usage API and no plain-HTTP login — it authenticates via
WorkOS AuthKit (a JS-driven form plus an anti-bot device-fingerprint signal),
so there is nothing to POST credentials to. The fetcher instead scrapes
`ollama.com/settings` with a browser session cookie — the same approach as
[CodexBar](https://github.com/steipete/CodexBar) and
[ollama-usage](https://git.sr.ht/~hrbrmstr/ollama-usage). The easy way to
obtain that cookie is `usage-dashboard login ollama` (see below); the manual
fallback is browser devtools (Application → Cookies → ollama.com), copying the
session cookie and storing it as `name=value`. When the cookie expires the tile
goes stale/offline and the log says so; mint a fresh one.

The Claude usage endpoint requires the `user:profile` OAuth scope. A
`claude setup-token` is scoped for inference only and returns `403` here, and
credentials copied from an interactive Claude session can't be used because the
dashboard would rotate the refresh token out from under that session. So the
dashboard mints its **own** dedicated token pair — see *Claude login* below.

### Claude login

The `login claude` command runs a one-time PKCE OAuth flow to mint a
dedicated token pair that belongs to the dashboard alone.  This avoids
sharing credentials with an interactive Claude session (which would break
that session when the dashboard rotates the refresh token).

```bash
# Option A: auto-opens a browser and catches the redirect on a local port
usage-dashboard login claude --port 8282

# Option B: prints a URL; after authorizing, Claude's page shows a
# CODE#STATE value — paste it back at the prompt
usage-dashboard login claude
```

The command prints the access token, refresh token, and client ID. Put them in
the Secret (`claude-token`, `claude-refresh-token`, `claude-client-id`) and
roll the server:

```bash
kubectl apply -f k8s/server-secret.yaml
kubectl -n usage-dashboard rollout restart deploy/usage-dashboard-server
```

After the first login, the server persists refreshed tokens to the PVC
(`/data/tokens.json`), so pod restarts survive token rotation without
re-login.  The k8s Secret values are used only for the initial seed.

### Ollama login

`login ollama` opens a real browser, lets you sign in to ollama.com by hand
(handling any WorkOS prompt), then extracts the session cookie and prints it
ready for the Secret. It needs the optional browser dependency:

```bash
pip install 'usage-dashboard[login]'
playwright install chromium
usage-dashboard login ollama
```

A browser window opens on `ollama.com`. Sign in, then press Enter at the
prompt; the command captures the `ollama.com` cookies, verifies they parse the
usage page, and prints an `ollama-cookie: "..."` line to load into the Secret.

Because of WorkOS's anti-bot signal, this is a **local, human-in-the-loop**
flow (run it on a machine with a display) rather than an unattended server-side
refresh — the cookie still expires, so re-run it when the Ollama tile goes
offline. `--headless` exists but rarely clears the anti-bot check.

Only providers with configured credentials are fetched.

## Development

```bash
uv venv && uv pip install -e ".[dev]"

# Run tests
.venv/bin/pytest -q

# Lint
.venv/bin/ruff check .

# Type check
.venv/bin/mypy src
```
