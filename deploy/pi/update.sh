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
    exit 1
fi

# Smoke check the new code before swapping the running app.
if ! "$VENV/bin/python" -c 'import usage_dashboard.client.gui' 2>/dev/null; then
    log "import smoke check failed"
    rollback
    exit 1
fi

log "restarting $SERVICE"
sudo systemctl restart "$SERVICE"
log "done (now at ${remote_rev:0:8})"
