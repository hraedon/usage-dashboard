"""CLI entry point for usage-dashboard.

Provider enrolment. Since Plan 005 the dashboard implements no OAuth flow of
its own: Codex enrols through the official App Server's device-code login (and
Codex keeps the tokens), and Claude enrols through the official Claude Code
login, whose credential is imported into the dashboard's token store and then
deleted from the temporary profile. Ollama and OpenCode Go still capture a
browser session cookie, which is all their usage pages expose.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from usage_dashboard.claude_credentials import (
    REQUIRED_SCOPE,
    ClaudeCredential,
    ClaudeCredentialError,
    parse_credentials,
)
from usage_dashboard.server.codex_app_server import (
    DEFAULT_CODEX_BIN,
    DEFAULT_CODEX_HOME,
    CodexAppServerClient,
    CodexAppServerConfig,
)
from usage_dashboard.server.fetch_claude import fetch_claude_usage
from usage_dashboard.server.fetch_types import FetchError
from usage_dashboard.server.token_store import TokenStore, TokenStoreError
from usage_dashboard.shared.models import Provider

logger = logging.getLogger(__name__)



# ---------------------------------------------------------------------------
# Claude enrolment (Plan 005)
#
# The dashboard no longer implements Claude's authorization-code/PKCE flow.
# Enrolment now drives the *official* Claude Code login in a throwaway config
# directory, imports the credential it writes into the dashboard's own token
# store, and then deletes that directory.
#
# Deleting it is the point, not tidiness: a refresh token has exactly one
# rightful refresher. If a copy were left behind under a Claude Code profile,
# both it and the dashboard would rotate the same family and race each other
# into a lockout (the WI-001 failure, re-created).
#
# Two dependencies here remain unsupported and are deliberately quarantined:
# reading `.credentials.json` (see claude_credentials.py) and calling
# GET /api/oauth/usage. Anthropic exposes no documented subscription-quota API,
# and `claude setup-token` cannot help — it mints an inference token without
# the `user:profile` scope that endpoint requires.
# ---------------------------------------------------------------------------

DEFAULT_CLAUDE_BIN = "claude"
# CLI account selector -> token-store key. The work account resolves and
# refreshes independently of the personal one.
CLAUDE_ACCOUNT_KEYS = {"personal": "claude", "work": "claude_work"}
_CREDENTIALS_FILENAME = ".credentials.json"
_DEFAULT_TOKEN_STORE = "/data/tokens.json"


def _claude_cli_version(claude_bin: str) -> str | None:
    """Best-effort `claude --version`, for naming in a schema error."""
    try:
        result = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, timeout=30.0
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _run_official_claude_login(claude_bin: str, config_dir: str) -> None:
    """Run the official login ceremony against an isolated config directory.

    `claude auth login --claudeai` is the documented entry point and works over
    SSH and in containers by displaying a code the operator pastes back. The
    caller's TTY is inherited deliberately so that interaction reaches them.
    """
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = config_dir
    try:
        result = subprocess.run([claude_bin, "auth", "login", "--claudeai"], env=env)
    except OSError as exc:
        raise ClaudeCredentialError(
            f"Could not run {claude_bin!r}: {exc}. Is the Claude Code CLI installed?"
        ) from exc
    if result.returncode != 0:
        raise ClaudeCredentialError(
            f"Claude Code login exited with status {result.returncode}; "
            "existing credentials were left untouched."
        )


def _read_imported_credential(config_dir: str, claude_bin: str) -> ClaudeCredential:
    """Parse the credential the official login just wrote."""
    path = Path(config_dir) / _CREDENTIALS_FILENAME
    if not path.exists():
        raise ClaudeCredentialError(
            f"Claude Code wrote no {_CREDENTIALS_FILENAME} in {config_dir}. "
            "The login did not complete, or this version stores credentials "
            "elsewhere (a system keychain rather than a file)."
        )
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ClaudeCredentialError(f"Could not read {path}: {exc}") from exc
    return parse_credentials(raw, cli_version=_claude_cli_version(claude_bin))


def _validate_usage_access(credential: ClaudeCredential, provider: Provider) -> None:
    """Prove the credential can actually read the usage endpoint.

    Checked *before* the store is touched, so a credential that cannot do the
    dashboard's one job never displaces a working one. The scope check is the
    cheap gate; the live call is the one that cannot be fooled by absent or
    optimistic metadata.
    """
    if credential.scopes_known and not credential.has_profile_scope:
        raise ClaudeCredentialError(
            f"Imported credential lacks the {REQUIRED_SCOPE!r} scope "
            f"(got: {', '.join(credential.scopes) or 'none'}). "
            "A `claude setup-token` token cannot be used here — the usage "
            "endpoint needs a full login."
        )
    try:
        fetch_claude_usage(credential.access_token, provider)
    except FetchError as exc:
        raise ClaudeCredentialError(
            f"Imported credential could not read Claude usage: {exc}. "
            "Existing credentials were left untouched."
        ) from exc


def login_claude(
    account: str = "personal",
    token_store_path: str | None = None,
    claude_bin: str = DEFAULT_CLAUDE_BIN,
) -> None:
    """Enrol a dedicated Claude credential via the official Claude Code login.

    Writes straight into the dashboard's token store on the PVC; no token is
    ever printed, and nothing needs pasting into a Kubernetes Secret.
    """
    try:
        store_key = CLAUDE_ACCOUNT_KEYS[account]
    except KeyError:
        print(
            f"Unknown account {account!r}; expected one of "
            f"{', '.join(sorted(CLAUDE_ACCOUNT_KEYS))}.",
            file=sys.stderr,
        )
        sys.exit(1)
    provider = Provider.CLAUDE if store_key == "claude" else Provider.CLAUDE_WORK
    store_path = token_store_path or os.environ.get("TOKEN_STORE_PATH") or _DEFAULT_TOKEN_STORE

    print(f"Enrolling the '{account}' Claude account (token-store key: {store_key}).")
    print("A one-time official Claude Code login follows. Over SSH or in a")
    print("container it shows a code to paste back into this terminal.\n")

    # mkdtemp is 0700 by default; the credential must not be world-readable
    # even for the seconds it exists.
    config_dir = tempfile.mkdtemp(prefix="usage-dashboard-claude-login-")
    try:
        _run_official_claude_login(claude_bin, config_dir)
        credential = _read_imported_credential(config_dir, claude_bin)
        _validate_usage_access(credential, provider)
        store = TokenStore(store_path)
        store.save(
            store_key,
            credential.access_token,
            credential.refresh_token,
            metadata=credential.metadata(),
        )
    except (ClaudeCredentialError, TokenStoreError) as exc:
        print(f"\nEnrolment failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        # Non-negotiable: leaving this behind would give the credential family
        # a second refresher.
        shutil.rmtree(config_dir, ignore_errors=True)

    plan = credential.subscription_type or "unknown"
    print(f"\nClaude '{account}' enrolled successfully (plan: {plan}).")
    print(f"  token store: {store_path}  key: {store_key}")
    print("  temporary Claude Code credential store removed.")
    print("\nRoll the server so the scheduler picks it up:")
    print("  kubectl -n usage-dashboard rollout restart deploy/usage-dashboard-server")


# ---------------------------------------------------------------------------
# Ollama login
#
# ollama.com authenticates via WorkOS AuthKit (hosted at signin.ollama.com): a
# JS-driven React form plus an anti-bot device-fingerprint signal, so there is
# no HTTP endpoint to POST credentials to. The fetcher only needs the resulting
# ollama.com session cookie, so this flow drives a real browser, lets the
# operator sign in by hand (handling any WorkOS challenge), and extracts the
# cookie to load into the ``ollama-cookie`` secret. Mirrors the Claude login's
# "mint then paste" UX; Playwright is a CLI-only optional dependency.
# ---------------------------------------------------------------------------

_OLLAMA_SETTINGS_URL = "https://ollama.com/settings"
# Match the User-Agent the server's fetcher sends, so the minted session is
# consistent with how it will later be used.
_OLLAMA_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def _ollama_cookies(cookies: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Keep only cookies scoped to the ollama.com domain."""
    return [c for c in cookies if "ollama.com" in str(c.get("domain", ""))]


def _serialize_cookie_header(cookies: Iterable[Mapping[str, Any]]) -> str:
    """Render cookie dicts as a ``name=value; ...`` Cookie header value."""
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def login_ollama(headless: bool = False, verify: bool = True) -> None:
    """Drive a browser through the ollama.com login and print the session cookie."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is required for ollama login. Install it with:\n"
            "  pip install 'usage-dashboard[login]'\n"
            "  playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Opening a browser to ollama.com ...")
    print(
        "Sign in (handling any WorkOS prompt). When you can see your ollama\n"
        "settings/usage page, return here and press Enter."
    )
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            context = browser.new_context(user_agent=_OLLAMA_USER_AGENT)
            page = context.new_page()
            page.goto(_OLLAMA_SETTINGS_URL)
            input("> Press Enter once you are signed in... ")
            raw_cookies = context.cookies()
            browser.close()
    except Exception as exc:  # noqa: BLE001 - surface any browser/launch failure
        print(f"Browser automation failed: {exc}", file=sys.stderr)
        if headless:
            print(
                "Headless login rarely clears WorkOS's anti-bot check; "
                "run without --headless on a machine with a display.",
                file=sys.stderr,
            )
        sys.exit(1)

    cookies = _ollama_cookies(raw_cookies)
    cookie_header = _serialize_cookie_header(cookies)
    if not cookie_header:
        print(
            "No ollama.com cookies captured — login may not have completed.",
            file=sys.stderr,
        )
        sys.exit(1)

    if verify:
        from usage_dashboard.server.fetch_ollama import fetch_ollama_usage

        try:
            fetch_ollama_usage(cookie_header)
            print("\nVerified: ollama.com settings page parsed with this cookie.")
        except Exception as exc:  # noqa: BLE001 - verification is best-effort
            print(
                f"\nWarning: captured a cookie but the usage fetch failed: {exc}\n"
                "The cookie may still be valid; check the secret after loading it.",
                file=sys.stderr,
            )

    print("\nOllama session cookie captured.\n")
    print("Load it into the k8s Secret (server-secret.yaml):\n")
    print(f'  ollama-cookie: "{cookie_header}"')
    print()
    print("Then update the Secret and roll the server, e.g.:")
    print(
        "  kubectl -n usage-dashboard patch secret server-secrets --type merge -p \\\n"
        '    "{\\"stringData\\":{\\"ollama-cookie\\":\\"$COOKIE\\"}}"'
    )
    print("  kubectl -n usage-dashboard rollout restart deploy/usage-dashboard-server")


# ---------------------------------------------------------------------------
# OpenCode Go login
#
# Same shape as the ollama flow — a human-in-the-loop browser session, because
# opencode.ai has no usage API and no unattended login. It captures two things:
# the `auth` cookie and the `wrk_…` workspace id, both of which the fetcher
# needs. Playwright's context is ephemeral (its own profile, discarded on
# close), which is what the operator otherwise gets by using a private window:
# the point is to end the flow by *closing the browser*, never by signing out —
# signing out invalidates the cookie server-side and the captured value dies
# with it.
# ---------------------------------------------------------------------------

_OPENCODE_HOME_URL = "https://opencode.ai/auth"
_OPENCODE_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
# Workspace ids are ULID-shaped: `wrk_` + Crockford base32.
_WORKSPACE_ID_RE = re.compile(r"\bwrk_[0-9A-HJKMNP-TV-Z]{26}\b")


def _opencode_auth_cookie(cookies: Iterable[Mapping[str, Any]]) -> str | None:
    """The bare ``auth`` cookie value for opencode.ai, if the session set one."""
    for cookie in cookies:
        if cookie.get("name") == "auth" and "opencode.ai" in str(cookie.get("domain", "")):
            return str(cookie["value"])
    return None


def _workspace_id_from(*sources: str) -> str | None:
    """First ``wrk_…`` id found across *sources* (page URL, then page HTML)."""
    for source in sources:
        match = _WORKSPACE_ID_RE.search(source or "")
        if match is not None:
            return match.group(0)
    return None


def login_opencode(headless: bool = False, verify: bool = True) -> None:
    """Drive a browser through the opencode.ai login and print both credentials."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is required for opencode login. Install it with:\n"
            "  pip install 'usage-dashboard[login]'\n"
            "  playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Opening a browser to opencode.ai ...")
    print(
        "Sign in, then navigate to your workspace's Go page\n"
        "(opencode.ai/workspace/<wrk_...>/go) so the workspace id can be read\n"
        "from the URL. Return here and press Enter.\n"
        "Do NOT sign out afterwards — that invalidates the cookie you just took."
    )
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            context = browser.new_context(user_agent=_OPENCODE_USER_AGENT)
            page = context.new_page()
            page.goto(_OPENCODE_HOME_URL)
            input("> Press Enter once you are on the workspace Go page... ")
            raw_cookies = context.cookies()
            page_url = page.url
            try:
                page_html = page.content()
            except Exception:  # noqa: BLE001 - content() races a navigation
                page_html = ""
            browser.close()
    except Exception as exc:  # noqa: BLE001 - surface any browser/launch failure
        print(f"Browser automation failed: {exc}", file=sys.stderr)
        sys.exit(1)

    cookie = _opencode_auth_cookie(raw_cookies)
    if not cookie:
        print(
            "No opencode.ai `auth` cookie captured — login may not have completed.",
            file=sys.stderr,
        )
        sys.exit(1)

    workspace_id = _workspace_id_from(page_url, page_html)
    if not workspace_id:
        print(
            "Captured the cookie but found no wrk_... id on the page. Open the\n"
            "workspace Go page and read the id out of the URL, then set\n"
            "opencode-workspace-id by hand.",
            file=sys.stderr,
        )

    if verify and workspace_id:
        from usage_dashboard.server.fetch_opencode import fetch_opencode_usage

        try:
            fetch_opencode_usage(workspace_id, cookie)
            print("\nVerified: opencode.ai workspace usage parsed with these credentials.")
        except Exception as exc:  # noqa: BLE001 - verification is best-effort
            print(
                f"\nWarning: captured credentials but the usage fetch failed: {exc}\n"
                "Check the secret after loading it.",
                file=sys.stderr,
            )

    print("\nOpenCode Go credentials captured.\n")
    print("Load them into the k8s Secret (server-secret.yaml):\n")
    print(f'  opencode-workspace-id: "{workspace_id or "<wrk_... not found>"}"')
    print(f'  opencode-cookie: "{cookie}"')
    print()
    print("Then update the Secret and roll the server, e.g.:")
    print(
        "  kubectl -n usage-dashboard patch secret server-secrets --type merge -p \\\n"
        '    "{\\"stringData\\":{\\"opencode-workspace-id\\":\\"$WORKSPACE\\",'
        '\\"opencode-cookie\\":\\"$COOKIE\\"}}"'
    )
    print("  kubectl -n usage-dashboard rollout restart deploy/usage-dashboard-server")


# ---------------------------------------------------------------------------
# Codex enrolment (Plan 005)
#
# The dashboard no longer mints, stores, refreshes or transmits an OpenAI
# token. `codex` owns all of that; enrolment just drives the App Server's
# device-code login against the same CODEX_HOME the server runs with, so the
# credential lands on the PVC where the scheduler will find it.
#
# Re-enrolment caution: never run two App Server processes against one
# CODEX_HOME. Set CODEX_MODE=disabled, roll the pod, enrol, then restore
# app_server and roll again (see the README runbook).
# ---------------------------------------------------------------------------


def login_codex(
    codex_home: str | None = None,
    codex_bin: str | None = None,
    timeout: float = 900.0,
) -> None:
    """Enrol a ChatGPT account through the Codex App Server's device-code flow.

    Prints only the verification URL and one-time user code; no token material
    is returned, printed or copied anywhere.
    """
    config = CodexAppServerConfig(
        binary=codex_bin or os.environ.get("CODEX_BIN") or DEFAULT_CODEX_BIN,
        home=codex_home or os.environ.get("CODEX_HOME") or DEFAULT_CODEX_HOME,
    )
    print(f"Enrolling Codex against CODEX_HOME={config.home}")
    print("Codex owns the resulting tokens; the dashboard never sees them.\n")

    def present(url: str, code: str) -> None:
        print("Open this URL and enter the code:\n")
        print(f"  {url}")
        print(f"  code: {code}\n")
        print("Waiting for the login to complete...")

    # persistent=False: enrolment is a one-shot ceremony, and the per-start
    # CODEX_HOME residue the runtime avoids is irrelevant for a handful of runs.
    client = CodexAppServerClient(config, persistent=False)
    try:
        client.device_code_login(present, timeout=timeout)
    except FetchError as exc:
        print(f"\nCodex enrolment failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()

    # Verify with a *fresh* App Server process. Re-reading through the same
    # child would only prove it remembered its own login; spawning a new one
    # proves the credential actually reached CODEX_HOME on disk, which is what
    # the server will read after the next rollout.
    verifier = CodexAppServerClient(config, persistent=False)
    try:
        account, rate_limits = verifier.read_account_and_rate_limits()
    except FetchError as exc:
        print(
            f"\nLogin reported success but verification failed: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        verifier.close()

    detail = account.get("account") or {}
    print("\nCodex enrolled successfully.")
    print(f"  auth mode: {detail.get('type')}  plan: {detail.get('planType')}")
    buckets = rate_limits.get("rateLimitsByLimitId") or {}
    print(f"  rate-limit buckets visible: {', '.join(sorted(buckets)) or 'rateLimits only'}")
    print("\nSet CODEX_MODE=app_server (if it is not already) and roll the server:")
    print("  kubectl -n usage-dashboard rollout restart deploy/usage-dashboard-server")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="usage-dashboard",
        description="Usage dashboard CLI",
    )
    sub = parser.add_subparsers(dest="command")

    login_parser = sub.add_parser("login", help="Log in to a provider")
    login_parser.add_argument(
        "provider",
        choices=["claude", "ollama", "opencode", "codex"],
        help="Provider to log in to",
    )
    login_parser.add_argument(
        "--account",
        choices=sorted(CLAUDE_ACCOUNT_KEYS),
        default="personal",
        help="[claude] Which Claude account to enrol (default: personal)",
    )
    login_parser.add_argument(
        "--token-store",
        default=None,
        help="[claude] Path to the token store (default: $TOKEN_STORE_PATH or "
        "/data/tokens.json)",
    )
    login_parser.add_argument(
        "--claude-bin",
        default=DEFAULT_CLAUDE_BIN,
        help="[claude] Claude Code executable to run the official login with",
    )
    login_parser.add_argument(
        "--codex-home",
        default=None,
        help="[codex] CODEX_HOME to enrol into (default: $CODEX_HOME or /data/codex). "
        "Must match the server's, or the login will not be visible to it",
    )
    login_parser.add_argument(
        "--codex-bin",
        default=None,
        help="[codex] Codex executable (default: $CODEX_BIN or `codex`)",
    )
    login_parser.add_argument(
        "--headless",
        action="store_true",
        help="[ollama|opencode] Run the browser headless (rarely clears an "
        "anti-bot check; opencode's sign-in needs a real window in practice)",
    )
    login_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="[ollama|opencode] Skip fetching the usage page to validate the cookie",
    )

    args = parser.parse_args()

    if args.command == "login":
        if args.provider == "claude":
            login_claude(
                account=args.account,
                token_store_path=args.token_store,
                claude_bin=args.claude_bin,
            )
        elif args.provider == "ollama":
            login_ollama(headless=args.headless, verify=not args.no_verify)
        elif args.provider == "opencode":
            login_opencode(headless=args.headless, verify=not args.no_verify)
        elif args.provider == "codex":
            login_codex(codex_home=args.codex_home, codex_bin=args.codex_bin)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
