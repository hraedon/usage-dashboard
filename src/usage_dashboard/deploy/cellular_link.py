"""The privileged half of the Pi cellular fallback (Plan 010): the link helper.

Bringing a cellular link up and routing through it needs root; reading the
dashboard does not. So the unit splits in two. An unprivileged worker
(:mod:`usage_dashboard.client.cellular_worker`, its own system user) decides
*when* the metered link is used, and asks this helper — through sudo, with one
word — to do the three things that need privilege:

``route``    make sure the firewall is in place, bring the pre-provisioned
             NetworkManager profile up, and select the cellular route **for the
             worker's UID only**;
``unroute``  withdraw that selection (the link may stay up);
``down``     withdraw it and take the link down.

Everything here fails closed. The helper never creates or edits the connection
profile, never supplies an APN or a PIN, and refuses to bring a profile up
unless it already isolates itself: no default route, no carrier DNS, no
autoconnect, metered. Its job is to enforce what the operator provisioned, and
to take the link back down the moment the system is not in the state it expects.

The order inside ``route`` is the contract's second half, "apply the deny
policy before enabling the link": the firewall is loaded and verified first,
so there is no instant at which the modem interface is up without it.

Each verb is a separate short-lived root process and keeps no state of its own.
What exists is what the kernel and NetworkManager say exists, so a helper that
crashes halfway is corrected by the next call rather than by a stale lock file.

Logic is separated from execution the same way ``deploy.redeploy`` does it:
every command goes through an injected runner, so the decisions are unit-tested
without root. ``tests/test_cellular_link_kernel.py`` then loads the rendered
firewall and routing for real, as root, against a simulated link.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

ENV_FILE = Path("/etc/usage-dashboard-cellular.env")
RUN_DIR = Path("/run/usage-dashboard-cellular")
RESOLV_CONF = Path("/etc/resolv.conf")
# systemd-resolved, where used, keeps the real upstream servers here and points
# /etc/resolv.conf at its own stub.
RESOLVED_CONF = Path("/run/systemd/resolve/resolv.conf")

TABLE_NAME = "usage_dashboard_cellular"
# Arbitrary, high, and outside the ranges the usual policy-routing tools use
# (docker, VPNs, NetworkManager's own). Both are settings; the final
# verification proves the selection works regardless of what else is installed.
DEFAULT_TABLE = 17010
DEFAULT_RULE_PREF = 17010
DEFAULT_WORKER_USER = "usage-dashboard-cellular"
# 0 local, 253 default, 254 main, 255 unreachable-less: never ours to own.
_RESERVED_TABLES = frozenset({0, 253, 254, 255})

# What the worker may ask for, one argument-free word at a time. ``firewall``
# only loads the deny policy — it can restrict traffic and nothing else — so
# the worker can put it in place as early as it likes.
WORKER_VERBS = ("firewall", "route", "unroute", "down")
OPERATOR_VERBS = ("status", "check", "inspect")

# The worker gives up on a ``route`` after 150 s and kills ``sudo``. Killing
# sudo does not kill what it started, so the helper keeps its own deadline,
# lower than the worker's, and unwinds when it expires — otherwise a helper
# the worker has already written off could finish installing a route nobody
# is supervising.
HELPER_DEADLINE_SECONDS = 100

_INTERFACE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$")
_USER_NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_CONNECT_WAIT_SECONDS = 60
# Uids asked about when proving nobody but the worker is carried by the modem:
# root, and the ordinary user a Pi ships with.
_OTHER_UIDS_PROBED = (0, 1000)
DHCP = "dhcp"
DHCPV6 = "dhcpv6"
NONE = "none"   # permit no link-setup packets at all (raw-IP modems need none)
_LINK_SETUP_CHOICES = (NONE, DHCP, DHCPV6)
_NDP_TYPES = "{ nd-router-solicit, nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert }"

# Everything the profile must already say. Single-valued settings first.
_PROFILE_SINGLE = (
    "connection.type",
    "connection.interface-name",
    "connection.autoconnect",
    "connection.multi-connect",
    "connection.metered",
    "ipv4.method",
    "ipv4.never-default",
    "ipv4.ignore-auto-routes",
    "ipv4.ignore-auto-dns",
    "ipv4.route-table",
    "ipv6.method",
    "ipv6.never-default",
    "ipv6.ignore-auto-routes",
    "ipv6.ignore-auto-dns",
    "ipv6.route-table",
)
# Single-valued settings with exactly one acceptable value, or a short set.
# Each is one line of defense: together they are what keeps the carrier's
# default route, routes and DNS out of the system's own tables.
_PROFILE_FIXED: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("does not autoconnect", "connection.autoconnect", ("no",)),
    # nmcli prints this enum as its number: 0 is "single".
    ("one connection at a time", "connection.multi-connect", ("single", "0")),
    ("metered", "connection.metered", ("yes",)),
    ("IPv4 by DHCP or static", "ipv4.method", ("auto", "manual")),
    ("no IPv4 default route", "ipv4.never-default", ("yes",)),
    ("no carrier IPv4 routes", "ipv4.ignore-auto-routes", ("yes",)),
    ("no carrier IPv4 DNS", "ipv4.ignore-auto-dns", ("yes",)),
    ("helper owns the IPv4 routing table", "ipv4.route-table", ("0", "")),
    ("no IPv6 default route", "ipv6.never-default", ("yes",)),
    ("no carrier IPv6 routes", "ipv6.ignore-auto-routes", ("yes",)),
    ("no carrier IPv6 DNS", "ipv6.ignore-auto-dns", ("yes",)),
    ("helper owns the IPv6 routing table", "ipv6.route-table", ("0", "")),
)
# IPv6 methods allowed when CELLULAR_IPV6=1; otherwise only off/ignored.
_IPV6_METHODS_ON = ("auto", "manual", "disabled", "ignore")
_IPV6_METHODS_OFF = ("disabled", "ignore")

# What nmcli prints for connection.type. The Ethernet setting is named by its
# settings-group name; only "gsm" is spelled the way one would guess.
_ETHERNET_TYPES = ("ethernet", "802-3-ethernet")

# Serial ports an Ethernet-style modem may expose for AT commands: CDC-ACM
# (/dev/ttyACMn, what the SIM7670G HAT does) or usb-serial (/dev/ttyUSBn).
_MODEM_PORTS: tuple[str, ...] = tuple(
    f"/dev/ttyACM{i}" for i in range(4)
) + tuple(f"/dev/ttyUSB{i}" for i in range(4))
# Status questions only, for ``inspect``. None of them changes anything. The
# SIMCom-specific ones simply answer ERROR on another vendor's modem.
# ``ATI`` is left out on purpose: it prints the IMEI, which the record does
# not need.
_MODEM_QUESTIONS = (
    "AT+CGMM",        # model
    "AT+CGMR",        # firmware revision
    "AT$MYCONFIG?",   # SIMCom: USB network mode (0 RNDIS, 1 ECM) and card name
    "AT+DIALMODE?",   # SIMCom: 0 = the module dials by itself, 1 = it does not
    "AT+CFUN?",
    "AT+CPIN?",       # READY, or which PIN it wants — never the PIN itself
    "AT+COPS?",       # the registered operator, once registered
    "AT+CGDCONT?",    # the PDP contexts and their APNs
    "AT+CEREG?",      # LTE registration state
    "AT+CSQ",         # signal; 99,99 means none detectable
    "AT+USBNETIP?",   # SIMCom: the address the module's DHCP hands out
)
_AT_READ_LIMIT = 8192   # a diagnostic port streams forever; read no more than this

# Multi-valued settings that must be empty: a static route or resolver entry
# here would route traffic or name lookups through the modem by a path the
# helper does not own and cannot verify.
_PROFILE_EMPTY = (
    "ipv4.dns",
    "ipv4.dns-search",
    "ipv4.dns-options",
    "ipv4.routes",
    "ipv4.routing-rules",
    "ipv6.dns",
    "ipv6.dns-search",
    "ipv6.dns-options",
    "ipv6.routes",
    "ipv6.routing-rules",
)

Runner = Callable[[list[str], float], CompletedProcess[str]]


class ConfigError(ValueError):
    """The settings file is missing or contradictory. Nothing is touched."""


class LinkError(Exception):
    """A verb could not reach its end state. ``reason`` is a short stable word
    the worker records (``absent``, ``connect``, ``leak`` …); the message is
    for the operator."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# -- settings -----------------------------------------------------------------


def parse_env_file(text: str) -> dict[str, str]:
    """Read ``KEY=VALUE`` lines the way systemd's ``EnvironmentFile`` and
    ``deploy/pi/update.sh``'s ``env_or_file`` do: ``#`` comments, the last
    occurrence of a key wins, and a matching pair of quotes around the value is
    not part of it (systemd accepts either style; update.sh's ``env_or_file``
    strips only double quotes, so prefer those). Only a whole line may be a
    comment: an inline ``#`` stays in the value. The file is never *sourced*:
    it carries a credential."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


@dataclass(frozen=True)
class LinkSettings:
    connection: str
    interface: str
    dest_v4: tuple[str, ...]
    dest_v6: tuple[str, ...]
    worker_user: str
    worker_uid: int
    table: int
    rule_pref: int
    link_setup: frozenset[str]
    ipv6: bool
    env_file: Path = ENV_FILE
    resolv_conf: Path = RESOLV_CONF
    resolved_conf: Path = RESOLVED_CONF
    run_dir: Path = RUN_DIR

    @classmethod
    def load(
        cls,
        env_file: Path = ENV_FILE,
        *,
        runner: Runner | None = None,
        environ: dict[str, str] | None = None,
        resolv_conf: Path = RESOLV_CONF,
        resolved_conf: Path = RESOLVED_CONF,
        run_dir: Path = RUN_DIR,
    ) -> LinkSettings:
        run = runner if runner is not None else real_runner
        try:
            text = env_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read {env_file}: {exc}") from exc
        values = environ if environ is not None else parse_env_file(text)

        connection = _required(values, "CELLULAR_CONNECTION")
        interface = _required(values, "CELLULAR_IFACE")
        if not _INTERFACE_NAME.match(interface):
            raise ConfigError("CELLULAR_IFACE must be a plain interface name")
        addresses = _addresses(_required(values, "CELLULAR_DEST"))
        dest_v4 = tuple(a for a in addresses if ipaddress.ip_address(a).version == 4)
        dest_v6 = tuple(a for a in addresses if ipaddress.ip_address(a).version == 6)
        if not dest_v4 and not dest_v6:
            raise ConfigError("CELLULAR_DEST must list at least one address")

        user = values.get("CELLULAR_WORKER_USER") or DEFAULT_WORKER_USER
        if not _USER_NAME.match(user):
            raise ConfigError("CELLULAR_WORKER_USER must be a plain system user name")
        uid = _resolve_uid(run, user)
        table = _number(values, "CELLULAR_TABLE", DEFAULT_TABLE, 1, 2**32 - 1)
        if table in _RESERVED_TABLES:
            raise ConfigError(f"CELLULAR_TABLE {table} is a reserved routing table")
        pref = _number(values, "CELLULAR_RULE_PREF", DEFAULT_RULE_PREF, 1, 2**32 - 1)

        ipv6 = _flag(values, "CELLULAR_IPV6")
        if dest_v6 and not ipv6:
            raise ConfigError("CELLULAR_IPV6=1 is required to use an IPv6 destination")
        setup = frozenset(
            part for part in re.split(r"[,\s]+", values.get("CELLULAR_LINK_SETUP") or DHCP)
            if part
        )
        unknown = setup - set(_LINK_SETUP_CHOICES)
        if unknown:
            raise ConfigError(
                f"CELLULAR_LINK_SETUP accepts only {', '.join(_LINK_SETUP_CHOICES)}; "
                f"got {', '.join(sorted(unknown))}"
            )
        if NONE in setup and len(setup) > 1:
            raise ConfigError("CELLULAR_LINK_SETUP=none cannot be combined with other packets")
        if DHCPV6 in setup and not ipv6:
            raise ConfigError("CELLULAR_LINK_SETUP=dhcpv6 requires CELLULAR_IPV6=1")
        # "permit nothing" is the empty set, not a packet kind.
        setup = setup - {NONE}

        return cls(
            connection=connection,
            interface=interface,
            dest_v4=dest_v4,
            dest_v6=dest_v6,
            worker_user=user,
            worker_uid=uid,
            table=table,
            rule_pref=pref,
            link_setup=setup,
            ipv6=ipv6,
            env_file=env_file,
            resolv_conf=resolv_conf,
            resolved_conf=resolved_conf,
            run_dir=run_dir,
        )

    @property
    def destinations(self) -> tuple[str, ...]:
        return self.dest_v4 + self.dest_v6


def _required(values: dict[str, str], name: str) -> str:
    value = (values.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _number(values: dict[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = (values.get(name) or "").strip()
    if not raw:
        return default
    try:
        number = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc
    if not low <= number <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {number}")
    return number


def _flag(values: dict[str, str], name: str) -> bool:
    return (values.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _addresses(raw: str) -> list[str]:
    addresses: list[str] = []
    for token in re.split(r"[,\s]+", raw.strip()):
        if not token:
            continue
        try:
            addresses.append(str(ipaddress.ip_address(token)))
        except ValueError as exc:
            raise ConfigError(
                f"CELLULAR_DEST entries must be IP literals; {token!r} is not"
            ) from exc
    return list(dict.fromkeys(addresses))


def _resolve_uid(run: Runner, user: str) -> int:
    try:
        proc = run(["id", "-u", user], 5.0)
    except (LinkError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"cannot look up the worker user {user!r}: {exc}") from exc
    if proc.returncode != 0:
        raise ConfigError(
            f"the worker user {user!r} does not exist; run deploy/pi/cellular-install.sh"
        )
    try:
        uid = int(proc.stdout.strip())
    except ValueError as exc:
        raise ConfigError(f"cannot read the uid of {user!r}: {proc.stdout.strip()!r}") from exc
    if uid < 1:
        raise ConfigError("the worker user must not be root")
    return uid


# -- the firewall -----------------------------------------------------------------


def render_policy(
    interface: str,
    worker_uid: int,
    dest_v4: Iterable[str],
    dest_v6: Iterable[str],
    *,
    link_setup: Iterable[str] = (DHCP,),
) -> str:
    """The nftables ruleset for the modem interface, as one atomic reload.

    The first three lines are the idiom for replacing a table in one
    transaction: declare it (harmless if it exists), delete it (now known to
    exist), recreate it. Nothing is ever loaded in pieces, so there is no
    moment with a half-applied policy — and a re-load after an edit cannot fail
    halfway and leave the old rules in place.

    What it says, per chain:

    * ``output`` — on the modem interface, nothing leaves except what the
      worker user sends to a pinned address on TCP 443, plus the link-setup
      packets the qualified modem mode needs. ``meta skuid`` is what makes it
      "this one user": another process sending to the very same address is
      dropped. Everything else is counted and dropped.
    * ``input`` — the link is for outgoing requests; only their replies come
      back, plus the DHCP offer a DHCP-mode modem sends.
    * ``forward`` — nothing is ever routed *through* the modem in either
      direction, whatever ``ip_forward`` or a container runtime does later.

    One ``inet`` table covers both address families. With IPv6 not enabled
    there is no IPv6 allow rule at all, so every IPv6 packet on the interface —
    including the router solicitations a Linux host sends as soon as the
    interface has a link-local address — is dropped.
    """
    if not _INTERFACE_NAME.match(interface):
        raise ValueError(f"not a usable interface name: {interface!r}")
    if worker_uid < 1:
        raise ValueError("the worker uid must be a positive integer")
    setup = set(link_setup) - {NONE}
    unknown = setup - set(_LINK_SETUP_CHOICES)
    if unknown:
        raise ValueError(f"unknown link-setup packets: {sorted(unknown)}")

    output: list[str] = []
    ingress: list[str] = ["ct state established,related accept"]
    if DHCP in setup:
        # A DHCP client speaks from its port 68 to the server's 67 (RFC 2131).
        # The source port narrows the one allowance every user has on the link;
        # NetworkManager's built-in client and dhcpcd both use it.
        output.append("udp sport 68 udp dport 67 accept")
        ingress.append("udp sport 67 udp dport 68 accept")
    v4 = sorted({str(ipaddress.ip_address(a)) for a in dest_v4}, key=_address_order)
    v6 = sorted({str(ipaddress.ip_address(a)) for a in dest_v6}, key=_address_order)
    if v4:
        output.append(
            f"meta skuid {worker_uid} ip daddr {_address_match(v4)} "
            "tcp dport 443 accept"
        )
    if v6:
        output.append(
            f"meta skuid {worker_uid} ip6 daddr {_address_match(v6)} "
            "tcp dport 443 accept"
        )
    if DHCPV6 in setup:
        # The DHCPv6 client listens on 546 and talks to the relay on 547.
        output.append("udp sport 546 udp dport 547 accept")
        ingress.append("udp sport 547 udp dport 546 accept")
    if v6 or DHCPV6 in setup:
        # Neighbour discovery, and in both directions. An IPv6 device route is
        # unusable until the far end's link address is known: the solicitations
        # have to go out AND the advertisements have to come back. Those are
        # not replies in the conntrack sense, so the input chain's
        # established-state rule does not cover them — without the second rule
        # the link is up, the route is selected, and nothing resolves.
        output.append(f"icmpv6 type {_NDP_TYPES} accept")
        ingress.append(f"icmpv6 type {_NDP_TYPES} accept")

    lines = [
        f"table inet {TABLE_NAME} {{ }}",
        f"delete table inet {TABLE_NAME}",
        f"table inet {TABLE_NAME} {{",
        "\tchain output {",
        "\t\ttype filter hook output priority 0; policy accept;",
        f'\t\toifname "{interface}" jump cellular_output',
        "\t}",
        "\tchain cellular_output {",
        *(f"\t\t{rule}" for rule in output),
        "\t\tcounter drop",
        "\t}",
        "\tchain input {",
        "\t\ttype filter hook input priority 0; policy accept;",
        f'\t\tiifname "{interface}" jump cellular_input',
        "\t}",
        "\tchain cellular_input {",
        *(f"\t\t{rule}" for rule in ingress),
        "\t\tcounter drop",
        "\t}",
        "\tchain forward {",
        "\t\ttype filter hook forward priority 0; policy accept;",
        f'\t\tiifname "{interface}" counter drop',
        f'\t\toifname "{interface}" counter drop',
        "\t}",
        "}",
        "",
    ]
    return "\n".join(lines)


def _address_order(address: str) -> tuple[int, int]:
    """Sort addresses the way nftables lists them: by value, not as text."""
    ip = ipaddress.ip_address(address)
    return ip.version, int(ip)


def _address_match(addresses: Iterable[str]) -> str:
    """How nftables prints a destination list: a set for several addresses,
    sorted by value — ``192.0.2.9`` before ``192.0.2.10``, where sorting the
    text would put them the other way round — and, verified against the
    kernel, a bare address for one."""
    ordered = sorted({str(ipaddress.ip_address(a)) for a in addresses}, key=_address_order)
    if len(ordered) == 1:
        return ordered[0]
    return "{ " + ", ".join(ordered) + " }"


# -- execution ----------------------------------------------------------------------


def real_runner(command: list[str], timeout: float) -> CompletedProcess[str]:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise LinkError("timeout", f"{' '.join(command)} did not finish in {timeout:.0f}s") from exc
    except OSError as exc:
        raise LinkError("missing", f"cannot run {command[0]}: {exc}") from exc


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _tool(name: str) -> str:
    """Where a command lives, as a path.

    ``nft`` is in ``/usr/sbin``: on sudo's secure_path, but not on an ordinary
    user's. Found on the work Pi, where the package was installed and a
    hand-run helper still could not have found it."""
    found = shutil.which(name)
    if found:
        return found
    for directory in ("/usr/sbin", "/sbin"):
        candidate = f"{directory}/{name}"     # a POSIX path, whatever this file runs on
        if os.path.exists(candidate):
            return candidate
    return name


def _connection_type(value: str) -> str:
    """nmcli's name for a profile's type, normalised to "ethernet" / "gsm"."""
    return "ethernet" if value in _ETHERNET_TYPES else value


def _un_terse(text: str) -> str:
    r"""Undo nmcli's terse-mode escaping (``\:`` for a colon in a value)."""
    return re.sub(r"\\(.)", r"\1", text)


def _split_terse(line: str) -> list[str]:
    """Split a terse line on its field separators: a colon nmcli did NOT escape."""
    return [_un_terse(field) for field in re.split(r"(?<!\\):", line)]


def _profile_applies(
    interface: str, mac: str, patterns: Sequence[str], target: str, target_mac: str
) -> bool:
    """Whether an Ethernet profile would be offered to the interface *target*.

    NetworkManager picks a device by name — ``match.interface-name`` when set,
    else ``connection.interface-name``; an empty name means any device — and,
    when no name is given, by MAC address. Where this cannot be sure it answers
    True: a false alarm costs an operator a glance, a missed profile costs a
    default route over a metered link.
    """
    patterns = [pattern for pattern in patterns if pattern]
    if patterns:
        return any(fnmatch.fnmatchcase(target, pattern) for pattern in patterns)
    if interface:
        return fnmatch.fnmatchcase(target, interface)
    if mac:
        # An unknown device address cannot rule the profile out.
        return target_mac == "" or mac.lower() == target_mac
    return True


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


class LinkHelper:
    """The verbs. Every method converges the system on one end state and
    raises :class:`LinkError` if it cannot get there."""

    def __init__(self, settings: LinkSettings, *, runner: Runner | None = None) -> None:
        self.s = settings
        self._run = runner if runner is not None else real_runner
        self._nft = _tool("nft")

    # -- primitive commands ---------------------------------------------------

    def _cmd(self, *command: str, timeout: float = 15.0) -> CompletedProcess[str]:
        return self._run(list(command), timeout)

    def _must(
        self, *command: str, reason: str, what: str, timeout: float = 15.0
    ) -> CompletedProcess[str]:
        proc = self._cmd(*command, timeout=timeout)
        if proc.returncode != 0:
            detail = _first_line(proc.stderr) or _first_line(proc.stdout)
            raise LinkError(reason, f"{what} failed: {' '.join(command)}: {detail}")
        return proc

    def _nm(self, *args: str, timeout: float = 20.0) -> CompletedProcess[str]:
        return self._cmd("nmcli", "--terse", *args, timeout=timeout)

    def _nm_values(self, field: str, connection: str) -> list[str]:
        """The values ``nmcli -g FIELD connection show CONN`` reports.

        The shape was read off NetworkManager 1.52 rather than assumed: a
        single-valued setting prints one line, a multi-valued one prints all
        its values on ONE line joined by ``" | "`` (``ip4.dns`` of a connection
        with two servers is ``192.168.1.53 | 192.168.1.54``), and an unset one
        prints an empty line. An indexed name such as ``IP4.ADDRESS[1]`` is
        rejected outright in this mode; ask for the property.
        """
        proc = self._nm("--get-values", field, "connection", "show", connection)
        if proc.returncode != 0:
            raise LinkError(
                "profile", f"cannot read {field} of {connection!r}: "
                f"{_first_line(proc.stderr)}"
            )
        values: list[str] = []
        for line in proc.stdout.splitlines():
            values.extend(value.strip() for value in line.split(" | ") if value.strip())
        return values

    def _nm_one(self, field: str, connection: str) -> str:
        values = self._nm_values(field, connection)
        return values[0] if values else ""

    # -- what the system looks like right now -----------------------------------

    def _interface_exists(self) -> bool:
        return self._cmd("ip", "link", "show", "dev", self.s.interface).returncode == 0

    def _interface_mac(self) -> str:
        proc = self._cmd("ip", "link", "show", "dev", self.s.interface)
        found = re.search(r"link/ether\s+([0-9a-fA-F:]{17})", proc.stdout)
        return found.group(1).lower() if found else ""

    def _connection_active(self) -> bool:
        # An inactive profile prints an empty GENERAL.STATE rather than a word.
        return self._nm_one("GENERAL.STATE", self.s.connection) == "activated"

    def _policy_text(self) -> str:
        return render_policy(
            self.s.interface, self.s.worker_uid, self.s.dest_v4, self.s.dest_v6,
            link_setup=self.s.link_setup,
        )

    def _policy_stamp(self) -> Path:
        return self.s.run_dir / "policy.sha256"

    def _policy_listed(self) -> str:
        proc = self._cmd(self._nft, "list", "table", "inet", TABLE_NAME)
        return proc.stdout if proc.returncode == 0 else ""

    def _kernel_policy(self) -> str:
        """The table as the kernel holds it, without anything that changes on
        its own. ``--stateless`` leaves the counters out, so two readings of an
        untouched table are identical (checked on a real kernel)."""
        proc = self._cmd(self._nft, "--stateless", "list", "table", "inet", TABLE_NAME)
        return proc.stdout if proc.returncode == 0 else ""

    def _policy_fingerprint(self) -> dict[str, str]:
        return {
            "policy": hashlib.sha256(self._policy_text().encode()).hexdigest(),
            "kernel": hashlib.sha256(self._kernel_policy().encode()).hexdigest(),
        }

    def _policy_is_current(self) -> bool:
        """True if the kernel holds exactly the policy we would load now.

        Two things are compared, both taken from the kernel rather than from
        our own records: the listing as it is now, and the one captured when
        the policy was loaded. A table someone else flushed, trimmed or added
        to is therefore reloaded instead of trusted.
        """
        if not self._kernel_policy():
            return False
        try:
            stamp = self._policy_stamp().read_text(encoding="utf-8")
        except OSError:
            return False
        return stamp == json.dumps(self._policy_fingerprint(), sort_keys=True)

    def _ensure_policy(self) -> None:
        """Load the firewall unless the kernel already holds this exact one.
        Loading it again would zero the drop counters, which are the evidence
        the pilot reads."""
        if self._policy_is_current():
            return
        text = self._policy_text()
        self.s.run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.s.run_dir, 0o700)
        path = self.s.run_dir / "policy.nft"
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o600)
        self._must(self._nft, "--check", "--file", str(path), reason="policy",
                   what="the firewall ruleset was refused")
        self._must(self._nft, "--file", str(path), reason="policy",
                   what="loading the firewall ruleset")
        self._policy_stamp().write_text(
            json.dumps(self._policy_fingerprint(), sort_keys=True), encoding="utf-8"
        )
        self._verify_policy()

    def _verify_policy(self) -> None:
        listed = self._policy_listed()
        s = self.s
        needles = [
            f'oifname "{s.interface}"',
            f'iifname "{s.interface}"',
            f"meta skuid {s.worker_uid}",
            "tcp dport 443",
            "hook forward",
        ]
        if s.dest_v4:
            needles.append("ip daddr " + _address_match(s.dest_v4))
        if s.dest_v6:
            needles.append("ip6 daddr " + _address_match(s.dest_v6))
        missing = [needle for needle in needles if needle not in listed]
        if missing:
            raise LinkError("policy", "the loaded firewall does not contain: " + "; ".join(missing))

    # -- the profile ------------------------------------------------------------

    def profile_checks(self) -> list[Check]:
        """Every isolation property the profile must already have. Read-only, so
        ``check`` can run it before the link is ever brought up."""
        s = self.s
        checks: list[Check] = []
        proc = self._nm("--get-values", ",".join(_PROFILE_SINGLE), "connection", "show",
                        s.connection)
        if proc.returncode != 0:
            raise LinkError("profile", f"no NetworkManager profile named {s.connection!r}: "
                             f"{_first_line(proc.stderr)}")
        lines = proc.stdout.splitlines()
        values = {name: lines[i].strip() if i < len(lines) else "" for i, name in
                  enumerate(_PROFILE_SINGLE)}
        if len(lines) < len(_PROFILE_SINGLE):
            checks.append(Check("profile readable", False,
                                f"nmcli returned {len(lines)} values "
                                f"for {len(_PROFILE_SINGLE)} settings"))
            return checks

        kind = values["connection.interface-name"]
        connection_type = _connection_type(values["connection.type"])
        checks.append(Check(
            "profile is gsm or ethernet",
            connection_type in ("gsm", "ethernet"),
            f"connection.type is {values['connection.type']!r}",
        ))
        # An Ethernet-style modem (ECM/RNDIS/NCM) is bound by interface name, and
        # that name must be the one the firewall is written for. A modem
        # NetworkManager manages picks its own interface, so there the name may
        # be unset, or "*" ("any") as nmcli writes it — but if a real name is
        # set it must still be the right one.
        bound = (
            kind == s.interface
            if connection_type == "ethernet"
            else kind in ("", "*", s.interface)
        )
        checks.append(Check(
            "profile bound to the modem interface", bound,
            f"connection.interface-name is {kind!r}, "
            f"the modem is {s.interface!r}",
        ))
        for label, setting, allowed in _PROFILE_FIXED:
            checks.append(Check(
                label, values[setting] in allowed, f"{setting} is {values[setting]!r}",
            ))
        v6_methods = _IPV6_METHODS_ON if s.ipv6 else _IPV6_METHODS_OFF
        checks.append(Check(
            "IPv6 off unless enabled",
            values["ipv6.method"] in v6_methods,
            f"ipv6.method is {values['ipv6.method']!r}",
        ))

        proc = self._nm("--fields", ",".join(_PROFILE_EMPTY), "connection", "show",
                        s.connection)
        if proc.returncode == 0:
            filled = _filled_multi_values(proc.stdout)
        else:
            filled = list(_PROFILE_EMPTY)
        checks.append(Check("no static routes, rules or resolver entries", not filled,
                            "set: " + ", ".join(filled) if filled else ""))
        return checks

    def _verify_profile(self) -> None:
        failed = [c for c in self.profile_checks() if not c.ok]
        if failed:
            # Nothing has been brought up yet: the refusal costs no data.
            raise LinkError("profile", "; ".join(f"{c.name} ({c.detail})" for c in failed))
        hazards = self.autoconnect_hazards()
        if hazards:
            raise LinkError(
                "profile",
                f"other NetworkManager profiles would bring {self.s.interface} up by "
                f"themselves: {', '.join(hazards)}. Each would install a default route "
                f"and DNS over the metered link, and would come back the moment this "
                f"link is taken down. Delete them, or list the device under "
                f"[main] no-auto-default",
            )

    # -- what else might use the modem ------------------------------------------------

    def autoconnect_hazards(self) -> list[str]:
        """Other NetworkManager profiles that would bring the modem's interface
        up by themselves.

        NetworkManager creates a profile — "Wired connection N", auto-connect,
        DHCP, default route — for any Ethernet device that has none. That is
        exactly what an Ethernet-style modem looks like, and it was found live
        on the first unit with the HAT fitted: the Pi's preferred default route
        went out the modem, and two dead DNS servers the modem's DHCP invented
        went to the top of the resolver list. No firewall around the worker can
        cover traffic another profile invited, so the helper refuses to use the
        link while one of these exists.
        """
        listed = self._nm("--fields", "NAME,TYPE", "connection", "show")
        if listed.returncode != 0:
            raise LinkError(
                "profile", f"cannot list NetworkManager profiles: {_first_line(listed.stderr)}"
            )
        device_mac = self._interface_mac()
        hazards: list[str] = []
        for line in listed.stdout.splitlines():
            fields = _split_terse(line)
            if len(fields) != 2:
                continue
            name, kind = fields
            if name == self.s.connection or _connection_type(kind) != "ethernet":
                continue
            if self._autoconnects_here(name, device_mac):
                hazards.append(name)
        return hazards

    def _autoconnects_here(self, name: str, device_mac: str) -> bool:
        proc = self._nm(
            "--get-values", "connection.autoconnect,connection.interface-name,"
                            "802-3-ethernet.mac-address",
            "connection", "show", name,
        )
        lines = proc.stdout.splitlines()
        if proc.returncode != 0 or len(lines) < 3 or lines[0].strip() != "yes":
            return False
        match = self._nm("--get-values", "match.interface-name", "connection", "show", name)
        patterns = (
            self._nm_values("match.interface-name", name) if match.returncode == 0 else []
        )
        return _profile_applies(
            lines[1].strip(), lines[2].strip(), patterns, self.s.interface, device_mac
        )

    def active_others(self) -> list[str]:
        """Profiles other than ours that are using the modem's interface now."""
        listed = self._nm("--fields", "NAME,DEVICE", "connection", "show", "--active")
        if listed.returncode != 0:
            return []
        active: list[str] = []
        for line in listed.stdout.splitlines():
            fields = _split_terse(line)
            if len(fields) != 2:
                continue
            name, device = fields
            if device == self.s.interface and name != self.s.connection:
                active.append(name)
        return active

    # -- bringing the link up ------------------------------------------------------

    def _connect(self) -> None:
        self._must("nmcli", "--wait", str(_CONNECT_WAIT_SECONDS), "connection", "up",
                   self.s.connection, reason="connect", what="bringing the profile up",
                   timeout=_CONNECT_WAIT_SECONDS + 30)
        try:
            self._verify_after_up()
        except LinkError:
            # A profile that came up wrong is taken straight back down.
            self._nm("--wait", "10", "connection", "down", self.s.connection, timeout=40.0)
            self._verify_down()
            raise

    def _verify_after_up(self) -> None:
        s = self.s
        if not self._connection_active():
            raise LinkError("connect", f"{s.connection!r} did not reach state activated")
        devices = [
            line.strip() for line in self._nm_values("GENERAL.DEVICES", s.connection)
            if line.strip()
        ]
        if devices != [s.interface]:
            raise LinkError(
                "leak", f"{s.connection!r} came up on {devices or 'no device'} instead of "
                        f"{s.interface!r}; refusing to use it"
            )
        addresses = self._addresses(s.connection)
        if not addresses:
            raise LinkError("connect", f"{s.connection!r} is up but has no address")
        if self._default_routes(s.interface):
            raise LinkError("leak", "the connection installed a default route in the main table")
        shared = self._shared_resolvers(s.connection)
        if shared:
            raise LinkError("leak", f"the connection's DNS servers are in the system resolver: "
                                    f"{', '.join(sorted(shared))}")
        conflict = self._on_link_conflict()
        if conflict:
            raise LinkError("conflict", conflict)

    def _addresses(self, connection: str) -> list[str]:
        return [
            value
            for field_name in ("IP4.ADDRESS", "IP6.ADDRESS")
            for value in self._nm_values(field_name, connection)
            if value
        ]

    def _on_link_conflict(self) -> str:
        """Why the modem's own subnet would shadow part of the Pi's normal
        networking while the link is up, or ''.

        Bringing the interface up adds a connected route for the modem's
        subnet to the main table — no profile setting prevents that — and it
        outranks anything the Pi reaches only through a gateway. If something
        the Pi needs for its *normal* operation sits in that subnet, it is
        black-holed for as long as the link is up, and the worker could never
        see the normal route recover. Modems ship on common private subnets
        (SIMCom's default is 192.168.0.0/24), so this can happen anywhere those
        are already in use.
        """
        s = self.s
        try:
            networks = [
                ipaddress.ip_network(value, strict=False)
                for value in self._nm_values("IP4.ADDRESS", s.connection)
            ]
        except ValueError:
            return ""
        networks = [network for network in networks if network.prefixlen < 32]
        if not networks:
            return ""
        keep: list[tuple[str, str]] = [
            ("a system resolver", address) for address in sorted(self._system_resolvers())
        ]
        keep.extend(
            (f"the gateway of the default route on {device}", gateway)
            for gateway, device in self._other_default_gateways()
        )
        keep.extend(("a pinned destination", dest) for dest in s.dest_v4)
        for label, address in keep:
            try:
                target = ipaddress.ip_address(address)
            except ValueError:
                continue
            for network in networks:
                if target in network:
                    return (
                        f"the modem's subnet {network} also contains {address} ({label}): "
                        f"while the link is up the Pi would send that traffic to the modem. "
                        f"Move the modem to an unused subnet with its own AT command "
                        f"(AT+USBNETIP on SIMCom modules)"
                    )
        return ""

    def _other_default_gateways(self) -> list[tuple[str, str]]:
        """``(gateway, device)`` for every IPv4 default route that is not the modem's."""
        proc = self._cmd("ip", "-4", "route", "show", "default")
        found: list[tuple[str, str]] = []
        for line in proc.stdout.splitlines():
            words = line.split()
            if "via" in words and "dev" in words:
                gateway, device = words[words.index("via") + 1], words[words.index("dev") + 1]
                if device != self.s.interface:
                    found.append((gateway, device))
        return found

    def _default_routes(self, interface: str) -> list[str]:
        found: list[str] = []
        for family in ("-4", "-6"):
            proc = self._cmd("ip", family, "route", "show", "default", "dev", interface)
            if proc.returncode == 0:
                found.extend(line for line in proc.stdout.splitlines() if line.strip())
        return found

    def _shared_resolvers(self, connection: str) -> set[str]:
        return self._connection_dns(connection) & self._system_resolvers()

    def _connection_dns(self, connection: str) -> set[str]:
        servers: set[str] = set()
        for field_name in ("ip4.dns", "ip6.dns"):
            for value in self._nm_values(field_name, connection):
                try:
                    servers.add(str(ipaddress.ip_address(value)))
                except ValueError:
                    continue   # not an address, so nothing a resolver could share
        return servers

    def _system_resolvers(self) -> set[str]:
        """Nameserver addresses the *system* resolver is configured with —
        from /etc/resolv.conf and, behind systemd-resolved, its real upstream
        list as well."""
        servers: set[str] = set[str]()
        for path in (self.s.resolv_conf, self.s.resolved_conf):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    try:
                        servers.add(str(ipaddress.ip_address(parts[1])))
                    except ValueError:
                        pass
        return servers

    # -- selecting the route for the worker ----------------------------------------

    def _gateway(self) -> str:
        gateway = self._nm_one("IP4.GATEWAY", self.s.connection).strip()
        # Some modems report an all-zero gateway when they have none.
        return "" if gateway in ("", "0.0.0.0", "--") else gateway

    def _install_selection(self) -> None:
        s = self.s
        try:
            gateway = self._gateway()
        except LinkError:
            gateway = ""
        for dest in s.dest_v4:
            via = ["via", gateway] if gateway else []
            self._must("ip", "route", "replace", f"{dest}/32", *via, "dev", s.interface,
                       "table", str(s.table), reason="route", what="installing the cellular route")
        for dest in s.dest_v6:
            self._must("ip", "-6", "route", "replace", f"{dest}/128", "dev", s.interface,
                       "table", str(s.table), reason="route", what="installing the cellular route")

        for family, dests in (("-4", s.dest_v4), ("-6", s.dest_v6)):
            if not dests:
                continue
            # The selector is two words: iproute2 matches argv tokens, so
            # "uidrange 1-1" passed as one argument is refused outright.
            command = ["ip", family, "rule", "add", "pref", str(s.rule_pref),
                       "uidrange", f"{s.worker_uid}-{s.worker_uid}", "lookup", str(s.table)]
            proc = self._cmd(*command)
            if proc.returncode != 0 and not self._rule_present(family):
                raise LinkError("rule", f"cannot add the routing rule at preference {s.rule_pref}: "
                                        f"{_first_line(proc.stderr)}")

    def _rule_present(self, family: str) -> bool:
        """Whether the rule at our preference is ours: same UID range, same table."""
        proc = self._cmd("ip", family, "rule", "show", "pref", str(self.s.rule_pref))
        if proc.returncode != 0:
            return False
        wanted = (f"uidrange {self.s.worker_uid}-{self.s.worker_uid}", f"lookup {self.s.table}")
        return any(all(token in line for token in wanted) for line in proc.stdout.splitlines())

    def _verify_selection(self) -> None:
        """Prove, by asking the kernel for the routing decision itself, that the
        worker is carried by the modem and nobody else is.

        ``ip route get … uid N`` runs the same rule walk a packet from that uid
        would. Root and an ordinary user are both asked: the one thing this
        selection must never do is carry anyone but the worker.
        """
        s = self.s
        on_modem = re.compile(r"\bdev " + re.escape(s.interface) + r"(?![\w.-])")
        for family, dests in (("-4", s.dest_v4), ("-6", s.dest_v6)):
            for dest in dests:
                chosen = self._route_get(family, dest, s.worker_uid)
                if not on_modem.search(chosen) or f"table {s.table}" not in chosen:
                    raise LinkError(
                        "select", f"uid {s.worker_uid} would not reach {dest} through table "
                                  f"{s.table} on {s.interface}: {chosen or 'no route'}"
                    )
                for uid in _OTHER_UIDS_PROBED:
                    other = self._route_get(family, dest, uid)
                    if on_modem.search(other):
                        raise LinkError(
                            "leak", f"another user (uid {uid}) can reach {dest} through "
                                    f"{s.interface}: {other}"
                        )

    def _route_get(self, family: str, dest: str, uid: int) -> str:
        proc = self._cmd("ip", family, "route", "get", dest, "uid", str(uid))
        return _first_line(proc.stdout) if proc.returncode == 0 else ""

    def _remove_selection(self) -> None:
        s = self.s
        problems: list[str] = []
        for family, dests in (("-4", s.dest_v4), ("-6", s.dest_v6)):
            if dests and self._rule_present(family):
                self._cmd("ip", family, "rule", "del", "pref", str(s.rule_pref),
                          "uidrange", f"{s.worker_uid}-{s.worker_uid}",
                          "lookup", str(s.table))
            for dest in dests:
                # rc 2 ("No such process") just means it was never there.
                self._cmd("ip", family, "route", "del", dest, "table", str(s.table))
        for family, dests in (("-4", s.dest_v4), ("-6", s.dest_v6)):
            if dests and self._rule_present(family):
                problems.append(f"the routing rule at preference {s.rule_pref} is still present")
        if problems:
            raise LinkError("unroute", "; ".join(problems))

    # -- the verbs --------------------------------------------------------------------

    def route(self) -> None:
        s = self.s
        if not self._interface_exists():
            # The data interface exists as soon as the modem is enumerated —
            # for a modem NetworkManager manages, before any connection is
            # attempted. So "absent" means the HAT is not there.
            raise LinkError("absent", f"the modem interface {s.interface!r} does not exist")
        # The firewall first, the link second. See the module docstring. Each
        # of these refuses while the link is still untouched.
        self._ensure_policy()
        self._verify_profile()
        try:
            if not self._connection_active():
                self._connect()
            else:
                self._verify_after_up()
            self._install_selection()
            self._verify_selection()
        except Exception:
            self._abandon()
            raise

    def _abandon(self) -> None:
        """After a refusal, leave the system as if this call had never happened:
        no selection, and no link.

        The worker takes a failed ``route`` to mean "the link is down" and goes
        back to probing the normal route. If a routing rule were left behind,
        that very probe would leave over cellular — uncounted against the
        budget — and be reported as the normal route recovering. So nothing
        installed on the way to a refusal may survive it, whichever way it
        failed: a verification that did not pass, a command that errored, or
        the deadline expiring.
        """
        _disarm_deadline()
        try:
            self._remove_selection()
        except LinkError:
            pass   # the refusal being raised says why; the worker's `down` retries
        try:
            if self._connection_active():
                self._nm("--wait", "10", "connection", "down", self.s.connection, timeout=40.0)
        except LinkError:
            pass

    def firewall(self) -> None:
        """Put the deny policy in place and nothing else. Never touches the link,
        so it is safe at boot, before NetworkManager, and from the worker."""
        self._ensure_policy()

    def unroute(self) -> None:
        self._remove_selection()

    def down(self) -> None:
        self._remove_selection()
        if self._connection_active_quietly():
            self._must("nmcli", "--wait", "30", "connection", "down", self.s.connection,
                       reason="down", what="taking the profile down", timeout=60.0)
            self._verify_down()

    def _connection_active_quietly(self) -> bool:
        """Like :meth:`_connection_active`, but a profile that does not exist
        is simply not active. ``down`` must succeed whenever there is nothing
        to take down — the worker asks for it at every start, and after every
        refused ``route``."""
        try:
            return self._connection_active()
        except LinkError:
            return False

    def _verify_down(self) -> None:
        if self._connection_active():
            raise LinkError("down", f"{self.s.connection!r} is still active")

    # -- operator verbs -----------------------------------------------------------------

    def checks(self) -> list[Check]:
        """Pre-flight for ``check``: everything that can be judged before the
        link is ever brought up."""
        s = self.s
        missing = [tool for tool in ("nft", "ip", "nmcli") if _tool(tool) == tool]
        taken = self._rule_pref_taken_by()
        out = [
            Check("tools present", not missing, "missing: " + ", ".join(missing)),
            Check(f"interface {s.interface} exists", self._interface_exists(),
                  "modem absent (not plugged in, or CELLULAR_IFACE names the wrong interface)"),
            Check(f"routing rule preference {s.rule_pref} is free or ours", not taken, taken),
        ]
        if self._interface_exists():
            try:
                hazards = self.autoconnect_hazards()
                out.append(Check(
                    f"no other profile auto-connects on {s.interface}", not hazards,
                    "would bring the modem up by themselves, with a default route: "
                    + ", ".join(hazards),
                ))
            except LinkError as exc:
                out.append(Check(f"profiles on {s.interface} can be listed", False, str(exc)))
            active = self.active_others()
            out.append(Check(
                f"no other profile is using {s.interface}", not active,
                "active on the modem now: " + ", ".join(active),
            ))
        try:
            out.extend(self.profile_checks())
        except LinkError as exc:
            out.append(Check("profile exists", False, str(exc)))
        return out

    def _rule_pref_taken_by(self) -> str:
        """Why the rule preference cannot be used, or '' if it can."""
        wanted = (f"uidrange {self.s.worker_uid}-{self.s.worker_uid}", f"lookup {self.s.table}")
        for family in ("-4", "-6"):
            proc = self._cmd("ip", family, "rule", "show", "pref", str(self.s.rule_pref))
            others = [
                line.strip() for line in proc.stdout.splitlines()
                if line.strip() and not all(token in line for token in wanted)
            ]
            if others:
                return "; ".join(others)
        return ""

    def print_status(self) -> None:
        s = self.s
        print(f"profile:    {s.connection}")
        print(f"interface:  {s.interface} (present: {self._interface_exists()})")
        print(f"worker:     {s.worker_user} (uid {s.worker_uid})")
        print(f"destinations: {', '.join(s.destinations)} on tcp/443")
        print(f"table:      {s.table}, rule preference {s.rule_pref}")
        print(f"policy:     {'loaded and current' if self._policy_is_current() else 'NOT loaded'}")
        active = self._connection_active()
        print(f"link:       {'up' if active else 'down'}")
        if active:
            print(f"  address:  {', '.join(self._addresses(s.connection)) or 'none'}")
            conflict = self._on_link_conflict()
            subnet = "clear of the Pi's own networking" if not conflict else conflict
            print(f"  subnet:   {subnet}")
            default = self._default_routes(s.interface)
            print(f"  default routes on the interface: {default or 'none'}")
            shared = self._shared_resolvers(s.connection)
            print(f"  connection DNS in the system resolver: {sorted(shared) or 'none'}")
        for family, dests in (("-4", s.dest_v4), ("-6", s.dest_v6)):
            for dest in dests:
                as_worker = self._route_get(family, dest, s.worker_uid) or "none"
                print(f"  route {dest} as worker: {as_worker}")
                print(f"  route {dest} as root:   {self._route_get(family, dest, 0) or 'none'}")
        listed = self._policy_listed()
        drops = [line.strip() for line in listed.splitlines() if "drop" in line]
        if drops:
            print("firewall drop counters:")
            for line in drops:
                print("  " + line)

    def print_checks(self) -> int:
        failed = 0
        for check in self.checks():
            print(f"{'ok  ' if check.ok else 'FAIL'} {check.name}"
                  + (f" — {check.detail}" if check.detail and not check.ok else ""))
            failed += not check.ok
        return 1 if failed else 0

    def inspect(self) -> None:
        """The qualification record Plan 010 asks for: the exact modem, its
        USB identity and mode, the driver, NetworkManager's view, the kernel,
        the resolver arrangement and the clock. Read-only, and it prints no
        secret — modem paths, version strings and status answers only.

        The modem is asked what it is with status questions only (see
        :data:`_MODEM_QUESTIONS`). If ModemManager is installed, stop it
        first: it keeps the modem's ports busy."""
        s = self.s
        commands: list[tuple[str, list[str], Callable[[str], bool] | None]] = [
            ("kernel", ["uname", "-r"], None),
            ("usb devices", ["lsusb"], None),
            ("usb devices and their drivers", ["lsusb", "-t"], None),
            ("network interfaces", ["ip", "-br", "link"], None),
            ("modem interface (udev)", ["udevadm", "info", "--query=property",
                                        f"--path=/sys/class/net/{s.interface}"],
             lambda line: line.startswith("E: ID_")),
            ("NetworkManager", ["nmcli", "--version"], None),
            ("NetworkManager devices", ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION",
                                        "device"], None),
            ("NetworkManager profiles", ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE,AUTOCONNECT",
                                         "con", "show"], None),
            ("default routes", ["ip", "-4", "route", "show", "default"], None),
            ("policy routing rules", ["ip", "rule"], None),
            # `-m 0` would add the equipment identifier; the record does not
            # need it.
            ("modems (ModemManager)", ["mmcli", "-L"], None),
            ("nftables", [self._nft, "--version"], None),
            ("resolver", ["readlink", "-f", "/etc/resolv.conf"], None),
            ("real-time clock", ["sh", "-c", "ls /dev/rtc* 2>/dev/null "
                                          "|| echo 'no rtc device: the clock starts from its "
                                          "last synchronisation'"], None),
            ("time synchronisation", ["timedatectl", "show", "-p", "NTPSynchronized",
                                      "-p", "NTP"], None),
        ]
        for label, command, keep in commands:
            print(f"== {label}")
            try:
                proc = self._cmd(*command, timeout=20.0)
            except LinkError as exc:
                print(f"   (unavailable: {exc})")
                continue
            output = (proc.stdout or proc.stderr).rstrip()
            for line in output.splitlines():
                if keep is None or keep(line):
                    print("   " + line)
            if not output:
                print("   (not installed, or no output)")
        print("== modem (status questions only)")
        answers = ask_modem()
        for question, answer in answers:
            print(f"   {question:16} {answer}")
        if not answers:
            print("   (no serial port answered AT — is the modem's USB port connected?)")


# -- talking to the modem ----------------------------------------------------------------


def _tidy_at(reply: str, command: str) -> str:
    """A modem's reply without the echo of the command and the final status,
    on one line. ``ERROR`` on its own is reported as "not supported" rather
    than hidden: which questions a firmware answers is part of the record."""
    lines = [line.strip() for line in reply.replace("\r", "").split("\n") if line.strip()]
    if lines and lines[0].replace(" ", "").upper() == command.replace(" ", "").upper():
        lines = lines[1:]
    said = [line for line in lines if line not in ("OK", "ERROR")]
    if not said:
        return "not supported (ERROR)" if "ERROR" in reply else "OK"
    return " / ".join(said)


def _at_exchange(port: str, command: str, *, wait: float = 2.0) -> str | None:
    """Send one AT command to *port* and return its reply (see
    :func:`_tidy_at`), or None when the port did not answer in time.

    Reads no more than :data:`_AT_READ_LIMIT`: a Qualcomm-based module's
    diagnostic port streams binary logs continuously and never says OK."""
    import select
    import termios

    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[1] = attrs[3] = 0        # raw: no line discipline at all
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        os.write(fd, command.encode("ascii") + b"\r")
        buf = b""
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and len(buf) < _AT_READ_LIMIT:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    buf += os.read(fd, 2048)
                except OSError:
                    break
                if b"\r\nOK" in buf or b"ERROR" in buf:
                    break
        reply = buf.decode("utf-8", "replace")
    finally:
        os.close(fd)
    if "OK" not in reply and "ERROR" not in reply:
        return None
    return _tidy_at(reply, command)


def ask_modem(ports: Sequence[str] = _MODEM_PORTS) -> list[tuple[str, str]]:
    """Ask the modem what it is, changing nothing.

    Returns ``(question, answer)`` pairs beginning with the port that
    answered, or an empty list when no port answers a bare ``AT``. Only the
    first port that does is asked anything else; the others — a diagnostic
    log, a NMEA stream, a second command port — are left alone after ``AT``.
    """
    if sys.platform == "win32":
        return []
    port = next((candidate for candidate in ports if _answers(candidate)), None)
    if port is None:
        return []
    answers = [("port", port)]
    for question in _MODEM_QUESTIONS:
        answer = _at_exchange(port, question)
        if answer is not None:
            answers.append((question, answer))
    return answers


def _answers(port: str) -> bool:
    try:
        return _at_exchange(port, "AT", wait=1.0) is not None
    except (OSError, ValueError):
        # No such port, or termios cannot be applied to it (termios.error is
        # not an OSError). A port that cannot be talked to is simply skipped.
        return False


def _filled_multi_values(text: str) -> list[str]:
    """Setting names in ``nmcli --terse --fields a,b,c connection show`` output
    that carry a value. Terse lines are ``name[index]:value``; an empty
    multi-valued setting prints ``name:``. Colons inside a value (IPv6
    addresses) are backslash-escaped and do not matter for "is it empty"."""
    filled: list[str] = []
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        name, value = raw.split(":", 1)
        name = re.sub(r"\[\d+\]$", "", name).strip()
        if value.strip():
            filled.append(name)
    return filled


# -- the deadline -----------------------------------------------------------------------


def _disarm_deadline() -> None:
    if hasattr(signal, "alarm"):
        signal.alarm(0)


def _arm_deadline(seconds: int) -> None:
    """Fail, and so unwind, before the worker stops waiting (POSIX only)."""
    if not hasattr(signal, "alarm"):
        return

    def expired(signum: int, frame: Any) -> None:
        raise LinkError("timeout", f"the helper did not finish in {seconds}s")

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)


# -- entry point -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="usage-dashboard-cellular-link",
        description="Bring the Pi's cellular fallback up, firewalled, for the readings worker.",
    )
    parser.add_argument("verb", choices=WORKER_VERBS + OPERATOR_VERBS)
    parser.add_argument("--env-file", default=str(ENV_FILE), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if getattr(os, "geteuid", lambda: 0)() != 0:
        print("this helper must run as root (use sudo)", file=sys.stderr)
        return 2
    try:
        settings = LinkSettings.load(Path(args.env_file))
    except (ConfigError, LinkError) as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 2
    helper = LinkHelper(settings)
    try:
        if args.verb in WORKER_VERBS:
            _arm_deadline(HELPER_DEADLINE_SECONDS)
            try:
                getattr(helper, args.verb)()
            finally:
                _disarm_deadline()
        elif args.verb == "check":
            return helper.print_checks()
        elif args.verb == "status":
            helper.print_status()
        else:
            helper.inspect()
    except LinkError as exc:
        # The first line is the reason the worker records; the rest is for the
        # operator reading the journal.
        print(exc.reason, file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
