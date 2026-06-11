# usage-dashboard

A two-component system for monitoring AI usage across [Claude](https://claude.ai), [z.ai](https://z.ai), and [Ollama](https://ollama.com). A server fetches usage data from all three providers, normalizes it into a unified format, stores it in SQLite, and serves it via an authenticated API. A client polls the server and renders usage as color-coded progress bars on a 240x320 ST7789 LCD display (designed for a Pi Zero).

## Architecture

```
┌─────────────────┐         ┌─────────────────┐
│   AI Providers  │         │    Pi Zero       │
│  Claude / z.ai  │         │  ST7789 LCD      │
│    / Ollama     │         │  240×320 px      │
└────────┬────────┘         └────────▲─────────┘
         │                           │
         ▼                           │
┌─────────────────┐    HTTP API      │
│  Server (k8s)   │◄────────────────┘
│  FastAPI+SQLite │  Bearer auth
└─────────────────┘
```

## Server

Fetches usage from all configured providers every 5 minutes and exposes a `/readings` endpoint. Runs as a Kubernetes Deployment with a Longhorn-backed PVC for persistent SQLite storage.

### API

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /readings` | Bearer token | Returns latest reading per provider as JSON |
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
  "stale": false
}
```

Status values: `current` | `stale` | `offline`

## Client

Polls the server API with adaptive refresh (60s when values change, 5min when stable) and renders a dashboard image. Designed for the Pi Zero with SPI display but outputs a PNG to `/tmp/dashboard.png` by default for headless use.

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
| `ollama-email` | No | Ollama account email |
| `ollama-password` | No | Ollama account password |

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
