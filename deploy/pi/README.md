# Touch GUI client — Raspberry Pi 4B + 5" touch display

The `usage-dashboard-gui` client is a fullscreen pygame app for a Pi with a DSI
touch panel. It polls the server's `/readings` API and shows a grid of provider
tiles (session + weekly bars, reset countdowns); **tap a tile** for a detail
view, **tap again** to go back. It renders directly on the display via KMS/DRM,
so no desktop session is needed.

The window uses the panel's **native resolution** (auto-detected in fullscreen),
so the same build works on the 5" panel, the 7" official display, or a dev
window — no resolution config required.

## 1. Display

The official touch displays are detected automatically by Raspberry Pi OS. If
the image is upside down for your mounting, rotate it in `/boot/firmware/config.txt`:

```ini
# 180° rotation for a DSI panel (use 90/270 for portrait)
display_lcd_rotate=2
```

## 2. Install

On the Pi (Raspberry Pi OS Bookworm or later, Python ≥ 3.12):

```bash
pip install --user 'usage-dashboard[gui]'   # or: pip install --user '.[gui]' from a checkout
```

`pygame-ce` ships its own SDL2; KMS/DRM output additionally needs the system
`libgbm`/`libdrm` (present by default on Pi OS).

## 3. Configure

Create `/etc/usage-dashboard-gui.env` (root-owned, `chmod 600`) with the server
URL and the shared API key (the same `api-key` the server uses):

```ini
SERVER_URL=http://<server-host>:8080
API_KEY=<the shared bearer token>
# Optional: GUI_FPS=10
```

## 4. Run as a service

```bash
sudo cp deploy/pi/usage-dashboard-gui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now usage-dashboard-gui
journalctl -u usage-dashboard-gui -f      # watch it
```

The unit runs as user `pi` in `video`/`render`/`input` groups for DRM + touch.
Adjust `User=` and the `ExecStart` path if you installed elsewhere or as a
different user (`which usage-dashboard-gui`).

## 5. Stop the screen blanking

Console blanking will dim an idle kiosk. Disable it:

```bash
# /boot/firmware/cmdline.txt — append:  consoleblank=0
sudo systemctl mask systemd-backlight@.service   # optional, keeps backlight on
```

## Dev / windowed mode

Off the Pi (or in any desktop session) run it in a window instead of fullscreen:

```bash
GUI_FULLSCREEN=0 GUI_WIDTH=800 GUI_HEIGHT=480 \
  SERVER_URL=http://localhost:8080 API_KEY=dev usage-dashboard-gui
```

Press `Esc` or `q` to quit in windowed mode.
