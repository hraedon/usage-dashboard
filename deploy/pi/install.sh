#!/usr/bin/env bash
# One-shot Pi bootstrap for the usage-dashboard touch GUI.
#
# Target: Raspberry Pi 4B + official Raspberry Pi Touch Display 2 (720x1280),
# Raspberry Pi OS *Trixie* (Lite is fine). Trixie ships Python 3.13; the package
# requires >=3.12, so Bookworm's 3.11 will NOT install it without a newer Python.
#
# The GUI runs under a *minimal X server* (xinit), not bare KMS/DRM: SDL's
# kmsdrm backend cannot present to this panel (black screen on every OS we
# tried). install.sh sets up that X session, the landscape rotation, and a
# workaround service for the Goodix touch controller's boot probe race.
#
# Idempotent: safe to re-run (re-execs from a stable copy before the git
# checkout so a `git reset --hard` can't rewrite the running script). Run
# as the normal login user (NOT root); it calls sudo for the privileged
# bits. Override any default via env, e.g.:
#   XRANDR_ROTATE=left ./install.sh                                   # other way
#   REPO_URL=git@github.com:hraedon/usage-dashboard.git ./install.sh   # private
set -euo pipefail

# --- config (override via env) ---------------------------------------------
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_URL="${REPO_URL:-https://github.com/hraedon/usage-dashboard.git}"
# Default the app dir to wherever this checkout already lives, so it doesn't
# matter which directory you cloned it into. Falls back to ~/usage-dashboard.
APPDIR="${APPDIR:-$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null || echo "$HOME/usage-dashboard")}"
VENV="${VENV:-$APPDIR/.venv}"
RUNUSER="${RUNUSER:-$(id -un)}"
UPDATE_REF="${UPDATE_REF:-main}"
# Landscape rotation, applied under X by xrandr (the panel is portrait-native).
# One of: normal | left | right | inverted. Touch follows automatically.
XRANDR_ROTATE="${XRANDR_ROTATE:-right}"

# Bookworm/Trixie put these under /boot/firmware; older images use /boot.
CMDLINE="/boot/firmware/cmdline.txt"
[ -f "$CMDLINE" ] || CMDLINE="/boot/cmdline.txt"
ENV_FILE="/etc/usage-dashboard-gui.env"
UNIT_DIR="/etc/systemd/system"

if [ "$(id -u)" = 0 ]; then
    echo "Run this as your normal user, not root (it uses sudo as needed)." >&2
    exit 1
fi

echo "==> usage-dashboard Pi setup"
echo "    user=$RUNUSER  appdir=$APPDIR  ref=$UPDATE_REF  rotate=$XRANDR_ROTATE"
# Prime sudo. Try the passwordless path first: a headless install over SSH (no
# tty) where the user has NOPASSWD for commands but the %sudo group rule still
# requires a password would otherwise fail here — `sudo -v` has no command to
# match, so it falls through to the password-required group rule.
sudo -n true 2>/dev/null || sudo -v

# --- 1. system packages -----------------------------------------------------
echo "==> apt packages"
sudo apt-get update -qq
# git/venv for the app; the mesa GBM/EGL/GLES stack for GL; and a minimal X
# server + libinput input driver + xinit/xrandr/xinput for the X session that
# actually drives this panel. xserver-xorg-legacy + the Xwrapper config below
# let xinit start X from a systemd service that has no controlling tty.
sudo apt-get install -y \
    git python3-venv python3-pip \
    libgbm1 libdrm2 libegl1 libegl-mesa0 libgles2 \
    xserver-xorg-core xserver-xorg-legacy xserver-xorg-input-libinput \
    xinit x11-xserver-utils xinput

# --- 2. groups for DRM + touch ---------------------------------------------
echo "==> groups (video render input)"
sudo usermod -aG video,render,input "$RUNUSER"

# --- 3. allow xinit to start X without a console session --------------------
# The GUI service runs xinit over systemd (no tty/seat); permit the setuid
# Xorg.wrap to start X for it.
echo "==> Xwrapper.config (allow headless X start)"
sudo install -d /etc/X11
printf 'allowed_users=anybody\nneeds_root_rights=yes\n' \
    | sudo tee /etc/X11/Xwrapper.config >/dev/null

# --- 4. checkout ------------------------------------------------------------
# Re-exec from a stable copy before touching the working tree, so a
# `git reset --hard` that rewrites this file mid-run can't garble execution
# (bash reads lazily by byte-offset; if the on-disk content changes, the
# running shell continues at the same offset into the NEW content). This
# happens when re-running install.sh on an existing checkout after main
# has advanced. The copy is removed on exit.
if [ -z "${_INSTALL_SH_REEXECED:-}" ]; then
    _STABLE_COPY="$(mktemp --suffix=.sh)"
    cp "$0" "$_STABLE_COPY"
    chmod +x "$_STABLE_COPY"
    export _STABLE_COPY _INSTALL_SH_REEXECED=1
    exec "$_STABLE_COPY" "$@"
fi
trap 'rm -f "${_STABLE_COPY:-}"' EXIT

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

# --- 5. venv + install ------------------------------------------------------
# Trixie marks the system Python externally-managed (PEP 668), so a venv is the
# supported way to install — `pip install --user` is refused there.
echo "==> venv + install (.[gui])"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
( cd "$APPDIR" && "$VENV/bin/pip" install --quiet -e '.[gui]' )

# --- 6. env file (never clobber an existing one) ----------------------------
if [ ! -f "$ENV_FILE" ]; then
    echo "==> installing $ENV_FILE (EDIT SERVER_URL + API_KEY)"
    sudo cp "$HERE/usage-dashboard-gui.env.example" "$ENV_FILE"
    sudo chown root:root "$ENV_FILE"
    sudo chmod 600 "$ENV_FILE"
else
    echo "==> $ENV_FILE exists, leaving it"
fi

# --- 7. display: stop console blanking --------------------------------------
# Rotation is handled under X by xrandr (step 9), not the kernel cmdline, so we
# only need to keep the backlight from blanking. The console stays portrait-
# native; that's cosmetic (it's only visible for a second before X takes over).
if [ ! -f "$CMDLINE" ]; then
    echo "==> WARNING: no cmdline.txt found (looked in /boot/firmware and /boot)."
    echo "    Skipping consoleblank config; set it by hand later if the panel dims."
else
    echo "==> display config ($CMDLINE)"
    if grep -qF -- "consoleblank=0" "$CMDLINE"; then
        echo "    already set: consoleblank=0"
    else
        sudo cp "$CMDLINE" "$CMDLINE.bak.$(date +%s)"
        sudo sed -i "1 s|\$| consoleblank=0|" "$CMDLINE"
        echo "    added: consoleblank=0"
    fi
fi

# --- 8. touch probe-race workaround -----------------------------------------
echo "==> Goodix touch rebind (boot probe-race workaround)"
sudo install -m 0755 "$HERE/goodix-touch-rebind.sh" /usr/local/bin/goodix-touch-rebind.sh
sudo cp "$HERE/goodix-touch-rebind.service" "$UNIT_DIR/goodix-touch-rebind.service"

# --- 9. X session launcher --------------------------------------------------
echo "==> X session launcher"
sed -e "s|@VENV@|$VENV|g" "$HERE/usage-dashboard-xsession" \
    | sudo tee /usr/local/bin/usage-dashboard-xsession >/dev/null
sudo chmod 0755 /usr/local/bin/usage-dashboard-xsession

# --- 10. systemd units (fill placeholders, install, enable) -----------------
echo "==> systemd units"
render_unit() {  # render_unit <src> <dst>
    sed -e "s|@RUNUSER@|$RUNUSER|g" \
        -e "s|@APPDIR@|$APPDIR|g" \
        -e "s|@VENV@|$VENV|g" \
        -e "s|@XRANDR_ROTATE@|$XRANDR_ROTATE|g" \
        "$1" | sudo tee "$2" >/dev/null
}
render_unit "$HERE/usage-dashboard-gui.service"    "$UNIT_DIR/usage-dashboard-gui.service"
render_unit "$HERE/usage-dashboard-update.service" "$UNIT_DIR/usage-dashboard-update.service"
sudo cp "$HERE/usage-dashboard-update.timer" "$UNIT_DIR/usage-dashboard-update.timer"

# Stable copy of the updater so a git reset can't rewrite the running script.
sed -e "s|@APPDIR@|$APPDIR|g" -e "s|@VENV@|$VENV|g" \
    "$HERE/update.sh" | sudo tee /usr/local/bin/usage-dashboard-update >/dev/null
sudo chmod 755 /usr/local/bin/usage-dashboard-update

# Privileged redeploy helper for opt-in auto-redeploy (AUTO_REDEPLOY=1). Static
# (it recovers per-unit values from the installed files at runtime), so no
# templating. Inert until update.sh invokes it.
sudo install -m 0755 "$HERE/usage-dashboard-redeploy" /usr/local/bin/usage-dashboard-redeploy

# Updater sudo rules, each scoped to one exact command (no args): restart the GUI
# (the app path), and run the redeploy helper (the infra path). The helper itself
# does the daemon-reload/unit-write/restarts as root once invoked — see WI-016.
printf '%s\n%s\n' \
    "$RUNUSER ALL=(root) NOPASSWD: /usr/bin/systemctl restart usage-dashboard-gui.service" \
    "$RUNUSER ALL=(root) NOPASSWD: /usr/local/bin/usage-dashboard-redeploy" \
    | sudo tee /etc/sudoers.d/usage-dashboard-update >/dev/null
sudo chmod 440 /etc/sudoers.d/usage-dashboard-update

sudo systemctl daemon-reload
# Enable (don't start yet): the GUI needs SERVER_URL/API_KEY first, and a reboot
# brings the whole chain (touch rebind -> X -> GUI) up cleanly. The touch rebind
# and update timer are safe to enable now.
sudo systemctl enable goodix-touch-rebind.service
sudo systemctl enable usage-dashboard-gui.service
sudo systemctl enable --now usage-dashboard-update.timer

echo
echo "============================================================"
echo " Setup done. Two steps left:"
echo "------------------------------------------------------------"
echo " 1. Set the server URL + key:"
echo "      sudo nano $ENV_FILE"
echo "    (fill in SERVER_URL and API_KEY, save with Ctrl-O Enter, exit Ctrl-X)"
echo
echo " 2. Reboot to start the dashboard (touch rebind -> X -> GUI):"
echo "      sudo reboot"
echo "------------------------------------------------------------"
echo " After reboot, watch it with:"
echo "      journalctl -u usage-dashboard-gui -f"
echo "============================================================"
echo "    Updates: systemctl list-timers usage-dashboard-update.timer"
