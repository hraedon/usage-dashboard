"""CLI entry point for usage-dashboard.

Provides the `usage-dashboard login claude` command for minting a dedicated
OAuth token pair via the Authorization Code + PKCE flow.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import logging
import secrets
import string
import sys
import webbrowser
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

_CLAUDE_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
_CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_CLAUDE_CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
_CLAUDE_SCOPES = "user:profile user:inference"
_REDIRECT_URI = "http://localhost/callback"
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
) -> tuple[str, str]:
    """Exchange an authorization code for access + refresh tokens."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
    }
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
    error: str | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if "code" in params:
            _CallbackHandler.code = params["code"][0]
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


def _wait_for_code(port: int) -> tuple[str | None, str | None]:
    """Start a local HTTP server and wait for the OAuth callback."""
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 300  # 5 minutes
    while _CallbackHandler.code is None and _CallbackHandler.error is None:
        server.handle_request()
    server.server_close()
    return _CallbackHandler.code, _CallbackHandler.error


def login_claude(port: int | None = None, no_browser: bool = False) -> None:
    """Run the PKCE OAuth login flow for Claude and print the token pair."""
    redirect_uri = _REDIRECT_URI
    if port is not None:
        redirect_uri = f"http://localhost:{port}/callback"

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
    _CallbackHandler.error = None

    if port is not None:
        print(f"Starting local callback server on port {port}...")
        print(f"Opening browser to:\n  {authorize_url}\n")
        if not no_browser:
            webbrowser.open(authorize_url)

        code, error = _wait_for_code(port)
        if error:
            print(f"Authorization failed: {error}", file=sys.stderr)
            sys.exit(1)
        if code is None:
            print("Timed out waiting for authorization.", file=sys.stderr)
            sys.exit(1)
    else:
        print("Open this URL in a browser to authorize:\n")
        print(f"  {authorize_url}\n")
        print("After authorizing, paste the full redirect URL (or just the code):")
        raw = input("> ").strip()
        # Accept either the full redirect URL or bare code.
        if raw.startswith("http"):
            parsed = urlparse(raw)
            params_dict = parse_qs(parsed.query)
            code = params_dict.get("code", [None])[0]
        else:
            code = raw
        if not code:
            print("No authorization code provided.", file=sys.stderr)
            sys.exit(1)

    try:
        access_token, refresh_token = _exchange_code(code, verifier, redirect_uri)
    except httpx.HTTPError as exc:
        print(f"Token exchange failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\nDedicated Claude OAuth tokens minted successfully.\n")
    print("Load these into the k8s Secret (server-secret.yaml):\n")
    print(f"  claude-token: \"{access_token}\"")
    print(f"  claude-refresh-token: \"{refresh_token}\"")
    print(f"  claude-client-id: \"{_CLAUDE_CLIENT_ID}\"")
    print()
    print("Then apply:  kubectl apply -f k8s/server-secret.yaml")
    print("             kubectl rollout restart deployment/server -n usage-dashboard")


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

    login_parser = sub.add_parser("login", help="OAuth login for a provider")
    login_parser.add_argument("provider", choices=["claude"], help="Provider to log in to")
    login_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Local port for OAuth callback server (omit for manual code paste)",
    )
    login_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't auto-open the browser (print URL instead)",
    )

    args = parser.parse_args()

    if args.command == "login":
        if args.provider == "claude":
            login_claude(port=args.port, no_browser=args.no_browser)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
