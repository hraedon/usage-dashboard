"""Rehearsal on a real kernel: the cellular firewall and routing, as root.

Everything else in this suite proves the helper *decides* correctly against a
fake system. This module proves the decisions survive contact with Linux: it
stands in for the modem with a veth pair, runs the real ``route()`` so the
rendered nftables ruleset and the policy route actually go into the kernel, and
then sends real packets as different users and reads the kernel's own drop
counters back. Only ``nmcli`` is faked — NetworkManager cannot be run inside a
test — so the firewall, the routing rule, the route selection and the counters
are all genuine. The fake answers in the shapes a real NetworkManager 1.52 was
seen to print (see ``tests/test_cellular_link.py`` for what those are).

It is Plan 010's first acceptance step ("rehearse routing and filtering using
simulated links"), kept runnable rather than performed once and written up.
It needs root, veth, network namespaces and nftables, so it is skipped unless
asked for:

    sudo UD_CELLULAR_KERNEL_TEST=1 .venv/bin/pytest tests/test_cellular_link_kernel.py

The simulated topology. The "Pi" is the test host's own namespace; the carrier
network is a separate namespace, because a real modem's far side is not the Pi:

    [ Pi ]  udcell0 10.99.0.2/30 ── udpeer0 10.99.0.1/30  [ carrier namespace ]
                                                     which also hosts
                                                     192.0.2.10  the pinned dashboard
                                                     192.0.2.9   a second pinned address
                                                     198.51.100.7  an ordinary host
                                                     2001:db8:1::10  the same, over IPv6

The dashboard is deliberately *not* on the modem's own subnet. A connected
route for that subnet lands in the main table whatever the profile says, so an
on-link address would make "nobody else can route to the dashboard through the
modem" trivially false and prove nothing. Behind the carrier gateway it is only
reachable through the table the helper owns — as a public address is. And it is
deliberately not an address of the Pi itself: the kernel answers for its own
addresses from the ``local`` table before any other rule is consulted, which
would bypass the very rule under test.

``192.0.2.10`` and ``192.0.2.9`` are pinned together in one phase on purpose:
their text order and numeric order differ, and nftables lists a set by value.
The helper verifies its own rule by comparing the kernel's listing, so writing
the set in text order makes it refuse its own firewall — a failure only two
addresses can show.

Every probe that is expected to be dropped is aimed where only the modem could
carry it, so nothing here ever depends on — or leaks onto — the host's real
network.
"""
from __future__ import annotations

import os
import re
import select
import socket
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import pytest

from usage_dashboard.deploy import cellular_link as cl

pytestmark = pytest.mark.skipif(
    (
        os.environ.get("UD_CELLULAR_KERNEL_TEST") != "1"
        or sys.platform != "linux"
        or getattr(os, "geteuid", lambda: 1)() != 0
    ),
    reason="needs root on Linux; opt in with UD_CELLULAR_KERNEL_TEST=1",
)

IFACE = "udcell0"                 # the "modem" interface, on the Pi
PEER = "udpeer0"                  # the carrier's end of it
CARRIER_NS = "ud-rehearse-carrier"
LAN_NS = "ud-rehearse-lan"        # a network behind the Pi, for forwarding
LAN_IFACE = "udlan0"
MODEM_ADDRESS = "10.99.0.2/30"
GATEWAY = "10.99.0.1"
PIN = "192.0.2.10"                # the dashboard, behind the carrier gateway
PIN_LOW = "192.0.2.9"             # sorts before PIN by value, after it as text
OTHER = "198.51.100.7"            # an ordinary internet host, also behind it
V6_GATEWAY = "2001:db8:1::1"
# A /126 holds the two link ends and nothing else. A /64 would put the
# dashboard below on the modem's own subnet, where a connected route in the
# main table carries everyone — the helper refuses exactly that, and did.
V6_MODEM = "2001:db8:1::2/126"
V6_PIN = "2001:db8:1::10"
WORKER_UID = 12345                # needs no passwd entry; setpriv sets it
ORDINARY_UID = 1000               # the uid a Pi's login user has
TABLE = 17010
PREF = 17010
# The probes run as other uids, so they need an interpreter every uid can
# read. A venv's Python often lives under a home directory they cannot enter.
PROBE_PYTHON = "/usr/bin/python3" if os.access("/usr/bin/python3", os.X_OK) else sys.executable


def _run(*command: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command), capture_output=True, text=True, check=check, timeout=60
    )


# -- the stand-in for NetworkManager ------------------------------------------


class FakeNetworkManager:
    """Answers ``nmcli`` the way an isolated, activated profile would, and moves
    the interface's link state to match. Every other command runs for real."""

    PROFILE = {
        "connection.type": "802-3-ethernet",
        "connection.interface-name": IFACE,
        "connection.autoconnect": "no",
        "connection.multi-connect": "0",
        "connection.metered": "yes",
        "ipv4.method": "manual",
        "ipv4.never-default": "yes",
        "ipv4.ignore-auto-routes": "yes",
        "ipv4.ignore-auto-dns": "yes",
        "ipv4.route-table": "0",
        "ipv6.method": "disabled",
        "ipv6.never-default": "yes",
        "ipv6.ignore-auto-routes": "yes",
        "ipv6.ignore-auto-dns": "yes",
        "ipv6.route-table": "0",
    }
    NAME = "cellular-test"
    RUNTIME = {
        "IP4.ADDRESS": "10.99.0.2/30",
        "IP6.ADDRESS": "",
        "IP4.GATEWAY": GATEWAY,
    }

    def __init__(self) -> None:
        self.active = False
        self.log: list[tuple[str, ...]] = []
        self.dns: list[str] = []                   # what the connection hands out
        self.others: list[tuple[str, str]] = []    # (name, type) of other profiles

    def __call__(self, command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.log.append(tuple(command))
        if command[0] != "nmcli":
            return cl.real_runner(command, timeout)
        args = [a for a in command[1:] if a not in ("--terse", "--wait", "10", "30", "60")]
        return self._nmcli(args)

    def _nmcli(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        def out(text: str) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, text, "")

        def fail(text: str) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 1, "", text)

        if "up" in args:
            self.active = True
            _run("ip", "link", "set", "dev", IFACE, "up")
            return out("Connection successfully activated\n")
        if "down" in args:
            self.active = False
            _run("ip", "link", "set", "dev", IFACE, "down")
            return out("")
        listing = args[args.index("--fields") + 1] if "--fields" in args else ""
        if listing == "NAME,TYPE":
            rows = self.others + [(self.NAME, self.PROFILE["connection.type"])]
            return out("".join(f"{name}:{kind}\n" for name, kind in rows))
        if listing == "NAME,DEVICE":
            return out(f"{self.NAME}:{IFACE}\n" if self.active else "")
        for flag in ("--get-values", "--fields"):
            if flag in args:
                field = args[args.index(flag) + 1]
                break
        else:
            return fail(f"unsupported fake nmcli call: {args}")
        if flag == "--get-values":
            if args[-1] != self.NAME:
                # Another profile, as NetworkManager generates one for a device
                # it has none for: bound to the port by name, auto-connecting.
                known = {
                    "connection.autoconnect": "yes",
                    "connection.interface-name": IFACE,
                    "802-3-ethernet.mac-address": "",
                    "match.interface-name": "",
                }
                return out("".join(known.get(name, "") + "\n" for name in field.split(",")))
            names = field.split(",")
            if names == list(cl._PROFILE_SINGLE):
                return out("\n".join(self.PROFILE[n] for n in names) + "\n")
            if field == "GENERAL.STATE":
                return out("activated\n" if self.active else "\n")
            if field == "GENERAL.DEVICES":
                return out(IFACE + "\n" if self.active else "\n")
            if field in ("ip4.dns", "ip6.dns"):
                return out(" | ".join(self.dns) + "\n")
            if field in self.RUNTIME:
                return out((self.RUNTIME[field] if self.active else "") + "\n")
        else:
            # Terse multi-valued settings: every one empty.
            return out("".join(f"{name}:\n" for name in field.split(",")))
        return fail(f"unknown field {field!r}")


# -- fixtures ----------------------------------------------------------------------


def _teardown() -> None:
    _run("ip", "link", "del", IFACE)
    _run("ip", "netns", "del", CARRIER_NS)
    _run("ip", "netns", "del", LAN_NS)
    _run("ip", "link", "del", LAN_IFACE)
    _run("nft", "delete", "table", "inet", cl.TABLE_NAME)
    for family in ("-4", "-6"):
        _run("ip", family, "rule", "del", "pref", str(PREF))
        _run("ip", family, "route", "flush", "table", str(TABLE))


@pytest.fixture(scope="module")
def lab(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """The simulated modem link, the carrier namespace behind it, and a helper
    wired to the fake NetworkManager."""
    run_dir = tmp_path_factory.mktemp("cellular-link")
    _teardown()   # an earlier interrupted run must not skew this one
    _run("ip", "netns", "add", CARRIER_NS, check=True)
    _run("ip", "link", "add", IFACE, "type", "veth", "peer", "name", PEER, check=True)
    _run("ip", "link", "set", "netns", CARRIER_NS, "dev", PEER, check=True)
    _run("ip", "netns", "exec", CARRIER_NS, "ip", "link", "set", "dev", PEER, "up", check=True)
    for address in (f"{GATEWAY}/30", f"{PIN}/32", f"{PIN_LOW}/32", f"{OTHER}/32",
                    f"{V6_GATEWAY}/126", f"{V6_PIN}/128"):
        _run("ip", "netns", "exec", CARRIER_NS, "ip", "address", "add", address,
             "dev", PEER, "nodad", check=True)
    # The modem's interface starts with no link, as a real one does before the
    # profile is brought up.
    _run("ip", "address", "add", MODEM_ADDRESS, "dev", IFACE, check=True)
    nm = FakeNetworkManager()
    settings = cl.LinkSettings(
        connection=FakeNetworkManager.NAME, interface=IFACE, dest_v4=(PIN,), dest_v6=(),
        worker_user="a-worker", worker_uid=WORKER_UID, table=TABLE, rule_pref=PREF,
        link_setup=frozenset({cl.DHCP}), ipv6=False,
        resolv_conf=Path("/etc/resolv.conf"),
        resolved_conf=Path("/run/systemd/resolve/resolv.conf"),
        run_dir=run_dir,
    )
    yield {"nm": nm, "helper": cl.LinkHelper(settings, runner=nm), "settings": settings}
    _teardown()


_LISTEN = """
import socket, sys, time
sockets = []
for spec in sys.argv[1:]:
    host, port = spec.rsplit(":", 1)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, int(port)))
    sock.listen(16)
    sock.setblocking(False)
    sockets.append(sock)
print("ready", flush=True)
while True:
    for sock in sockets:
        try:
            connection, _ = sock.accept()
            connection.close()
        except BlockingIOError:
            pass
    time.sleep(0.02)
"""


@pytest.fixture(scope="module")
def carrier_servers(lab: dict[str, Any]) -> Iterator[None]:
    """A listening server behind the "carrier" on every address and port the
    probes use, so an allowed connection completes and a failure can only be
    the firewall's doing."""
    targets = [f"{PIN}:443", f"{PIN}:8443", f"{PIN_LOW}:443", f"{OTHER}:443",
               f"{GATEWAY}:443", f"{V6_PIN}:443", f"{V6_GATEWAY}:443"]
    server = subprocess.Popen(
        ["ip", "netns", "exec", CARRIER_NS, sys.executable, "-I", "-u", "-c", _LISTEN, *targets],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert server.stdout is not None
        ready, _unread, _exceptional = select.select([server.stdout], [], [], 20)
        assert ready, "the carrier-side server did not start"
        assert server.stdout.readline().strip() == "ready"
        yield
    finally:
        server.terminate()
        server.wait(timeout=10)


# -- sending packets as a given user ---------------------------------------------


def _as(uid: int, *command: str) -> subprocess.CompletedProcess[str]:
    """Run *command* as *uid*, which needs no passwd entry."""
    return _run("setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", *command)


def _probe(uid: int, script: str, *args: str) -> str:
    proc = _as(uid, PROBE_PYTHON, "-I", "-c", script, *args)
    return proc.stdout.strip() or proc.stderr.strip()[-120:]


_CONNECT = """
import socket, sys
host, port, device = sys.argv[1], int(sys.argv[2]), sys.argv[3]
family = socket.AF_INET6 if ":" in host else socket.AF_INET
sock = socket.socket(family, socket.SOCK_STREAM)
sock.settimeout(1.5)
if device:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device.encode() + b"\\0")
try:
    sock.connect((host, port))
    print("connected")
except TimeoutError:
    print("timeout")          # no answer at all: a drop
except OSError as exc:
    print("errno", exc.errno)  # the kernel refused before anything was sent
"""

_UDP = """
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(1.5)
try:
    sock.sendto(b"query", (host, port))
    sock.recvfrom(512)
    print("answered")
except TimeoutError:
    print("timeout")
except OSError as exc:
    print("errno", exc.errno)
"""

# Bind the DHCP client's port and send to the DHCP server's port, on the modem.
_DHCP_CLIENT = """
import socket, sys
host, device = sys.argv[1], sys.argv[2]
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device.encode() + b"\\0")
    sock.bind(("", 68))
    sock.sendto(b"discover", (host, 67))
    print("sent")
except OSError as exc:
    print("errno", exc.errno)
"""

# One ICMP echo request on the modem, from a raw socket (root only). The
# kernel reports a netfilter drop to a raw socket at the send, as EPERM.
_PING = """
import os, socket, struct, sys
host, device = sys.argv[1], sys.argv[2]
sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device.encode() + b"\\0")
sock.settimeout(1.5)
packet = struct.pack("!BBHHH", 8, 0, 0, os.getpid() & 0xFFFF, 1) + b"usage-dashboard"
try:
    sock.sendto(packet, (host, 0))
except OSError as exc:
    print("errno", exc.errno)   # refused at the send: the firewall's drop
    sys.exit(0)
try:
    sock.recvfrom(1024)
    print("replied")
except TimeoutError:
    print("timeout")
"""


def _connect(uid: int, host: str, port: int = 443, device: str = "") -> str:
    return _probe(uid, _CONNECT, host, str(port), device)


def _udp(uid: int, host: str, port: int) -> str:
    return _probe(uid, _UDP, host, str(port))


DROPPED = "timeout"                # a stream: the firewall gives no answer at all
REFUSED = "errno 1"                 # a datagram: the drop is reported at the syscall
UNROUTABLE = "errno 101"            # ENETUNREACH: no route out of this host
NO_PRIVILEGE = "errno 13"           # EACCES: a port below ip_unprivileged_port_start


def _drop_packets() -> dict[str, int]:
    """Per-chain drop counters, read from the kernel's own listing of the table."""
    listed = _run("nft", "list", "table", "inet", cl.TABLE_NAME).stdout
    counters: dict[str, int] = {}
    chain = ""
    for line in listed.splitlines():
        found = re.search(r"chain (\S+) \{", line)
        if found:
            chain = found.group(1)
        found = re.search(r"counter packets (\d+) bytes \d+ drop", line)
        if found:
            counters[chain] = counters.get(chain, 0) + int(found.group(1))
    return counters


def _route_get(family: str, dest: str, uid: int) -> str:
    proc = _run("ip", family, "route", "get", dest, "uid", str(uid))
    return proc.stdout.strip()


def _interface_exists() -> bool:
    return _run("ip", "link", "show", "dev", IFACE).returncode == 0


def _link_is_up() -> bool:
    return "LOWER_UP" in _run("ip", "link", "show", "dev", IFACE).stdout


def _nothing_left() -> None:
    """What a refused or finished ``route`` must leave: no rule, no route in the
    helper's table, no link."""
    assert _run("ip", "-4", "rule", "show", "pref", str(PREF)).stdout.strip() == ""
    assert _run("ip", "-6", "rule", "show", "pref", str(PREF)).stdout.strip() == ""
    assert _run("ip", "-4", "route", "show", "table", str(TABLE)).stdout.strip() == ""
    assert _run("ip", "-6", "route", "show", "table", str(TABLE)).stdout.strip() == ""
    assert _link_is_up() is False


def _helper(lab: dict[str, Any], **changes: Any) -> cl.LinkHelper:
    return cl.LinkHelper(replace(lab["settings"], **changes), runner=lab["nm"])


# -- the rehearsal --------------------------------------------------------------------


class TestRehearsal:
    def test_the_modem_starts_down_and_unfirewalled(self, lab: dict[str, Any]) -> None:
        assert _link_is_up() is False
        assert _run("nft", "list", "table", "inet", cl.TABLE_NAME).returncode != 0
        # Asked for as a routing decision, not sent: with no route through the
        # modem this host would use its own real default route instead.
        assert f"dev {IFACE}" not in _route_get("-4", PIN, WORKER_UID)

    def test_route_puts_the_firewall_in_place_before_the_link_exists(
        self, lab: dict[str, Any]
    ) -> None:
        lab["helper"].route()
        log = lab["nm"].log
        loaded = next(
            i for i, c in enumerate(log)
            if os.path.basename(c[0]) == "nft" and c[1] == "--file"
        )
        link_up = next(i for i, c in enumerate(log) if c[0] == "nmcli" and "up" in c)
        assert loaded < link_up, "the deny policy must be in place before the link is up"

    def test_the_kernel_holds_exactly_the_policy_we_rendered(self) -> None:
        listed = _run("nft", "list", "table", "inet", cl.TABLE_NAME).stdout
        # The strings the helper verifies against, as the kernel really prints
        # them. If nftables' formatting ever differed, route() would refuse
        # every attempt — this is what tells us it does not.
        assert f'oifname "{IFACE}" jump cellular_output' in listed
        assert f'iifname "{IFACE}" jump cellular_input' in listed
        # nftables prints a one-address allow-list as a bare address and only
        # uses set braces from two addresses up.
        assert f"meta skuid {WORKER_UID} ip daddr {PIN} tcp dport 443" in listed
        assert "udp sport 68 udp dport 67 accept" in listed
        assert "type filter hook forward priority filter; policy accept;" in listed
        # The end of every cellular chain is a counted drop — two in the
        # forward chain, one per direction.
        drops = re.findall(r"counter packets \d+ bytes \d+ drop", listed)
        assert len(drops) == 4
        assert "ip6 daddr" not in listed, "IPv6 is disabled: there is no IPv6 allow rule"

    def test_the_worker_uid_reaches_the_dashboard_and_only_the_dashboard(
        self, lab: dict[str, Any], carrier_servers: None
    ) -> None:
        assert _connect(WORKER_UID, PIN) == "connected"
        before = _drop_packets()
        # Same server, a port the link does not permit.
        assert _connect(WORKER_UID, PIN, port=8443) == DROPPED
        # An ordinary internet host: no route in the worker's table, and the
        # firewall would refuse it anyway.
        assert f"dev {IFACE}" not in _route_get("-4", OTHER, WORKER_UID)
        # The carrier gateway, which *is* on the modem's subnet and so is
        # routable for everyone: the address allow-list is what stops it.
        assert _connect(WORKER_UID, GATEWAY) == DROPPED
        assert _udp(WORKER_UID, GATEWAY, 53) == REFUSED      # DNS over the modem
        assert _udp(WORKER_UID, GATEWAY, 123) == REFUSED     # NTP over the modem
        after = _drop_packets()
        assert after["cellular_output"] > before["cellular_output"]

    def test_another_user_cannot_use_the_modem_even_for_the_pinned_address(
        self, lab: dict[str, Any], carrier_servers: None
    ) -> None:
        # Nothing else has a route to the dashboard through the modem...
        for uid in (0, ORDINARY_UID, WORKER_UID + 1):
            chosen = _route_get("-4", PIN, uid)
            assert f"dev {IFACE}" not in chosen, (uid, chosen)
            assert f"table {TABLE}" not in chosen, (uid, chosen)
        # ...and if a process forces the interface anyway, the uid match is what
        # stops it. Root can bind to a device; an address on the modem's own
        # subnet is routable for it; it still gets nothing.
        before = _drop_packets()
        forced = _connect(0, GATEWAY, device=IFACE)
        if "PermissionError" in forced:
            pytest.skip("SO_BINDTODEVICE is not permitted for root here")
        assert forced == DROPPED
        assert _drop_packets()["cellular_output"] > before["cellular_output"]

    def test_icmp_does_not_cross_the_modem(self) -> None:
        # A raw socket on the modem, as root: no uid or port to hide behind.
        # The kernel reports a netfilter drop to a raw socket at the send, as
        # it does for a datagram — so the request is refused, not unanswered.
        before = _drop_packets()
        assert _probe(0, _PING, GATEWAY, IFACE) == REFUSED
        assert _drop_packets()["cellular_output"] > before["cellular_output"]

    def test_the_dhcp_allowance_is_root_only_by_the_kernels_own_rule(self) -> None:
        # The one allowance on the link that is not the worker's is a DHCP
        # client's request. Binding its port (68) needs privilege on a default
        # kernel, so an ordinary user cannot use that allowance as a channel.
        before = _drop_packets()
        assert _probe(WORKER_UID, _DHCP_CLIENT, GATEWAY, IFACE) == NO_PRIVILEGE
        assert _probe(ORDINARY_UID, _DHCP_CLIENT, GATEWAY, IFACE) == NO_PRIVILEGE
        assert _drop_packets() == before          # nothing was sent
        # Root's DHCP client (NetworkManager's) does get through.
        assert _probe(0, _DHCP_CLIENT, GATEWAY, IFACE) == "sent"
        assert _drop_packets() == before          # ...and is not a drop

    def test_nothing_comes_in_from_the_modem_unasked(self, carrier_servers: None) -> None:
        # A service listening on the Pi at its modem address. The carrier side
        # tries to reach it: the link is for requests the Pi makes, and only
        # their replies come back.
        service = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        service.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        service.bind(("10.99.0.2", 8443))
        service.listen(4)
        service.settimeout(0.2)
        stopped = threading.Event()

        def accept() -> None:
            while not stopped.is_set():
                try:
                    connection, _ = service.accept()
                    connection.close()
                except OSError:
                    pass

        threading.Thread(target=accept, daemon=True).start()
        try:
            probe = _run("ip", "netns", "exec", CARRIER_NS, PROBE_PYTHON, "-I", "-c",
                         _CONNECT, "10.99.0.2", "8443", "")
            assert probe.stdout.strip() == DROPPED, probe.stdout + probe.stderr
        finally:
            stopped.set()
            service.close()

    def test_the_route_is_selected_by_uid_not_by_destination(self) -> None:
        as_worker = _route_get("-4", PIN, WORKER_UID)
        assert f"dev {IFACE}" in as_worker and f"table {TABLE}" in as_worker
        as_root = _route_get("-4", PIN, 0)
        assert f"dev {IFACE}" not in as_root and f"table {TABLE}" not in as_root
        rule = _run("ip", "-4", "rule", "show", "pref", str(PREF)).stdout
        assert f"uidrange {WORKER_UID}-{WORKER_UID}" in rule
        assert f"lookup {TABLE}" in rule

    def test_nothing_is_forwarded_through_the_modem(self, carrier_servers: None) -> None:
        if _run("ip", "netns", "add", LAN_NS).returncode != 0:
            pytest.skip("a second network namespace could not be created")
        _run("ip", "link", "add", LAN_IFACE, "type", "veth", "peer", "name", "lan0", check=True)
        _run("ip", "link", "set", "netns", LAN_NS, "dev", "lan0")
        _run("ip", "link", "set", LAN_IFACE, "up")
        _run("ip", "address", "add", "10.88.0.1/24", "dev", LAN_IFACE)
        for command in (
            ["ip", "link", "set", "dev", "lan0", "up"],
            ["ip", "address", "add", "10.88.0.2/24", "dev", "lan0"],
            ["ip", "route", "add", "default", "via", "10.88.0.1"],
        ):
            _run("ip", "netns", "exec", LAN_NS, *command, check=True)
        forward = Path("/proc/sys/net/ipv4/ip_forward")
        saved = forward.read_text()
        forward.write_text("1")
        # Make forwarding even possible: by hand, add the route the helper would
        # never put in the main table. This is the misconfiguration the forward
        # chain exists to back up.
        _run("ip", "route", "add", f"{PIN}/32", "via", GATEWAY, "dev", IFACE)
        try:
            before = _drop_packets()
            probe = _run("ip", "netns", "exec", LAN_NS, PROBE_PYTHON, "-I", "-c",
                         _CONNECT, PIN, "443", "")
            assert probe.stdout.strip() == DROPPED, probe.stdout + probe.stderr
            assert _drop_packets()["forward"] > before["forward"]
        finally:
            _run("ip", "route", "del", f"{PIN}/32", "via", GATEWAY, "dev", IFACE)
            forward.write_text(saved)
            _run("ip", "link", "del", LAN_IFACE)
            _run("ip", "netns", "del", LAN_NS)

    def test_two_pinned_addresses_whose_orders_differ(
        self, lab: dict[str, Any], carrier_servers: None
    ) -> None:
        # nftables lists a set by value (192.0.2.9, 192.0.2.10); sorting the text
        # gives the other order. The helper checks its own rule against the
        # kernel's listing, so a helper that wrote the set in text order would
        # refuse its own firewall here, every time.
        _helper(lab, dest_v4=(PIN, PIN_LOW)).route()   # raises if its own check fails
        listed = _run("nft", "list", "table", "inet", cl.TABLE_NAME).stdout
        assert f"ip daddr {{ {PIN_LOW}, {PIN} }} tcp dport 443" in listed
        assert _connect(WORKER_UID, PIN) == "connected"
        assert _connect(WORKER_UID, PIN_LOW) == "connected"
        # An address between the two, not pinned, is not carried by the modem.
        # Asked of the routing table, not sent: unpinned, it is the normal
        # route's business — which here would be this host's real network.
        assert f"dev {IFACE}" not in _route_get("-4", "192.0.2.11", WORKER_UID)
        for dest in (PIN, PIN_LOW):
            assert f"dev {IFACE}" not in _route_get("-4", dest, 0)
        _helper(lab, dest_v4=(PIN, PIN_LOW)).unroute()
        assert f"dev {IFACE}" not in _route_get("-4", PIN_LOW, WORKER_UID)

    def test_a_relay_port_replaces_443(
        self, lab: dict[str, Any], carrier_servers: None
    ) -> None:
        # CELLULAR_DEST_PORT: the pinned address is a stable TCP relay on its
        # own port. The worker reaches that port and nothing else — 443 on
        # the same address included — and nobody else reaches either.
        helper = _helper(lab, dest_port=8443)
        try:
            helper.route()   # reloads the policy; raises if its own check fails
            listed = _run("nft", "list", "table", "inet", cl.TABLE_NAME).stdout
            assert f"ip daddr {PIN} tcp dport 8443 accept" in listed
            assert "dport 443 " not in listed
            assert _connect(WORKER_UID, PIN, port=8443) == "connected"
            assert _connect(WORKER_UID, PIN, port=443) == DROPPED
            assert _connect(0, PIN, port=8443, device=IFACE) == DROPPED
        finally:
            helper.unroute()
        # The later tests expect the 443 policy back.
        lab["helper"].route()
        assert _connect(WORKER_UID, PIN) == "connected"

    def test_ipv6_when_it_is_enabled(self, lab: dict[str, Any], carrier_servers: None) -> None:
        _run("ip", "-6", "address", "add", V6_MODEM, "dev", IFACE, "nodad", check=True)
        helper = _helper(lab, dest_v4=(PIN,), dest_v6=(V6_PIN,), ipv6=True)
        try:
            helper.route()      # reloads the policy with the IPv6 allowance
            listed = _run("nft", "list", "table", "inet", cl.TABLE_NAME).stdout
            assert f"ip6 daddr {V6_PIN} tcp dport 443" in listed
            # Neighbour discovery is what makes an IPv6 device route usable at
            # all, and it has to work both ways: the solicitations go out, and
            # the advertisements come back in — one rule in each chain.
            solicit, advertise = (
                line.strip() for line in listed.splitlines()
                if "icmpv6 type { nd-router-solicit" in line
            )
            assert solicit.endswith("accept") and advertise.endswith("accept")
            # (The base chains only hold the hook and a jump; the rules live
            # in the chains they jump to.)
            for chain in ("cellular_output", "cellular_input"):
                body = re.search(rf"chain {chain} \{{(.*?)\n\t\}}", listed, re.S)
                assert body and "icmpv6 type" in body.group(1), (
                    f"no neighbour-discovery rule in {chain}"
                )
            as_worker = _route_get("-6", V6_PIN, WORKER_UID)
            assert f"dev {IFACE}" in as_worker and f"table {TABLE}" in as_worker
            assert f"dev {IFACE}" not in _route_get("-6", V6_PIN, 0)
            assert _connect(WORKER_UID, V6_PIN) == "connected"
            # A v6 address that is not pinned, and root forcing the interface.
            assert _connect(WORKER_UID, V6_GATEWAY) == DROPPED
            assert _connect(0, V6_GATEWAY, device=IFACE) == DROPPED
        finally:
            # Both rules are the IPv6 settings' to remove; the v4-only helper
            # the later tests use does not know about the -6 one.
            helper.unroute()
        assert f"dev {IFACE}" not in _route_get("-6", V6_PIN, WORKER_UID)

    def test_unroute_withdraws_the_selection_and_leaves_the_link(
        self, lab: dict[str, Any], carrier_servers: None
    ) -> None:
        lab["helper"].unroute()
        assert _run("ip", "-4", "rule", "show", "pref", str(PREF)).stdout.strip() == ""
        chosen = _route_get("-4", PIN, WORKER_UID)
        assert f"dev {IFACE}" not in chosen and f"table {TABLE}" not in chosen
        # The link itself is untouched, so the next read needs no fresh attach.
        assert lab["nm"].active is True
        assert _link_is_up() is True
        lab["helper"].unroute()   # idempotent

    def test_route_again_resumes_without_reloading_the_firewall(
        self, lab: dict[str, Any], carrier_servers: None
    ) -> None:
        # The policy in the kernel is still the IPv6 one; this helper's is
        # IPv4-only, so this first call legitimately reloads it.
        lab["helper"].route()
        loaded = lambda: [  # noqa: E731
            c for c in lab["nm"].log if os.path.basename(c[0]) == "nft" and c[1] == "--file"
        ]
        loaded_before, log_length = loaded(), len(lab["nm"].log)
        counted_before = sum(_drop_packets().values())
        # Move the counters: a refused packet is counted before it is dropped.
        assert _connect(0, GATEWAY, device=IFACE) == DROPPED
        counted_after = sum(_drop_packets().values())
        assert counted_after > counted_before
        lab["helper"].route()
        assert _connect(WORKER_UID, PIN) == "connected"
        # Only lookups since; a reload would have zeroed the evidence counters.
        # The listing is compared without its counters, so traffic that moved
        # them does not look like a changed table.
        assert loaded() == loaded_before
        assert sum(_drop_packets().values()) >= counted_after
        # ...because it did look: the second `route` read the table back from
        # the kernel, and found it unchanged.
        reread = [
            c for c in lab["nm"].log[log_length:]
            if os.path.basename(c[0]) == "nft" and "--stateless" in c
        ]
        assert reread, "the policy was not re-read from the kernel"

    def test_a_firewall_someone_trimmed_is_put_back_whole(self, lab: dict[str, Any]) -> None:
        # Remove just the chain that drops other users' output; the worker's
        # allowance and the forward chain survive. The kernel's listing no
        # longer matches the one taken at load, so it is reloaded.
        _run("nft", "delete", "chain", "inet", cl.TABLE_NAME, "output", check=True)
        listed = _run("nft", "list", "table", "inet", cl.TABLE_NAME).stdout
        assert "chain output {" not in listed
        lab["helper"].route()
        listed = _run("nft", "list", "table", "inet", cl.TABLE_NAME).stdout
        assert "chain output {" in listed

    def test_a_firewall_that_was_removed_is_put_back(self, lab: dict[str, Any]) -> None:
        _run("nft", "delete", "table", "inet", cl.TABLE_NAME)
        lab["helper"].route()
        assert _run("nft", "list", "table", "inet", cl.TABLE_NAME).returncode == 0

    def test_a_refusal_on_a_live_link_leaves_no_rule_no_route_and_no_link(
        self, lab: dict[str, Any], carrier_servers: None
    ) -> None:
        # The link is up from the previous test, as it is between reads. Now a
        # later `route` finds a reason to refuse: the connection has started
        # advertising a resolver the system already uses. That link is
        # black-holing the Pi's name lookups, so it must not be left up.
        resolvers = re.findall(r"^nameserver\s+(\S+)", Path("/etc/resolv.conf").read_text(), re.M)
        assert resolvers, "the rehearsal needs a system resolver to collide with"
        lab["nm"].dns = [resolvers[0]]
        try:
            with pytest.raises(cl.LinkError) as refused:
                lab["helper"].route()
            assert refused.value.reason == "leak"
            _nothing_left()
            assert lab["nm"].active is False
            # And the worker's next question — "has the normal route recovered?"
            # — is answered by the normal route again.
            assert f"dev {IFACE}" not in _route_get("-4", PIN, WORKER_UID)
        finally:
            lab["nm"].dns = []

    def test_a_failed_final_check_unwinds_what_it_installed(
        self, lab: dict[str, Any], carrier_servers: None
    ) -> None:
        # The rule and route are in place and the final check does not pass:
        # someone else can reach the dashboard through the modem too. This is
        # the state an independent review found left behind — rule installed,
        # link up, worker believing both gone.
        lab["helper"].route()
        assert _connect(WORKER_UID, PIN) == "connected"
        # A second routing rule ahead of ours that sends root the same way.
        _run("ip", "-4", "rule", "add", "pref", str(PREF - 1), "to", f"{PIN}/32",
             "lookup", str(TABLE), check=True)
        try:
            with pytest.raises(cl.LinkError) as refused:
                lab["helper"].route()
            assert refused.value.reason == "leak"
        finally:
            _run("ip", "-4", "rule", "del", "pref", str(PREF - 1))
        _nothing_left()
        assert lab["nm"].active is False

    def test_down_takes_the_link_and_the_selection_away(self, lab: dict[str, Any]) -> None:
        lab["helper"].route()
        lab["helper"].down()
        assert lab["nm"].active is False
        _nothing_left()
        # The firewall deliberately stays: whatever brings this interface up
        # later finds it locked.
        assert _run("nft", "list", "table", "inet", cl.TABLE_NAME).returncode == 0
        lab["helper"].down()   # idempotent

    def test_the_firewall_verb_loads_the_policy_and_leaves_the_modem_alone(
        self, lab: dict[str, Any]
    ) -> None:
        # What runs at boot, before NetworkManager: the link is down, the
        # policy goes in, and nothing else happens.
        _run("nft", "delete", "table", "inet", cl.TABLE_NAME)
        lab["nm"].log.clear()
        lab["helper"].firewall()
        assert _run("nft", "list", "table", "inet", cl.TABLE_NAME).returncode == 0
        assert _link_is_up() is False
        assert not any(c[0] == "nmcli" and ("up" in c or "down" in c) for c in lab["nm"].log)
        assert _run("ip", "-4", "rule", "show", "pref", str(PREF)).stdout.strip() == ""

    def test_another_auto_connecting_profile_blocks_the_link_before_anything_moves(
        self, lab: dict[str, Any]
    ) -> None:
        # What NetworkManager makes of a modem it has no profile for: "Wired
        # connection N", bound to the port, auto-connecting.
        lab["nm"].others = [("Wired connection 2", "802-3-ethernet")]
        lab["nm"].log.clear()
        try:
            with pytest.raises(cl.LinkError) as refused:
                lab["helper"].route()
            assert refused.value.reason == "profile"
            assert "Wired connection 2" in str(refused.value)
            assert _link_is_up() is False
            assert not any(c[0] == "nmcli" and "up" in c for c in lab["nm"].log)
        finally:
            lab["nm"].others = []

    def test_route_refuses_while_the_modem_is_absent(self, lab: dict[str, Any]) -> None:
        # Unplugging the HAT: the Pi's end of the link disappears, and with it
        # the peer. (Deleting the carrier namespace is not a stand-in for that
        # while the carrier-side server is still running inside it.)
        _run("ip", "link", "del", IFACE)
        assert not _interface_exists()
        try:
            with pytest.raises(cl.LinkError) as refused:
                lab["helper"].route()
            assert refused.value.reason == "absent"
        finally:
            _teardown()
