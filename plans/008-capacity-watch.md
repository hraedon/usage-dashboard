# Plan 008 — Reset-aware agent capacity watcher

Status: implemented on `feature/capacity-watch-20261002`, rebased onto Plan 007
(the agent capacity API) on 2026-10-17. Numbered 006 until main's browser
enrolment pane took that number. Not deployed.

The owner authorized sustained useful project work on 2026-10-02, including
reading usage to stop at a cap or automatic reset. The existing capacity CLI
returns one snapshot. Add a bounded read-only watcher over that same endpoint
so callers can identify a selected window's exhaustion or observed rollover
without treating stale data, elapsed countdowns, or percentage drops as proof.

## Contract

- `usage-dashboard watch-capacity --account ID --window NAME` selects exactly
  one account and window. `--scope account|provider_scoped` defaults to account.
  Inactive scoped windows remain unknown. No provider/model is selected for work.
- `--until exhausted|reset|either` defaults to either. Exhaustion requires a fresh
  active selected window with valid 100% usage and consistent exhausted state.
  Reset requires a strictly newer fresh observation, observed after the previous
  reset boundary, with a later future boundary. A pre-boundary deadline change
  emits `window_changed` and establishes an explicitly new baseline. It is not
  a confirmed automatic reset. A usage drop alone is not a reset.
- Missing, expired, invalid, future-dated, stale, offline, ambiguous, inactive,
  or out-of-order evidence cannot satisfy a condition. Conflicting values at
  one observation timestamp are unknown. Original fresh baselines survive gaps.
- Stream versioned JSON Lines: initial/changed evidence, transitions to unknown,
  recovered evidence, condition met, timeout, interruption or request error.
  Repeated unchanged polls stay quiet. Output includes only selected capacity
  fields, reason codes, identity and timestamps; no raw provider details/keys.
- Poll interval defaults to 60 seconds (bounds 5–3600); overall timeout defaults
  to 3600 seconds (bounds 1–604800). Use a monotonic deadline, limit the final
  request timeout and sleep to remaining time, and do not retry a failed HTTP
  request automatically. A request error exits distinctly rather than implying
  success, reset, or spare capacity.
- Exit 0 for the requested condition, 1 for configuration/request failures,
  2 for timeout, 130 for interruption. CLI argument errors retain argparse's 2.
- Reuse operator-supplied URL/token and cached read-only agent endpoint. Do not
  refresh providers, read Kubernetes Secrets, alter quotas, reserve work, spend
  credits, execute jobs, or change deployment. No automatic background service.

## Acceptance and evidence

Pure state-machine tests with independent timestamps, offsets, unknown gaps,
rollovers, corrected deadlines, reordered responses, scoped-window selection,
invalid shapes/numbers and exact-boundary cases. Deterministic runner tests
cover output suppression, monotonic timeout, bounded sleeps, errors, and
interruption. Real loopback HTTP/CLI subprocess tests exercise streaming and
exit codes with synthetic readings and no provider scheduler. Keep the original
single-snapshot CLI unchanged. Run Ruff, strict mypy, package checks and relevant
regressions; record existing host-specific failures separately.

Baseline at `d2e1d25`: Windows full suite has 17 failures before changes: one
test converts naive UTC to epoch using local timezone, and 16 shell cases invoke
an unavailable WSL Bash launcher. Other baseline cases passed. Linux qualification
will cover the shell tests rather than relabeling them as Windows successes.
Repair the two related naive-UTC fixture constructions to retain UTC when
converting to epoch; this is a test-only portability correction.
