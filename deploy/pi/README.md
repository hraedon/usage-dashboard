# Prepping a Pi — Raspberry Pi 4B + Touch Display 2

The `usage-dashboard-gui` client is a fullscreen pygame app that renders the
provider tiles straight on the panel via KMS/DRM — **no desktop session
needed**. It polls the server's `/readings` API; **tap a tile** for a detail
view, **tap again** to go back.

Target hardware: **Raspberry Pi 4B (8 GB)** + the **official Raspberry Pi Touch
Display 2** (5", 720×1280 portrait-native, DSI), on **Raspberry Pi OS Bookworm
(Lite is fine)**.

## TL;DR — one command

On a freshly imaged Pi, as your normal login user:

```bash
git clone https://github.com/hraedon/usage-dashboard.git
cd usage-dashboard
./deploy/pi/install.sh                    # landscape (rotate 90) by default
sudo nano /etc/usage-dashboard-gui.env    # set SERVER_URL + API_KEY, save, exit
sudo reboot                               # applies rotation AND starts the dashboard
```

That installs system deps, a venv, both systemd services + the auto-update
timer, and the display-rotation/no-blank boot config. The dashboard does **not**
run until that reboot (it needs the URL/key you set, and the rotation only takes
effect on reboot) — so don't worry if `systemctl status` shows it inactive
beforehand. Everything below is what the script does and how to vary it.

You can clone into any directory — the installer points the service at wherever
this checkout actually lives.

## 0. Image the SD card

Use **Raspberry Pi Imager** → Raspberry Pi OS (64-bit), **Lite** is enough.
In the gear/⚙ "OS customisation": set the **hostname** (give each unit a unique
one — e.g. `usage-dash-01`), **username/password**, **Wi-Fi** (or use
Ethernet), and **enable SSH**. First boot then needs no monitor.

The Touch Display 2 is auto-detected on Bookworm — no driver install.

## 1. Orientation (display **and** touch)

The panel is **portrait-native (720×1280)**. We run it **landscape (1280×720)**
by rotating 90°. Two things must rotate together:

- **The image** — kernel KMS rotation in `/boot/firmware/cmdline.txt`:
  ```
  video=DSI-1:720x1280@60,rotate=90
  ```
  > The old `display_lcd_rotate=` / `display_rotate=` firmware options are
  > **ignored** by the default KMS driver (`vc4-kms-v3d`) — don't use them.
- **The touch** — there's no desktop compositor to apply a transform, so the
  panel keeps reporting in its portrait frame. The GUI corrects this itself via
  `GUI_TOUCH_ROTATE` (set in the service to match `rotate=`).

`install.sh` sets both. To run **portrait** instead:

```bash
DISPLAY_ROTATE=0 GUI_TOUCH_ROTATE=0 ./deploy/pi/install.sh
```

If after rebooting the **image** is rotated the wrong way, change `rotate=90` →
`270` (or `180`) in `cmdline.txt`. If the image is right but **taps land
mirrored / on the wrong tile**, change `GUI_TOUCH_ROTATE` in
`/etc/systemd/system/usage-dashboard-gui.service` to match (try `270`), then
`sudo systemctl daemon-reload && sudo systemctl restart usage-dashboard-gui`.

## 2. What `install.sh` does

Idempotent — safe to re-run. Run as your normal user (it uses `sudo` itself).

1. `apt install git python3-venv python3-pip libgbm1 libdrm2`
2. adds you to the `video`, `render`, `input` groups (DRM + touch)
3. clones/updates the repo to `~/usage-dashboard`
4. creates a **venv** at `~/usage-dashboard/.venv` and installs `.[gui]`
   — Bookworm refuses `pip install --user` (PEP 668 "externally-managed"), so a
   venv is the supported path
5. installs `/etc/usage-dashboard-gui.env` (chmod 600; **never overwrites** an
   existing one)
6. sets `video=DSI-1:…rotate=N` and `consoleblank=0` in `cmdline.txt`
   (backs it up first; skips if already present)
7. installs + enables the GUI service and the auto-update timer

Config knobs (env vars): `DISPLAY_ROTATE`, `GUI_TOUCH_ROTATE`, `UPDATE_REF`,
`APPDIR`, `REPO_URL` (use the SSH URL + a deploy key for a private repo).

## 3. Configure

Edit `/etc/usage-dashboard-gui.env` (root-owned, `chmod 600`):

```ini
SERVER_URL=http://<server-host>:8080
API_KEY=<the shared bearer token>     # same api-key the server uses
# UPDATE_REF=main                     # or pin a tag, e.g. v0.2.0
# GUI_FPS=10
```

Then `sudo systemctl restart usage-dashboard-gui`.

## 4. Operate

```bash
journalctl -u usage-dashboard-gui -f          # watch the GUI
systemctl status usage-dashboard-gui
systemctl list-timers usage-dashboard-update.timer
journalctl -u usage-dashboard-update --since today   # update history
```

## 5. Auto-update

A `usage-dashboard-update.timer` checks `git` every ~15 min (3 min after boot,
with a randomised jitter so a fleet doesn't sync up). On each tick the updater:

1. `git fetch`es the tracked ref (`UPDATE_REF`, default `main`);
2. if nothing changed, **does nothing** (no restart);
3. otherwise `reset --hard`s to it, reinstalls, runs an **import smoke check**,
   and only then restarts the GUI;
4. if the install or smoke check fails, it **rolls back** to the previous
   commit and leaves the running app untouched.

The updater runs from a stable copy at `/usr/local/bin/usage-dashboard-update`
(so a `git reset` can't rewrite the script mid-run) and is allowed to restart
just the GUI via a scoped `sudoers.d` drop-in.

**Pin a fleet to a release:** set `UPDATE_REF=v0.2.0` in each
`/etc/usage-dashboard-gui.env`. They'll hold there until you bump it — cut your
release on GitHub, then change the ref (or push to `main` to roll everyone).

Force a check now:

```bash
sudo systemctl start usage-dashboard-update.service
journalctl -u usage-dashboard-update -n 20
```

> Updates pull from GitHub over HTTPS, so the Pi needs outbound network and (for
> a **private** repo) a deploy key — clone with the SSH `REPO_URL` in that case.

## 6. Stop screen blanking

`install.sh` adds `consoleblank=0`. If the backlight still dims when idle:

```bash
sudo systemctl mask systemd-backlight@.service
```

## Dev / windowed mode (off the Pi)

Run in a window instead of fullscreen (no rotation, mouse acts as touch):

```bash
GUI_FULLSCREEN=0 GUI_WIDTH=1280 GUI_HEIGHT=720 \
  SERVER_URL=http://localhost:8080 API_KEY=dev \
  .venv/bin/usage-dashboard-gui
```

Press `Esc` or `q` to quit.
