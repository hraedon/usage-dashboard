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

Note there is **no client id**. That is fine: ``refresh_claude_token`` omits
``client_id`` from the refresh payload when it is None, which is the path a
credential imported from here takes.

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

        ``None`` values are meaningful: :meth:`TokenStore.save` removes those
        keys, so re-enrolling with a credential that no longer carries a client
        id does not leave the old one behind.
        """
        return {
            "client_id": self.client_id,
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
