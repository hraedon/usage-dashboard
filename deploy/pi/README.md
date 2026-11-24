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
10. installs the **redeploy helper** (`/usr/local/bin/usage-dashboard-redeploy`) +
    its scoped `sudoers.d` rule, for opt-in auto-redeploy (§6) — inert until enabled

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
# BRIGHTNESS_STEPS=10                  # status overlay -/+ notches (tap status line); try 9/11
# AUTO_REDEPLOY=1                      # also self-redeploy units/scripts (see §6); off by default
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

> **Scope:** by default the updater only swaps the **Python app** and restarts
> the service. The **installer-managed components** — the updater script itself,
> the systemd units, the X launcher, the touch workaround — change only on a
> manual `install.sh` re-run, *unless* you opt into auto-redeploy (below).

The updater runs from a stable copy at `/usr/local/bin/usage-dashboard-update`
(so a `git reset` can't rewrite the script mid-run) and may restart just the GUI
via a scoped `sudoers.d` drop-in.

### Auto-redeploy of installer-managed components (opt-in)

Set `AUTO_REDEPLOY=1` in `/etc/usage-dashboard-gui.env` and the updater also
re-applies the installer-managed components from the pulled checkout each cycle,
so an infra change (e.g. an edited unit file or the updater itself) reaches the
fleet without a per-unit `install.sh` run. It is **off by default** — a unit only
begins re-applying root-owned files once you opt in.

How it stays safe:

- **Content-addressed:** a component is touched only when the rendered source
  differs from what's installed — a no-op in steady state.
- **Atomic writes:** every file is written to a temp sibling and `os.replace`-d
  into place, so replacing the running updater/wrapper mid-cycle can't corrupt it
  (the live process keeps its handle to the old inode).
- **Verify before install:** unit files are checked with `systemd-analyze verify`;
  a unit that fails is **skipped** (the working one stays), never installed.
- **Rollback:** the GUI service is bounced only if it was already running, then
  health-checked — if it doesn't come back active, its prior unit/launcher is
  restored and restarted. (A unit that *starts* but renders a black panel can't be
  auto-detected; that residual risk is why this is opt-in.)

The privileged work runs via a root-owned helper
(`/usr/local/bin/usage-dashboard-redeploy`) the updater calls through a single
argument-free `NOPASSWD` rule; the helper recovers the per-unit template values
(`RUNUSER`/rotation from the GUI unit, `APPDIR`/`VENV` from the updater) from the
already-installed files. The decision logic lives in
`usage_dashboard.deploy.redeploy` and is unit-tested.

> **Trust note:** auto-redeploy lets a push to the tracked ref change root-owned
> systemd units on the fleet — a real escalation over "run app code as the GUI
> user." Pair it with a pinned `UPDATE_REF` if that matters. Also: the `NOPASSWD`
> helper runs the venv Python + `usage_dashboard.deploy.redeploy`, which live in
> the run user's (writable) checkout — so enabling this effectively gives the run
> user passwordless root through the helper. That's fine when the run user is the
> trusted sudoer who ran `install.sh` (the intended setup); don't enable it for a
> run user you wouldn't already hand `sudo`.
>
> **Bootstrap:** a unit already running the *old* updater can't pull this in
> automatically (its installed updater predates the feature). Re-run `install.sh`
> once per existing unit to lay down the new updater + helper + sudoers rule;
> after that, future infra changes propagate on their own.

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

## 8. Cellular fallback (opt-in — Plan 010)

A unit whose only link is a phone hotspot goes dark whenever the hotspot is
missing or has no internet. An opted-in Pi with a cellular HAT can instead
keep showing readings over the metered link — and nothing else. Wi-Fi or
Ethernet always stays the preferred path.

The split is deliberate, and everything below follows from it:

- **The server polls the providers.** The Pi only ever fetches the stored
  snapshot (`GET /api/v1/readings`). The panel renders it locally.
- **One unprivileged process may use the modem.** The *cellular readings
  worker* (`usage-dashboard-cellular.service`) runs as its own system user,
  separate from the GUI and the updater, and holds a read-only *display
  credential* that reaches the readings route and nothing else.
- **The kernel enforces it, not the code's good intentions.** An nftables
  policy on the modem interface permits that one UID to the dashboard's
  address on TCP 443, plus the link-setup packets the modem needs, and drops
  everything else — other users sending to the *same* address included. A
  policy route selects the modem for the worker's UID only. Nothing is
  forwarded through the modem. The policy is loaded at every boot, before
  NetworkManager starts, and again whenever the worker starts.
- **The normal route decides.** The worker asks for the readings over the
  normal route with bounded timeouts. Only when that gets no HTTP answer at
  all (twice) does it fall back; a server that *answers* — even with 401 or
  429 — is reachable. The first success on the normal route ends the fallback.
- **A refusal leaves nothing behind.** If the privileged helper refuses at any
  point, the routing rule it may have installed is withdrawn and the link is
  taken down before it reports the failure; the worker then asks for `down`
  again regardless, and systemd takes the link down whenever the worker stops.

What it never does over cellular: updates, package downloads, DNS, NTP, SSH,
the GUI's own traffic, the updater's, or any other host app. The backlight
schedule falls back to the local one. The refresh button re-reads the stored
snapshot instead of asking the server to poll the providers — but only within
limits: never more than once per minimum gap (a minute), and never while a
wait is running that follows a read which failed or was refused. A tap outside
those limits is declined, and the panel says "refresh deferred".

### The HAT this was built against

Waveshare's *Cat-1/GNSS HAT for Raspberry Pi* (SIM7670G). Every row was read
off the first unit on 2026-11-23 with status queries only; none of it is
assumed. `sudo usage-dashboard-cellular-link inspect` prints the same record
for any other HAT.

| | |
|---|---|
| Module | SIMCom `SIM7670G-MNGV`, firmware V1.9.05 |
| USB identity | `05c6:9330` ("QualComm Compo") — a Qualcomm composite ID, *not* SIMCom's `1e0e` |
| Network interface | `usb0`, driver `rndis_host` (the module's default, `usbnetmode=0`) |
| Serial ports | `/dev/ttyACM0` AT · `ACM1` diagnostic log — a continuous binary stream, don't open it · `ACM2` AT/modem · `ACM3` NMEA |
| What its DHCP hands the Pi | `192.168.0.100/24`, gateway `192.168.0.1`, and two DNS servers that answer nothing |
| Auto-dial | **off** by default (`AT+DIALMODE?` → `1`): with nothing configured the modem accepts DHCP and leads nowhere |
| ModemManager | not involved — leave `INSTALL_MODEM_MANAGER` off for this HAT |
| The Pi | Debian 13, kernel 6.18, NetworkManager 1.52.1; `nftables` already installed; no real-time clock |

### First: stop NetworkManager adopting the modem

NetworkManager gives every Ethernet device it has no profile for a "Wired
connection N": auto-connect, DHCP, a default route, DNS. An Ethernet-style
modem *is* such a device. With the HAT plugged in and nothing configured, that
is exactly what happened on the first unit — the modem became the Pi's
*preferred* default route (its metric beats Ethernet's), the two dead DNS
servers it advertises went to the top of the resolver list (about 8 seconds per
lookup), and NTP stopped syncing.

Do this before anything else, and before the interface is renamed (both names,
in case the rename is done first):

```bash
nmcli -f NAME,TYPE,DEVICE con show            # find the profile on usb0
sudo nmcli connection delete "Wired connection 2"
printf '[main]\nno-auto-default=usb0,cell0\n' \
  | sudo tee /etc/NetworkManager/conf.d/90-cellular-no-auto-default.conf
sudo systemctl reload NetworkManager
```

The helper refuses to use the link while such a profile exists — `check` names
it — because no firewall around the worker can cover traffic another profile
invited. Until it is removed, the boot-time policy blocks that traffic (so no
data leaks), but the Pi's default route still points into the modem: it is
offline, not leaking.

### Rename the interface, then install

The firewall and the profile are both written against the interface's name, and
`usb0`/`usb1` depends on enumeration order. Give it a stable one:

```bash
# /etc/udev/rules.d/70-usage-cellular.rules
SUBSYSTEM=="net", ACTION=="add", ATTRS{idVendor}=="05c6", ATTRS{idProduct}=="9330", NAME="cell0"
```

Replug the HAT (or `sudo udevadm control --reload && sudo udevadm trigger`), then
on a unit `install.sh` has already set up, as the normal login user:

```bash
./deploy/pi/cellular-install.sh
```

It installs `nftables`, creates the `usage-dashboard-cellular` system user, the
worker's unit, a unit that loads the firewall at boot
(`usage-dashboard-cellular-firewall.service`), a root-owned link helper
(`/usr/local/bin/usage-dashboard-cellular-link`), a sudoers rule letting the
worker ask that helper for exactly four argument-free verbs (`firewall`,
`route`, `unroute`, `down`), and adds `CELLULAR_CACHE` to the GUI's env file.
It is idempotent. It **does not** provision the modem or the NetworkManager
profile (those hold the APN and SIM PIN) or write the settings file (that holds
the credential), and it never starts the worker: until the settings file exists
both units are inert, not failed.

The worker's unit, the wrapper and the sudoers rule are **not** part of
auto-update or auto-redeploy (§6): a bad link configuration must not be able to
reach a unit on its own, so they change only when this script is re-run by
hand. The helper's *logic* is Python in the package, and updates with
everything else in the checkout — as does the GUI's.

### Provision the modem (once, over AT)

The Pi's profile is plain Ethernet with DHCP, so the module has to dial by
itself. That is three settings, on the AT port (the `dialout` group may open
it; `picocom -b 115200 /dev/ttyACM0`, or `atinout`):

```
AT+CGDCONT=1,"IP","<APN>"       # the carrier's APN
AT+DIALMODE=0                   # the module connects by itself (default 1: it does not)
AT$MYCONFIG="usbnetmode",0      # RNDIS — the mode the record above was taken in; the module restarts
```

These are SIMCom's documented commands for this family (its *A76XX Series Linux
USB Application Note*, and Waveshare's page for this HAT). They have **not been
run on the unit** — it was only queried. Check each with its `?` form
afterwards, and `AT+COPS?`, `AT+CEREG?` and `AT+CSQ` once it should be on the
air (`AT+CSQ` read `99,99` on the first unit: no signal detectable, which
usually means the antennas are not fitted).

`AT$MYCONFIG` restarts the module and can change what `lsusb` shows; if the USB
identity changes, the udev rule above needs the new one — run `inspect` again.

If the network the Pi sits on uses `192.168.0.0/24` (the modem's default —
confirm with `AT+USBNETIP?`, which answered `0,192.168.0.100` on the first
unit), move the modem's own LAN first with `AT+USBNETIP=…`, because while the
link is up its subnet would shadow anything the Pi reaches there. The helper
refuses to use the link in that case and says which address collides.

### Provision the NetworkManager profile

The helper never creates or edits the profile, and it **refuses to bring one
up unless it is already isolated**: no default route, no carrier routes or
DNS, no static routes or resolver entries, no autoconnect, metered, and bound
to the modem's interface. Those properties are what keep the carrier's network
out of the system's own routing and resolver tables; the firewall is the second
line behind them.

For this HAT:

```bash
sudo nmcli connection add type ethernet con-name cellular-dashboard ifname cell0 \
  ipv4.method auto \
  ipv4.never-default yes ipv4.ignore-auto-routes yes ipv4.ignore-auto-dns yes \
  ipv6.method disabled \
  ipv6.never-default yes ipv6.ignore-auto-routes yes ipv6.ignore-auto-dns yes \
  connection.autoconnect no connection.metered yes
```

Then confirm the profile passes:

```bash
sudo /usr/local/bin/usage-dashboard-cellular-link check
```

Every line must read `ok`. The same checks run again, from the helper, before
it ever brings the link up.

A HAT with a different module may be one ModemManager manages instead (QMI or
MBIM: a `wwan0` interface, and `mmcli -L` lists it). That shape was not
available to qualify; for it, install with `INSTALL_MODEM_MANAGER=1` and add the
profile as `type gsm` with `gsm.apn` (and `gsm.pin` if the SIM needs one) plus
the same `ipv4.*`/`ipv6.*`/`connection.*` isolation properties above.

### Settings

```bash
sudo cp deploy/pi/usage-dashboard-cellular.env.example /etc/usage-dashboard-cellular.env
sudo nano /etc/usage-dashboard-cellular.env && sudo chmod 600 /etc/usage-dashboard-cellular.env
```

The file is documented key by key. The six that matter:

| Key | What it must be |
|-----|-----------------|
| `SERVER_URL` | The dashboard, `https://`, port 443 — as in the GUI's env file |
| `CELLULAR_DISPLAY_KEY` | The server's `display-api-key` (see the secrets table in the root README). **Not** the GUI's `API_KEY` |
| `CELLULAR_DEST` | A **fixed public** address as an IP literal. The home WAN address behind `usage.hraedon.com` changes (DDNS), so the units pin the stable relay on beacon instead: `40.160.144.139` with `CELLULAR_DEST_PORT=8443` (homelab `vps/usage-relay`). The relay passes TLS through untouched to whatever the name resolves to, and forwards only a ClientHello for `usage.hraedon.com`. The hostname still goes out as SNI and `Host`, so the certificate is verified against the name end to end. (Pinning the ingress itself, port 443, works only where its public address is stable. The name is split-horizon, so never pin the address the home network resolves.) |
| `CELLULAR_DEST_PORT` | The TCP port on `CELLULAR_DEST`: `8443` for the beacon relay, `443` (the default) for a directly pinned ingress. The firewall permits exactly this port; `SERVER_URL` stays on 443 |
| `CELLULAR_IFACE` | `cell0` as renamed above |
| `CELLULAR_BUDGET_MB` | A monthly cap in decimal MB, required. Counted from the modem interface's own packet counters, so it includes the TLS handshake — about 20 KB a read, so the default cadence (one read per 10 minutes, only while the panel is lit) is on the order of 20 MB a month. Over the cap, the link stays off until next month. The count survives a restart and a wrong clock at boot |

### Start, watch, stop

```bash
sudo systemctl restart usage-dashboard-gui.service   # picks up CELLULAR_CACHE
sudo systemctl start usage-dashboard-cellular.service
journalctl -u usage-dashboard-cellular -f            # the worker
sudo /usr/local/bin/usage-dashboard-cellular-link status   # link, firewall, counters, routes
```

On the panel, the status line names the route while on cellular
(`… · cellular · refresh 10m …`) and states the readings' age once they are
older than the cadence explains. Tapping that line opens the diagnostics
overlay, which gains a **Cellular** row — and a **Data** row — whenever the
unit is on cellular, over budget, carrying an error, the worker has stopped,
or most of the month's data is used.

To take the feature off a unit:

```bash
sudo systemctl disable --now usage-dashboard-cellular.service
sudo systemctl disable usage-dashboard-cellular-firewall.service
sudo /usr/local/bin/usage-dashboard-cellular-link down
sudo rm /etc/usage-dashboard-cellular.env     # both units can no longer start
sudo systemctl restart usage-dashboard-gui.service   # after removing CELLULAR_CACHE
```

The worker also takes the link down whenever it stops, so there is no link
left up that nobody is supervising. The firewall itself stays until the next
reboot; `sudo nft delete table inet usage_dashboard_cellular` removes it
sooner.

### What has and has not been proven

- **The firewall and the routing, on a real kernel.**
  `tests/test_cellular_link_kernel.py` loads the helper's real ruleset and
  routing rule into the kernel (as root, with a veth "modem" and the carrier in
  its own namespace) and sends real packets: only the worker's UID reaches the
  dashboard on 443 — not on another port, not another address, not by ICMP, not
  with two addresses pinned, not over IPv6; another user forcing the interface
  gets nothing; nothing comes in or is forwarded; a refused `route` leaves no
  rule, no route and no link; a trimmed table is reloaded. Run it on the Pi
  itself:
  `sudo UD_CELLULAR_KERNEL_TEST=1 .venv/bin/pytest tests/test_cellular_link_kernel.py`
  Only `nmcli` is faked there.
- **NetworkManager's output, against the real one.** The helper's parsing was
  checked command by command against NetworkManager 1.52.1 on the unit. It
  found four places where the real tool prints other than its documentation
  suggests, each of which would have made the helper refuse every link; they
  are corrected and pinned by tests.
- **"Pin the address, verify TLS against the hostname", over a real handshake**
  (`tests/test_cellular_tls.py`).
- **Observed on the unit, read-only:** everything in the table above;
  NetworkManager adopting `usb0` as described; and the clock a week behind at
  boot (no real-time clock; it starts from its last NTP synchronisation) with
  HTTPS to the dashboard still validating — the certificate there has a
  90-day life, so a unit that was off across a renewal would fail TLS until it
  synchronises, which needs the normal route.
- **The worker's decisions** — when to fall back, when to stop spending, what a
  refresh tap may do, what to do on a 401 — are unit-tested against fakes.
- **Not yet done on hardware:** the modem provisioning above (the module was
  only queried); the module dialling by itself; anything needing a live SIM —
  APN, registration, signal, data counters against the carrier's meter; the
  boot-time unit's ordering on a real boot; the udev rename; the ModemManager
  path. Those are the bench items in
  [Plan 010](../../plans/010-pi-cellular-fallback.md), and the first work-site
  test should be one you can reach locally to roll back.

> **Trust note:** the sudoers rule lets the worker user run one root-owned
> wrapper, which runs the venv's Python and `usage_dashboard.deploy.cellular_link`
> from the run user's checkout — the same arrangement as the auto-redeploy
> helper in §6, with the same consequence: the run user effectively holds root
> through it. That is fine when the run user is the trusted sudoer who ran
> `install.sh`; do not enable this for a run user you would not already hand
> `sudo`. The worker user itself gains nothing beyond the four verbs.

## Dev / windowed mode (off the Pi)

Run in a window instead of fullscreen (no rotation, mouse acts as touch):

```bash
GUI_FULLSCREEN=0 GUI_WIDTH=1280 GUI_HEIGHT=720 \
  SERVER_URL=http://localhost:8080 API_KEY=dev \
  .venv/bin/usage-dashboard-gui
```

Press `Esc` or `q` to quit.
