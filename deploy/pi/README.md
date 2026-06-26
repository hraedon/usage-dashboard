# Prepping a Pi — Raspberry Pi 4B + Touch Display 2

The `usage-dashboard-gui` client is a fullscreen pygame app that shows the
provider tiles on the panel. It polls the server's `/readings` API; **tap a
tile** for a detail view, **tap again** to go back.

Target hardware: **Raspberry Pi 4B (8 GB)** + the **official Raspberry Pi Touch
Display 2** (5", 720×1280 portrait-native, DSI), on **Raspberry Pi OS Trixie
(Lite is fine)**.

> **Why Trixie, and why an X server?** Two hard-won constraints, both validated
> on real hardware:
>
> - **Python:** this package requires **Python ≥ 3.12**. Trixie ships 3.13;
>   Bookworm ships 3.11 and the install will refuse. Use Trixie.
> - **Display:** the GUI runs under a **minimal X server**, *not* bare KMS/DRM.
>   SDL 2.32.10's `kmsdrm` backend cannot present to this panel — a full-screen
>   fill stays **black** on both Bookworm and Trixie even though the DRM pipeline
>   reports the framebuffer bound and active. Under a tiny X server (`xinit` +
>   the GUI, no desktop) the same app paints correctly, and `xrandr` gives real
>   landscape rotation. `install.sh` sets all of this up for you.

## TL;DR — one command

On a freshly imaged Pi, as your normal login user:

```bash
git clone https://github.com/hraedon/usage-dashboard.git    # (git: sudo apt install -y git)
cd usage-dashboard
./deploy/pi/install.sh                    # landscape (rotate=right) by default
sudo nano /etc/usage-dashboard-gui.env    # set SERVER_URL + API_KEY, save, exit
sudo reboot                               # starts the dashboard
```

That installs system deps (incl. the minimal X stack), a venv, the X-session
launcher, the touch-rebind workaround, both systemd services + the auto-update
timer. The dashboard does **not** run until that reboot (it needs the URL/key
you set) — so don't worry if `systemctl status` shows it inactive beforehand.

You can clone into any directory — the installer points the services at wherever
this checkout actually lives. **Raspberry Pi OS Lite ships without `git`**; if
the clone fails, `sudo apt-get install -y git` first (or let the first
`install.sh` run, which installs it).

## 0. Image the SD card

Use **Raspberry Pi OS (64-bit), Lite, Trixie** — Lite is enough; you don't need
(or want) the Desktop image, since `install.sh` brings up its own minimal X.

Easiest is **Raspberry Pi Imager** → in the gear/⚙ "OS customisation" set the
**hostname** (unique per unit), **username/password**, **Wi-Fi**, and **enable
SSH**. Imager writes the right first-boot files for whichever OS you pick, so
first boot needs no monitor.

**Imaging headlessly (e.g. `dd` from another box)?** The first-boot mechanism
changed by OS release — put the files at the boot-partition (FAT) root:

- **Trixie (Debian 13) and newer → cloud-init.** Three files: `user-data`
  (cloud-config; first line must be `#cloud-config`), `network-config`
  (netplan v2 `wifis:` — multiple SSIDs supported), and an empty `meta-data`.
  `custom.toml` is **ignored** on Trixie. Validate the YAML before unmounting;
  debug with `cloud-init status --long` + `/var/log/cloud-init.log`.
- **Bookworm → `custom.toml`** (the `raspberrypi-sys-mods` firstboot format).
  But note Bookworm's Python 3.11 won't install this package — prefer Trixie.

The Touch Display 2 panel is auto-detected — no driver install. (Its **touch**
controller needs the workaround in §4, which `install.sh` handles.)

## 1. Orientation (display **and** touch)

The panel is **portrait-native (720×1280)**; we run it **landscape (1280×720)**.
Under the X server this is one knob — `xrandr`'s rotation — and `install.sh`
bakes it into the GUI service as `XRANDR_ROTATE` (default `right`):

```bash
XRANDR_ROTATE=left ./deploy/pi/install.sh     # rotate the other way
```

Values: `normal | left | right | inverted`. The **touch** transform follows
automatically — the session launcher (`usage-dashboard-xsession`) applies a
matching `xinput` Coordinate Transformation Matrix, because libinput does *not*
auto-rotate this Goodix device under bare modesetting. If the image is right but
taps land 90° off, you picked a rotation whose matrix doesn't match; re-run with
the opposite `XRANDR_ROTATE` (or edit it in
`/etc/systemd/system/usage-dashboard-gui.service` and
`daemon-reload && systemctl restart usage-dashboard-gui`).

> The old kmsdrm-era knobs (`video=DSI-1:…rotate=N` in `cmdline.txt`,
> `GUI_TOUCH_ROTATE`) are **not** used on the X path. The console may show
> portrait for a second before X takes over — that's cosmetic.

## 2. What `install.sh` does

Idempotent — safe to re-run. Run as your normal user (it uses `sudo` itself).

1. `apt install` the app deps **and** a minimal X stack (`xserver-xorg-core`,
   `xserver-xorg-legacy`, `xserver-xorg-input-libinput`, `xinit`,
   `x11-xserver-utils`, `xinput`) plus the mesa GBM/EGL/GLES libs
2. adds you to the `video`, `render`, `input` groups (DRM + touch)
3. writes `/etc/X11/Xwrapper.config` so `xinit` can start X from a systemd
   service that has no controlling tty
4. clones/updates the repo and creates a **venv** at `…/.venv` with `.[gui]`
5. installs `/etc/usage-dashboard-gui.env` (chmod 600; **never overwrites** an
   existing one)
6. adds `consoleblank=0` to `cmdline.txt` (no rotation token — X handles that)
7. installs the **touch rebind** script + `goodix-touch-rebind.service` (§4)
8. installs the **X session launcher** to `/usr/local/bin/usage-dashboard-xsession`
9. installs + enables the GUI service (xinit-wrapped) and the auto-update timer

Config knobs (env vars): `XRANDR_ROTATE`, `UPDATE_REF`, `APPDIR`, `VENV`,
`RUNUSER`, `REPO_URL` (use the SSH URL + a deploy key for a private repo).

## 3. Configure

Edit `/etc/usage-dashboard-gui.env` (root-owned, `chmod 600`):

```ini
SERVER_URL=https://<server-host>
API_KEY=<the shared bearer token>     # same api-key the server uses
# UPDATE_REF=main                     # or pin a tag, e.g. v0.2.0
# GUI_FPS=10
# BACKLIGHT_SLEEP=1                    # blank the panel on a schedule (tap to wake)
# UNIT_ID=mpmusageNN                   # this unit's key in the schedules ConfigMap
# BACKLIGHT_SCHEDULE=daily 00:00-08:00; fri 18:00-mon 08:00   # local fallback
# BRIGHTNESS_STEPS=10                  # -/+ notches (tap the status line); try 9/11
```

Then `sudo systemctl restart usage-dashboard-gui`.

> **Backlight sleep** is opt-in: set `BACKLIGHT_SLEEP=1` (and a `UNIT_ID` so the
> server can hand this unit its schedule). The panel dims its `brightness` to 0
> on schedule and wakes on a tap until the next sleep boundary. See the main
> [README](../../README.md#backlight-sleep-schedule) for the schedule grammar
> and the server-side ConfigMap. This is distinct from §7 below, which only
> stops the *console* from blanking before X takes over.

## 4. Touch: the Goodix boot probe-race workaround

The Touch Display 2 pairs an **ILI9881C** DSI panel with a **Goodix GT911**
touch controller, and the touch IC is powered from the panel rail. At boot the
Goodix I²C driver probes (~5.5 s) right around when the panel finishes binding
(~6.0 s); when it loses that race it fails with:

```
Goodix-TS 10-005d: I2C communication failure: -5
Goodix-TS 10-005d: probe with driver Goodix-TS failed with error -5
```

…and never retries, so **the touchscreen doesn't enumerate** (`/proc/bus/input/
devices` shows no Goodix). Once the panel is powered, a manual driver *bind*
succeeds. `goodix-touch-rebind.service` (oneshot, ordered `Before` the GUI) runs
`goodix-touch-rebind.sh`, which retries the bind until the input node appears.

Check it after boot:

```bash
systemctl status goodix-touch-rebind.service     # active (exited)
grep -i goodix /proc/bus/input/devices           # the touchscreen is present
```

If touch is ever missing on a running unit, `sudo systemctl start
goodix-touch-rebind.service` rebinds it live.

## 5. Operate

```bash
journalctl -u usage-dashboard-gui -f          # watch the GUI (+ its X server)
systemctl status usage-dashboard-gui
systemctl list-timers usage-dashboard-update.timer
journalctl -u usage-dashboard-update --since today   # update history
```

The X server runs on **vt1**; the GUI service `Conflicts=getty@tty1.service` so
X owns the panel.

## 6. Auto-update

A `usage-dashboard-update.timer` checks `git` every ~15 min (3 min after boot,
with a randomised jitter so a fleet doesn't sync up). On each tick the updater:

1. `git fetch`es the tracked ref (`UPDATE_REF`, default `main`);
2. if nothing changed, **does nothing** (no restart);
3. otherwise `reset --hard`s to it, reinstalls `.[gui]`, runs an **import smoke
   check**, and only then restarts the GUI;
4. if the install or smoke check fails, it **rolls back** to the previous commit
   and leaves the running app untouched.

> **Scope:** the updater only swaps the **Python app** and restarts the service.
> It does **not** re-run `install.sh` or re-render systemd units / the X
> launcher / the touch workaround. Changes to *those* (anything under
> `deploy/pi/` except the app code) need a manual `install.sh` re-run on each
> unit; pushing them to `main` will **not** disturb already-provisioned Pis.

The updater runs from a stable copy at `/usr/local/bin/usage-dashboard-update`
(so a `git reset` can't rewrite the script mid-run) and may restart just the GUI
via a scoped `sudoers.d` drop-in.

**Pin a fleet to a release:** set `UPDATE_REF=v0.2.0` in each
`/etc/usage-dashboard-gui.env`. They'll hold there until you bump it.

Force a check now:

```bash
sudo systemctl start usage-dashboard-update.service
journalctl -u usage-dashboard-update -n 20
```

> Updates pull from GitHub over HTTPS, so the Pi needs outbound network and (for
> a **private** repo) a deploy key — clone with the SSH `REPO_URL` in that case.

## 7. Stop screen blanking

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
