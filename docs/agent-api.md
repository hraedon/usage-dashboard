# Agent API — capacity v1

Use `GET /api/v1/agent/capacity` to read account usage without HTML scraping,
provider credentials, database access or Kubernetes permissions. The server
reads its cached observations once; GET never triggers provider polling.
For shell-based agents, [`usage-dashboard capacity`](agent-cli.md) wraps this
request and emits JSON without requiring a hand-written HTTP client.
Authentication is `Authorization: Bearer <token>`. Use the operator-provisioned
dashboard credential, never a provider OAuth token or API key.

Operators can set a distinct `AGENT_API_KEY` (Kubernetes Secret key
`agent-api-key`) to grant access **only to this endpoint**. It cannot call
`/refresh`, `/history`, `/readings` or `/schedule`. The existing `API_KEY` also
works, but retains its existing broader rights. Equal keys are rejected at
startup; an empty agent key disables the additional credential. Provision and
rotate credentials through the operator's existing secret distribution path;
agents do not need permission to retrieve Kubernetes Secrets. This is one
shared read-only credential, not per-agent identity or account-level grants.

## Request

- `account`: optional exact configured account ID (`claude`, `claude_work`,
  `codex`, `zai`, `ollama`, `opencode`). Omit for all configured accounts.
  `claude` and `claude_work` remain separate. Umans is not currently a supported
  dashboard account; no balance is fabricated.
- `max_age_seconds`: acceptable observation age, default 2100, range 30–86400.
  This controls trust in observations, not collection frequency. A larger value
  accepts older evidence; it cannot increase actual capacity.
  The default accommodates the standard scheduler's 30-minute idle interval plus
  five minutes of margin. Adjust it for a customized polling schedule. Requiring
  900 seconds is supported, but can return unknown during normal idle polling.

HTTP 200 means the snapshot was returned, including unknown or blocked accounts.
401 means missing/invalid credential. 404 with `detail=account_not_configured`
means the selected known account has no configuration. 422 means invalid account
ID or query bounds. Network/5xx failures mean capacity is unknown. Responses
carry `Cache-Control: no-store`. There is no legacy `/agent/capacity` alias.

```python
import os
import httpx

# Operator supplies a server origin and read-only token via the environment.
# Do not print headers or put credentials in URLs.
with httpx.Client(timeout=15) as client:
    result = client.get(
        os.environ["USAGE_DASHBOARD_URL"].rstrip("/") + "/api/v1/agent/capacity",
        headers={"Authorization": "Bearer " + os.environ["USAGE_DASHBOARD_AGENT_TOKEN"]},
        params={"account": "claude_work"},
    )
    result.raise_for_status()
    snapshot = result.json()
    if snapshot["schema_version"] != "agent-capacity-v1":
        raise ValueError("unsupported capacity schema")
    print(snapshot)
```

## Response contract

The response is typed in the server's OpenAPI schema (`/openapi.json`, accessible
on the private server origin). The public ingress exposes the authenticated
capacity route under its existing `/api` prefix, not private docs routes.

Top-level fields: `schema_version=agent-capacity-v1`, UTC `generated_at`,
`max_age_seconds`, `accounts`, `assessment_scope=reported_limits_only`, and
`job_fit=not_assessed`. Clients should tolerate additional fields and treat
unknown assessment/reason values conservatively.

Each account contains `account_id`, UTC `observed_at` (null when no reading),
`age_seconds`, `freshness`, `assessment`, `reasons`, `windows`, `throttle`, and
`alert`. Raw provider detail strings and credentials are excluded.

| Assessment | Meaning |
|---|---|
| `known_blocked` | Fresh observation reports a boxed provider or an exhausted account/active scoped limit |
| `no_known_quota_block` | Fresh, interpretable reported limits have headroom and no throttle is reported |
| `unknown` | Missing, offline, stale, future-dated, ambiguous, throttled or incomplete evidence |

`freshness` is `fresh`, `stale`, `offline`, `missing`, or `invalid`. Age is
computed at request time, so a stopped scheduler cannot leave an indefinitely
fresh cached row. A future observation is invalid, not age zero.

Windows contain `name`, `scope` (`account` or `provider_scoped`), `is_active`,
`used_percent`, `remaining_percent`, UTC `resets_at`, `state` (`headroom`,
`exhausted`, `unknown`), and `reasons`. Percentages are observations in that
window's own units, never tokens, dollars, or comparable work across accounts.
Missing/nonfinite/out-of-range values become null. A missing reset time remains
null; it is not a deadline. An elapsed reset makes the window unknown until a
new observation supplies a usable window. Stale windows retain observed numbers
but have unknown state; never act on numbers without checking freshness/state.

Only reported windows appear: weekly-only accounts do not acquire an invented
session limit. Scoped names are provider labels, not a verified model catalog.
OpenCode's extra `Monthly` window is normalized as an active account-wide limit,
even though the legacy reading schema stores it in `scoped_limits`.
An exhausted inactive scoped limit returns `scope_selection_required`, because
this endpoint does not select a model. `is_active` comes from the provider's
reading; absence is not proof a limit cannot constrain a particular model.

Stable account reason codes: `no_reading`, `observation_in_future`,
`provider_offline`, `stale_observation`, `provider_boxed`, `quota_exhausted`,
`uncertain_window`, `scope_selection_required`, `provider_throttled`,
`no_reported_limits`, `reported_limits_have_headroom`.
Window reasons: `missing_usage`, `invalid_usage`, `reset_elapsed`,
`untrusted_observation`.

No assessment reserves quota, verifies model availability/account permission,
predicts completion of a long job, or authorizes spending. Other sessions can
consume capacity after the observation. Admission and account selection belong
to the coordinator; do not silently substitute a model when an account blocks.
