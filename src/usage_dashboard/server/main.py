from __future__ import annotations

import hashlib
import logging
import os
import sys

import uvicorn

from usage_dashboard.server.api import create_app
from usage_dashboard.server.claude_profile import (
    DEFAULT_CLAUDE_BIN,
    ClaudeProfileConfig,
    is_enrolled,
    profile_dir,
    read_credential,
)
from usage_dashboard.server.cli_home import home_lock, recover_interrupted_swap
from usage_dashboard.server.codex_app_server import (
    DEFAULT_CODEX_BIN,
    DEFAULT_CODEX_HOME,
    CodexAppServerClient,
    CodexAppServerConfig,
)
from usage_dashboard.server.db import Database
from usage_dashboard.server.enrolment import EnrolmentService
from usage_dashboard.server.fetch_antigravity import (
    DEFAULT_AGY_BIN,
    DEFAULT_ANTIGRAVITY_HOME,
    AntigravityConfig,
)
from usage_dashboard.server.fetch_types import FetchError
from usage_dashboard.server.schedule_config import ScheduleConfig
from usage_dashboard.server.scheduler import FetchScheduler
from usage_dashboard.server.token_store import TokenStore

logger = logging.getLogger(__name__)


def _resolve_claude_profile(
    token_store: TokenStore, store_key: str, claude_bin: str
) -> ClaudeProfileConfig | None:
    """Decide whether a Claude account is configured (Plan 009).

    The official CLI owns the credential under the account's persistent
    ``CLAUDE_CONFIG_DIR``; the account is configured exactly when that profile
    holds a credential file. Re-run after enrolment, so a new sign-in is
    adopted without a rollout.

    The token store no longer holds Claude OAuth tokens, and a pre-Plan-009
    entry is never *used*. It does still matter in two ways:

    * With no profile yet, it marks an account that was configured before the
      upgrade. That account stays configured (its tile reads "login required")
      rather than silently vanishing, until it is migrated or re-enrolled.
    * With a profile, the entry is kept until the profile's refresh token
      differs from it, which proves the CLI has refreshed the migrated
      credential at least once. Until then it is the only fallback copy of a
      working pair, so pruning it on sight could destroy the last good login.
    """
    config_dir = profile_dir(store_key)
    config = ClaudeProfileConfig(config_dir=config_dir, binary=claude_bin)
    with home_lock(config_dir):
        # A swap that died between its renames must be settled before the
        # profile is judged absent, or a restart would drop the account.
        recover_interrupted_swap(config_dir)
        enrolled = is_enrolled(config_dir)
    _, legacy_refresh = token_store.get(store_key)
    if not enrolled:
        if legacy_refresh:
            logger.warning(
                "A pre-Plan-009 %s credential is in the token store but there is "
                "no Claude CLI profile at %s; it is not used. Migrate it with "
                "scripts/migrate_claude_store_to_profile.py or re-enrol on /login",
                store_key,
                config_dir,
            )
            return config
        return None
    if token_store.has_entry(store_key):
        _prune_legacy_claude_entry(token_store, store_key, config_dir, legacy_refresh)
    return config


def _prune_legacy_claude_entry(
    token_store: TokenStore,
    store_key: str,
    config_dir: str,
    legacy_refresh: str | None,
) -> None:
    """Drop a pre-Plan-009 store entry once the CLI has demonstrably taken over."""
    if legacy_refresh:
        try:
            current = read_credential(config_dir)
        except FetchError:
            return  # unreadable profile: keep the fallback copy
        if current.refresh_token == legacy_refresh:
            logger.info(
                "Keeping the pre-Plan-009 %s store entry until the CLI first "
                "refreshes the migrated credential",
                store_key,
            )
            return
    token_store.remove(store_key)
    logger.info(
        "Removed the pre-Plan-009 %s entry from the token store; the CLI "
        "profile at %s owns the credential",
        store_key,
        config_dir,
    )


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

    For a single opaque credential string: only (re)seed from the env when the Secret
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


def _build_antigravity_config(mode: str, home: str, binary: str) -> AntigravityConfig | None:
    """The Antigravity CLI config for *mode*, or None if disabled (Plan 007).

    ``cli`` is the only enabled mode. As with Codex, anything else leaves the
    provider unconfigured, so its tile is absent rather than showing a
    failure the operator never asked for.
    """
    if mode != "cli":
        if mode not in ("disabled", ""):
            logger.warning(
                "Unknown ANTIGRAVITY_MODE %r; Antigravity is disabled. Expected "
                "'cli' or 'disabled'.",
                mode,
            )
        return None
    logger.info("Antigravity enabled in cli mode (home=%s, bin=%s)", home, binary)
    return AntigravityConfig(binary=binary, home=home)


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
    # Claude (Plan 009): the official CLI owns each account's login and
    # refresh under a persistent CLAUDE_CONFIG_DIR on the PVC. The optional
    # work account is configured only once it has been enrolled; the dashboard
    # then shows a muted second set of Claude bars.
    claude_bin = os.environ.get("CLAUDE_BIN") or DEFAULT_CLAUDE_BIN
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
    # Optional Google Antigravity quota (Plan 007). The official `agy` CLI
    # owns the Google login under ANTIGRAVITY_HOME; the dashboard holds none.
    antigravity_mode = (os.environ.get("ANTIGRAVITY_MODE") or "disabled").strip().lower()
    antigravity_home = os.environ.get("ANTIGRAVITY_HOME") or DEFAULT_ANTIGRAVITY_HOME
    agy_bin = os.environ.get("AGY_BIN") or DEFAULT_AGY_BIN
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

    # Token store lives on the PVC alongside the DB: scraped session cookies
    # (seeded from the k8s Secret) and the OpenCode workspace id. Claude's
    # credentials live in CLI profiles beside it (Plan 009), not here.
    token_store = TokenStore(os.path.join(db_dir or "/data", "tokens.json"))

    # Raw env values, kept BEFORE resolution overwrites the locals: the
    # enrolment reload re-runs the same seed-from-env resolution, and it must
    # see the Secret's values, not the store's (Plan 006).
    env_credentials = {
        "ollama_cookie": ollama_cookie,
        "opencode_cookie": opencode_cookie,
        "opencode_workspace_id": opencode_workspace_id,
    }

    claude_profile = _resolve_claude_profile(token_store, "claude", claude_bin)
    claude_work_profile = _resolve_claude_profile(token_store, "claude_work", claude_bin)
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

    antigravity_config = _build_antigravity_config(
        antigravity_mode, antigravity_home, agy_bin
    )

    scheduler = FetchScheduler(
        db=database,
        claude_profile=claude_profile,
        claude_work_profile=claude_work_profile,
        zai_key=zai_api_key,
        zai_tokens_warn=zai_tokens_warn,
        zai_tokens_crit=zai_tokens_crit,
        ollama_cookie=ollama_cookie,
        opencode_workspace_id=opencode_workspace_id,
        opencode_cookie=opencode_cookie,
        codex_client=codex_client,
        codex_factory=codex_factory,
        umans_key=umans_key,
        antigravity_config=antigravity_config,
        interval_seconds=fetch_interval,
        failure_cap_seconds=failure_backoff_cap,
        retention_days=retention_days,
    )

    def _reload_credentials() -> None:
        """Adopt whatever enrolment just wrote, without a rollout (Plan 006).

        Re-runs the same store-authoritative resolution as startup — the
        Secret can still only seed an empty entry — and swaps the result
        into the running scheduler, then fetches so the tiles show it.
        """
        scheduler.update_credentials(
            claude_profile=_resolve_claude_profile(token_store, "claude", claude_bin),
            claude_work_profile=_resolve_claude_profile(
                token_store, "claude_work", claude_bin
            ),
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
        if provider == "antigravity":
            # The ceremony swapped a verified login into ANTIGRAVITY_HOME;
            # there is no credential to reload, only a fetch to run.
            scheduler.fetch_now()
            return
        _reload_credentials()
        scheduler.fetch_now()

    enrolment = EnrolmentService(
        token_store,
        claude_bin=claude_bin,
        codex_home=codex_home,
        codex_bin=codex_bin,
        on_success=_after_enrolment,
        codex_pause=scheduler.pause_codex,
        codex_resume=scheduler.resume_codex,
        antigravity_home=antigravity_home if antigravity_config is not None else None,
        agy_bin=agy_bin,
    )

    app = create_app(
        api_key=api_key,
        db=database,
        configured_providers=scheduler.configured_providers,
        schedule_config=ScheduleConfig.load(os.environ.get("SCHEDULES_DIR") or None),
        scheduler=scheduler,
        enrolment=enrolment,
        agent_api_key=os.environ.get("AGENT_API_KEY") or None,
    )

    scheduler.start()

    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    finally:
        scheduler.stop()


if __name__ == "__main__":
    main()
