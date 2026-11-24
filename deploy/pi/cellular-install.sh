#!/usr/bin/env bash
# Opt-in installer for the Pi cellular fallback (Plan 010).
#
# Adds the cellular readings worker to a unit that deploy/pi/install.sh has
# already set up: its own system user, the worker's systemd unit, the root-owned
# link helper, and a sudoers rule that lets the worker ask the helper for
# exactly four things. Nothing here runs unless an operator runs it by hand.
#
# It also installs a unit that puts the firewall around the modem in place at
# every boot, BEFORE NetworkManager starts. The deny policy is what makes the
# link readings-only; before it exists nothing restricts the modem's interface,
# and NetworkManager brings up any Ethernet device it finds with a default
# route — a modem that presents itself as a network card included.
#
# It is deliberately NOT part of auto-update or auto-redeploy: a bad link
# configuration reaching a unit by itself could orphan it, so this is the only
# path that changes the worker's unit, wrapper or sudoers rule. Re-run it to
# apply a change to any of them.
#
# What it does NOT do, on purpose:
#   - create the NetworkManager profile (it holds the APN and the SIM PIN);
#   - write /etc/usage-dashboard-cellular.env (it holds the display credential).
# Both are the operator's, described in deploy/pi/README.md §8. Until the settings
# file exists the worker's unit is inert, not failed.
#
# Run as the normal login user (NOT root); it uses sudo for the privileged
# bits. Idempotent — safe to re-run. Override via env:
#   INSTALL_MODEM_MANAGER=1 ./deploy/pi/cellular-install.sh   # a HAT that
#       ModemManager manages (QMI/MBIM). Ethernet-style HATs (ECM/RNDIS/NCM)
#       do not need it and should leave it off.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
GUI_UNIT="/etc/systemd/system/usage-dashboard-gui.service"
UPDATER="/usr/local/bin/usage-dashboard-update"
GUI_ENV="/etc/usage-dashboard-gui.env"
ENV_FILE="/etc/usage-dashboard-cellular.env"
STATE_DIR="/var/lib/usage-dashboard-cellular"
UNIT_DIR="/etc/systemd/system"
UNIT_NAME="usage-dashboard-cellular.service"
FIREWALL_UNIT_NAME="usage-dashboard-cellular-firewall.service"
HELPER="/usr/local/bin/usage-dashboard-cellular-link"
SUDOERS_FILE="/etc/sudoers.d/usage-dashboard-cellular"
WORKER_USER="${WORKER_USER:-usage-dashboard-cellular}"
INSTALL_MODEM_MANAGER="${INSTALL_MODEM_MANAGER:-0}"

if [ "$(id -u)" = 0 ]; then
    echo "Run this as your normal user, not root (it uses sudo as needed)." >&2
    exit 1
fi

# The worker user's name is written into a unit file and a sudoers rule, so it
# may only be a plain user name — never shell or sudoers syntax.
if ! printf '%s' "$WORKER_USER" | grep -Eq '^[a-z_][a-z0-9_-]{0,31}$'; then
    echo "ERROR: WORKER_USER must be a plain lower-case user name." >&2
    exit 1
fi

echo "==> usage-dashboard cellular fallback setup"
echo "    worker user=$WORKER_USER  state=$STATE_DIR"
# Prime sudo: the passwordless path first (headless SSH with NOPASSWD for
# commands but a password-required group rule), as deploy/pi/install.sh does.
sudo -n true 2>/dev/null || sudo -v

_TMPDIR="$(mktemp -d)"
trap 'rm -rf "$_TMPDIR"' EXIT

# --- 1. the existing install ------------------------------------------------
# RUNUSER comes from the installed GUI unit and VENV from the installed
# updater, exactly as usage-dashboard-redeploy recovers them. This script
# therefore works from any checkout and takes no paths as arguments.
RUNUSER="$(sed -n 's/^User=//p' "$GUI_UNIT" 2>/dev/null | head -1)"
VENV="$(sed -n 's/^VENV="\(.*\)"$/\1/p' "$UPDATER" 2>/dev/null | head -1)"
if [ -z "$RUNUSER" ] || [ -z "$VENV" ] || [ ! -x "$VENV/bin/python" ]; then
    echo "ERROR: could not find the GUI unit or its venv." >&2
    echo "       Run deploy/pi/install.sh on this unit first." >&2
    exit 1
fi
RUNGROUP="$(id -gn "$RUNUSER")"
echo "    gui user=$RUNUSER  venv=$VENV"

# --- 2. packages -------------------------------------------------------------
echo "==> apt packages"
sudo apt-get update -qq
# nftables enforces the link; iproute2 selects the route for the worker.
sudo apt-get install -y nftables iproute2
if [ "$INSTALL_MODEM_MANAGER" = "1" ]; then
    sudo apt-get install -y modemmanager usb-modeswitch
fi

# --- 3. the worker's own system user -----------------------------------------
# Separate from the GUI/updater user on purpose: the firewall permits "this
# one UID, to this one address", which only means something if nothing else
# shares it. No home, no shell, no supplementary groups.
if id "$WORKER_USER" >/dev/null 2>&1; then
    echo "==> system user $WORKER_USER exists, leaving it"
else
    echo "==> system user $WORKER_USER"
    sudo useradd --system --user-group --no-create-home \
        --shell /usr/sbin/nologin "$WORKER_USER"
fi

# --- 4. state and signal directories -----------------------------------------
# The worker owns the state dir and publishes the readings cache there
# world-readable; the GUI owns the request subdir inside it and is the only
# writer there. That is the whole channel between the two.
echo "==> directories"
sudo install -d -o "$WORKER_USER" -g "$WORKER_USER" -m 0755 "$STATE_DIR"
sudo install -d -o "$RUNUSER" -g "$RUNGROUP" -m 0755 "$STATE_DIR/request"

# --- 5. the root-owned link helper -------------------------------------------
# Installed by rename, so a helper that is running when this script re-runs
# keeps its handle to the old file.
echo "==> link helper $HELPER"
sudo install -m 0755 "$HERE/usage-dashboard-cellular-link" "$HELPER.new"
sudo mv -f "$HELPER.new" "$HELPER"

# --- 6. the worker's unit, and the boot-time firewall ------------------------------
# Both verified before either can replace a working unit.
echo "==> systemd units"
sed -e "s|@WORKER_USER@|$WORKER_USER|g" -e "s|@VENV@|$VENV|g" \
    "$HERE/usage-dashboard-cellular.service" > "$_TMPDIR/$UNIT_NAME"
cp "$HERE/usage-dashboard-cellular-firewall.service" "$_TMPDIR/$FIREWALL_UNIT_NAME"
# Each candidate carries its real unit name: systemd-analyze refuses to verify
# a file whose name has no unit suffix ("Failed to prepare filename").
for candidate in "$_TMPDIR/$UNIT_NAME" "$_TMPDIR/$FIREWALL_UNIT_NAME"; do
    if ! systemd-analyze verify "$candidate" >/dev/null 2>"$_TMPDIR/verify.err"; then
        echo "ERROR: $(basename "$candidate") did not pass systemd-analyze verify; not installed:" >&2
        sed 's/^/       /' "$_TMPDIR/verify.err" >&2
        exit 1
    fi
done
sudo install -m 0644 "$_TMPDIR/$UNIT_NAME" "$UNIT_DIR/$UNIT_NAME"
sudo install -m 0644 "$_TMPDIR/$FIREWALL_UNIT_NAME" "$UNIT_DIR/$FIREWALL_UNIT_NAME"

# --- 7. what the worker may ask root to do --------------------------------------
# One rule, four argument-free commands: three that manage the link, and one
# that only loads the deny policy. Validated with visudo BEFORE it goes
# anywhere near /etc/sudoers.d — an unparseable file there breaks sudo for
# everyone — and installed by rename so it is never seen half-written.
echo "==> sudoers rule"
cat > "$_TMPDIR/sudoers" <<SUDOERS
# Plan 010 (deploy/pi/cellular-install.sh): the cellular readings worker may
# ask the root-owned link helper for exactly four argument-free commands.
# \`firewall\` only loads the deny policy. The helper enforces that policy first
# and refuses an unsafe link.
$WORKER_USER ALL=(root) NOPASSWD: $HELPER firewall, $HELPER route, $HELPER unroute, $HELPER down
SUDOERS
if ! sudo visudo -cf "$_TMPDIR/sudoers" >"$_TMPDIR/visudo.out" 2>&1; then
    echo "ERROR: the sudoers rule did not pass visudo; not installed:" >&2
    sed 's/^/       /' "$_TMPDIR/visudo.out" >&2
    exit 1
fi
sudo install -m 0440 -o root -g root "$_TMPDIR/sudoers" "$SUDOERS_FILE.new"
sudo mv -f "$SUDOERS_FILE.new" "$SUDOERS_FILE"

# --- 8. point the GUI at the cache -----------------------------------------------
# One line in the GUI's env file, added once. It tells the GUI where the
# worker publishes; the GUI then also tells the worker when the panel is lit.
echo "==> GUI cache setting"
if sudo grep -q '^CELLULAR_CACHE=' "$GUI_ENV" 2>/dev/null; then
    echo "    $GUI_ENV already sets CELLULAR_CACHE, leaving it"
else
    printf '\n# Plan 010: read the cellular readings worker'"'"'s cache (cellular-install.sh).\nCELLULAR_CACHE=%s\n' \
        "$STATE_DIR" | sudo tee -a "$GUI_ENV" >/dev/null
    echo "    added CELLULAR_CACHE=$STATE_DIR to $GUI_ENV"
fi

# --- 9. enable, but do not start ---------------------------------------------------
# Both units carry ConditionPathExists=/etc/usage-dashboard-cellular.env, so a
# start is skipped — not failed — until the settings file exists. The firewall
# unit runs at the next boot (and the worker loads the same policy when it
# starts). A re-run does not restart a running worker: that is the operator's
# call.
sudo systemctl daemon-reload
sudo systemctl enable "$UNIT_NAME" "$FIREWALL_UNIT_NAME"

echo
echo "============================================================"
echo " Cellular fallback installed. Left to do by hand:"
echo "------------------------------------------------------------"
if [ -f "$ENV_FILE" ]; then
    echo " 1. $ENV_FILE exists."
else
    echo " 1. Create the settings file (it carries a credential):"
    echo "      sudo cp $HERE/usage-dashboard-cellular.env.example $ENV_FILE"
    echo "      sudo nano $ENV_FILE && sudo chmod 600 $ENV_FILE"
fi
echo " 2. Provision the NetworkManager profile (README §8), then confirm it"
echo "    passes the helper's pre-flight:"
echo "      sudo $HELPER check"
echo " 3. Restart the GUI so it picks up CELLULAR_CACHE, and start the worker:"
echo "      sudo systemctl restart usage-dashboard-gui.service"
echo "      sudo systemctl start $UNIT_NAME"
echo "------------------------------------------------------------"
echo " The deny policy around the modem is loaded at every boot from now on"
echo " ($FIREWALL_UNIT_NAME), before NetworkManager."
echo " Then: journalctl -u ${UNIT_NAME%.service} -f"
echo "       sudo $HELPER status"
echo "============================================================"
