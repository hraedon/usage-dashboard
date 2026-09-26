from __future__ import annotations

import hashlib
import logging
import os
import sys

import uvicorn

from usage_dashboard.server.api import create_app
from usage_dashboard.server.codex_app_server import (
    DEFAULT_CODEX_BIN,
    DEFAULT_CODEX_HOME,
    CodexAppServerClient,
    CodexAppServerConfig,
)
from usage_dashboard.server.db import Database
from usage_dashboard.server.enrolment import EnrolmentService
from usage_dashboard.server.schedule_config import ScheduleConfig
from usage_dashboard.server.scheduler import FetchScheduler
from usage_dashboard.server.token_store import TokenStore

logger = logging.getLogger(__name__)


def _resolve_claude_tokens(
    token_store: TokenStore,
    env_access: str | None,
    env_refresh: str | None,
    store_key: str = "claude",
) -> tuple[str | None, str | None]:
    """Decide which Claude tokens to run with (WI-001, revised by Plan 005).

    The token store is now **authoritative**. Claude credentials are created by
    ``usage-dashboard login claude``, which writes straight into the store, so
    the Secret is only ever a migration seed:

    * an empty entry is seeded once from the environment (a pre-Plan-005
      deployment carrying its credentials in ``server-secrets``);
    * once an entry exists, the environment can no longer displace it.

    The old behaviour re-seeded whenever the Secret's value differed from a
    recorded marker. That is unsafe now: the Secret keeps the originally
    provisioned tokens forever, so after enrolment writes a fresh credential, a
    restart would find the marker unchanged... but any hand-edit of the Secret
    would silently overwrite the newly enrolled credential with a stale pair
    that has already been rotated away. Only enrolment replaces a credential.

    *store_key* namespaces the credentials so a second account ("claude_work")
    resolves and persists independently of the primary one.
    """
    persisted_access, persisted_refresh = token_store.get(store_key)
    if persisted_access and persisted_refresh:
        return persisted_access, persisted_refresh

    if env_access and env_refresh:
        # One-time import for a deployment that predates enrolment.
        logger.info(
            "Seeding %s credentials from the environment into the empty token store",
            store_key,
        )
        token_store.save(store_key, env_access, env_refresh)
        return env_access, env_refresh

    return persisted_access or env_access, persisted_refresh or env_refresh


def _resolve_opencode_workspace(
    token_store: TokenStore,
    env_value: str | None,
) -> str | None:
    """Workspace id store-first (Plan 006): a pane-submitted ``wrk_…`` id
    persists beside the cookie, with the env var as the original seed."""
    stored = token_store.get_metadata("opencode", "workspace_id")
    return stored if isinstance(stored, str) and stored else env_value


def _resolve_cookie(
    token_store: TokenStore,
    env_cookie: str | None,
    store_key: str,
) -> str | None:
    """Decide which scraped session cookie to run with (WI-017).

    Mirrors ``_resolve_claude_tokens`` but for a single opaque credential
    string (no refresh pair): only (re)seed from the env when the Secret
    differs from what we last seeded (first boot or a deliberate re-login);
    otherwise prefer the persisted cookie so a pod restart keeps it.

    *store_key* namespaces the credential, so each cookie-authenticated
    provider (ollama, opencode) seeds and persists independently.
    """
    persisted = token_store.get_credential(store_key)

    if env_cookie:
        marker = hashlib.sha256(env_cookie.encode()).hexdigest()
        if marker != token_store.get_seed_marker(store_key):
            token_store.save_credential(store_key, env_cookie)
            token_store.set_seed_marker(store_key, marker)
            return env_cookie

    return persisted or env_cookie


def _build_codex_client(
    mode: str,
    home: str,
    binary: str,
) -> CodexAppServerClient | None:
    """Construct the Codex App Server client for *mode*, or None if disabled.

    ``app_server`` is the only enabled mode: the dashboard drives the official
    `codex` App Server. Anything else (including the default) leaves Codex
    unconfigured, so its tile is absent rather than showing a failure the
    operator never asked for.
    """
    if mode != "app_server":
        if mode not in ("disabled", ""):
            logger.warning(
                "Unknown CODEX_MODE %r; Codex is disabled. Expected 'app_server' "
                "or 'disabled'.",
                mode,
            )
        return None
    logger.info("Codex enabled in app_server mode (CODEX_HOME=%s, bin=%s)", home, binary)
    # persistent: one resident child for the pod's life. Explicit here because
    # it is a deployment-shaped decision — a child per poll leaks ~29 kB of
    # uncheckpointed SQLite WAL and a temp dir into CODEX_HOME each start,
    # which fills the PVC (and kills the readings DB with it) in about four
    # months. FetchScheduler.stop() closes it.
    return CodexAppServerClient(
        CodexAppServerConfig(binary=binary, home=home), persistent=True
    )


def _optional_int_env(name: str) -> int | None:
    """Parse an optional integer env var; unset/empty means None (use defaults)."""
    value = os.environ.get(name)
    return int(value) if value else None


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    api_key = os.environ.get("API_KEY", "")
    if not api_key:
        logger.error("API_KEY environment variable is required")
        sys.exit(1)

    db_path = os.environ.get("DB_PATH", "/data/readings.db")
    claude_token = os.environ.get("CLAUDE_TOKEN") or None
    claude_refresh_token = os.environ.get("CLAUDE_REFRESH_TOKEN") or None
    claude_client_id = os.environ.get("CLAUDE_CLIENT_ID") or None
    # Optional second Claude account (e.g. a work login). Absent unless its own
    # token is set — the dashboard then shows a muted second set of Claude bars.
    claude_work_token = os.environ.get("CLAUDE_WORK_TOKEN") or None
    claude_work_refresh_token = os.environ.get("CLAUDE_WORK_REFRESH_TOKEN") or None
    claude_work_client_id = os.environ.get("CLAUDE_WORK_CLIENT_ID") or None
    zai_api_key = os.environ.get("ZAI_API_KEY") or None
    # z.ai weekly-token thresholds (unset = the fetcher's defaults): the token
    # totals that colour the weekly-window line warn/crit. The weekly cap is
    # plan-dependent and empirically known, so these stay tunable without a
    # code change (defaults ≈ 80% / 95% of the Pro cap).
    zai_tokens_warn = _optional_int_env("ZAI_WEEK_TOKENS_WARN")
    zai_tokens_crit = _optional_int_env("ZAI_WEEK_TOKENS_CRIT")
    ollama_cookie = os.environ.get("OLLAMA_COOKIE") or None
    # Optional OpenCode Go workspace. Scraped like ollama, but needs two halves:
    # the workspace id (stable, `wrk_…`) and the `auth` browser cookie.
    opencode_workspace_id = os.environ.get("OPENCODE_WORKSPACE_ID") or None
    opencode_cookie = os.environ.get("OPENCODE_COOKIE") or None
    # Optional OpenAI Codex (ChatGPT-plan) account. Plan 005: the official
    # `codex` App Server owns the login, tokens and refresh; the dashboard
    # holds no OpenAI credential at all.
    codex_mode = (os.environ.get("CODEX_MODE") or "disabled").strip().lower()
    codex_home = os.environ.get("CODEX_HOME") or DEFAULT_CODEX_HOME
    codex_bin = os.environ.get("CODEX_BIN") or DEFAULT_CODEX_BIN
    # Optional Umans wallet key (Plan 004): adds the corner balance line on
    # the Pi; absent leaves the provider unconfigured.
    umans_key = os.environ.get("UMANS_API_KEY") or None
    fetch_interval = int(os.environ.get("FETCH_INTERVAL", "300"))
    failure_backoff_cap = int(os.environ.get("FAILURE_BACKOFF_CAP", "3600"))
    port = int(os.environ.get("PORT", "8080"))
    retention_days = int(os.environ.get("RETENTION_DAYS", "7"))

    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    database = Database(db_path)
    database.initialize()

    # Token store lives on the PVC alongside the DB.  Env-var tokens (from
    # the k8s Secret) seed it on first boot; refreshed tokens persist here
    # so pod restarts survive a rotation without touching the Secret.
    token_store = TokenStore(os.path.join(db_dir or "/data", "tokens.json"))

    # Raw env values, kept BEFORE resolution overwrites the locals: the
    # enrolment reload re-runs the same seed-from-env resolution, and it must
    # see the Secret's values, not the store's (Plan 006).
    env_credentials = {
        "claude_token": claude_token,
        "claude_refresh_token": claude_refresh_token,
        "claude_work_token": claude_work_token,
        "claude_work_refresh_token": claude_work_refresh_token,
        "ollama_cookie": ollama_cookie,
        "opencode_cookie": opencode_cookie,
        "opencode_workspace_id": opencode_workspace_id,
    }

    claude_token, claude_refresh_token = _resolve_claude_tokens(
        token_store, claude_token, claude_refresh_token
    )
    claude_work_token, claude_work_refresh_token = _resolve_claude_tokens(
        token_store, claude_work_token, claude_work_refresh_token,
        store_key="claude_work",
    )
    ollama_cookie = _resolve_cookie(token_store, ollama_cookie, "ollama")
    opencode_cookie = _resolve_cookie(token_store, opencode_cookie, "opencode")
    opencode_workspace_id = _resolve_opencode_workspace(token_store, opencode_workspace_id)
    codex_client = _build_codex_client(codex_mode, codex_home, codex_bin)
    # Rebuild hook for the enrolment pause/resume bracket (Plan 006): the
    # factory mirrors _build_codex_client's app_server construction exactly.
    codex_factory = (
        (
            lambda: CodexAppServerClient(
                CodexAppServerConfig(binary=codex_bin, home=codex_home), persistent=True
            )
        )
        if codex_client is not None
        else None
    )

    scheduler = FetchScheduler(
        db=database,
        claude_token=claude_token,
        claude_refresh_token=claude_refresh_token,
        claude_client_id=claude_client_id,
        claude_work_token=claude_work_token,
        claude_work_refresh_token=claude_work_refresh_token,
        claude_work_client_id=claude_work_client_id,
        zai_key=zai_api_key,
        zai_tokens_warn=zai_tokens_warn,
        zai_tokens_crit=zai_tokens_crit,
        ollama_cookie=ollama_cookie,
        opencode_workspace_id=opencode_workspace_id,
        opencode_cookie=opencode_cookie,
        codex_client=codex_client,
        codex_factory=codex_factory,
        umans_key=umans_key,
        interval_seconds=fetch_interval,
        failure_cap_seconds=failure_backoff_cap,
        token_store=token_store,
        retention_days=retention_days,
    )

    def _reload_credentials() -> None:
        """Adopt whatever enrolment just wrote, without a rollout (Plan 006).

        Re-runs the same store-authoritative resolution as startup — the
        Secret can still only seed an empty entry — and swaps the result
        into the running scheduler, then fetches so the tiles show it.
        """
        claude_t, claude_r = _resolve_claude_tokens(
            token_store,
            env_credentials["claude_token"],
            env_credentials["claude_refresh_token"],
        )
        work_t, work_r = _resolve_claude_tokens(
            token_store,
            env_credentials["claude_work_token"],
            env_credentials["claude_work_refresh_token"],
            store_key="claude_work",
        )
        scheduler.update_credentials(
            claude_token=claude_t,
            claude_refresh_token=claude_r,
            claude_work_token=work_t,
            claude_work_refresh_token=work_r,
            ollama_cookie=_resolve_cookie(
                token_store, env_credentials["ollama_cookie"], "ollama"
            ),
            opencode_cookie=_resolve_cookie(
                token_store, env_credentials["opencode_cookie"], "opencode"
            ),
            opencode_workspace_id=_resolve_opencode_workspace(
                token_store, env_credentials["opencode_workspace_id"]
            ),
        )

    def _after_enrolment(provider: str) -> None:
        if provider == "codex":
            # The resume bracket already rebuilt the client against the new
            # login; the next scheduled poll reads it.
            return
        _reload_credentials()
        scheduler.fetch_now()

    enrolment = EnrolmentService(
        token_store,
        claude_bin=os.environ.get("CLAUDE_BIN") or "claude",
        codex_home=codex_home,
        codex_bin=codex_bin,
        on_success=_after_enrolment,
        codex_pause=scheduler.pause_codex,
        codex_resume=scheduler.resume_codex,
    )

    app = create_app(
        api_key=api_key,
        db=database,
        configured_providers=scheduler.configured_providers(),
        schedule_config=ScheduleConfig.load(os.environ.get("SCHEDULES_DIR") or None),
        scheduler=scheduler,
        enrolment=enrolment,
    )

    scheduler.start()

    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    finally:
        scheduler.stop()


if __name__ == "__main__":
    main()
