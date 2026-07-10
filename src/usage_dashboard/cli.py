"""CLI entry point for usage-dashboard.

Provides the `usage-dashboard login claude` command for minting a dedicated
OAuth token pair via the Authorization Code + PKCE flow.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import logging
import secrets
import string
import sys
import webbrowser
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

# Claude Code's public OAuth client. These were confirmed against a current
# reference (binary analysis of the Claude Code CLI), per Plan 001's warning
# not to trust them from memory. The usage endpoint requires the user:profile
# scope; org:create_api_key + user:inference mirror what Claude Code requests.
_CLAUDE_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
_CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_CLAUDE_SCOPES = "org:create_api_key user:profile user:inference"
# Manual (no-port) flow: Claude's hosted callback renders the code as
# ``CODE#STATE`` for the operator to copy. Loopback flow uses a localhost
# redirect and reads the code straight off the query string.
_MANUAL_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
_TIMEOUT = 30.0

# PKCE verifier: unreserved chars per RFC 7636 (A-Z a-z 0-9 - . _ ~)
_VERIFIER_CHARS = string.ascii_letters + string.digits + "-._~"
_VERIFIER_LENGTH = 64


def _generate_verifier() -> str:
    return "".join(secrets.choice(_VERIFIER_CHARS) for _ in range(_VERIFIER_LENGTH))


def _generate_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _exchange_code(
    code: str,
    verifier: str,
    redirect_uri: str,
    state: str | None = None,
) -> tuple[str, str]:
    """Exchange an authorization code for access + refresh tokens.

    The redirect_uri must match the one sent to the authorize endpoint, and
    the public client_id must be included for a PKCE public-client exchange.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "client_id": _CLAUDE_CLIENT_ID,
    }
    if state is not None:
        data["state"] = state
    response = httpx.post(
        _CLAUDE_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    access_token: str = body["access_token"]
    refresh_token: str = body.get("refresh_token", "")
    return access_token, refresh_token


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler that captures the OAuth callback code."""

    code: str | None = None
    state: str | None = None
    error: str | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if "code" in params:
            _CallbackHandler.code = params["code"][0]
            _CallbackHandler.state = params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Login successful!</h1>"
                b"<p>You can close this tab and return to the terminal.</p>"
                b"</body></html>"
            )
        elif "error" in params:
            _CallbackHandler.error = params["error"][0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                f"<html><body><h1>Error: {params['error'][0]}</h1></body></html>".encode()
            )
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass  # silence request logs


def _parse_pasted_input(raw: str) -> tuple[str | None, str | None]:
    """Parse what the operator pastes back into ``(code, state)``.

    Accepts the full redirect URL (``?code=...&state=...``), Claude's hosted
    ``CODE#STATE`` form, or a bare code.
    """
    raw = raw.strip()
    if raw.startswith("http"):
        params = parse_qs(urlparse(raw).query)
        return params.get("code", [None])[0], params.get("state", [None])[0]
    if "#" in raw:
        code, _, state = raw.partition("#")
        return code or None, state or None
    return (raw or None), None


def _wait_for_code(port: int) -> tuple[str | None, str | None, str | None]:
    """Start a local HTTP server and wait for the OAuth callback.

    Returns ``(code, state, error)``.
    """
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 300  # 5 minutes
    while _CallbackHandler.code is None and _CallbackHandler.error is None:
        server.handle_request()
    server.server_close()
    return _CallbackHandler.code, _CallbackHandler.state, _CallbackHandler.error


def login_claude(port: int | None = None, no_browser: bool = False) -> None:
    """Run the PKCE OAuth login flow for Claude and print the token pair."""
    redirect_uri = (
        f"http://localhost:{port}/callback" if port is not None else _MANUAL_REDIRECT_URI
    )

    verifier = _generate_verifier()
    challenge = _generate_challenge(verifier)
    state = secrets.token_urlsafe(16)

    params = urlencode({
        "response_type": "code",
        "client_id": _CLAUDE_CLIENT_ID,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": redirect_uri,
        "scope": _CLAUDE_SCOPES,
        "state": state,
    })
    authorize_url = f"{_CLAUDE_AUTHORIZE_URL}?{params}"

    # Reset class state for repeated invocations (testing).
    _CallbackHandler.code = None
    _CallbackHandler.state = None
    _CallbackHandler.error = None

    returned_state: str | None
    if port is not None:
        print(f"Starting local callback server on port {port}...")
        print(f"Opening browser to:\n  {authorize_url}\n")
        if not no_browser:
            webbrowser.open(authorize_url)

        code, returned_state, error = _wait_for_code(port)
        if error:
            print(f"Authorization failed: {error}", file=sys.stderr)
            sys.exit(1)
        if code is None:
            print("Timed out waiting for authorization.", file=sys.stderr)
            sys.exit(1)
    else:
        print("Open this URL in a browser to authorize:\n")
        print(f"  {authorize_url}\n")
        print(
            "After authorizing, the page shows a code like CODE#STATE.\n"
            "Paste it here (the full CODE#STATE, or the redirect URL):"
        )
        raw = input("> ")
        code, returned_state = _parse_pasted_input(raw)
        if not code:
            print("No authorization code provided.", file=sys.stderr)
            sys.exit(1)

    # CSRF: the returned state must match what we sent (when the flow echoes
    # one back). A mismatch means the response isn't ours — refuse it.
    if returned_state is not None and returned_state != state:
        print("State mismatch — aborting (possible CSRF).", file=sys.stderr)
        sys.exit(1)

    try:
        access_token, refresh_token = _exchange_code(
            code, verifier, redirect_uri, state=state
        )
    except httpx.HTTPError as exc:
        print(f"Token exchange failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\nDedicated Claude OAuth tokens minted successfully.\n")
    print("Load these into the k8s Secret (server-secret.yaml):\n")
    print(f"  claude-token: \"{access_token}\"")
    print(f"  claude-refresh-token: \"{refresh_token}\"")
    print(f"  claude-client-id: \"{_CLAUDE_CLIENT_ID}\"")
    print()
    print("Then update the Secret keys and roll the server, e.g.:")
    print(
        "  kubectl -n usage-dashboard patch secret server-secrets --type merge -p \\\n"
        "    \"{\\\"stringData\\\":{\\\"claude-token\\\":\\\"$ACCESS\\\","
        "\\\"claude-refresh-token\\\":\\\"$REFRESH\\\","
        f"\\\"claude-client-id\\\":\\\"{_CLAUDE_CLIENT_ID}\\\"}}}}\""
    )
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
# Codex (OpenAI / ChatGPT-plan) login
#
# Mirrors the Claude PKCE flow against OpenAI's OAuth (auth.openai.com), using
# the public Codex CLI client. Endpoints/client_id/scopes were taken from the
# openai/codex source (not memory), same discipline as the Claude constants.
# Loopback-only: OpenAI's redirect allow-list expects http://localhost:1455/
# auth/callback, so there's no hosted "paste the code" page like Claude's.
# ---------------------------------------------------------------------------

_CODEX_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
_CODEX_TOKEN_URL_LOGIN = "https://auth.openai.com/oauth/token"
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_SCOPES = "openid profile email offline_access"
_CODEX_DEFAULT_PORT = 1455


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """Best-effort decode of a JWT's payload segment (no signature check)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _extract_codex_account_id(id_token: str) -> str | None:
    """Pull the chatgpt-account-id from the id_token's OpenAI auth claim."""
    payload = _decode_jwt_payload(id_token)
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        acc = auth.get("chatgpt_account_id") or auth.get("account_id")
        if acc:
            return str(acc)
    acc = payload.get("chatgpt_account_id")
    return str(acc) if acc else None


def _exchange_code_codex(
    code: str, verifier: str, redirect_uri: str
) -> tuple[str, str, str]:
    """Exchange an auth code for (access, refresh, id_token) — form-encoded."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": _CODEX_CLIENT_ID,
        "code_verifier": verifier,
    }
    response = httpx.post(
        _CODEX_TOKEN_URL_LOGIN,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    return (
        body["access_token"],
        body.get("refresh_token", ""),
        body.get("id_token", ""),
    )


def login_codex(port: int | None = None, no_browser: bool = False) -> None:
    """Run the PKCE OAuth login flow for Codex and print the token pair."""
    port = port or _CODEX_DEFAULT_PORT
    redirect_uri = f"http://localhost:{port}/auth/callback"

    verifier = _generate_verifier()
    challenge = _generate_challenge(verifier)
    state = secrets.token_urlsafe(16)

    params = urlencode({
        "response_type": "code",
        "client_id": _CODEX_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": _CODEX_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # These make the id_token carry the ChatGPT account/org, so we can
        # surface the chatgpt-account-id the usage endpoint needs.
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
    })
    authorize_url = f"{_CODEX_AUTHORIZE_URL}?{params}"

    _CallbackHandler.code = None
    _CallbackHandler.state = None
    _CallbackHandler.error = None

    print(f"Starting local callback server on port {port}...")
    print(f"Opening browser to:\n  {authorize_url}\n")
    if not no_browser:
        webbrowser.open(authorize_url)

    code, returned_state, error = _wait_for_code(port)
    if error:
        print(f"Authorization failed: {error}", file=sys.stderr)
        sys.exit(1)
    if code is None:
        print("Timed out waiting for authorization.", file=sys.stderr)
        sys.exit(1)
    if returned_state is not None and returned_state != state:
        print("State mismatch — aborting (possible CSRF).", file=sys.stderr)
        sys.exit(1)

    try:
        access_token, refresh_token, id_token = _exchange_code_codex(
            code, verifier, redirect_uri
        )
    except httpx.HTTPError as exc:
        print(f"Token exchange failed: {exc}", file=sys.stderr)
        sys.exit(1)

    account_id = _extract_codex_account_id(id_token)

    print("\nDedicated Codex OAuth tokens minted successfully.\n")
    print("Load these into the k8s Secret (server-secret.yaml):\n")
    print(f'  codex-token: "{access_token}"')
    print(f'  codex-refresh-token: "{refresh_token}"')
    print(f'  codex-client-id: "{_CODEX_CLIENT_ID}"')
    if account_id:
        print(f'  codex-account-id: "{account_id}"')
    else:
        print(
            "  codex-account-id: <not found in id_token — the usage endpoint "
            "may still work without it; set it if fetches 401/403>"
        )
    print()
    print("Then update the Secret keys and roll the server, e.g.:")
    acct = account_id or "$ACCOUNT"
    print(
        "  kubectl -n usage-dashboard patch secret server-secrets --type merge -p \\\n"
        "    \"{\\\"stringData\\\":{\\\"codex-token\\\":\\\"$ACCESS\\\","
        "\\\"codex-refresh-token\\\":\\\"$REFRESH\\\","
        f"\\\"codex-client-id\\\":\\\"{_CODEX_CLIENT_ID}\\\","
        f"\\\"codex-account-id\\\":\\\"{acct}\\\"}}}}\""
    )
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
        "provider", choices=["claude", "ollama", "codex"], help="Provider to log in to"
    )
    login_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="[claude] Local port for OAuth callback server (omit for manual paste); "
        "[codex] callback port (default 1455)",
    )
    login_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="[claude] Don't auto-open the browser (print URL instead)",
    )
    login_parser.add_argument(
        "--headless",
        action="store_true",
        help="[ollama] Run the browser headless (rarely clears WorkOS anti-bot)",
    )
    login_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="[ollama] Skip fetching the usage page to validate the cookie",
    )

    args = parser.parse_args()

    if args.command == "login":
        if args.provider == "claude":
            login_claude(port=args.port, no_browser=args.no_browser)
        elif args.provider == "ollama":
            login_ollama(headless=args.headless, verify=not args.no_verify)
        elif args.provider == "codex":
            login_codex(port=args.port, no_browser=args.no_browser)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
