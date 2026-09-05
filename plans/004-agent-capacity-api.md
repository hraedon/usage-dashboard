# Plan 004 — Agent capacity API

Status: implemented on `feat/agent-capacity-api`; not deployed. The API and CLI
received a separate Sol-agent review (not an independent-lineage assurance
claim). Review corrections cover OpenCode's account-wide monthly window,
freshness aligned with the standard idle polling cadence, shared credential
comparison, and keeping age-policy validation on the server.

Local validation: 681 tests passed with the worktree source on `PYTHONPATH` and
SDL dummy drivers; ruff, strict mypy (31 source files), and `git diff --check`
passed. Existing asyncio refresh tests stalled in the sandbox, so the complete
suite was run outside it. No live provider fetch, deployment, or remote CI
qualification is claimed.

Owner request: 2026-09-05, provide a dedicated API so agents do not scrape the
dashboard or obtain data/credentials from Kubernetes.

Implement a read-only `GET /api/v1/agent/capacity`, with optional `account`
filter and bounded `max_age_seconds`. Serve typed account records, UTC observation
times, normalized quota windows, freshness, assessment and stable reason codes.
Assess only observed limits, never predict job completion, choose models, launch
jobs, reserve quota, or infer tokens from percentages. Separate Claude accounts.
Missing, stale, invalid or rolled-over observations must not imply spare capacity.
Model-scoped limits remain separate; without a requested model, depleted inactive
scopes require model selection rather than globally blocking the account.

Use existing cached readings and authentication. Optional `AGENT_API_KEY` grants
access only to the agent endpoint; the existing `API_KEY` remains compatible.
Require distinct keys when the optional credential is set. Declare the route
externally accessible under the existing authenticated `/api` ingress prefix;
do not add a legacy alias for this new endpoint. Document the contract and a
Python client example with environment-based credentials. Provider credentials,
raw detail strings, prompts and Kubernetes permissions are unnecessary.

Acceptance: authentication and read-only credential scope; separate accounts;
fresh, aged, missing, offline and future-dated readings; quota reset without a
new observation; exhausted aggregate and active scoped limits; depleted inactive
scope; weekly-only readings; invalid numeric data; schema discovery; query
validation; no provider fetch on GET; unchanged existing API/ingress tests.

Follow-on under the owner's estate-improvement authorization: ship
`usage-dashboard capacity` as a thin JSON CLI over this same endpoint, using an
operator-provided URL and token from environment variables. Do not duplicate the
server's policy or introduce a scheduler in the client.

Deferred: plan metadata, Umans fetcher, empirical duration/consumption estimates,
MCP wrappers, account-specific access grants and coordinator admission.
