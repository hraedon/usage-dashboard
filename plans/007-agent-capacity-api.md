# Plan 007 — Agent capacity API

Status: rebased onto main on 2026-10-17 (after Plans 005 auth migration and
006 browser enrolment); not deployed. Originally implemented as commit
`a64d814` on `feat/agent-capacity-api` and carried as PR 37. Numbered 004,
then 005, then 007 as other plans landed on main first. Earlier qualification
records described pre-rebase trees and were dropped; the PR carries current
evidence.

Integration review (2026-09-12): preserved wallet rendering and the operator
API aliases; updated Umans query semantics to distinguish unconfigured (404)
from invalid account (422). A configured wallet has unknown quota capacity;
formatted balances are never interpreted as quota or permission to spend.

Regression tests reproduced a shared timestamp parser defect: dropping a
non-UTC offset could turn an elapsed reset into future headroom after a SQLite
round trip. The parser now converts to UTC before removing timezone information.
Tests cover both offset directions, half-hour offsets, scoped resets and a real
database/API round trip. The CLI handles URL/header construction failures with
the same short, credential-free diagnostic as transport failures.

Qualification also exercises the real HTTP server and CLI subprocess, both
credential scopes, account filters, unknown/blocked outcomes and query errors.
All fixtures are synthetic and the server has no provider scheduler.

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

Deferred: plan metadata, structured wallet amounts in the agent API,
empirical duration/consumption estimates,
MCP wrappers, account-specific access grants and coordinator admission.
