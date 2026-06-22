# BC-001 — `login claude` CLI fails with "invalid response" after browser auth

**Status:** active
**Filed:** 2026-06-22
**Severity:** medium (workaround exists)
**Area:** `src/usage_dashboard/cli.py` (Claude PKCE OAuth login); `plans/001`;
README "Claude login"

## Symptom
Running `usage-dashboard login claude` opens the browser and the sign-in
completes, but the CLI then fails with an **invalid response** when exchanging
the authorization code, so no token pair is produced. Confirmed 2026-06-22.

## Impact
The documented primary path to mint the dashboard's dedicated Claude token pair
(README "Claude login" / plan 001) does not work end-to-end. User-facing: the
README presents this as *the* way to set up the Claude tile.

## Workaround (used 2026-06-22)
Did a "sacrificial" Claude login on a separate VM that isn't used for Claude
Code, then loaded the resulting token pair into the k8s Secret by hand. The
Claude tile is currently running off that pair, so this is not urgent.

## Likely areas to investigate
- The token-exchange POST in `cli.py` (PKCE code → `platform.claude.com/v1/oauth/token`):
  response parsing, or an error being surfaced as "invalid response" — possibly
  a changed response shape, a redirect/PKCE param mismatch, or an issue specific
  to the loopback-redirect vs. manual `CODE#STATE` paste path.
- Diff against the working sacrificial-VM flow to see what actually differs.
- Re-verify the OAuth params in `plans/001` (client_id, authorize/token URLs,
  scopes) are still current against a live reference.

## Cross-refs
Contradicts `plans/001`'s "implemented" status in practice — the feature exists
but the browser-login path is currently broken. (plan 001 links here.)
