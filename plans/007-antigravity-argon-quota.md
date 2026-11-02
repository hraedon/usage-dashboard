# Plan 007 — Google Antigravity quota (Gemini 4 Argon first)

**Status:** implemented 2026-11-01 on branch `feat/007-antigravity` (WI-037).
Every CLI fact below was verified against the real `agy` 1.4.12 binary on a
logged-in Linux host, not taken from docs or third-party trackers. Treat this
the same way as the facts in the Codex App Server docstring: re-verify on any
version bump.

Runtime-verified locally (2026-11-01):
- The real server against this host's live login reported Argon 100% used,
  resetting 2026-11-04T15:18:24Z, on both `/readings` and `/dashboard`.
- Three forced refreshes added no CLI log files.
- A real pane ceremony through the API with the real binary surfaced the
  704-char Google link, unredacted, as `verification_url` (the transcript copy
  stays redacted) and a 60 s countdown.
- That ceremony then hit the real 60 s timeout and failed cleanly; a second
  one was cancelled (exit 143). Both left the existing login byte-identical
  with no staging dir and no stray `agy` process.
- Pi frames at 1280×720 and 720×1280 show the Codex/Gemini pair.

**Not yet verified:** the code exchange itself, which needs the owner's
Google sign-in on the deployed pane.

> Goal: show the owner's **Gemini 4 Argon weekly quota** on the dashboard.
> Gemini Flash/Pro (5h + weekly) come along for free in the same payload.

## Why the official CLI, not an HTTP fetcher

Community tools reach Antigravity quota in two ways. Both are rejected:

- **Direct `cloudcode-pa` / `daily-cloudcode-pa` calls** (`fetchAvailableModels`,
  `retrieveUserQuota`, `retrieveUserQuotaSummary`). These need Antigravity's own
  OAuth client id and a token we mint and refresh ourselves: exactly the
  surface Plan 005 removed for OpenAI. They are also unstable. As of Oct 2026,
  trackers report `fetchAvailableModels` stuck at `remainingFraction: 1.0`, and
  the endpoint used for live data has moved at least once.
- **The IDE's local language server.** It only exists on a desktop with the
  IDE running, so it can never work from the k8s pod.

The official CLI documents a non-agent path. From the `agy changelog`:

> Added non-interactive answers for the read-only slash commands in print mode,
> so `-p "/usage"`, `/quota`, … emit … a structured payload under
> `--output-format json` … without starting an agent turn, spending quota, or
> leaving a conversation behind.

So this is the Codex shape: the vendor's CLI owns auth and the backend, and we
read its structured output. It is a *supported* dependency, not an
unsupported one like Claude's `/api/oauth/usage`.

## Verified CLI facts (agy 1.4.12, linux x64, 2026-11-01)

Invocation (cwd = an empty dir, stdin = `/dev/null`):

```
agy -p /usage --output-format json --disable-slash-commands=false --log-file /dev/null
```

- **Cost:** `num_turns: 0`, all token counts 0, conversation count unchanged.
  About 4.9 s wall, about 1.1 s CPU, **221 MB peak RSS** (transient; the
  process exits).
- **Payload:** `command.data.groups[].buckets[]`, each with `id`, `name`,
  `window` (`"5h"` / `"weekly"`), `remaining_fraction` (0–1, *remaining*),
  `reset_time` (UTC `Z`), and an optional `description`. The payload has no
  identity fields. A live capture is in
  `tests/fixtures/antigravity_usage_1.4.12.json`. Buckets seen on an AI Pro
  account:

  | group | bucket id | window |
  |---|---|---|
  | Gemini Models (Flash, Pro) | `gemini-weekly`, `gemini-5h` | weekly, 5h |
  | Claude and GPT models | `3p-weekly`, `3p-5h` | weekly, 5h |
  | Gemini 4 Argon | `gemini-argon-wtu-weekly` | weekly only |

  On Pro, Argon is a separate, weekly-only bucket. On Ultra, Argon is folded
  into the larger Gemini bucket (per the group's own `description`), so the
  Argon bucket may be absent there. Third-party trackers report that the
  backend can mark a bucket `disabled: true` (shown as "Disabled" in the IDE)
  and that proto3 drops a zero `remaining_fraction`. The CLI did emit an
  explicit `0` for the exhausted Argon bucket, but parse both cases
  defensively.
- **Guard against the 1.4.9 bug** (`disableSlashCommands=true` saved in
  settings makes `-p /usage` a *model prompt*). Pass
  `--disable-slash-commands=false`. Then accept a result only if
  `status == "SUCCESS"`, `num_turns == 0`, and `command.name == "usage"`.
  Anything else is a `FetchError`.
- **Disk growth per start:** by default each run writes a new ~34 KB
  `log/cli-<ts>.log`, and logs are *not* pruned per run. At a 300 s poll that
  is about 10 MB/day, the same failure mode as the Codex WAL leak.
  `--log-file /dev/null` cuts this to zero. Do not use a fixed `--log-file`
  path: the CLI **appends** to it (30 KB → 99 KB over two runs). What remains
  is one ~225 B `implicit/*.pb` every few runs, which is negligible.
- **Auto-update:** print mode runs the updater (15-min throttle).
  `AGY_CLI_DISABLE_AUTO_UPDATE=true` disables it (log line
  `Auto-update disabled via environment variable`). **`=1` is silently
  ignored.** It must be `true`.
- **Unauthenticated:** prints a Google OAuth URL on stderr ("Authentication
  required. Please visit the URL to log in") and **blocks** waiting for it.
  `--print-timeout` does not end the wait. The fetcher must enforce its own
  hard timeout, kill the process group, and map this to `FetchAuthError`.
  Never log the stderr verbatim.
- **Credential:** a file, `$HOME/.gemini/antigravity-cli/antigravity-oauth-token`
  (0600; JSON `token` / `auth_method` / `id_token_email`). The binary falls
  back to file storage when no keyring is available, which is the pod case.
  The CLI owns the refresh, and it handles an externally changed token
  ("Skipping refreshed token save because stored token changed externally").
- **Install pin:** the release manifest at
  `https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_amd64.json`
  gives a versioned GCS tarball plus SHA-512. That fits the Dockerfile's
  pin-and-verify `clis` stage. The binary is glibc-linked, which suits
  `python:3.12-slim`. It is **234 MB**.

## Design

- **`Provider.ANTIGRAVITY`** with a new `server/fetch_antigravity.py`. It runs a
  per-fetch subprocess, not a resident child: there is no documented server
  mode for `/usage`, and with `--log-file /dev/null` the per-start cost is
  bytes. Environment: `HOME=/data/antigravity` (on the PVC),
  `AGY_CLI_DISABLE_AUTO_UPDATE=true`; cwd is an empty dir under it;
  `stdin=DEVNULL`; new session; hard timeout 45 s (`_DEFAULT_TIMEOUT`).
- **Reading mapping** (percent = *used* = `(1 − remaining_fraction) × 100`, as
  Codex does). This mirrors Claude + Fable, so no new bar machinery is needed:
  - `session_*` ← `gemini-5h`, `weekly_*` ← `gemini-weekly` (Flash/Pro)
  - `scoped_limits = [ScopedLimit("Argon", …)]` ← `gemini-argon-wtu-weekly`
  - Match buckets by `window` and group, not only by exact id, because the
    `-wtu-` infix looks experiment-flavoured.
  - `disabled: true`, or a bucket with `reset_time` but no fraction: treat as
    100% used.
  - The `3p-*` bucket (Claude/GPT inside Antigravity) is ignored for now. It
    is one more `ScopedLimit` if ever wanted.
- **Enrolment:** see "Enrolment via the `/login` pane" below.
- **Scheduler:** configured iff `ANTIGRAVITY_MODE=cli`, like Codex. Gating
  on the token file was rejected: signed out, the tile should say
  "login required — sign in on /login", not vanish, and a mode switch
  sidesteps the WI-035 startup-snapshot problem.
  - A signed-out fetch costs ~0.4 s: it is killed on the sign-in prompt,
    measured in the runtime image. It parks at the failure cap (1 h).
  - Pane enrolment fetches immediately. After `kubectl exec` enrolment, tap
    refresh on a panel (or `POST /refresh`).
  - The normal idle ladder applies; Argon is weekly-only and moves slowly.
- **Clients:** a tile titled `GEMINI`.
  - **Owner decision (2026-11-01): it pairs half-width with Codex, and that
    pair shows weekly windows only** (Codex `W`, Gemini `W` + Argon `A`).
    The detail view still lists every window.
  - Pairs are now explicit (`_PAIRED_ROWS`), so without Antigravity, Codex
    keeps its full-width row instead of pairing with z.ai.
  - Antigravity without Codex gets a full-width tile with all three windows.
  - On the tiny 240×320 fallback, five tiles switch portrait to three columns.
    Three rows left ~83 px per tile, short of two summary lines; the real
    renderer audit caught this.
  - The web `/dashboard` gets a `Gemini` card after Codex, showing every
    window, with parity locked in `test_render_parity.py`.
- **Image/Pod:** `AGY_VERSION` + `AGY_SHA512` build args in the `clis` stage,
  plus a `--version` smoke check. The image grows by about 234 MB. The pod
  sets no memory limit today, but budget for a 221 MB spike on top of the
  ~92 MB resident Codex child.

## Enrolment via the `/login` pane (same shape as Claude)

### Verified sign-in mechanics (agy 1.4.12, throwaway `HOME`, 2026-11-01)

The sign-in is a paste-back PKCE flow, the same interaction as Claude's:

1. Unauthenticated, `agy -p /usage --output-format json …` prints
   `Authentication required. Please visit the URL to log in:` followed by a
   one-line `accounts.google.com` URL (official Antigravity client,
   `redirect_uri=https://antigravity.google/oauth-callback`, `prompt=consent`).
2. It then prints `Waiting for authentication (timeout 60s)...` /
   `Or, paste the authorization code here and press Enter:` and reads stdin.
3. The callback page shows "Paste this code into your application to complete
   authentication" with a Copy button.
4. **The 60 s window is hard-coded** (a literal in the binary; no env knob
   found). On expiry the CLI prints `Error: authentication timed out.`, emits a
   JSON `status: ERROR` result, and exits 1. Nothing is written.
5. On success, the same process carries on and prints the `/usage` payload.
   So one run is both the ceremony **and** the verification.

There is no `agy login` subcommand. The interactive TUI's first-run path
(`Select login method` → `Google OAuth` → `Signing in...` → URL) has no 60 s
limit, but it is a full-screen redrawing UI: the URL wraps and paginates
("(1–20 of 29 lines)"), and after sign-in it drops into a chat session that
never exits. That makes it a poor transcript for the pane. It remains the
`kubectl exec` fallback (`HOME=/data/antigravity agy`), where a human drives
a real terminal.

### Design

- **Ceremony owner:** `usage-dashboard login antigravity` in `cli.py`. It
  creates a fresh **staging** `HOME` (`/data/antigravity.enrol-<id>`, 0700) and
  execs the print-mode command there, with stdin/stdout on the terminal. On
  exit 0 it parses stdout with the fetcher's parser. `SUCCESS`, `num_turns == 0`
  and a usage payload mean verified. It then swaps the staging dir into
  `/data/antigravity`, keeping the previous dir as `antigravity.prev` until
  the swap completes. Any failure deletes the staging dir, so **a failed or
  abandoned re-login never displaces a working credential** (the
  verify-then-store rule). The swap moves the CLI's own state directory
  wholesale and never parses the token file, so nothing here learns the
  credential format.
- **Pane:** `CAPABILITIES["antigravity"] = {"mode": "interactive"}` plus
  `EnrolmentService.start_antigravity()`, which spawns that CLI command on a
  PTY via the same code path as `start_claude`. The transcript, paste box and
  cancel already exist. The paste box used to be gated on
  `provider === "claude"`; it now keys on the job's own `accepts_input`
  (true iff a PTY is attached). Pane-only additions:
  - A prominent "Open Google sign-in" button once the URL appears.
  - A visible **60 s countdown**, started on the `Waiting for authentication`
    line.
  - A one-line hint that a timeout is harmless: press Start again.
- **One owner of `HOME`:** the swap and the fetcher take the same lock, so a
  fetch never runs mid-swap. The ceremony itself runs in staging, so unlike
  Codex it needs **no fetcher pause**.
- **After success:** `on_success("antigravity")`. The scheduler marks the
  provider configured and fetches immediately, with no rollout (same hook as
  the other providers; cf. WI-035).
- **Never copy a desktop token into the pod.** Each install owns its own token
  family. That is the Plan 005 rule, even though Google refresh tokens usually
  do not rotate.
- **Logout** (cf. WI-036) is deleting `/data/antigravity`. Out of scope here.

## Independent review (2026-11-01, fresh read-only reviewer)

No high-severity findings. Fixed:
- Old panels dropped *every* reading on one unknown provider. The client now
  skips unknown rows. **Deploy order:** let the Pis auto-update before
  rolling the server.
- The ceremony parsed all of stdout, so any chatter before the JSON result
  would reject a good sign-in. It now takes the last JSON line.
- The weekly-only pair could render blank tiles; it now falls back to every
  window.
- agy forks (it launches `xdg-open` when signed out, verified), and a Python
  PID 1 never reaps orphans. **tini** is now PID 1.
- A deliberately deleted home could be resurrected from `.prev`. Recovery
  now keys on a `.swapping` marker.
- A late Cancel could mark an installed login as failed. SIGTERM is ignored
  once the login is installed.
- The pool match was order-dependent. The exact `gemini-5h`/`gemini-weekly`
  ids now win.
- A paste could write to a closed PTY fd. This predated Plan 007; input is
  now detached under the job lock before close.
- Proxy env vars are now passed through.

Tests were added for each fix and for the reviewer's gaps (lock exclusion,
grandchild kill), and each was mutation-checked.

Kept as is: the ceremony's agy stays in the wrapper's session, because the
paste path was verified that way with the real binary. It is killed by pid;
tini reaps anything it orphans.

## Cross-lineage review (2026-11-01, kimi-k3 via ollama-cloud)

Verdict: pass, no blocking findings. Non-blocking items, all handled here:

- **Plan drift:** the timeout and the paste-box gating text above are now
  corrected.
- **Two concurrent `kubectl exec` ceremonies: last valid swap wins.** The
  loser's verified login is discarded without notice. Each swap is
  verify-then-rename, so no state breaks. The pane serializes its own
  ceremonies; don't run the exec path at the same time as the pane.

## Version-bump checklist (re-verify on every `AGY_VERSION` change)

1. `/usage` JSON shape and bucket ids (`gemini-5h`, `gemini-weekly`,
   `gemini-argon-*`). Refresh the fixture.
2. Signed out: the `Authentication required` marker still arrives on
   stderr **newline-terminated**. The fast-fail reads lines, so without a
   newline a fetch degrades to the 45 s timeout and a generic `FetchError`.
3. The Google sign-in URL is still printed alone on its line.
   `_GOOGLE_SIGNIN_URL` would absorb trailing punctuation.
4. The paste window and the `Waiting for authentication (timeout Ns)`
   wording (the countdown parses it).
5. `--log-file /dev/null` still suppresses the per-run log, and
   `AGY_CLI_DISABLE_AUTO_UPDATE=true` is still honoured.
6. Zero turns for `-p /usage`.

## Open items (verify before or while building)

1. **Complete one real sign-in from the pane.** Every step above is verified
   up to pasting the code. Exchanging a real code needs the owner's Google
   sign-in, and that is the first runtime check after deploy.
2. Whether 60 s is comfortable in practice (owner expects it to be). If it is not, the fallback is
   driving the TUI. Probed: still waiting after 5 min. On a 2000-col PTY
   (`TIOCSWINSZ`) it shows `→ Click here to authenticate` plus a paste box. The
   URL is carried **only** inside an OSC 8 hyperlink escape, which
   `enrolment._ANSI_ESCAPE` strips, so that path must extract the OSC 8 target
   before stripping. It must also send Enter for the login-method menu, and
   detect success by the token file appearing (the TUI never exits on its own).
3. ~~Tile placement~~: decided (paired with Codex, weekly-only) and
   frame-captured.
4. Ultra-plan shape (Argon folded into Gemini) is unobserved. Make sure the
   tile degrades to the two Gemini bars without error.

## Acceptance

1. `fetch_antigravity` parses the 1.4.12 fixture into Gemini 5h/weekly + an
   Argon scoped limit (Argon 100% used, reset 2026-11-04T15:18:24Z). It
   handles a missing/`disabled` bucket, a missing Argon group, the
   unauthenticated stderr (→ `FetchAuthError`, no hang), a turn-spending
   result (→ `FetchError`), a timeout, and malformed JSON.
2. A subprocess-level test asserts the argv/env contract:
   `--log-file /dev/null`, `--disable-slash-commands=false`,
   `AGY_CLI_DISABLE_AUTO_UPDATE=true`, `HOME` on the PVC, stdin closed.
3. The Dockerfile pins version + SHA-512, and the image smoke-runs
   `agy --version`.
4. Enrolment via CLI and `/login` produces a working token under
   `/data/antigravity`, and the scheduler adopts it without a restart. A
   timed-out, cancelled or unverifiable ceremony leaves the existing
   `/data/antigravity` byte-identical and removes its staging dir. Tests drive
   this with a stub `agy` script (cf. `command_override`) that emits the
   verified prompt lines, and a fixture for the success and timeout outputs.
5. `pytest -q`, `ruff check .`, `mypy src` clean, plus a `/verify` frame
   capture.
