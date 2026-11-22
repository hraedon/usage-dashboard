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

## References

- [Raspberry Pi OS NetworkManager guidance](https://www.raspberrypi.com/documentation/configuration/audio/)
- [ModemManager port and device detection](https://modemmanager.org/docs/modemmanager/port-and-device-detection/)
- [ModemManager WWAN device types](https://modemmanager.org/docs/modemmanager/wwan-device-types/)
- [NetworkManager connection settings](https://www.networkmanager.dev/docs/api/latest/nm-settings-nmcli.html)
- [Linux policy routing selectors](https://www.man7.org/linux/man-pages/man8/ip-rule.8.html)
- [nftables socket UID and output interface selectors](https://netfilter.org/projects/nftables/manpage.html)
