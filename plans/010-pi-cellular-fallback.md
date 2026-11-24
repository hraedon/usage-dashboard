# Plan 010 — Pi cellular fallback for dashboard readings

The work Pi usually reaches the dashboard through a phone hotspot. Give an
opted-in Pi a metered fallback when it detects its provisioned Waveshare modem
and the normal link cannot reach the dashboard. Keep Wi-Fi or Ethernet as the
preferred path. Pilot on the work Pi after bench qualification.

## Traffic contract

| Function | Wi-Fi or Ethernet | Cellular |
| --- | --- | --- |
| Read dashboard data | `GET /api/v1/readings` | Same GET over verified HTTPS |
| Apply backlight schedule | Server schedule and local fallback | Local fallback |
| Refresh button | May request a provider refresh | Re-read the stored snapshot from the server, respecting the polling rate limit |
| Updates, provisioning, SSH and other host apps | Normal network policy | Blocked |

The Pi renders the readings locally. Provider polling stays on the server.
The JSON already carries the bars, reset times, and status needed by the Pi.
Use a read-only display credential scoped to the readings route. The current
agent credential is scoped to `/api/v1/agent/capacity`, whose projection omits
display details. Qualify the display credential's route scope independently.

## Link selection and isolation

1. **Qualify the actual HAT.** Record the exact Waveshare SKU, firmware/USB
   mode, USB ID, driver, and data interface. For a ModemManager-supported
   modem, select the pre-provisioned NetworkManager profile for that device
   and SIM. A modem presenting just an Ethernet interface needs its own
   stable hardware binding. Keep the SIM PIN, APN credentials if applicable,
   and display key in the operator's local provisioning path.
2. **Isolate the cellular route.** Meter the profile and keep carrier default
   routes and DNS out of the system's normal routing and resolver tables.
   Use a separate routing table for the approved dashboard destination and
   select it only for a dedicated readings worker on fallback. Enforce
   egress on the modem interface with nftables. Permit only the worker UID to
   the approved destination addresses on TCP 443, plus any link setup packets
   the measured modem mode requires. Apply the deny policy before enabling
   the link and cover IPv4, IPv6, and forwarding. The GUI and updater currently
   share a service user, so give the readings worker a separate UID.
3. **Pin the destination for the first pilot.** Confirm an operator-managed
   address for the public dashboard ingress. Scope the address override to
   the worker and preserve the dashboard hostname for HTTPS certificate
   verification and virtual-host routing. Disable redirects and proxy
   inheritance for this worker. If the public address can change and no
   stable ingress is available, qualify how the worker will learn new addresses
   through a bounded resolver before enabling cellular. Keep carrier DNS out
   of the host resolver in either case. Check TLS from a cold boot with the
   actual Pi clock before relying on this mode.
4. **Fail over using dashboard reachability.** A connected hotspot can lack
   working internet. Use bounded timeouts on the normal route to determine
   whether to fall back. Surface authentication and rate-limit results
   without retrying them over cellular. Prefer the normal route again after a
   successful probe. Preserve the last successful payload and its timestamp
   when both routes fail, and show its age on the panel. The readings worker
   can publish atomically to a local cache that the GUI reads without network
   privileges for the cellular path.
5. **Budget the data.** Start with one poll per 10 minutes while the panel is
   awake, an immediate read on entering fallback or waking, and bounded
   retry backoff. Use the local sleep schedule and reuse HTTPS connections
   where practical. At 8 hours per workday and 22 workdays, that is about
   1,056 scheduled reads per month. At an illustrative 10 KiB per poll, that
   is about 10 MiB, before retries and other link overhead. Set the real
   budget from packet counters and the carrier meter during the pilot.

## Acceptance and rollout

- Rehearse routing and filtering using two simulated links. The dedicated
  worker reaches only the approved destination over the fallback. Prove that
  the updater UID and ordinary processes get zero cellular egress when they
  attempt DNS, Git, package downloads, NTP, or other internet traffic. Check
  both address families, including same-destination attempts by other UIDs.
- On the bench Pi, exercise modem absent, modem hot-plug, startup on LTE,
  hotspot with and without internet, hotspot recovery, sleep and wake, bad
  API token, and cellular loss. Verify the panel renders from cached readings,
  HTTPS validates, and the local backlight schedule still applies.
- Compare per-interface counters and a header-only capture with the expected
  flow and poll cadence. Verify that disabling the opt-in profile restores
  Wi-Fi-only behavior. Use local rollback access for the first work-site test.

Implementation needs the exact modem mode, carrier setup, and the work Pi's
effective NetworkManager version, network policy, clock behavior, and public
ingress addressing. This plan records the proposed behavior and test matrix.

## Implementation record (2026-11-23)

The behavior above is built. The hardware facts named in the last paragraph
of the plan were then read off the first unit with the HAT fitted (status
queries only), the helper's reading of NetworkManager was checked against the
real one, and an independent adversarial review of the code was run. Each
changed the implementation; this record supersedes the plan's assumptions
where they differ.

### What was built

| Piece | Where | Runs as |
| --- | --- | --- |
| Display credential, readings route only | `server/api.py`, `DISPLAY_API_KEY` / Secret `display-api-key` | server |
| Readings worker: failover decisions, pinned-address HTTPS, budget | `client/cellular_worker.py` | `usage-dashboard-cellular` |
| Link helper: firewall first, profile checks, route selection, verification, unwinding | `deploy/cellular_link.py` behind `/usr/local/bin/usage-dashboard-cellular-link` | root, via sudo |
| Boot-time deny policy | `deploy/pi/usage-dashboard-cellular-firewall.service` | root, before NetworkManager |
| Cache and signals between worker and GUI | `client/readings_cache.py`, `client/cache_view.py` | both |
| Panel: route and age on the status line, refresh on cellular, overlay rows | `client/gui.py`, `layout.py`, `diagnostics.py` | GUI user |
| Installer (opt-in, by hand) | `deploy/pi/cellular-install.sh` | operator |

### Qualification record — the first unit, 2026-11-23

Read-only, over SSH as the owner's `itadmin`, nothing changed on it.

| Fact | Value |
| --- | --- |
| HAT | Waveshare Cat-1/GNSS HAT, SIMCom `SIM7670G-MNGV`, firmware V1.9.05 |
| USB identity | `05c6:9330` ("QualComm Compo") — a Qualcomm composite ID, not SIMCom's `1e0e`; behind the HAT's own two hubs |
| Data interface | `usb0`, driver `rndis_host` (`AT$MYCONFIG?` → `usbnetmode`,0 = RNDIS) |
| Serial ports | `/dev/ttyACM0` AT, `ACM1` diagnostic stream, `ACM2` AT/modem, `ACM3` NMEA |
| DHCP from the module | `192.168.0.100/24`, gateway `192.168.0.1`, two DNS servers that answer nothing |
| Auto-dial | off (`AT+DIALMODE?` → 1); no APN; not registered; signal `99,99`; SIM ready |
| System | Debian 13, kernel 6.18, NetworkManager 1.52.1, `nftables` installed and unused, no ModemManager, no real-time clock |
| Public ingress | split-horizon: LAN resolvers return a private address, public resolvers the WAN address; the WAN address served the right certificate from inside the LAN (not evidence about outside) |

Three things observed on that unit changed the design:

- **NetworkManager adopted the modem.** It generated `Wired connection 2` for
  `usb0` (auto-connect, DHCP), which became the Pi's preferred default route
  into a modem with no service, and put the modem's two dead DNS servers first
  in the resolver list (about 8 s per lookup; NTP stopped syncing). The helper
  now refuses to use the link while such a profile exists, and the deny policy
  is loaded at boot.
- **NetworkManager prints other than documented.** Four differences, each of
  which would have made the helper refuse every link, are corrected and pinned.
- **The clock was seven days behind at boot** (no RTC; it starts from the last
  NTP synchronisation) and HTTPS to the dashboard still validated. The "clock
  not set" guard therefore tolerates an hour, not zero — and the ledger no
  longer resets when the clock is behind.

### Decisions made while implementing

- **The cellular route is selected only around a cellular read.** `route`
  brings the link up behind the firewall and adds the worker's rule, the read
  happens, `unroute` removes the rule; the link itself stays up between reads.
  Held for the whole fallback, the rule would also catch the worker's own
  "has the normal route recovered?" probe — to the same address — and answer
  it over cellular. A test pins the order `normal probe → route → read →
  unroute → normal probe`.
- **A refusal leaves nothing behind.** The helper installs the rule and route
  and then verifies them; a verification that fails unwinds both and takes the
  link down, on a link it had just brought up or one already up between reads.
  The worker follows every failed `route` with `down`, systemd takes the link
  down whenever the worker stops (`ExecStopPost`), and the helper keeps a
  deadline below the worker's so it unwinds rather than being killed
  half-installed. (The independent review found the original left the rule
  installed and the worker polling over cellular, uncounted.)
- **Failover needs two unanswered probes.** One lost packet should not cost an
  LTE attach. Any HTTP status, including 401 and 429, is "reachable".
- **The data budget is a required setting, not a default.** Counted from the
  modem interface's own sysfs counters, so it includes handshakes and
  keepalives. Over the cap, the link is released until the next month. The
  month rolls forward only, so a clock behind at boot cannot hand out a second
  budget.
- **A refresh tap may bring a read forward, never past a refusal.** It is
  limited by the minimum gap between cellular reads and by any wait that
  follows a read that failed or was refused.
- **Cellular reads only while the panel is lit.** The GUI writes a heartbeat
  the worker reads; the worker has no other way to know anyone is looking.
- **The helper refuses rather than repairs.** It never creates or edits the
  NetworkManager profile. It checks every isolation property before bringing
  the link up, re-checks the live system after (default routes, resolver,
  bound device, subnet collisions), and proves with `ip route get … uid` that
  the worker is selected and nobody else is.
- **The firewall exists from boot.** A oneshot unit loads it before
  `network-pre.target`; the worker loads it again at start. It is re-checked
  against a stateless kernel listing every `route`, so a table someone trimmed
  is reloaded rather than trusted.
- **RNDIS stays for the pilot.** It is the mode the unit was qualified in.
  ECM (`usbnetmode`,1) is documented by the vendor but was not tried.
- **Install-only, precisely.** The units, the wrapper and the sudoers rule are
  never auto-redeployed (a test pins it); the helper's logic is Python in the
  package and updates with everything else. The settings file and the
  NetworkManager profile are written by hand because they carry secrets.

### Acceptance status

| Item | Status |
| --- | --- |
| Routing and filtering rehearsed on simulated links | **Done, on a real kernel** — `tests/test_cellular_link_kernel.py` (root; veth as the modem, the carrier in its own network namespace, real `nft` and `ip`, real packets). Only the worker UID reaches the pinned address on 443: not another port, not another address, not by ICMP, not with two addresses pinned, not over IPv6; root and an ordinary user cannot, even forcing the interface; nothing comes in or is forwarded; a refused `route` leaves no rule, no route and no link; a trimmed table is reloaded. Only `nmcli` is faked |
| "Pin the address, verify TLS against the hostname" | **Done, over a real handshake** — `tests/test_cellular_tls.py` |
| NetworkManager output shapes | **Checked against the real 1.52.1** on the unit; four corrections, pinned by tests |
| Worker decisions (hotspot with and without internet, recovery, sleep and wake, bad token, cellular loss, modem absent, refused route, refresh limits, wrong clock) | **Unit-tested against fakes** — `tests/test_cellular_worker.py`, `test_cellular_gui.py` |
| Modem identity, USB mode, defaults, clock at boot | **Read off the unit** (table above) |
| Provisioning the modem (APN, auto-dial) and the module dialling by itself | **Not done** — the modem was only queried |
| Anything needing a live SIM: registration, signal, HTTPS from the Pi's clock over LTE, per-interface counters against a carrier meter | **Needs the bench** — the unit read no signal, which usually means no antennas fitted |
| Boot ordering of the firewall unit, the udev rename, the ModemManager path | **Needs the bench** |
| Disabling the opt-in restores Wi-Fi-only | By construction (separate user and units; steps in `deploy/pi/README.md` §8); **confirm on the bench** |

### Independent review

An adversarial review of the code (a separate pass, read-only) found two
high-severity defects and several smaller ones. Each was checked against the
code before being acted on:

- a refused `route` left the worker's routing rule installed, and the worker's
  next normal-route probe then went over cellular, uncounted, and was reported
  as a recovery — fixed by unwinding in the helper, `down` after every failed
  `route`, `ExecStopPost`, and the helper's own deadline;
- the firewall existed only after the worker's first `route` since boot —
  fixed by the boot-time unit and the `firewall` verb;
- a refresh tap bypassed the failure back-off and the "401/429 is not retried"
  rule — fixed;
- the month's ledger reset when the clock was behind at boot — fixed;
- the firewall check for two pinned addresses depended on nftables' listing
  order, which differs from text order — confirmed on a real kernel and fixed;
- the firewall's own check trusted a stamp rather than the kernel — now
  compared with a stateless listing;
- with IPv6 enabled, neighbour-discovery advertisements were dropped on the
  way in (found by the extended kernel rehearsal, not the review) — fixed.

### Still needed before the first work-site test

1. Provision the modem on the unit — APN, `AT+DIALMODE=0` — and fit antennas;
   confirm registration and signal.
2. Create the isolated NetworkManager profile and remove the one NetworkManager
   generated for the modem; confirm `usage-dashboard-cellular-link check`
   passes on the unit.
3. ~~Confirm that the public ingress address is stable enough to pin.~~ It is
   not: the home WAN address behind `usage.hraedon.com` changes (Porkbun
   DDNS). Resolved 2026-11-24 with a stable relay rather than a resolver on
   the metered path: HAProxy on beacon (`40.160.144.139:8443`, homelab
   `vps/usage-relay`) passes the TLS stream through to the DDNS name, checked
   by SNI, re-resolved every minute. The units pin `CELLULAR_DEST=40.160.144.139`,
   `CELLULAR_DEST_PORT=8443` (new setting, both the worker and the helper; the
   firewall permits exactly that port). TLS is still verified end to end
   against the dashboard's name, so the relay never sees the display key.
   Verified through the relay: a valid certificate and 401 without a key; a
   ClientHello for another name is dropped.
4. A `display-api-key` in the server Secret and a rollout restart (the drift
   guard reports both until then).
5. The bench items above, with local rollback access.

## References

- [Raspberry Pi OS NetworkManager guidance](https://www.raspberrypi.com/documentation/configuration/audio/)
- [ModemManager port and device detection](https://modemmanager.org/docs/modemmanager/port-and-device-detection/)
- [ModemManager WWAN device types](https://modemmanager.org/docs/modemmanager/wwan-device-types/)
- [NetworkManager connection settings](https://www.networkmanager.dev/docs/api/latest/nm-settings-nmcli.html)
- [Linux policy routing selectors](https://www.man7.org/linux/man-pages/man8/ip-rule.8.html)
- [nftables socket UID and output interface selectors](https://netfilter.org/projects/nftables/manpage.html)
