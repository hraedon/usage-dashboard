#!/usr/bin/env bash
# Auto-updater for the usage-dashboard touch GUI.
#
# install.sh seds @APPDIR@/@VENV@ and copies this to
# /usr/local/bin/usage-dashboard-update (a stable path, so a `git reset` of the
# checkout can't rewrite the script while it's running). Driven by the
# usage-dashboard-update.timer. Only restarts the GUI when the tracked ref
# actually moved and the new code installs + imports cleanly.
set -euo pipefail

APPDIR="@APPDIR@"
VENV="@VENV@"
SERVICE="usage-dashboard-gui.service"
ENV_FILE="/etc/usage-dashboard-gui.env"

log() { echo "usage-dashboard-update: $*"; }

# Read a knob: the systemd-injected environment first, the env file only as a
# fallback. The unit carries EnvironmentFile=, so systemd reads the 600
# root:root file as root and hands the values down. Grepping the file directly
# CANNOT work from the unprivileged run user — it fails "Permission denied",
# `|| true` swallows it, and the knob silently reads empty. That is how
# AUTO_REDEPLOY appeared to do nothing once set, and why UPDATE_REF had never
# taken effect on any unit. The file fallback is kept for a hand-run invocation
# as root, where the environment is not populated.
env_or_file() {  # name
    local name="$1" val=""
    val="$(printenv "$name" 2>/dev/null || true)"
    if [ -z "$val" ] && [ -r "$ENV_FILE" ]; then
        val="$(grep -E "^${name}=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"' || true)"
    fi
    printf '%s' "$val"
}

# Opt-in self-redeploy of the installer-managed components (update.sh itself, the
# systemd units, the X-session launcher, the touch-rebind helper). Off unless
# AUTO_REDEPLOY=1 in the env file, so a unit only starts re-applying root-owned
# files once an operator opts in. Best-effort: never fails the updater. Idempotent
# (a no-op when nothing drifted), so it's safe to call on every cycle.
maybe_redeploy() {
    local want=""
    want="$(env_or_file AUTO_REDEPLOY)"
    case "$want" in
        1|true|yes|on)
            if [ -x /usr/local/bin/usage-dashboard-redeploy ]; then
                log "auto-redeploy: re-applying installer-managed components"
                sudo /usr/local/bin/usage-dashboard-redeploy || log "auto-redeploy failed"
            else
                log "auto-redeploy enabled but helper not installed (re-run install.sh once)"
            fi
            ;;
    esac
}

# Status breadcrumbs the touch GUI reads for its diagnostics overlay. Best-effort
# (never abort the updater on a write failure). Keep this dir in lockstep with
# diagnostics.default_state_dir() on the Python side.
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/usage-dashboard"
now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
write_check() {  # result, commit
    mkdir -p "$STATE_DIR" 2>/dev/null || return 0
    printf '%s %s %s\n' "$(now_utc)" "$1" "${2:0:8}" \
        > "$STATE_DIR/update-last-check" 2>/dev/null || true
}
write_change() {  # old, new
    mkdir -p "$STATE_DIR" 2>/dev/null || return 0
    printf '%s %s %s\n' "$(now_utc)" "${1:0:8}" "${2:0:8}" \
        > "$STATE_DIR/update-last-change" 2>/dev/null || true
}

# Tracked ref: UPDATE_REF from the env file, else main.
REF="$(env_or_file UPDATE_REF)"
[ -n "$REF" ] || REF="main"

cd "$APPDIR"
local_rev="$(git rev-parse HEAD)"

# Resolve the tracked ref BEFORE anything that can abort the run (WI-031).
#
# A pinned ref that no longer exists is the realistic case, not a typo: merged
# PR branches are deleted, so pinning a unit to a feature branch to stage a
# rollout invalidates the pin at the moment the PR merges. Left bare under
# `set -e`, `git fetch` then killed the script *upstream of* write_check and
# maybe_redeploy, so three things happened at once and none were visible: the
# unit stopped updating, it stopped self-correcting its infra, and the GUI
# diagnostics overlay kept showing the last good timestamp — a stalled unit
# looked exactly like a healthy idle one, and only journalctl on the unit
# could tell them apart.
#
# Policy: a pin is honoured, so we do NOT silently fall back to main — a
# deliberate hold must stay held. But the failure is made loud, a breadcrumb
# is written so the panel can show "stalled" rather than merely "stale", and
# auto-redeploy still runs, because infra self-correction has nothing to do
# with whether the app ref resolves.
if ! git fetch --quiet origin "$REF"; then
    log "ERROR: tracked ref '$REF' cannot be resolved on origin"
    log "       (deleted upstream, or a typo). This unit is STALLED at"
    log "       ${local_rev:0:8} and will not update until UPDATE_REF is fixed"
    log "       in $ENV_FILE. Merged PR branches are deleted — pin to a tag."
    write_check bad-ref "$local_rev"
    maybe_redeploy  # a bad app pin must not disable infra self-correction
    exit 1
fi

# Resolve what was just fetched via FETCH_HEAD rather than "origin/$REF".
# `git fetch origin <ref>` sets FETCH_HEAD for branches, tags and HEAD alike,
# whereas a remote-tracking "origin/<ref>" only exists for branches — so the
# tag pinning that deploy/pi/README.md and the env example both advertise
# ("pin to a tag to freeze a fleet on a known-good release") could never have
# worked: rev-parse would fail on origin/v0.2.0 and kill the updater. ^{commit}
# dereferences an annotated tag to the commit it points at.
remote_rev="$(git rev-parse "FETCH_HEAD^{commit}")"

if [ "$local_rev" = "$remote_rev" ]; then
    log "up to date ($REF @ ${local_rev:0:8})"
    write_check up-to-date "$local_rev"
    maybe_redeploy  # in case infra drifted or AUTO_REDEPLOY was just enabled
    exit 0
fi

log "updating $REF: ${local_rev:0:8} -> ${remote_rev:0:8}"
git reset --hard --quiet "$remote_rev"

rollback() {
    log "rolling back to ${local_rev:0:8}"
    git reset --hard --quiet "$local_rev"
    "$VENV/bin/pip" install --quiet -e '.[gui]' || true
}

if ! "$VENV/bin/pip" install --quiet -e '.[gui]'; then
    log "pip install failed"
    rollback
    write_check pip-failed "$local_rev"  # running rev after rollback
    exit 1
fi

# Smoke check the new code before swapping the running app.
if ! "$VENV/bin/python" -c 'import usage_dashboard.client.gui' 2>/dev/null; then
    log "import smoke check failed"
    rollback
    write_check import-failed "$local_rev"  # running rev after rollback
    exit 1
fi

log "restarting $SERVICE"
sudo systemctl restart "$SERVICE"
write_check updated "$remote_rev"
write_change "$local_rev" "$remote_rev"
maybe_redeploy  # the new checkout may carry infra changes too
log "done (now at ${remote_rev:0:8})"
