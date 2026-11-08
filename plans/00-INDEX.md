# Plan ledger

Numbering rule, written down after the same accident happened twice: plan
numbers are claimed by adding BOTH the `NNN-<slug>.md` file AND its row here,
in the same PR. Two branches claimed 007 independently (the agent capacity
API and the Antigravity quota feature) and the collision cost a conflict
resolution at merge time; the parked capacity-watch branch's 008 collided
the same way. The ledger is small enough to read before numbering, and
`tests/test_plan_ledger.py` enforces that files and rows agree.

| # | Plan | Status |
|---|------|--------|
| 001 | [Dedicated Claude OAuth login](001-claude-dedicated-oauth-login.md) | implemented 2026-06-14 |
| 002 | [Backlight sleep schedule](002-backlight-sleep-schedule.md) | implemented |
| 003 | [Versioned API and ingress contract](003-versioned-api-and-ingress-contract.md) | implemented 2026-08-07 |
| 004 | [Umans wallet tracking](004-umans-wallet-tracking.md) | implemented |
| 005 | [Auth migration (token store on PVC)](005-auth-mig.md) | implemented; Phase C secret cleanup 2026-11-07 |
| 006 | [Browser enrolment pane](006-browser-enrolment-pane.md) | shipped (PR 41) |
| 007 | [Antigravity Argon quota](007-antigravity-argon-quota.md) | shipped (PR 51) — the agent capacity API was originally numbered 007 and renumbered to 008 |
| 008 | [Agent capacity API](008-agent-capacity-api.md) | shipped 2026-11-07 (PR 37, deployed same day) |
| 009 | [Claude credentials owned by the CLI](009-claude-cli-owned-credentials.md) | proposed 2026-11-08, after the refresh client_id outage |
