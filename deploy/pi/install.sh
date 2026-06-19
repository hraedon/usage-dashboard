#!/usr/bin/env bash
# One-shot Pi bootstrap for the usage-dashboard touch GUI.
#
# Target: Raspberry Pi 4B + official Raspberry Pi Touch Display 2 (720x1280),
# Raspberry Pi OS Bookworm (Lite is fine — no desktop needed).
#
# Idempotent: safe to re-run. Run as the normal login user (NOT root); it calls
# sudo for the privileged bits. Override any default via env, e.g.:
#   DISPLAY_ROTATE=270 GUI_TOUCH_ROTATE=270 ./install.sh
#   REPO_URL=git@github.com:hraedon/usage-dashboard.git ./install.sh   # private
set -euo pipefail

# --- config (override via env) ---------------------------------------------
REPO_URL="${REPO_URL:-https://github.com/hraedon/usage-dashboard.git}"
APPDIR="${APPDIR:-$HOME/usage-dashboard}"
VENV="${VENV:-$APPDIR/.venv}"
RUNUSER="${RUNUSER:-$(id -un)}"
UPDATE_REF="${UPDATE_REF:-main}"
# Landscape by default (panel is portrait-native; 90 turns it to 1280x720).
# Touch rotation must match the display rotation. Use 0 for portrait.
DISPLAY_ROTATE="${DISPLAY_ROTATE:-90}"
GUI_TOUCH_ROTATE="${GUI_TOUCH_ROTATE:-$DISPLAY_ROTATE}"

CMDLINE="/boot/firmware/cmdline.txt"
ENV_FILE="/etc/usage-dashboard-gui.env"
UNIT_DIR="/etc/systemd/system"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" = 0 ]; then
    echo "Run this as your normal user, not root (it uses sudo as needed)." >&2
    exit 1
fi

echo "==> usage-dashboard Pi setup"
echo "    user=$RUNUSER  appdir=$APPDIR  ref=$UPDATE_REF"
echo "    display rotate=$DISPLAY_ROTATE  touch rotate=$GUI_TOUCH_ROTATE"
sudo -v   # prime sudo up front

# --- 1. system packages -----------------------------------------------------
echo "==> apt packages"
sudo apt-get update -qq
sudo apt-get install -y git python3-venv python3-pip libgbm1 libdrm2

# --- 2. groups for DRM + touch ---------------------------------------------
echo "==> groups (video render input)"
sudo usermod -aG video,render,input "$RUNUSER"

# --- 3. checkout ------------------------------------------------------------
if [ -d "$APPDIR/.git" ]; then
    echo "==> updating checkout at $APPDIR"
    git -C "$APPDIR" fetch --quiet origin "$UPDATE_REF"
    git -C "$APPDIR" checkout --quiet "$UPDATE_REF"
    git -C "$APPDIR" reset --hard --quiet "origin/$UPDATE_REF"
else
    echo "==> cloning $REPO_URL -> $APPDIR"
    git clone --quiet "$REPO_URL" "$APPDIR"
    git -C "$APPDIR" checkout --quiet "$UPDATE_REF"
fi

# --- 4. venv + install ------------------------------------------------------
# Bookworm marks the system Python externally-managed (PEP 668), so a venv is
# the supported way to install — `pip install --user` is refused there.
echo "==> venv + install (.[gui])"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
( cd "$APPDIR" && "$VENV/bin/pip" install --quiet -e '.[gui]' )

# --- 5. env file (never clobber an existing one) ----------------------------
if [ ! -f "$ENV_FILE" ]; then
    echo "==> installing $ENV_FILE (EDIT SERVER_URL + API_KEY)"
    sudo cp "$HERE/usage-dashboard-gui.env.example" "$ENV_FILE"
    sudo chown root:root "$ENV_FILE"
    sudo chmod 600 "$ENV_FILE"
else
    echo "==> $ENV_FILE exists, leaving it"
fi

# --- 6. display: rotation + no console blanking -----------------------------
echo "==> display config ($CMDLINE)"
add_cmdline_token() {
    local tok="$1"
    if grep -qF -- "$tok" "$CMDLINE"; then
        echo "    already set: $tok"
    else
        sudo cp "$CMDLINE" "$CMDLINE.bak.$(date +%s)"
        # cmdline.txt must stay a single line; append space-separated.
        sudo sed -i "1 s|\$| $tok|" "$CMDLINE"
        echo "    added: $tok"
    fi
}
if grep -q "video=DSI-1" "$CMDLINE" && ! grep -qF "rotate=$DISPLAY_ROTATE" "$CMDLINE"; then
    echo "    NOTE: a different video=DSI-1 line is already present; leaving it."
    echo "          Edit $CMDLINE by hand if the rotation is wrong."
else
    add_cmdline_token "video=DSI-1:720x1280@60,rotate=$DISPLAY_ROTATE"
fi
add_cmdline_token "consoleblank=0"

# --- 7. systemd units (fill placeholders, install, enable) ------------------
echo "==> systemd units"
render_unit() {  # render_unit <src> <dst>
    sed -e "s|@RUNUSER@|$RUNUSER|g" \
        -e "s|@APPDIR@|$APPDIR|g" \
        -e "s|@VENV@|$VENV|g" \
        -e "s|@TOUCH_ROTATE@|$GUI_TOUCH_ROTATE|g" \
        "$1" | sudo tee "$2" >/dev/null
}
render_unit "$HERE/usage-dashboard-gui.service"    "$UNIT_DIR/usage-dashboard-gui.service"
render_unit "$HERE/usage-dashboard-update.service" "$UNIT_DIR/usage-dashboard-update.service"
sudo cp "$HERE/usage-dashboard-update.timer" "$UNIT_DIR/usage-dashboard-update.timer"

# Stable copy of the updater so a git reset can't rewrite the running script.
sed -e "s|@APPDIR@|$APPDIR|g" -e "s|@VENV@|$VENV|g" \
    "$HERE/update.sh" | sudo tee /usr/local/bin/usage-dashboard-update >/dev/null
sudo chmod 755 /usr/local/bin/usage-dashboard-update

# Let the updater restart the GUI without a password (scoped to that one verb).
echo "$RUNUSER ALL=(root) NOPASSWD: /usr/bin/systemctl restart usage-dashboard-gui.service" \
    | sudo tee /etc/sudoers.d/usage-dashboard-update >/dev/null
sudo chmod 440 /etc/sudoers.d/usage-dashboard-update

sudo systemctl daemon-reload
sudo systemctl enable --now usage-dashboard-gui.service
sudo systemctl enable --now usage-dashboard-update.timer

echo
echo "==> done."
echo "    1. Edit $ENV_FILE  (set SERVER_URL + API_KEY), then:"
echo "         sudo systemctl restart usage-dashboard-gui"
echo "    2. Reboot once to apply the display rotation:  sudo reboot"
echo "    Logs:    journalctl -u usage-dashboard-gui -f"
echo "    Updates: systemctl list-timers usage-dashboard-update.timer"
