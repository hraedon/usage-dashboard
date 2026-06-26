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
REF="main"
if [ -f "$ENV_FILE" ]; then
    val="$(grep -E '^UPDATE_REF=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"' || true)"
    [ -n "$val" ] && REF="$val"
fi

cd "$APPDIR"
git fetch --quiet origin "$REF"
local_rev="$(git rev-parse HEAD)"
remote_rev="$(git rev-parse "origin/$REF")"

if [ "$local_rev" = "$remote_rev" ]; then
    log "up to date ($REF @ ${local_rev:0:8})"
    write_check up-to-date "$local_rev"
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
log "done (now at ${remote_rev:0:8})"
