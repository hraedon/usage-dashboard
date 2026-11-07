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
