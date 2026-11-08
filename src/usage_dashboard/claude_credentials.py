"""Read a credential written by the official Claude Code CLI (Plan 005).

This is an intentionally **quarantined compatibility layer**. Claude Code's
``.credentials.json`` is not a documented interface, so everything that knows
its shape lives here, fails closed on anything unexpected, and names the
installed CLI version in the error so the next person knows what to compare
against.

Schema as emitted by ``claude`` 2.1.276 on Linux (read from a real ``/login``,
not from documentation)::

    {"claudeAiOauth": {
        "accessToken":           "...",
        "refreshToken":          "...",
        "expiresAt":             1789832809000,
        "refreshTokenExpiresAt": 1792424809000,
        "scopes":                ["user:inference", "user:profile", ...],
        "subscriptionType":      "pro",
        "rateLimitTier":         "default_claude_ai"}}

Note there is **no client id** in the file. The token endpoint nonetheless
REQUIRES one on every ``refresh_token`` grant: since 2026-11-07 (observed)
``platform.claude.com/v1/oauth/token`` answers a client-id-less JSON refresh
with ``400 invalid_request_error "Invalid request format"`` — the CLI's own
refresh payload always carries ``client_id``. So an imported credential
persists :data:`CLAUDE_CODE_CLIENT_ID` as store metadata, and the scheduler
sends it when refreshing. Every credential this system can hold belongs to
that public client: enrolment runs the official CLI's login, and the
pre-Plan-005 custom flow (Plan 001) used the same public client id.

``user:profile`` is what ``GET /api/oauth/usage`` requires, and it is the scope
``claude setup-token`` cannot grant — which is why enrolment uses the full
``/login`` ceremony. A real interactive login does carry it (verified).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The key the CLI nests the OAuth credential under.
_OAUTH_KEY = "claudeAiOauth"
# Required for GET /api/oauth/usage; `claude setup-token` cannot grant it.
REQUIRED_SCOPE = "user:profile"

# The Claude Code CLI's public OAuth client id, used as the client_id of every
# refresh_token grant this system makes. Extracted from the pinned CLI binary
# (2.1.276: CLIENT_ID beside the oauth callback route — the same analysis
# Plan 001 used for the pre-Plan-005 flow), NOT a guess: the token endpoint
# rejects a refresh without it (400 "Invalid request format", observed
# 2026-11-07 when Plan 005 Phase C removed the CLAUDE_CLIENT_ID env that had
# been supplying one). It is not a secret — it ships inside every Claude Code
# install.
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


class ClaudeCredentialError(Exception):
    """The credential file was missing, unreadable, or of an unknown shape."""


@dataclass(frozen=True)
class ClaudeCredential:
    """One imported Claude Code OAuth credential."""

    access_token: str
    refresh_token: str
    expires_at: int | None = None
    scopes: tuple[str, ...] = ()
    subscription_type: str | None = None
    client_id: str | None = None

    @property
    def scopes_known(self) -> bool:
        """True when the file carried scope metadata to check against."""
        return bool(self.scopes)

    @property
    def has_profile_scope(self) -> bool:
        return REQUIRED_SCOPE in self.scopes

    def metadata(self) -> dict[str, Any]:
        """Optional fields to persist alongside the token pair.

        The client id falls back to :data:`CLAUDE_CODE_CLIENT_ID` because the
        credential file carries none and the token endpoint requires one on
        refresh; a value parsed from a future CLI version still wins.

        Other ``None`` values are meaningful: :meth:`TokenStore.save` removes
        those keys, so re-enrolling with a credential that no longer carries a
        field does not leave a stale one behind.
        """
        return {
            "client_id": self.client_id or CLAUDE_CODE_CLIENT_ID,
            "expires_at": self.expires_at,
            "scopes": list(self.scopes) or None,
            "subscription_type": self.subscription_type,
        }


def _unknown(detail: str, cli_version: str | None) -> ClaudeCredentialError:
    version = cli_version or "unknown"
    return ClaudeCredentialError(
        f"Unrecognised Claude Code credential schema: {detail}. "
        f"Installed Claude Code version: {version}. "
        "Compare against the schema documented in claude_credentials.py before "
        "changing this parser."
    )


def parse_credentials(raw: object, *, cli_version: str | None = None) -> ClaudeCredential:
    """Parse the contents of ``.credentials.json``, failing closed.

    Anything that is not recognisably the documented shape raises
    :class:`ClaudeCredentialError` naming *cli_version* — an unknown structure
    must never be silently accepted, because the result would be a credential
    the dashboard cannot refresh.
    """
    if not isinstance(raw, dict):
        raise _unknown("top level is not a JSON object", cli_version)

    oauth = raw.get(_OAUTH_KEY)
    if not isinstance(oauth, dict):
        raise _unknown(f"no {_OAUTH_KEY!r} object at the top level", cli_version)

    access = oauth.get("accessToken")
    refresh = oauth.get("refreshToken")
    if not isinstance(access, str) or not access:
        raise _unknown(f"{_OAUTH_KEY}.accessToken is missing or not a string", cli_version)
    if not isinstance(refresh, str) or not refresh:
        raise _unknown(f"{_OAUTH_KEY}.refreshToken is missing or not a string", cli_version)

    raw_scopes = oauth.get("scopes")
    if raw_scopes is None:
        scopes: tuple[str, ...] = ()
    elif isinstance(raw_scopes, list) and all(isinstance(s, str) for s in raw_scopes):
        scopes = tuple(raw_scopes)
    else:
        raise _unknown(f"{_OAUTH_KEY}.scopes is not a list of strings", cli_version)

    expires_at = oauth.get("expiresAt")
    if expires_at is not None and (
        isinstance(expires_at, bool) or not isinstance(expires_at, int)
    ):
        raise _unknown(f"{_OAUTH_KEY}.expiresAt is not an integer", cli_version)

    subscription = oauth.get("subscriptionType")
    if subscription is not None and not isinstance(subscription, str):
        raise _unknown(f"{_OAUTH_KEY}.subscriptionType is not a string", cli_version)

    # Not emitted by 2.1.276, but accepted if a later version adds it.
    client_id = oauth.get("clientId")
    if client_id is not None and not isinstance(client_id, str):
        raise _unknown(f"{_OAUTH_KEY}.clientId is not a string", cli_version)

    return ClaudeCredential(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        scopes=scopes,
        subscription_type=subscription,
        client_id=client_id,
    )
