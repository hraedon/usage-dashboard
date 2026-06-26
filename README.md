# usage-dashboard

A two-component system for monitoring AI usage across [Claude](https://claude.ai), [z.ai](https://z.ai), [Ollama](https://ollama.com), and [umans](https://umans.ai). A server fetches usage data from all configured providers, normalizes it into a unified format, stores it in SQLite, and serves it via an authenticated API. A client polls the server and renders usage as color-coded progress bars. The primary client is a fullscreen touch GUI for a **Raspberry Pi 4B + Touch Display 2** (with optional scheduled backlight-sleep + tap-to-wake, and a tap-the-status-line overlay for unit diagnostics + brightness); a legacy 240×320 ST7789 PNG renderer (Pi Zero) is also kept. umans (whose plan has no percentage quotas) renders as a single text line of requests and tokens, color-coded if the account is throttled. The server also serves a mobile-friendly HTML view at `/dashboard`.

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
| `GET /schedule` | Bearer token | Returns the backlight sleep-schedule spec for the requesting unit (`?unit=<UNIT_ID>`, falling back to the `default` entry), or `null`. See *Backlight sleep schedule* |
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
  "detail": null,
  "models": null,
  "throttle": "none"
}
```

Status values: `current` | `stale` | `offline`

`detail` is an optional pre-formatted text line for providers that don't fit
the percentage model; umans uses it (e.g. `"req 161  tok 63.9M"`).

`models` is an optional per-model breakdown (Ollama's weekly segments, z.ai's
tool calls), sorted by share — the clients show the top two on the tile title
and the top several in the detail view.

`throttle` is a severity signal for quota-less providers (umans): `none`,
`low` (deprioritised — over the concurrency threshold), or `boxed` (penalty
box, account locked for the window). The clients colour the umans line
yellow/red accordingly, and on `boxed` replace its metrics with a countdown to
when the box clears.

## Clients

Both clients poll the server API with adaptive refresh (60s when values change,
5min when stable). They share the colour/threshold and countdown logic
(`client/format.py`), so they never disagree on what "85% is red" means.

- **PNG renderer** (`usage-dashboard`, `client/main.py`) — the original Pi Zero
  client. Renders a 240×320 image and writes it to `/tmp/dashboard.png` for an
  SPI display (or headless use).
- **Touch GUI** (`usage-dashboard-gui`, `client/gui.py`) — the primary client: a
  fullscreen pygame app for a **Raspberry Pi 4B + official Touch Display 2**
  (5", 720×1280). Runs under a **minimal X server** (`xinit` + `xrandr`), *not*
  bare KMS/DRM — SDL's `kmsdrm` backend presents black on this panel; the X path
  also gives real landscape rotation (see [`deploy/pi/README.md`](deploy/pi/README.md)).
  Provider tiles with session/weekly bars and reset countdowns; tap a tile for a
  detail view. Resolution-independent (works portrait or landscape, and in a dev
  window). Optionally blanks the backlight on a schedule with tap-to-wake (see
  *Backlight sleep schedule*). Prep a Pi with one command —
  `./deploy/pi/install.sh` sets up a venv, the systemd service, display rotation,
  and a git-based auto-update timer.

## Deploy

### Kubernetes

```bash
# Apply manifests
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/server-pvc.yaml
kubectl apply -f k8s/server-secret.yaml      # edit with real values first
kubectl apply -f k8s/server-deployment.yaml
kubectl apply -f k8s/server-service.yaml

# Optional: per-unit backlight sleep schedules for the touch clients
# (see *Backlight sleep schedule*)
kubectl apply -f k8s/server-schedules-configmap.yaml

# Optional: expose the dashboard (edit the CHANGE-ME host/TLS/class values
# first; see the comments in the file). This is two ingresses — an external one
# exposing only the bearer-protected /readings + /schedule, and an internal one
# exposing the full app incl. the unauthenticated /dashboard.
kubectl apply -f k8s/server-ingress.yaml
```

The Service is `ClusterIP`, so the dashboard is in-cluster only until you apply
`server-ingress.yaml`. The view is **responsive** — a fluid card grid that flows
from one column on a phone to up to four on a desktop — and the bare hostname
(`/`) redirects to `/dashboard`. It's unauthenticated by design (usage stats
only), so keep the hostname internal.

Images are built and pushed to `ghcr.io/hraedon/usage-dashboard-server` and `ghcr.io/hraedon/usage-dashboard-client` via GitHub Actions on push to `main`.

### Secrets

| Key | Required | Description |
|-----|----------|-------------|
| `api-key` | Yes | Shared Bearer token for server-client auth |
| `claude-token` | No | Claude OAuth access token |
| `claude-refresh-token` | No | Claude OAuth refresh token |
| `claude-client-id` | No | Claude OAuth client ID |
| `claude-work-token` | No | Second Claude account's OAuth access token (see *Two Claude accounts*) |
| `claude-work-refresh-token` | No | Second Claude account's OAuth refresh token |
| `claude-work-client-id` | No | Second Claude account's OAuth client ID |
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

Run it **on your own computer** (the one with a web browser) — not on the Pi or
the cluster. It only prints tokens for you to copy into the k8s Secret; it
doesn't talk to the server. First install the CLI into a throwaway venv:

```bash
git clone https://github.com/hraedon/usage-dashboard.git
cd usage-dashboard
python3 -m venv .venv
.venv/bin/pip install -e .
```

Then run the login (use `.venv/bin/usage-dashboard` if it's not on your PATH):

```bash
# Option A — opens a browser and catches the redirect automatically:
usage-dashboard login claude --port 8282

# Option B — no local port (works over SSH): prints a URL; after you authorize,
# Claude's page shows a CODE#STATE value — paste it back at the prompt:
usage-dashboard login claude
```

A browser opens to Claude's sign-in. Sign in as the account you want to track,
approve, and the command prints three values — `claude-token`,
`claude-refresh-token`, and `claude-client-id`. Put them in the Secret and roll
the server:

```bash
kubectl apply -f k8s/server-secret.yaml
kubectl -n usage-dashboard rollout restart deploy/usage-dashboard-server
```

After the first login, the server persists refreshed tokens to the PVC
(`/data/tokens.json`), so pod restarts survive token rotation without
re-login.  The k8s Secret values are used only for the initial seed.

### Two Claude accounts

To watch a second Claude account (e.g. a work login) alongside your personal
one, run the **same `login claude` command again** but sign in as the *other*
account. (If your browser is already logged into the first account, use a
private/incognito window or log out first, so you authorize the right one.)

```bash
usage-dashboard login claude --port 8282   # sign in as the SECOND account
kubectl apply -f k8s/server-secret.yaml
kubectl -n usage-dashboard rollout restart deploy/usage-dashboard-server
```

> The command always labels its output `claude-token` / `claude-refresh-token` /
> `claude-client-id` (it can't tell which account you signed in as). For the
> second account, copy those three values into the **`claude-work-`** keys
> instead — `claude-work-token`, `claude-work-refresh-token`,
> `claude-work-client-id` — leaving your first account's `claude-*` keys alone.

The work account is fetched and refreshed independently (its own
`/data/tokens.json` namespace, its own rotation). The dashboard then shows it as
a **second, muted set of bars in the Claude tile** — `me` and `work` — rather
than a separate tile. With no `claude-work-*` keys set it stays completely
hidden, and the Claude tile looks exactly as before. (The legacy ST7789 PNG
client doesn't render the work account — it's a touch-GUI / web-dashboard
feature.)

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

## Backlight sleep schedule

The touch GUI can blank the panel backlight on a time-of-day schedule and wake
on a tap — handy for an always-on desk display that's just glowing overnight.
It's **opt-in per unit** (`BACKLIGHT_SLEEP=1`) and off by default.

- **Mechanism:** the GUI dims `brightness` to 0 (fully dark on the Touch Display
  2) when asleep and restores it on wake — `brightness` is writable by the GUI
  user's `video` group, so no root/udev/privileged helper is needed. Touch is
  independent of the backlight, so a tap is caught even while dark.
- **Tap-to-wake:** a tap during sleep wakes the panel until the *earlier of* the
  current sleep window's end or the next local midnight, then it re-sleeps. (The
  waking tap isn't also routed to a tile.)
- **Double-tap-to-sleep:** two quick taps (within ~350ms, in roughly the same
  spot) blank the panel immediately and return it to the home grid; the next tap
  wakes it. This works even with the schedule disabled — it's a manual override —
  but only when the backlight is actually controllable (no-op in dev/windowed
  mode). The same-spot position tolerance keeps a fast open-tile-then-tap-back
  from being read as a sleep gesture; single-tap navigation stays instant.
- **Schedule source (highest wins):** the server (`/schedule`, per `UNIT_ID`) →
  the `BACKLIGHT_SCHEDULE` env override → a built-in default (nightly
  `00:00-08:00` + weekend `Fri 18:00 → Mon 08:00`). A remote ConfigMap edit
  takes effect on the client's next poll — no restart.

**Schedule grammar** (rules joined by `;`):

```
daily HH:MM-HH:MM            # applied to all seven days (may cross midnight)
<day> HH:MM-<day> HH:MM      # a single span (may cross days / the week)
# e.g. daily 00:00-08:00; fri 18:00-mon 08:00
```

**Per-unit schedules** live in the `usage-dashboard-schedules` ConfigMap, keyed
by `UNIT_ID` (or `default`). To change one:

```bash
kubectl -n usage-dashboard edit configmap usage-dashboard-schedules
kubectl -n usage-dashboard rollout restart deploy/usage-dashboard-server
```

**Enable on a unit:** set `BACKLIGHT_SLEEP=1` and `UNIT_ID=<name>` in
`/etc/usage-dashboard-gui.env`, then restart the GUI (or let the auto-updater
do it). A malformed/unset schedule degrades gracefully to the built-in default.

## Status overlay (diagnostics + brightness)

Tap the status line (the "Updated … · refresh … · N providers" row at the bottom
of the grid) to open a card with **unit diagnostics on the left** and
**brightness `−`/`+` on the right**; tap anywhere outside the card to close it.

**Left — diagnostics** answer "how do I reach this unit and is it healthy?"
without an SSH session:

- **Host** and **IP**(s) — the hostname and reachable addresses.
- **Server** — which server host this client points at.
- **Commit** — the running short SHA, tagged `(current)` or `(rolled back)`.
- **Update** — the auto-updater's last result (`ok` / `pip failed` /
  `import failed`) and how long ago; a failure is shown in red. **No record** (red)
  means the updater hasn't written a status yet.
- **Changed** — when the code last actually moved.

  Update health comes from two tiny files the updater writes under the state dir
  (`update-last-check`, `update-last-change`); no daemon, no config. Hostname/IPs
  are read from the OS; the running commit from the checkout (or the updater's
  record).

**Right — brightness** drives the panel `brightness` node directly (the same
writable-by-`video` node used for sleep, so no root/udev helper), and never dims
to 0 — blanking is what sleep is for:

- **Granularity:** `BRIGHTNESS_STEPS` (default `10`) sets how many `−`/`+` notches
  span dimmest→full. Try `9`/`11`/etc. by changing it and restarting the GUI — no
  code change.
- **Survives sleep/wake:** a chosen level is also used as the wake-restore level,
  so the panel comes back at *your* brightness after a scheduled or double-tap
  sleep, not the startup default.
- **Survives reboot:** the chosen *level* is persisted (best-effort) to
  `$XDG_STATE_HOME/usage-dashboard/brightness` (default
  `~/.local/state/usage-dashboard/brightness`) and re-applied at startup. Set
  `BRIGHTNESS_STATE_FILE` to relocate it, or to empty to disable persistence. An
  unwritable path degrades to "remembered within the session only".
- **No-op without a controllable backlight** (dev/windowed mode): the `−`/`+`
  show `—` and do nothing — but the overlay still opens for the diagnostics.

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
