# Plan 002 — Scheduled backlight sleep + tap-to-wake

**Status:** planned 2026-06-22

> Goal: let the touch panels turn their backlight off on a time-of-day schedule
> (to save the backlight and stop a near-static image glowing all night), and
> wake on a tap. Schedules must be updatable remotely, without reflashing or
> re-running `install.sh` on each unit.

## Problem

The dashboard is a glanceable instrument panel that's powered on 24/7. For long
idle stretches (overnight, weekends on the work unit) it shows a near-static
image. The Touch Display 2 is an IPS LCD, so there's no permanent burn-in risk,
but the LED backlight ages (uniformly) under continuous full-brightness use, and
an always-lit panel overnight is just noise. We want it dark when nobody's
looking, and instantly back when they are.

## Decisions (settled with the user 2026-06-22)

- **Daily window:** sleep `00:00–08:00`, every unit.
- **Unified schedule with weekend sleep:** all units also sleep
  `Fri 18:00 → Mon 08:00`. One schedule for the whole fleet; per-unit override
  stays available (see remote delivery) if the personal units should later stay
  awake on weekends.
- **Tap-to-wake semantics — "skip to the next sleep event":** a tap during a
  sleep window keeps the panel awake until **the earlier of (a) the current
  sleep window's natural end, or (b) the next local midnight**, then it
  re-evaluates and sleeps again if still scheduled to. Worked examples:
  - Tap **Fri 8pm** (in the weekend block) → next midnight Sat 00:00 → on until
    **Friday midnight**, then re-sleeps. *(This is the behaviour the user asked
    for by name.)*
  - Tap **Sat 2pm** → on until Sunday midnight.
  - Tap **Tue 2am** (in the nightly window) → window ends 08:00, before next
    midnight → wakes until its normal **8am** (no 22-hour-on surprise).
- The first tap that wakes the panel **only wakes it** — it is swallowed, not
  also routed into a tile/detail tap.

## Why tap-to-wake is feasible here

On this hardware the **touchscreen is independent of the backlight**: the Goodix
controller keeps emitting touch events with the backlight off, and the pygame
GUI (`client/gui.py`) already runs an event loop at ~10fps consuming
`FINGERDOWN`. So "blank the panel but keep listening" needs no DPMS/X tricks —
the client turns the backlight off via sysfs and watches for the next touch.

## Architecture

**Sleep/wake + backlight + tap logic lives in the pygame client.** It already
owns the loop, the touch events, and is the X foreground app. No new process.

- **Backlight control:** a small helper writes
  `/sys/class/backlight/<panel>/bl_power` (`0` = on, `1` = off). The GUI service
  unit already has `SupplementaryGroups=video`, which RPi udev normally makes
  the backlight node writable by. No-ops gracefully when no backlight device is
  present (dev machine / windowed mode).
- **Schedule source — served by the k8s server, with a local fallback.** The
  client fetches its schedule on the same poll it already makes for readings,
  keyed per unit (its API key / a `UNIT_ID`). It caches the schedule and falls
  back to a baked-in default (`SLEEP_SCHEDULE` env on the unit) if the server is
  unreachable, so a network blip never strands a panel in the wrong state.
  **Remote update = change the server-side schedule config and roll out; every
  client picks it up on its next poll, no client redeploy.**
- **Power note:** while asleep the client can also pause/relax server polling
  (it's only reading cached readings); resume on wake. Minor, optional.

## Work items

### Slice 1 — client-side sleep + tap-to-wake (local default)
1. `client/schedule.py` (pure, unit-tested): parse a schedule spec into sleep
   windows; `is_asleep(now)`; `wake_until(now)` implementing the "earlier of
   window-end or next midnight" rule. No I/O — testable like `layout`/`format`.
2. `client/backlight.py`: locate the backlight device, `set_power(on: bool)`;
   safe no-op when absent.
3. `gui.py` loop integration: track a `wake_until` override; on each tick decide
   on/off; swallow the wake tap; (optional) throttle rendering while dark.
4. `SLEEP_SCHEDULE` env in `usage-dashboard-gui.env(.example)` as the local
   default/fallback; document in README.
5. Unit tests for `schedule.py` (the worked examples above) and `backlight.py`
   (no-op path).

### Slice 2 — server-served per-unit schedules (remote update)
6. Server: schedule config (ConfigMap/env or DB-backed), keyed by unit; expose
   via the existing client API (extend the readings response or add `/schedule`).
7. Client fetcher: send unit identity, receive + cache schedule, fall back to the
   Slice-1 local default on error.
8. Docs: how to change a unit's schedule and roll out.

### Deploy
- Client changes ride the existing **auto-update** path (no image rebuild).
- Server change (Slice 2) is an image rebuild + `kubectl rollout restart`,
  same as any server change.

## On-device findings (confirmed 2026-06-22 on mpmusage01/02)
- Device is `/sys/class/backlight/panel_backlight@1`. `bl_power` is **root-only**
  (`-rw-r--r-- root root`), but `brightness` is **`video`-group writable**
  (`-rw-rw-r-- root video`, range 0–31). The GUI user (`itadmin`) is in `video`.
- `brightness=0` is **fully dark** (verified by eye), so we drive `brightness`
  (off=0, wake=prior level) — no udev rule, no privileged helper, no `install.sh`
  re-run; ships over the client auto-update path. The real `backlight.py` was
  exercised against the live panel (15→0→15, redundant write skipped).

## Open questions / to confirm
- Whether to also pause server polling while asleep (power vs. instant-fresh on
  wake). Lean: pause, refresh once on wake.
- Schedule spec format (keep it small): e.g.
  `daily:00:00-08:00;fri:18:00-;sat,sun:all;mon:-08:00` — finalise in Slice 1.
- Which unit is "work" for any future per-unit divergence (`mpmusage02` per the
  user's note); irrelevant while the schedule is unified.
