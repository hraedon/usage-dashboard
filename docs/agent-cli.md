# Agent capacity CLI

The CLI reads the server's cached capacity observation through the dedicated
agent endpoint:

```sh
export USAGE_DASHBOARD_URL=https://usage-dashboard.example
export USAGE_DASHBOARD_AGENT_TOKEN='operator-provisioned-read-only-token'
usage-dashboard capacity
```

`USAGE_DASHBOARD_URL` is normally the server origin. For convenience, a value
ending in `/api/v1` or `/api/v1/` is also accepted. The command rejects
non-HTTP(S) URLs, embedded URL credentials, query strings, fragments, and
other paths. The token is sent only in the `Authorization` header and is never
printed.

Use the optional filters when a coordinator needs one configured account or a
different observation age bound:

```sh
usage-dashboard capacity --account claude_work --max-age-seconds 3600
```

When `--max-age-seconds` is omitted, the server chooses its documented default
(currently 2100 seconds to accommodate normal idle polling). See the
[API contract](agent-api.md) for assessment and freshness semantics.
The server also validates the supported age bounds, so a client upgrade is not
required when server policy changes.

The command prints the server's JSON snapshot to stdout. HTTP 200 responses
are successful observations even when an account's assessment is `unknown` or
`known_blocked`; clients must inspect the returned account fields. Missing
credentials, network failures, non-200 responses, invalid JSON, and an
unsupported `schema_version` produce a nonzero exit status and a short error
on stderr. Response bodies and request credentials are excluded from errors.

Requests have a finite 15-second timeout. This command only reads cached
observations: it does not trigger provider jobs, choose a model, submit work,
or require Kubernetes credentials.

## Wait for a quota condition

```sh
usage-dashboard watch-capacity --account codex --window weekly \
  --until either --timeout-seconds 14400
```

Use account IDs and exact window names from `usage-dashboard capacity`. The
watcher uses the same URL and read-only token. It polls cached observations;
it does not increase the server's provider refresh rate. It requires the
`agent-capacity-v1` endpoint from Plan 007 to be deployed and configured.

`--until` accepts `exhausted`, `reset`, or `either` (default). Exhaustion means
the selected fresh active window reports 100% with a matching exhausted state.
A reset requires a newer observation made at or after the prior reset boundary
with a later future boundary. This confirms an observed window rollover; the
API cannot prove whether the provider changed that boundary automatically or
an operator intervened. A falling percentage or an elapsed countdown alone
does not confirm reset. Starting after a reset establishes a baseline; the
watcher does not reconstruct earlier history.

`--scope` defaults to `account`; use `provider_scoped` to select a named scoped
window. Inactive windows remain unknown. The watcher observes only the selected
window, so success says nothing about other quota windows, throttling, model
availability, credits, or whether a particular job will fit.

The default interval is 60 seconds (`--interval-seconds`, 5–3600). The default
deadline is one hour (`--timeout-seconds`, 1–604800). A monotonic clock bounds
the loop and sleep. Each request's HTTPX timeout is at most 15 seconds or the
remaining budget. HTTPX timeouts apply per network operation, so a request may
finish after the deadline; such a response is rejected as a timeout. The
watcher does not retry failed HTTP requests. `--max-age-seconds` is forwarded
to the server. Keep the local clock synchronized: future timestamps fail closed.

Output is flushed JSON Lines with schema `capacity-watch-v1`. Each line has
`event`, `checked_at`, `account_id`, `window`, and `scope`. Valid observations
also include `observed_at`, `used_percent`, `remaining_percent`, `resets_at`,
`previous_resets_at`, `conditions`, and `reasons`. The fields describe only the
selected window; raw account payloads, provider diagnostics and tokens are
excluded.

| Event | Meaning |
| --- | --- |
| `baseline` | First accepted observation |
| `changed` | Usage changed within the current boundary |
| `window_changed` | Boundary changed without evidence of rollover; baseline rebased |
| `reset` | Rollover observed while waiting specifically for exhaustion |
| `unknown` | Missing, stale, inactive, ambiguous, conflicting or invalid evidence |
| `recovered` | Valid unchanged evidence returned after an unknown gap |
| `condition_met` | Requested exhaustion or reset observed; inspect `conditions` |
| `timeout` | Deadline reached without the requested condition |
| `error` | Request failed; diagnostic is deliberately sanitized |
| `interrupted` | User interrupted the watcher |

Unchanged polls and repeated identical unknown reasons produce no output.
Fresh baselines survive unknown gaps, so a later fresh rollover can still be
recognized. Pre-boundary deadline corrections rebase immediately. Out-of-order
or conflicting observations cannot replace the accepted baseline.

Exit codes: **0** condition met, **1** configuration/request error, **2** deadline
reached (also argparse usage errors), **130** interrupted. A timeout or unknown
event must not be interpreted as spare capacity or successful reset.
