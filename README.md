# usage-dashboard

A two-component system for monitoring AI usage across [Claude](https://claude.ai), [z.ai](https://z.ai), [Ollama](https://ollama.com), [Codex](https://chatgpt.com/codex), and [OpenCode Go](https://opencode.ai). A server fetches usage data from all configured providers, normalizes it into a unified format, stores in SQLite, and serves it via an authenticated API. A client polls the server and renders usage as color-coded progress bars — a fullscreen touch GUI for a **Raspberry Pi 4B + Touch Display 2** (with optional scheduled backlight-sleep + tap-to-wake, and a tap-the-status-line overlay for unit diagnostics + brightness). Plans that discount off-peak use get a glanceable z.ai hint: its tile title tints green off-peak / orange during peak hours, with a countdown to the next boundary. The server also serves a mobile-friendly HTML view at `/dashboard`.

## Architecture

```
┌─────────────────┐         ┌─────────────────┐
│   AI Providers  │         │   Pi 4B         │
│  Claude / z.ai  │         │ Touch Display 2 │
│ Ollama / Codex  │         │  720×1280 px    │
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
| `GET /history` | Bearer token | Returns stored readings for one provider over a trailing window (`?provider=<name>&hours=<1..168>`, default 24), oldest first |
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
  "throttle": "none",
  "alert": "none"
}
```

Status values: `current` | `stale` | `offline`

`detail` is an optional pre-formatted text line shown under the percentage
bars (z.ai's weekly-window token total, e.g. `week req 2,273  tok 284.0M`) or
as the whole tile body for quota-less providers.

`models` is an optional per-model breakdown (Ollama's weekly segments, z.ai's
tool calls), sorted by share — the clients show the top two on the tile title
and the top several in the detail view.

`throttle` is a severity signal reserved for quota-less providers: `none`,
`low` (deprioritised routing), `low_interactivity` (a heavy-day penalty where
requests queue behind interactive sessions), `rate_limited` (a limit hit that
keeps the account serving at low priority for the window), or `boxed` (penalty
box, account locked for the window; any unexpired `boxed_until` *without* the
known-soft `rate_limited` reason). `alert` is an advisory volume cue
(`none`/`warn`/`crit`) for how close a provider's window token total is to the
heavy-usage threshold (z.ai's weekly window, tuned via `ZAI_WEEK_TOKENS_WARN` /
`ZAI_WEEK_TOKENS_CRIT`); it colours the detail line orange/red. No quota-less
provider is currently configured, so `throttle` stays `none`.

Peak-window hints: the z.ai tile title tint (green off-peak / orange peak) is
joined by a countdown in the tile's name bar (`peak in 3h 24m` while off-peak,
`ends in 1h 24m` in peak).

## Clients

The touch GUI polls the server API with adaptive refresh (60s when values
change, 5min when stable). Colour/threshold and countdown logic lives in
`client/format.py`.

- **Touch GUI** (`usage-dashboard-gui`, `client/gui.py`) — the client: a
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
| `claude-token` | No | *Deprecated (Plan 005)* — seeds an empty token-store entry only; enrol with `usage-dashboard login claude` |
| `claude-refresh-token` | No | *Deprecated* — as above |
| `claude-client-id` | No | *Deprecated* — as above |
| `claude-work-token` | No | *Deprecated* — work account seed |
| `claude-work-refresh-token` | No | *Deprecated* — work account seed |
| `claude-work-client-id` | No | *Deprecated* — work account seed |
| `zai-api-key` | No | z.ai API key |
| `ollama-cookie` | No | ollama.com browser session cookie (`name=value`; see below) |
| `ollama-email` | No | Unused — see *Ollama login* (kept only as a placeholder) |
| `ollama-password` | No | Unused — see *Ollama login* (kept only as a placeholder) |
| `opencode-workspace-id` | No | OpenCode Go workspace id (`wrk_…`; see below) |
| `opencode-cookie` | No | opencode.ai `auth` browser cookie (bare value, no `auth=` prefix) |

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

OpenCode Go is the same shape: no usage API, so the fetcher scrapes
`opencode.ai/workspace/<wrk_…>/go` with the `auth` browser cookie. It needs
**both** halves — the workspace id and the cookie — and stays hidden unless both
are set. Get them with `usage-dashboard login opencode` (see below).

Two things about this provider are worth knowing before you debug it:

- **An expired cookie and a wrong workspace id are indistinguishable.** Both
  redirect to `auth.opencode.ai/authorize` and return HTTP 200 on the final
  hop; there is no 401 and no marker in the body. The card's detail line says
  `cookie expired or bad workspace — re-login` because the server genuinely
  cannot tell which it is. Check the workspace id before re-minting a cookie.
- **Capture the cookie from a private/incognito window, and close it rather
  than signing out.** Signing out invalidates the cookie server-side, taking
  the captured value with it. (`login opencode` uses an ephemeral browser
  context, which gets this right by construction.)

The Claude usage endpoint requires the `user:profile` OAuth scope. A
`claude setup-token` is scoped for inference only and returns `403` here, and
credentials copied from an interactive Claude session can't be used because the
dashboard would rotate the refresh token out from under that session. So the
dashboard enrols a **dedicated** credential through the official Claude Code
login and becomes its sole refresher — see *Provider enrolment* below.

> **Support boundary, stated plainly.** Codex usage comes from a supported
> interface: the official App Server's `account/rateLimits/read`. Claude usage
> does not. Anthropic publishes no subscription-quota API, so the dashboard
> reads the undocumented `GET /api/oauth/usage` and parses the credential file
> Claude Code writes. Both of those dependencies are deliberately quarantined
> (`fetch_claude.py`, `claude_credentials.py`) and fail closed, but they can
> break on any Claude Code release.

### Provider enrolment (Plan 005)

Codex and Claude are enrolled **inside the server pod**, through each vendor's
own official CLI. Nothing is minted by this project, no token is printed, and
nothing is pasted into a Kubernetes Secret. The server image ships pinned
`codex` and `claude` binaries for exactly this purpose.

Enrolment has to happen in the pod because the Longhorn PVC holding the token
store is **RWO** — only the running server can mount it. Do not mount it from a
second live pod.

```bash
POD=$(kubectl -n usage-dashboard get pod -l app=usage-dashboard-server \
        -o jsonpath='{.items[0].metadata.name}')
kubectl -n usage-dashboard exec -it "$POD" -- <command>
```

#### Codex

Codex authenticates itself. The official App Server owns the OAuth flow, stores
its credentials under `CODEX_HOME` (`/data/codex`, on the PVC) and refreshes
them on its own; the dashboard only asks it for rate-limit windows.

```bash
kubectl -n usage-dashboard exec -it "$POD" -- usage-dashboard login codex
```

It prints a verification URL and a one-time code. Open the URL, enter the code,
and it confirms enrolment by re-reading the account through a *fresh* App Server
process — which is what proves the credential actually landed on the PVC.

Set `CODEX_MODE=app_server` in the deployment (the default in the manifest) and
roll the server.

> **Re-enrolling:** never run two App Server processes against one `CODEX_HOME`.
> Set `CODEX_MODE=disabled`, roll the pod, run the login, then restore
> `app_server` and roll again.

#### Claude

`login claude` runs the *official* Claude Code login in a throwaway config
directory, imports the credential it writes into the dashboard's token store,
and then **deletes that directory**. The deletion is the point: a refresh token
has exactly one rightful refresher, and leaving a second copy behind would have
both rotate the same family into a lockout.

```bash
# personal account
kubectl -n usage-dashboard exec -it "$POD" -- usage-dashboard login claude --account personal

# work account (independent tokens, independent rotation)
kubectl -n usage-dashboard exec -it "$POD" -- usage-dashboard login claude --account work
```

Over SSH or in a container the login shows a code to paste back into the
terminal. Before the store is touched, the command checks the credential
actually carries `user:profile` **and** can read the usage endpoint — so a
credential that cannot do the dashboard's job never displaces a working one.

> `claude setup-token` cannot be used here. It mints an inference token without
> the `user:profile` scope that the subscription-usage endpoint requires.

Roll the server afterwards so the scheduler picks up the new credential:

```bash
kubectl -n usage-dashboard rollout restart deploy/usage-dashboard-server
```

The work account shows as a **second, muted set of bars in the Claude tile** —
`me` and `work` — not a separate tile. With no work credential enrolled it stays
completely hidden.

#### Where credentials live

`/data/tokens.json` on the PVC is **authoritative** for Claude. The
`claude-*` Secret keys now only seed an *empty* store entry, so a pre-Plan-005
deployment keeps working across the upgrade and can roll back. Once an entry
exists, only enrolment replaces it — a stale Secret can no longer overwrite a
freshly enrolled credential.

Codex keeps its own credentials under `/data/codex`; the dashboard never reads
or writes them.

#### Telling expired auth from a transport failure

```bash
kubectl -n usage-dashboard logs deploy/usage-dashboard-server | grep -i codex
```

- **`Codex login required: …`** — the App Server has no usable ChatGPT login.
  Re-enrol. There is nothing to refresh; Codex owns the tokens.
- **`Codex App Server timed out …` / `… exited (code N)`** — transport or child
  process failure, not an auth problem. The scheduler backs off and retries; the
  next poll spawns a fresh process.
- **Claude going offline after a 401** — the dashboard refreshes and retries
  once by itself. Persistent failure means the refresh token is dead: re-enrol.

#### Rollback

Both halves are reversible for one soak period:

- Roll back to the prior image and restore `CODEX_TOKEN`/`CODEX_REFRESH_TOKEN`/
  `CODEX_CLIENT_ID`/`CODEX_ACCOUNT_ID` injection in the deployment. **Keep the
  old Secret values** until the new image has survived a token refresh and a
  pod restart.
- Claude needs no rollback step: the `claude-*` Secret keys are still present
  and the old image reads them directly.

Delete the deprecated Secret keys only after the soak.

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

### OpenCode Go login

`login opencode` is the same human-in-the-loop browser flow, capturing two
values instead of one:

```bash
pip install 'usage-dashboard[login]'
playwright install chromium
usage-dashboard login opencode
```

A browser opens on `opencode.ai`. Sign in, then **navigate to your workspace's
Go page** (`opencode.ai/workspace/<wrk_…>/go`) so the workspace id can be read
out of the URL, and press Enter. The command captures the `auth` cookie, reads
the `wrk_…` id, verifies the pair against the live page, and prints both lines
ready for the Secret. Do not sign out afterwards — that kills the cookie you
just captured.

Only providers with configured credentials are fetched.

### Where each provider is shown

OpenCode Go currently renders on the web `/dashboard` only — it is deliberately
**not** on the Pi panel. A fifth full-width tile would take the grid to four
rows, and because the per-tile overhead (title + padding) is charged once per
row, that cuts the height left for bars by roughly two thirds. See the comment
on `_PROVIDER_ORDER` in `client/layout.py` for what adding it would take.

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
