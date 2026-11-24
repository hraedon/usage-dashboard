"""The cellular link helper's decisions, against a fake system (Plan 010).

``tests/test_cellular_link_kernel.py`` shows the helper's commands are right for
a real kernel. This module is about everything that can go wrong around them,
which needs no root to exercise: the settings file, the rendered firewall, the
order the link comes up in, and — mostly — the ways ``route`` must refuse.

The rule under test is "fail closed": if the modem is absent, the firewall
cannot be loaded or verified, the profile is not already the isolated one, the
link comes up wrong, or the route would be selected for anyone but the worker,
then the link is not used. Several of those refusals have to *also* take the
link back down, because by then it is up.

The fake system models nftables' output the way the kernel really prints it
(one-element sets collapse to a bare address, ``priority 0`` reads back as
``priority filter``) — facts established by the kernel rehearsal, not assumed.
"""
from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

from usage_dashboard.deploy import cellular_link as cl

IFACE = "cell0"
WORKER = "usage-dashboard-cellular"
UID = 990
TABLE = 17010
PREF = 17010
PIN = "192.0.2.10"
PIN2 = "198.51.100.7"
PIN6 = "2001:db8::10"
GATEWAY = "10.64.64.1"

GOOD_PROFILE = {
    "connection.type": "802-3-ethernet",
    "connection.interface-name": IFACE,
    "connection.autoconnect": "no",
    "connection.multi-connect": "0",
    "connection.metered": "yes",
    "ipv4.method": "auto",
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
# A locally administered address, as these modems' interfaces have.
MAC = "22:89:84:0d:0a:07"


def _as_nft_lists(policy: str) -> str:
    """What ``nft list table`` prints for *policy*, as the kernel prints it."""
    out = []
    for line in policy.splitlines():
        line = line.replace("priority 0;", "priority filter;")
        single = re.fullmatch(r"(\s*)(ip6?) daddr \{ (\S+) \}(.*)", line)
        if single:
            line = f"{single.group(1)}{single.group(2)} daddr {single.group(3)}{single.group(4)}"
        line = line.replace("counter drop", "counter packets 0 bytes 0 drop")
        out.append(line)
    return "\n".join(out) + "\n"


def _terse(*fields: str) -> str:
    r"""A terse line: fields joined by a colon, a colon INSIDE a field escaped
    as ``\:`` — which is what nmcli does."""
    return ":".join(field.replace(":", r"\:") for field in fields)


class OtherProfile:
    """Another NetworkManager profile, as ``nmcli`` describes it."""

    def __init__(
        self,
        name: str,
        kind: str = "802-3-ethernet",
        autoconnect: str = "yes",
        interface: str = "",
        mac: str = "",
        match: tuple[str, ...] = (),
        device: str = "",
    ) -> None:
        self.name = name
        self.kind = kind
        self.autoconnect = autoconnect
        self.interface = interface
        self.mac = mac
        self.match = match
        self.device = device          # set when the profile is active


class FakeSystem:
    """A stand-in for ``ip``, ``nft``, ``nmcli`` and ``id`` that prints what the
    real tools print.

    The NetworkManager shapes here were read off 1.52.1 on the first unit with
    the HAT fitted, not guessed. An earlier version of this fake encoded
    assumptions; against the real tool every one of these would have failed:

    * ``connection.type`` is ``802-3-ethernet``, not ``ethernet``;
    * ``connection.multi-connect`` prints ``0``, not ``single``;
    * an indexed name like ``IP4.ADDRESS[1]`` is rejected in ``-g`` mode;
    * a multi-valued setting prints one line joined by ``" | "``, not a line
      per value.

    nftables' listing is modelled the same way (one-element sets collapse to a
    bare address, ``priority 0`` reads back as ``priority filter``).
    """

    def __init__(self, resolv_conf: Path, resolved_conf: Path, run_dir: Path) -> None:
        self.log: list[tuple[str, ...]] = []
        self.run_dir = run_dir
        self.resolv_conf = resolv_conf
        self.resolved_conf = resolved_conf
        self.iface_exists = True
        self.mac = MAC
        self.profile: dict[str, str] = dict(GOOD_PROFILE)
        self.profile_exists = True
        self.filled: list[str] = []            # multi-valued settings that are set
        self.others: list[OtherProfile] = []   # every other profile
        self.active = False
        self.up_fails = False
        self.devices = [IFACE]
        self.address = "10.64.64.2/30"
        self.v6_address = ""
        self.gateway = GATEWAY
        self.dns: list[str] = []               # what DHCP hands the connection
        self.default_routes: list[str] = []    # a default route on the interface
        self.other_defaults: list[str] = []    # default routes on other ports
        self.nft_check_fails = False
        self.nft_load_fails = False
        self.listed = ""                       # the kernel's view of the table
        self.tamper_listing = False
        self.foreign_rule: set[str] = set()    # families where the pref is taken
        self.rules: set[str] = set()           # families where our rule is in
        self.rule_stuck = False                # a rule that will not delete
        self.routes: set[tuple[str, str]] = set()   # (family, dest) we installed
        self.others_reach_via_iface = False    # a leak the final check must catch
        self.leak_uids: set[str] | None = None  # ...only for these uids, when set
        self.worker_not_selected = False
        self.selected_via = IFACE               # the device the kernel names for the worker
        self.route_install_fails: set[str] = set()   # families whose route replace fails

    # -- command table -------------------------------------------------------

    def __call__(self, command: list[str], timeout: float) -> CompletedProcess[str]:
        self.log.append(tuple(command))
        out, err, rc = self._answer(command)
        return CompletedProcess(command, rc, out, err)

    def _answer(self, c: list[str]) -> tuple[str, str, int]:
        tool = os.path.basename(c[0])   # the helper may call nft by full path
        if tool == "id":
            return f"{UID}\n", "", 0
        if tool == "ip":
            return self._ip(c[1:])
        if tool == "nft":
            return self._nft(c[1:])
        if tool == "nmcli":
            # The helper's calls differ only in flags that do not change what
            # is being asked; the fake models the question.
            skip_next = False
            words = []
            for word in c[1:]:
                if skip_next:
                    skip_next = False
                elif word == "--wait":
                    skip_next = True
                elif word != "--terse":
                    words.append(word)
            return self._nmcli(words)
        return "", f"{tool}: not modelled", 127

    def _ip(self, a: list[str]) -> tuple[str, str, int]:
        family = ""
        if a and a[0] in ("-4", "-6"):
            family, a = a[0], a[1:]
        kind = "-6" if family == "-6" else "-4"
        if a[:2] == ["link", "show"]:
            if not self.iface_exists:
                return "", f'Device "{IFACE}" does not exist.', 1
            return (
                f"3: {IFACE}: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UNKNOWN\n"
                f"    link/ether {self.mac} brd ff:ff:ff:ff:ff:ff\n"
            ), "", 0
        if a[:3] == ["route", "show", "default"]:
            mine = self.default_routes if kind == "-4" and self.active else []
            lines = mine if "dev" in a else mine + list(self.other_defaults)
            return "".join(line + "\n" for line in lines), "", 0
        if a[:2] == ["route", "replace"]:
            if kind in self.route_install_fails:
                return "", "RTNETLINK answers: Invalid argument", 2
            self.routes.add((kind, a[2].split("/")[0]))
            return "", "", 0
        if a[:2] == ["route", "del"]:
            if (kind, a[2]) in self.routes:
                self.routes.discard((kind, a[2]))
                return "", "", 0
            return "", "RTNETLINK answers: No such process", 2
        if a[:2] == ["route", "get"]:
            return self._route_get(kind, a[2], a[4]) + "\n", "", 0
        if a[:2] == ["rule", "add"]:
            if kind in self.rules or kind in self.foreign_rule:
                return "", "RTNETLINK answers: File exists", 2
            self.rules.add(kind)
            return "", "", 0
        if a[:2] == ["rule", "show"]:
            lines = []
            if kind in self.rules:
                lines.append(f"{PREF}:\tfrom all uidrange {UID}-{UID} lookup {TABLE}")
            if kind in self.foreign_rule:
                lines.append(f"{PREF}:\tfrom all lookup 100")
            return "".join(line + "\n" for line in lines), "", 0
        if a[:2] == ["rule", "del"]:
            if self.rule_stuck:
                return "", "RTNETLINK answers: Invalid argument", 2
            self.rules.discard(kind)
            return "", "", 0
        return "", f"ip: not modelled: {a}", 127

    def _route_get(self, kind: str, dest: str, uid: str) -> str:
        selected = (kind, dest) in self.routes and kind in self.rules
        if uid == str(UID):
            if selected and not self.worker_not_selected:
                return (f"{dest} via {GATEWAY} dev {self.selected_via} table {TABLE} "
                        f"src 10.64.64.2 uid {UID}")
            return f"{dest} via 192.168.1.1 dev wlan0 src 192.168.1.50 uid {UID}"
        leaking = (
            self.others_reach_via_iface if self.leak_uids is None else uid in self.leak_uids
        )
        if leaking:
            return f"{dest} via {GATEWAY} dev {IFACE} uid {uid}"
        return f"{dest} via 192.168.1.1 dev wlan0 src 192.168.1.50 uid {uid}"

    def _nft(self, a: list[str]) -> tuple[str, str, int]:
        stateless = a[:1] == ["--stateless"]
        if stateless:
            a = a[1:]
        if a[:1] == ["--check"]:
            return "", "Error: syntax error" if self.nft_check_fails else "", \
                1 if self.nft_check_fails else 0
        if a[:1] == ["--file"]:
            if self.nft_load_fails:
                return "", "Error: could not process rule", 1
            policy = Path(a[1]).read_text(encoding="utf-8")
            self.listed = _as_nft_lists(policy)
            if self.tamper_listing:
                self.listed = self.listed.replace(f"meta skuid {UID}", "meta skuid 1")
            return "", "", 0
        if a[:2] == ["list", "table"]:
            # --stateless leaves the counters' numbers out, so an untouched
            # table reads the same however much traffic it has dropped.
            listed = self._stateless() if stateless else self.listed
            return listed, "Error: No such file or directory" if not self.listed else "", \
                0 if self.listed else 1
        return "", "nft: not modelled", 127

    def _stateless(self) -> str:
        return re.sub(r"counter packets \d+ bytes \d+ drop", "counter drop", self.listed)

    # -- nmcli, in the shapes NetworkManager 1.52 really prints -------------------

    def _nmcli(self, a: list[str]) -> tuple[str, str, int]:
        if a[:2] == ["connection", "up"]:
            if self.up_fails or not self.profile_exists:
                return "", "Error: Connection activation failed", 1
            self.active = True
            return "Connection successfully activated\n", "", 0
        if a[:2] == ["connection", "down"]:
            self.active = False
            return "", "", 0
        # Everything else is a listing; the flag comes first:
        # `nmcli -t -g F connection show NAME`, `nmcli -t -f L connection show`.
        if "connection" not in a or "show" not in a:
            return "", "nmcli: not modelled", 127
        if "--active" in a:
            # `nmcli -t -f NAME,DEVICE connection show --active`
            using = [_terse(other.name, other.device) for other in self.others if other.device]
            if self.active:
                using.append(_terse("cellular", IFACE))
            return "".join(line + "\n" for line in using), "", 0
        if a[0] in ("--fields", "-f"):
            fields = a[1].split(",")
            if fields == ["NAME", "TYPE"]:
                rows = [_terse(other.name, other.kind) for other in self.others]
                if self.profile_exists:
                    rows.append(_terse("cellular", self.profile["connection.type"]))
                return "".join(line + "\n" for line in rows), "", 0
            if not self.profile_exists:
                return "", "Error: cellular - no such connection profile.", 10
            return "".join(self._listed_property(name) for name in fields), "", 0
        if a[0] == "--get-values":
            fields = a[1].split(",")
            if any(field.endswith("]") for field in fields):
                # "Error: 'connection show': invalid field 'IP4.ADDRESS[1]'..."
                return "", f"Error: 'connection show': invalid field '{fields[0]}'", 2
            name = a[-1]
            if name != "cellular":
                other = next((o for o in self.others if o.name == name), None)
                if other is None:
                    return "", f"Error: {name} - no such connection profile.", 10
                return "".join(self._other_value(field, other) + "\n" for field in fields), \
                    "", 0
            if not self.profile_exists:
                return "", "Error: cellular - no such connection profile.", 10
            return "".join(self._value(field) + "\n" for field in fields), "", 0
        return "", "nmcli: not modelled", 127

    def _value(self, field: str) -> str:
        if field == "GENERAL.STATE":
            return "activated" if self.active else ""
        if field == "GENERAL.DEVICES":
            return " | ".join(self.devices) if self.active else ""
        runtime = {
            "IP4.ADDRESS": self.address if self.active else "",
            "IP6.ADDRESS": self.v6_address if self.active else "",
            "IP4.GATEWAY": self.gateway if self.active else "",
        }
        if field in runtime:
            return runtime[field]
        if field in ("ip4.dns", "ip6.dns"):
            servers = self.dns if self.active and field == "ip4.dns" else []
            return " | ".join(servers)          # one line, whatever the count
        return self.profile[field]

    def _listed_property(self, name: str) -> str:
        # `nmcli -t -f a,b,c connection show X`: "name:" when empty.
        return f"{name}[1]: 203.0.113.53\n" if name in self.filled else f"{name}:\n"

    def _other_value(self, field: str, other: OtherProfile) -> str:
        known = {
            "connection.autoconnect": other.autoconnect,
            "connection.interface-name": other.interface,
            "802-3-ethernet.mac-address": other.mac,
            "match.interface-name": " | ".join(other.match),
        }
        return known.get(field, "")

    # -- what happened ---------------------------------------------------------

    def names(self) -> list[str]:
        """Short names of the commands run, for order assertions."""
        out = []
        for c in self.log:
            tool = os.path.basename(c[0])
            if tool == "nft" and c[1] == "--file":
                out.append("load-firewall")
            elif tool == "nft" and c[1] == "--check":
                out.append("check-firewall")
            elif tool == "nmcli" and "up" in c:
                out.append("link-up")
            elif tool == "nmcli" and "down" in c:
                out.append("link-down")
            elif tool == "ip" and "replace" in c:
                out.append("install-route")
            elif tool == "ip" and c[-4:-3] == ("uidrange",) and "add" in c:
                out.append("add-rule")
        return out


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # nft lives in /usr/sbin on a Pi; a test host may not have it at all.
    monkeypatch.setattr(
        cl.shutil, "which",
        lambda name: {"nft": "/usr/sbin/nft"}.get(name, f"/usr/bin/{name}"),
    )
    resolv = tmp_path / "resolv.conf"
    resolved = tmp_path / "resolved.conf"
    run_dir = tmp_path / "run"
    system = FakeSystem(resolv, resolved, run_dir)
    settings = cl.LinkSettings(
        connection="cellular", interface=IFACE, dest_v4=(PIN,), dest_v6=(),
        worker_user=WORKER, worker_uid=UID, table=TABLE, rule_pref=PREF,
        link_setup=frozenset({cl.DHCP}), ipv6=False,
        resolv_conf=resolv, resolved_conf=resolved, run_dir=run_dir,
    )
    return {
        "sys": system,
        "settings": settings,
        "helper": cl.LinkHelper(settings, runner=system),
        "resolv": resolv,
        "resolved": resolved,
        "run_dir": run_dir,
    }


def _reloaded(world: dict[str, Any], **changes: Any) -> cl.LinkHelper:
    return cl.LinkHelper(replace(world["settings"], **changes), runner=world["sys"])


def assert_left_nothing_behind(world: dict[str, Any]) -> None:
    """After a refusal, the system must look as if ``route`` never ran: no
    rule, no route in our table, no link. A rule left behind would carry the
    worker's next normal-route probe over cellular, uncounted, and report it
    as the normal route recovering."""
    sys = world["sys"]
    assert sys.rules == set(), "the worker's routing rule is still installed"
    assert sys.routes == set(), "routes are still in the table"
    assert sys.active is False, "the link is still up"



# -- settings -----------------------------------------------------------------


class TestSettings:
    def _load(self, tmp_path: Path, text: str, **kwargs: Any) -> cl.LinkSettings:
        env = tmp_path / "env"
        env.write_text(text)
        return cl.LinkSettings.load(
            env, runner=lambda c, t: CompletedProcess(c, 0, f"{UID}\n", ""),
            resolv_conf=tmp_path / "r", resolved_conf=tmp_path / "rr",
            run_dir=tmp_path / "run", **kwargs,
        )

    def test_minimal_file(self, tmp_path: Path) -> None:
        s = self._load(tmp_path, f"CELLULAR_CONNECTION=cellular\nCELLULAR_IFACE={IFACE}\n"
                                 f"CELLULAR_DEST={PIN}\n")
        assert (s.connection, s.interface, s.dest_v4, s.dest_v6) == ("cellular", IFACE, (PIN,), ())
        assert (s.worker_uid, s.table, s.rule_pref) == (UID, cl.DEFAULT_TABLE, cl.DEFAULT_RULE_PREF)
        assert s.link_setup == frozenset({cl.DHCP}) and s.ipv6 is False

    def test_the_file_is_read_not_sourced(self, tmp_path: Path) -> None:
        # It carries a credential next to these settings, so it is parsed as
        # data: no expansion, no commands, whatever the line looks like.
        s = self._load(
            tmp_path,
            "# a comment\n"
            f"CELLULAR_CONNECTION='cellular'\n"
            f'CELLULAR_IFACE="{IFACE}"\n'
            "CELLULAR_DEST=$(reboot)\n"
            f"CELLULAR_DEST={PIN}\n"
            "SPACED KEY = is ignored\n",
        )
        assert s.connection == "cellular" and s.interface == IFACE
        assert s.dest_v4 == (PIN,)

    def test_an_inline_comment_is_part_of_the_value(self, tmp_path: Path) -> None:
        # As with systemd's EnvironmentFile and update.sh's env_or_file, only a
        # whole-line comment is a comment. An inline one would silently become
        # part of a setting, so the example file says not to write them.
        s = self._load(
            tmp_path,
            "CELLULAR_CONNECTION=cellular # not a comment\n"
            f"CELLULAR_IFACE={IFACE}\nCELLULAR_DEST={PIN}\n",
        )
        assert s.connection == "cellular # not a comment"

    @pytest.mark.parametrize("name", ["CELLULAR_CONNECTION", "CELLULAR_IFACE", "CELLULAR_DEST"])
    def test_each_link_fact_must_be_stated(self, tmp_path: Path, name: str) -> None:
        lines = {"CELLULAR_CONNECTION": "cellular", "CELLULAR_IFACE": IFACE, "CELLULAR_DEST": PIN}
        del lines[name]
        with pytest.raises(cl.ConfigError, match=name):
            self._load(tmp_path, "".join(f"{k}={v}\n" for k, v in lines.items()))

    @pytest.mark.parametrize(
        "line,complaint",
        [
            ("CELLULAR_IFACE=cell 0", "CELLULAR_IFACE"),
            (f"CELLULAR_IFACE={IFACE};reboot", "CELLULAR_IFACE"),
            ("CELLULAR_DEST=dashboard.example", "IP literals"),
            ("CELLULAR_TABLE=254", "reserved"),
            ("CELLULAR_TABLE=x", "whole number"),
            ("CELLULAR_RULE_PREF=0", "between"),
            ("CELLULAR_IPV6=0", "CELLULAR_IPV6"),
            ("CELLULAR_LINK_SETUP=gre", "CELLULAR_LINK_SETUP"),
            ("CELLULAR_LINK_SETUP=none,dhcp", "cannot be combined"),
            ("CELLULAR_WORKER_USER=Bad Name", "CELLULAR_WORKER_USER"),
        ],
    )
    def test_contradictions_are_refused_before_anything_runs(
        self, tmp_path: Path, line: str, complaint: str
    ) -> None:
        base = f"CELLULAR_CONNECTION=cellular\nCELLULAR_IFACE={IFACE}\n"
        dest = f"CELLULAR_DEST={PIN6}\n" if line == "CELLULAR_IPV6=0" else f"CELLULAR_DEST={PIN}\n"
        if line == "CELLULAR_LINK_SETUP=none,dhcp":
            line = "CELLULAR_LINK_SETUP=none,dhcp"
        with pytest.raises(cl.ConfigError, match=complaint):
            self._load(tmp_path, base + dest + line + "\n")

    def test_dhcpv6_needs_ipv6_and_none_is_allowed(self, tmp_path: Path) -> None:
        base = f"CELLULAR_CONNECTION=cellular\nCELLULAR_IFACE={IFACE}\nCELLULAR_DEST={PIN6}\n"
        with pytest.raises(cl.ConfigError, match="CELLULAR_IPV6"):
            self._load(tmp_path, base)
        s = self._load(tmp_path, base + "CELLULAR_IPV6=1\nCELLULAR_LINK_SETUP=none\n")
        assert s.ipv6 is True and s.dest_v6 == (PIN6,) and s.link_setup == frozenset()

    @pytest.mark.parametrize(
        "rc,out", [(1, ""), (0, "0\n"), (0, "not-a-number\n"), (0, "\n")]
    )
    def test_the_worker_uid_must_resolve_to_an_unprivileged_user(
        self, tmp_path: Path, rc: int, out: str
    ) -> None:
        env = tmp_path / "env"
        env.write_text(f"CELLULAR_CONNECTION=c\nCELLULAR_IFACE={IFACE}\nCELLULAR_DEST={PIN}\n")
        with pytest.raises(cl.ConfigError):
            cl.LinkSettings.load(
                env, runner=lambda c, t: CompletedProcess(c, rc, out, ""),
                resolv_conf=tmp_path / "r", resolved_conf=tmp_path / "rr",
                run_dir=tmp_path / "run",
            )

    def test_a_missing_file_is_a_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(cl.ConfigError, match="cannot read"):
            cl.LinkSettings.load(
                tmp_path / "absent", runner=lambda c, t: CompletedProcess(c, 0, "", "")
            )

    def test_addresses_are_split_by_family_and_deduplicated(self, tmp_path: Path) -> None:
        s = self._load(tmp_path, f"CELLULAR_CONNECTION=c\nCELLULAR_IFACE={IFACE}\n"
                                 f"CELLULAR_DEST={PIN}, {PIN2},{PIN}\nCELLULAR_IPV6=1\n")
        assert s.dest_v4 == (PIN, PIN2) and s.destinations == (PIN, PIN2)


# -- the rendered firewall -------------------------------------------------------


class TestRenderPolicy:
    def test_default_shape(self) -> None:
        policy = cl.render_policy(IFACE, UID, [PIN], [])
        lines = policy.splitlines()
        # One transaction: declare, delete, recreate. Nothing is applied in pieces.
        assert lines[:2] == [f"table inet {cl.TABLE_NAME} {{ }}",
                             f"delete table inet {cl.TABLE_NAME}"]
        assert lines[2] == f"table inet {cl.TABLE_NAME} {{"
        output = lines[lines.index("\tchain cellular_output {"):lines.index("\t\tcounter drop")]
        # Link setup first, exactly one permit for the worker, and the drop last.
        assert output[1:] == [
            "\t\tudp sport 68 udp dport 67 accept",
            f"\t\tmeta skuid {UID} ip daddr {PIN} tcp dport 443 accept",
        ]
        joined = "\n".join(lines)
        assert f'oifname "{IFACE}" jump cellular_output' in joined
        assert f'iifname "{IFACE}" jump cellular_input' in joined
        assert "ct state established,related accept" in joined
        # The DHCP offer, inbound — and only to a client's port.
        assert "\t\tudp sport 67 udp dport 68 accept" in joined
        assert f'\t\tiifname "{IFACE}" counter drop' in joined
        assert f'\t\toifname "{IFACE}" counter drop' in joined
        for hook in ("output", "input", "forward"):
            assert f"type filter hook {hook} priority 0; policy accept;" in joined
        assert "ip6" not in joined and "icmpv6" not in joined

    def test_several_addresses_form_a_set(self) -> None:
        policy = cl.render_policy(IFACE, UID, [PIN2, PIN], [])
        assert f"meta skuid {UID} ip daddr {{ {PIN}, {PIN2} }} tcp dport 443 accept" in policy

    def test_ipv6_and_dhcpv6(self) -> None:
        policy = cl.render_policy(IFACE, UID, [PIN], [PIN6], link_setup={"dhcp", "dhcpv6"})
        assert f"meta skuid {UID} ip6 daddr {PIN6} tcp dport 443 accept" in policy
        assert "udp sport 546 udp dport 547 accept" in policy
        assert policy.count("icmpv6 type { nd-router-solicit") == 2   # out and in
        self._assert_neighbour_discovery_both_ways(policy)

    def test_ipv6_alone_needs_neighbour_discovery_both_ways(self) -> None:
        # The kernel rehearsal found this one: with the inbound rule missing,
        # the solicitations went out and the advertisements were dropped, so
        # the route was selected and nothing resolved. Counting the rules is
        # not enough — which chain each sits in is the whole point.
        self._assert_neighbour_discovery_both_ways(
            cl.render_policy(IFACE, UID, [], [PIN6], link_setup={"none"})
        )

    def _assert_neighbour_discovery_both_ways(self, policy: str) -> None:
        chains = dict(
            (name, body)
            for name, body in re.findall(r"chain (\S+) \{(.*?)\n\t\}", policy, re.S)
        )
        assert "icmpv6 type" in chains["cellular_output"], "solicitations cannot go out"
        assert "icmpv6 type" in chains["cellular_input"], "advertisements cannot come back"

    def test_no_link_setup_packets_at_all(self) -> None:
        policy = cl.render_policy(IFACE, UID, [PIN], [], link_setup={"none"})
        assert "udp" not in policy

    @pytest.mark.parametrize(
        "args,kwargs",
        [
            (("cell 0", UID, [PIN], []), {}),
            ((IFACE, 0, [PIN], []), {}),
            ((IFACE, UID, [PIN], []), {"link_setup": {"gre"}}),
        ],
    )
    def test_it_refuses_to_render_what_it_cannot_quote(self, args: Any, kwargs: Any) -> None:
        with pytest.raises(ValueError):
            cl.render_policy(*args, **kwargs)


# -- route -----------------------------------------------------------------------


class TestRoute:
    def test_happy_path_and_its_order(self, world: dict[str, Any]) -> None:
        sys = world["sys"]
        world["helper"].route()
        assert sys.names() == [
            "check-firewall", "load-firewall", "link-up", "install-route", "add-rule",
        ]
        # The firewall is verified from the kernel before the link is touched:
        # the first listing AFTER it was loaded comes before `nmcli up`.
        log = sys.log
        written = ("--file", str(world["run_dir"] / "policy.nft"))
        loaded = next(i for i, c in enumerate(log) if c[1:3] == written)
        listed = next(
            i for i, c in enumerate(log)
            if i > loaded and os.path.basename(c[0]) == "nft" and c[1:3] == ("list", "table")
        )
        link_up = next(i for i, c in enumerate(log) if c[0] == "nmcli" and "up" in c)
        assert loaded < listed < link_up
        assert sys.active and sys.rules == {"-4"} and sys.routes == {("-4", PIN)}

    def test_the_route_goes_through_the_carrier_gateway(self, world: dict[str, Any]) -> None:
        world["helper"].route()
        command = next(c for c in world["sys"].log if "replace" in c)
        assert command == ("ip", "route", "replace", f"{PIN}/32", "via", GATEWAY,
                           "dev", IFACE, "table", str(TABLE))

    @pytest.mark.parametrize("gateway", ["", "0.0.0.0", "--"])
    def test_a_modem_without_a_gateway_gets_a_device_route(
        self, world: dict[str, Any], gateway: str
    ) -> None:
        world["sys"].gateway = gateway
        world["helper"].route()
        command = next(c for c in world["sys"].log if "replace" in c)
        assert command == ("ip", "route", "replace", f"{PIN}/32", "dev", IFACE,
                           "table", str(TABLE))

    def test_ipv6_destinations_get_their_own_route_and_rule(self, world: dict[str, Any]) -> None:
        world["sys"].profile["ipv6.method"] = "manual"
        _reloaded(world, dest_v6=(PIN6,), ipv6=True).route()
        commands = world["sys"].log
        assert ("ip", "-6", "route", "replace", f"{PIN6}/128", "dev", IFACE,
                "table", str(TABLE)) in commands
        assert world["sys"].rules == {"-4", "-6"}

    def test_an_absent_modem_stops_everything(self, world: dict[str, Any]) -> None:
        world["sys"].iface_exists = False
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "absent"
        assert world["sys"].names() == []

    @pytest.mark.parametrize("knob", ["nft_check_fails", "nft_load_fails", "tamper_listing"])
    def test_a_firewall_that_cannot_be_loaded_and_verified_stops_the_link(
        self, world: dict[str, Any], knob: str
    ) -> None:
        setattr(world["sys"], knob, True)
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "policy"
        assert world["sys"].active is False
        assert "link-up" not in world["sys"].names()

    @pytest.mark.parametrize(
        "setting,value",
        [(k, v) for k, v in GOOD_PROFILE.items()] + [("connection.type", "wifi")],
    )
    def test_a_profile_that_is_not_already_isolated_is_refused(
        self, world: dict[str, Any], setting: str, value: str
    ) -> None:
        # Flip each setting to the opposite of what isolation needs, in the
        # words nmcli uses for it.
        opposite = {
            "connection.type": "wifi",
            "connection.interface-name": "eth0",
            "connection.autoconnect": "yes",
            "connection.multi-connect": "2",      # "multiple", as nmcli prints it
            "connection.metered": "no",
            "ipv4.method": "shared",
            "ipv4.never-default": "no",
            "ipv4.ignore-auto-routes": "no",
            "ipv4.ignore-auto-dns": "no",
            "ipv4.route-table": "100",
            "ipv6.method": "auto",
            "ipv6.never-default": "no",
            "ipv6.ignore-auto-routes": "no",
            "ipv6.ignore-auto-dns": "no",
            "ipv6.route-table": "100",
        }
        world["sys"].profile[setting] = opposite[setting]
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "profile"
        assert setting in str(refused.value), refused.value
        assert "link-up" not in world["sys"].names()
        assert world["sys"].active is False

    @pytest.mark.parametrize("name", ["", "*", IFACE])
    def test_a_gsm_profile_may_leave_the_interface_to_networkmanager(
        self, world: dict[str, Any], name: str
    ) -> None:
        world["sys"].profile["connection.type"] = "gsm"
        world["sys"].profile["connection.interface-name"] = name
        world["helper"].route()
        assert world["sys"].active

    def test_a_gsm_profile_naming_another_interface_is_refused(
        self, world: dict[str, Any]
    ) -> None:
        world["sys"].profile["connection.type"] = "gsm"
        world["sys"].profile["connection.interface-name"] = "wwan1"
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "profile"
        assert "wwan1" in str(refused.value)

    def test_a_missing_profile_is_refused(self, world: dict[str, Any]) -> None:
        world["sys"].profile_exists = False
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "profile"
        assert "link-up" not in world["sys"].names()

    def test_static_routes_or_resolver_entries_in_the_profile_are_refused(
        self, world: dict[str, Any]
    ) -> None:
        world["sys"].filled = ["ipv4.routes"]
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "profile" and "ipv4.routes" in str(refused.value)

    def test_ipv6_may_be_enabled_deliberately(self, world: dict[str, Any]) -> None:
        world["sys"].profile["ipv6.method"] = "auto"
        with pytest.raises(cl.LinkError):
            world["helper"].route()
        _reloaded(world, ipv6=True).route()
        assert world["sys"].active

    def test_a_profile_that_will_not_come_up(self, world: dict[str, Any]) -> None:
        world["sys"].up_fails = True
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "connect"
        assert world["sys"].routes == set()

    def test_a_profile_that_comes_up_without_an_address(self, world: dict[str, Any]) -> None:
        world["sys"].address = ""
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "connect"
        assert "link-down" in world["sys"].names()

    @pytest.mark.parametrize(
        "wrong,reason",
        [
            ("devices", "leak"), ("default_route", "leak"), ("dns", "leak"),
        ],
    )
    def test_a_link_that_comes_up_leaking_is_taken_straight_back_down(
        self, world: dict[str, Any], wrong: str, reason: str
    ) -> None:
        sys = world["sys"]
        world["resolv"].write_text("nameserver 192.168.1.1\n")
        if wrong == "devices":
            sys.devices = ["usb1"]
        elif wrong == "default_route":
            sys.default_routes = [f"default via {GATEWAY} dev {IFACE}"]
        else:
            sys.dns = ["203.0.113.53"]
            world["resolved"].write_text("nameserver 203.0.113.53\n")
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == reason
        assert sys.names()[-1] == "link-down"
        assert sys.active is False
        assert sys.routes == set() and sys.rules == set()

    def test_connection_dns_that_is_not_the_system_resolver_is_fine(
        self, world: dict[str, Any]
    ) -> None:
        world["sys"].dns = ["203.0.113.53"]
        world["resolv"].write_text("nameserver 192.168.1.1\n")
        world["helper"].route()
        assert world["sys"].active

    def test_another_users_route_to_the_dashboard_is_a_leak(self, world: dict[str, Any]) -> None:
        world["sys"].others_reach_via_iface = True
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "leak"
        assert_left_nothing_behind(world)

    def test_a_selection_that_does_not_select_is_refused(self, world: dict[str, Any]) -> None:
        world["sys"].worker_not_selected = True
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "select"
        assert_left_nothing_behind(world)

    def test_a_rule_preference_taken_by_someone_else_is_refused(
        self, world: dict[str, Any]
    ) -> None:
        world["sys"].foreign_rule = {"-4"}
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "rule"

    def test_route_on_an_active_link_does_not_bring_it_up_again(
        self, world: dict[str, Any]
    ) -> None:
        world["helper"].route()
        world["sys"].log.clear()
        world["helper"].route()
        names = world["sys"].names()
        assert "link-up" not in names and "load-firewall" not in names
        assert names == ["install-route", "add-rule"]

    def test_a_changed_firewall_is_reloaded(self, world: dict[str, Any]) -> None:
        world["helper"].route()
        world["sys"].log.clear()
        _reloaded(world, dest_v4=(PIN, PIN2)).route()
        assert world["sys"].names()[0:2] == ["check-firewall", "load-firewall"]

    def test_a_firewall_that_vanished_is_reloaded(self, world: dict[str, Any]) -> None:
        world["helper"].route()
        world["sys"].log.clear()
        world["sys"].listed = ""
        world["helper"].route()
        assert world["sys"].names() == [
            "check-firewall", "load-firewall", "install-route", "add-rule",
        ]


# -- unroute and down -------------------------------------------------------------


class TestWithdraw:
    def test_unroute_removes_the_rule_and_the_route_only(self, world: dict[str, Any]) -> None:
        world["helper"].route()
        world["sys"].log.clear()
        world["helper"].unroute()
        sys = world["sys"]
        assert sys.rules == set() and sys.routes == set()
        assert sys.active is True                    # the link stays up
        assert sys.listed != ""                      # and the firewall stays loaded
        assert ("ip", "-4", "rule", "del", "pref", str(PREF), "uidrange", f"{UID}-{UID}",
                "lookup", str(TABLE)) in sys.log
        assert ("ip", "-4", "route", "del", PIN, "table", str(TABLE)) in sys.log

    def test_unroute_is_idempotent(self, world: dict[str, Any]) -> None:
        world["helper"].unroute()
        world["helper"].route()
        world["helper"].unroute()
        world["helper"].unroute()

    def test_a_rule_that_will_not_delete_is_an_error(self, world: dict[str, Any]) -> None:
        world["helper"].route()
        world["sys"].rule_stuck = True
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].unroute()
        assert refused.value.reason == "unroute"

    def test_down_takes_the_link_down_and_leaves_the_firewall(self, world: dict[str, Any]) -> None:
        world["helper"].route()
        world["sys"].log.clear()
        world["helper"].down()
        sys = world["sys"]
        assert sys.active is False and sys.rules == set() and sys.routes == set()
        assert sys.names()[-1] == "link-down"
        assert sys.listed != ""

    def test_down_is_idempotent_and_quiet_when_already_down(self, world: dict[str, Any]) -> None:
        world["helper"].down()
        assert world["sys"].names() == []


# -- operator views ------------------------------------------------------------------


class TestOperatorViews:
    def test_check_reports_each_isolation_property(self, world: dict[str, Any]) -> None:
        world["sys"].profile["connection.autoconnect"] = "yes"
        world["sys"].iface_exists = False
        checks = world["helper"].checks()
        failed = {c.name for c in checks if not c.ok}
        assert f"interface {IFACE} exists" in failed
        assert "does not autoconnect" in failed
        assert not any("metered" in c.name and not c.ok for c in checks)

    def test_check_names_a_missing_profile(self, world: dict[str, Any]) -> None:
        world["sys"].profile_exists = False
        assert any(not c.ok and c.name == "profile exists" for c in world["helper"].checks())

    def test_check_names_a_taken_rule_preference(self, world: dict[str, Any]) -> None:
        world["sys"].foreign_rule = {"-4"}
        taken = [c for c in world["helper"].checks() if "routing rule preference" in c.name]
        assert taken and not taken[0].ok and "lookup 100" in taken[0].detail

    def test_print_checks_exit_code(
        self, world: dict[str, Any], capsys: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(cl.shutil, "which", lambda tool: f"/usr/sbin/{tool}")
        assert world["helper"].print_checks() == 0
        world["sys"].profile["connection.metered"] = "no"
        assert world["helper"].print_checks() == 1
        assert "FAIL metered — connection.metered is 'no'" in capsys.readouterr().out

    def test_filled_multi_values_parser(self) -> None:
        text = "ipv4.dns:\nipv6.dns[1]:2001\\:db8\\:\\:1\nipv4.routes:\nipv4.routing-rules:\n"
        assert cl._filled_multi_values(text) == ["ipv6.dns"]
        assert cl._filled_multi_values("ipv4.dns: \n") == []


# -- entry point ----------------------------------------------------------------------


class TestMain:
    def _env(self, tmp_path: Path) -> str:
        env = tmp_path / "env"
        env.write_text(f"CELLULAR_CONNECTION=cellular\nCELLULAR_IFACE={IFACE}\n"
                       f"CELLULAR_DEST={PIN}\n")
        return str(env)

    def test_an_unknown_verb_is_a_usage_error(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exited:
            cl.main(["reboot", "--env-file", self._env(tmp_path)])
        assert exited.value.code == 2

    def test_non_root_is_refused(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setattr(cl.os, "geteuid", lambda: 1000, raising=False)
        assert cl.main(["route", "--env-file", self._env(tmp_path)]) == 2

    def test_a_missing_settings_file_is_a_config_error(
        self, tmp_path: Path, monkeypatch: Any, capsys: Any
    ) -> None:
        monkeypatch.setattr(cl.os, "geteuid", lambda: 0, raising=False)
        assert cl.main(["route", "--env-file", str(tmp_path / "absent")]) == 2
        assert "config:" in capsys.readouterr().err

    def test_the_worker_verbs_are_exactly_what_sudoers_grants(self) -> None:
        assert cl.WORKER_VERBS == ("firewall", "route", "unroute", "down")


# -- what the real NetworkManager taught us ------------------------------------------------


class TestRealNetworkManagerFormats:
    def test_the_fake_speaks_as_the_real_one_did(self, world: dict[str, Any]) -> None:
        sys, helper = world["sys"], world["helper"]
        sys.active = True
        sys.address = "192.168.0.100/24"
        sys.dns = ["192.168.0.2", "192.168.0.3"]
        assert helper._nm_one("connection.type", "cellular") == "802-3-ethernet"
        assert helper._nm_one("connection.multi-connect", "cellular") == "0"
        assert helper._nm_values("ip4.dns", "cellular") == ["192.168.0.2", "192.168.0.3"]
        assert helper._nm_values("IP4.ADDRESS", "cellular") == ["192.168.0.100/24"]
        assert helper._nm_values("ip6.dns", "cellular") == []
        assert helper._interface_mac() == MAC

    def test_a_healthy_link_passes_every_check_with_those_shapes(
        self, world: dict[str, Any]
    ) -> None:
        # Before the formats were corrected this failed four different ways.
        world["helper"].route()
        assert world["sys"].active

    def test_it_never_asks_for_an_indexed_field(self, world: dict[str, Any]) -> None:
        # NetworkManager rejects "IP4.ADDRESS[1]" in -g mode outright.
        world["helper"].route()
        fields = [
            command[command.index("--get-values") + 1] for command in world["sys"].log
            if command[0] == "nmcli" and "--get-values" in command
        ]
        assert fields and not any(field.endswith("]") for field in fields), fields

    def test_two_dns_servers_from_the_modem_are_both_caught(self, world: dict[str, Any]) -> None:
        # The work Pi's modem hands out two servers on one nmcli line.
        world["sys"].dns = ["192.168.0.2", "192.168.0.3"]
        world["resolv"].write_text("nameserver 192.168.1.53\nnameserver 192.168.0.3\n")
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "leak"
        assert "192.168.0.3" in str(refused.value)

    @pytest.mark.parametrize(
        "raw,type_",
        [("802-3-ethernet", "ethernet"), ("gsm", "gsm"), ("802-11-wireless", "802-11-wireless")],
    )
    def test_type_names(self, raw: str, type_: str) -> None:
        assert cl._connection_type(raw) == type_

    def test_terse_escaping(self) -> None:
        assert cl._un_terse(r"Wired\: 2") == "Wired: 2"

    def test_nft_is_found_where_the_package_puts_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not on an ordinary user's PATH on the Pi; in /usr/sbin.
        monkeypatch.setattr(cl.shutil, "which", lambda name: None)
        monkeypatch.setattr(cl.os.path, "exists", lambda path: path == "/usr/sbin/nft")
        assert cl._tool("nft") == "/usr/sbin/nft"
        assert cl._tool("no-such-tool") == "no-such-tool"


# -- other profiles that would use the modem by themselves -----------------------------------


class TestAutoconnectingProfiles:
    def test_an_auto_generated_wired_profile_blocks_the_link(self, world: dict[str, Any]) -> None:
        # What NetworkManager made of usb0 on the first unit with the HAT: a
        # profile nobody asked for, auto-connecting, with a default route.
        world["sys"].others = [OtherProfile("Wired connection 2", interface=IFACE, device=IFACE)]
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "profile"
        assert "Wired connection 2" in str(refused.value)
        assert "link-up" not in world["sys"].names()
        failed = {check.name for check in world["helper"].checks() if not check.ok}
        assert f"no other profile auto-connects on {IFACE}" in failed
        assert f"no other profile is using {IFACE}" in failed

    def test_one_that_does_not_autoconnect_is_left_alone(self, world: dict[str, Any]) -> None:
        world["sys"].others = [
            OtherProfile("spare", autoconnect="no", interface=IFACE, device=IFACE),
        ]
        world["helper"].route()
        assert world["sys"].active
        failed = {check.name for check in world["helper"].checks() if not check.ok}
        assert failed == {f"no other profile is using {IFACE}"}

    def test_one_bound_by_mac_only_is_a_hazard(self, world: dict[str, Any]) -> None:
        world["sys"].others = [OtherProfile("by-mac", mac=MAC)]
        assert world["helper"].autoconnect_hazards() == ["by-mac"]
        world["sys"].others = [OtherProfile("other-mac", mac="00:11:22:33:44:55")]
        assert world["helper"].autoconnect_hazards() == []

    def test_one_bound_by_a_name_pattern(self, world: dict[str, Any]) -> None:
        world["sys"].others = [OtherProfile("glob", match=("cell*", "usb?"))]
        assert world["helper"].autoconnect_hazards() == ["glob"]
        world["sys"].others = [OtherProfile("elsewhere", match=("eth*",))]
        assert world["helper"].autoconnect_hazards() == []

    def test_a_profile_for_another_port_or_another_kind_is_ignored(
        self, world: dict[str, Any]
    ) -> None:
        world["sys"].others = [
            OtherProfile("Wired connection 1", interface="eth0", device="eth0"),
            OtherProfile("a phone", kind="802-11-wireless", device="wlan0"),
        ]
        assert world["helper"].autoconnect_hazards() == []
        assert world["helper"].active_others() == []

    def test_an_ethernet_profile_with_nothing_to_bind_it_applies_to_every_port(
        self, world: dict[str, Any]
    ) -> None:
        world["sys"].others = [OtherProfile("any cable")]
        assert world["helper"].autoconnect_hazards() == ["any cable"]

    def test_a_name_containing_a_colon(self, world: dict[str, Any]) -> None:
        world["sys"].others = [OtherProfile("Wired: 2", interface=IFACE)]
        listed, _err, _rc = world["sys"]._nmcli(["--fields", "NAME,TYPE", "connection", "show"])
        assert r"Wired\: 2:802-3-ethernet" in listed   # escaped by nmcli...
        assert world["helper"].autoconnect_hazards() == ["Wired: 2"]   # ...undone by the helper

    def test_which_profiles_are_using_the_modem_now(self, world: dict[str, Any]) -> None:
        world["sys"].others = [
            OtherProfile("Wired connection 2", interface=IFACE, device=IFACE),
            OtherProfile("Wired connection 1", interface="eth0", device="eth0"),
        ]
        assert world["helper"].active_others() == ["Wired connection 2"]

    def test_the_decision_function_errs_toward_caution(self) -> None:
        applies = cl._profile_applies
        assert applies("", "", [], IFACE, MAC) is True             # binds to anything
        assert applies("", MAC, [], IFACE, MAC) is True            # this device's MAC
        assert applies("", "00:11:22:33:44:55", [], IFACE, MAC) is False
        assert applies("", MAC, [], IFACE, "") is True             # device MAC unknown
        assert applies(IFACE, "", [], IFACE, MAC) is True
        assert applies("eth0", "", [], IFACE, MAC) is False
        assert applies("eth0", "", ["cell*"], IFACE, MAC) is True  # match.* governs


# -- the modem's own subnet -----------------------------------------------------------------------


class TestOnLinkSubnet:
    def test_the_first_unit_s_numbers_are_clear(self, world: dict[str, Any]) -> None:
        # The shape of the first unit: the modem on its default LAN, the Pi
        # with two resolvers on one network and two default gateways on two
        # others. Nothing the Pi needs sits inside 192.168.0.0/24.
        sys = world["sys"]
        sys.address = "192.168.0.100/24"
        sys.gateway = "192.168.0.1"
        world["resolv"].write_text("nameserver 192.168.1.53\nnameserver 192.168.1.54\n")
        sys.other_defaults = [
            "default via 192.168.10.1 dev eth0 proto dhcp src 192.168.10.129 metric 101",
            "default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.62 metric 600",
        ]
        world["helper"].route()
        assert sys.active

    @pytest.mark.parametrize(
        "setup,label",
        [
            ("resolver", "a system resolver"),
            ("gateway", "the gateway of the default route on wlan0"),
            ("pinned", "a pinned destination"),
        ],
    )
    def test_whatever_the_pi_needs_inside_that_subnet_is_a_conflict(
        self, world: dict[str, Any], setup: str, label: str
    ) -> None:
        sys = world["sys"]
        sys.address = "192.168.0.100/24"
        if setup == "resolver":
            world["resolv"].write_text("nameserver 192.168.0.2\n")
        elif setup == "gateway":
            sys.other_defaults = ["default via 192.168.0.1 dev wlan0 metric 600"]
        else:
            world["settings"] = replace(world["settings"], dest_v4=("192.168.0.9",))
            world["helper"] = _reloaded(world, dest_v4=("192.168.0.9",))
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "conflict"
        assert "192.168.0.0/24" in str(refused.value) and label in str(refused.value)
        # The link was brought up to learn its subnet, so it is taken back down.
        assert world["sys"].names()[-1] == "link-down"
        assert sys.active is False

    def test_a_lan_that_shares_only_a_prefix_is_not_a_conflict(
        self, world: dict[str, Any]
    ) -> None:
        sys = world["sys"]
        sys.address = "192.168.0.100/24"
        world["resolv"].write_text("nameserver 192.168.1.53\n")
        sys.other_defaults = ["default via 192.168.10.1 dev wlan0"]
        world["helper"].route()
        assert sys.active

    def test_a_bare_host_address_is_not_a_subnet(self, world: dict[str, Any]) -> None:
        sys = world["sys"]
        sys.address = "192.168.0.100/32"
        sys.other_defaults = ["default via 192.168.0.1 dev wlan0"]
        world["helper"].route()
        assert sys.active

    def test_status_says_so(self, world: dict[str, Any], capsys: Any) -> None:
        world["helper"].route()
        world["helper"].print_status()
        assert "subnet:   clear of the Pi's own networking" in capsys.readouterr().out


# -- the modem's own answers ----------------------------------------------------------------------


class TestModemAnswers:
    @pytest.mark.parametrize(
        "reply,expected",
        [
            ("AT+CGMM\r\r\n+CGMM: \r\r\nSIM7670G-MNGV\r\r\n\r\r\nOK\r\r\n",
             "+CGMM: / SIM7670G-MNGV"),
            ("AT+DIALMODE?\r\r\n+DIALMODE: \r\r\n1\r\r\n\r\r\nOK\r\r\n",
             "+DIALMODE: / 1"),
            ("AT+USBNETMAC?\r\r\nERROR\r\r\n", "not supported (ERROR)"),
            ("AT\r\r\nOK\r\r\n", "OK"),
        ],
    )
    def test_replies_are_tidied(self, reply: str, expected: str) -> None:
        assert cl._tidy_at(reply, reply.split("\r")[0]) == expected

    def test_the_questions_are_all_status_questions(self) -> None:
        for question in cl._MODEM_QUESTIONS:
            assert question.startswith("AT")
            # Nothing that assigns: no "=" anywhere, and no value after a
            # command. "AT+CGMM"/"AT+CGMR" are the two argumentless executions.
            assert "=" not in question
            assert question.endswith("?") or question in ("AT+CGMM", "AT+CGMR", "AT+CSQ")

    def test_nothing_is_sent_on_a_system_without_serial_ports(self) -> None:
        assert cl.ask_modem(ports=()) == []

    def test_imei_is_not_asked_for(self) -> None:
        assert "ATI" not in cl._MODEM_QUESTIONS


# -- a refusal leaves nothing behind (an independent review found this hole) ------------------


class TestRefusalsLeaveNothingBehind:
    def test_a_rule_that_cannot_be_installed_unwinds_the_rest(self, world: dict[str, Any]) -> None:
        sys = world["sys"]
        sys.route_install_fails = {"-6"}
        sys.profile["ipv6.method"] = "manual"
        helper = _reloaded(world, dest_v6=("2001:db8::10",), ipv6=True)
        with pytest.raises(cl.LinkError) as refused:
            helper.route()
        assert refused.value.reason == "route"
        assert_left_nothing_behind(world)

    def test_a_link_that_was_already_up_is_taken_down_when_reused_and_refused(
        self, world: dict[str, Any]
    ) -> None:
        # The link deliberately stays up between reads. If a later `route`
        # finds a reason to refuse — here something the Pi needs has moved onto
        # the modem's own subnet — that link is black-holing the Pi's normal
        # networking and must not be left up.
        world["sys"].address = "192.168.0.100/24"
        world["helper"].route()
        # Then the hotspot the Pi is also on hands out a resolver in 192.168.0.x.
        world["resolv"].write_text("nameserver 192.168.0.2\n")
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "conflict"
        assert_left_nothing_behind(world)

    @pytest.mark.parametrize("uid", ["0", "1000"])
    def test_any_other_user_on_the_modem_is_a_leak(self, world: dict[str, Any], uid: str) -> None:
        world["sys"].leak_uids = {uid}
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "leak"
        assert f"uid {uid}" in str(refused.value)
        assert_left_nothing_behind(world)

    def test_a_route_through_a_similarly_named_device_is_not_ours(
        self, world: dict[str, Any]
    ) -> None:
        # "dev cell01" contains "dev cell0" as text; it is not the modem.
        world["sys"].selected_via = IFACE + "1"
        with pytest.raises(cl.LinkError) as refused:
            world["helper"].route()
        assert refused.value.reason == "select"
        assert_left_nothing_behind(world)

    def test_down_still_works_after_a_refusal(self, world: dict[str, Any]) -> None:
        world["sys"].others_reach_via_iface = True
        with pytest.raises(cl.LinkError):
            world["helper"].route()
        world["helper"].down()      # what the worker asks for next; must be quiet
        assert world["sys"].active is False


# -- is the kernel still holding our firewall? ------------------------------------------------


class TestFirewallIsRechecked:
    def test_an_unchanged_table_is_not_reloaded(self, world: dict[str, Any]) -> None:
        world["helper"].route()
        world["sys"].log.clear()
        world["helper"].route()
        assert world["sys"].names() == ["install-route", "add-rule"]

    def test_a_table_someone_trimmed_is_reloaded(self, world: dict[str, Any]) -> None:
        # The worker's allowance and the forward chain survive; the chain that
        # actually drops other users' output is gone. A check of the stamp
        # alone would call this "current".
        world["helper"].route()
        listed = world["sys"].listed
        start = listed.index("\tchain output {")
        end = listed.index("\tchain cellular_output {")
        world["sys"].listed = listed[:start] + listed[end:]
        world["sys"].log.clear()
        world["helper"].route()
        assert world["sys"].names()[:2] == ["check-firewall", "load-firewall"]
        assert "\tchain output {" in world["sys"].listed

    def test_a_flushed_table_is_reloaded(self, world: dict[str, Any]) -> None:
        world["helper"].route()
        world["sys"].listed = ""
        world["sys"].log.clear()
        world["helper"].route()
        assert world["sys"].names()[:2] == ["check-firewall", "load-firewall"]

    def test_counters_do_not_make_the_table_look_changed(self, world: dict[str, Any]) -> None:
        world["helper"].route()
        counted = world["sys"].listed.replace(
            "counter packets 0 bytes 0", "counter packets 57 bytes 9104"
        )
        world["sys"].listed = counted
        world["sys"].log.clear()
        world["helper"].route()
        assert "load-firewall" not in world["sys"].names()


# -- the firewall on its own ----------------------------------------------------------------------


class TestFirewallVerb:
    def test_it_loads_the_policy_and_never_touches_the_link(self, world: dict[str, Any]) -> None:
        world["helper"].firewall()
        sys = world["sys"]
        assert sys.names() == ["check-firewall", "load-firewall"]
        assert sys.active is False and sys.rules == set() and sys.routes == set()
        sys.log.clear()
        world["helper"].firewall()        # and again: a no-op
        assert sys.names() == []

    def test_it_needs_no_modem(self, world: dict[str, Any]) -> None:
        world["sys"].iface_exists = False
        world["helper"].firewall()
        assert "load-firewall" in world["sys"].names()

    def test_it_needs_no_profile(self, world: dict[str, Any]) -> None:
        world["sys"].profile_exists = False
        world["helper"].firewall()
        assert "load-firewall" in world["sys"].names()


# -- the order nftables really lists addresses in ------------------------------------------------


class TestAddressOrder:
    @pytest.mark.parametrize(
        "given,expected",
        [
            (["192.0.2.10", "192.0.2.9"], "{ 192.0.2.9, 192.0.2.10 }"),
            (["192.0.2.100", "192.0.2.20", "198.51.100.7"],
             "{ 192.0.2.20, 192.0.2.100, 198.51.100.7 }"),
        ],
    )
    def test_by_value_not_as_text(self, given: list[str], expected: str) -> None:
        # Read off a real kernel: nftables lists a set sorted by value, which
        # differs from text order as soon as the lengths differ. Written in
        # text order, the helper's own check would refuse its own firewall.
        assert cl._address_match(given) == expected
        assert "ip daddr " + expected in cl.render_policy(IFACE, UID, given, [])

    def test_ipv6_too(self) -> None:
        assert cl._address_match(["2001:db8::10", "2001:db8::9", "2001:db8:1::1"]) == (
            "{ 2001:db8::9, 2001:db8::10, 2001:db8:1::1 }"
        )

    def test_two_addresses_pass_the_helpers_own_check(self, world: dict[str, Any]) -> None:
        _reloaded(world, dest_v4=("192.0.2.10", "192.0.2.9")).route()
        assert world["sys"].active
        routes = sorted(dest for kind, dest in world["sys"].routes if kind == "-4")
        assert routes == ["192.0.2.10", "192.0.2.9"]


# -- smaller things the review pointed out -------------------------------------------------------


class TestSmallerPoints:
    def test_a_colon_inside_a_name(self) -> None:
        assert cl._split_terse(r"Wired\: 2:802-3-ethernet") == ["Wired: 2", "802-3-ethernet"]
        assert cl._split_terse("plain:802-3-ethernet") == ["plain", "802-3-ethernet"]

    def test_an_empty_value_is_not_a_value(self, world: dict[str, Any]) -> None:
        assert world["helper"]._nm_values("ip6.dns", "cellular") == []
        assert world["helper"]._nm_values("IP4.ADDRESS", "cellular") == []   # not active yet
        assert world["helper"]._nm_one("GENERAL.STATE", "cellular") == ""

    def test_the_deadline_is_below_the_workers_patience(self) -> None:
        # cellular_worker.SudoLink gives a `route` 150 s before it kills sudo.
        assert cl.HELPER_DEADLINE_SECONDS + 30 < 150

    @pytest.mark.skipif(
        not hasattr(cl.signal, "alarm"), reason="the deadline uses signal.alarm (POSIX)"
    )
    def test_the_deadline_unwinds_a_hung_verb(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        import time as _time

        settings = cl.LinkSettings(
            connection="cellular", interface=IFACE, dest_v4=(PIN,), dest_v6=(),
            worker_user=WORKER, worker_uid=UID, table=TABLE, rule_pref=PREF,
            link_setup=frozenset({cl.DHCP}), ipv6=False,
            resolv_conf=tmp_path / "r", resolved_conf=tmp_path / "rr", run_dir=tmp_path / "run",
        )
        monkeypatch.setattr(cl.os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setattr(cl, "HELPER_DEADLINE_SECONDS", 1)
        monkeypatch.setattr(cl.LinkSettings, "load", lambda *a, **k: settings)
        ended: list[str] = []

        def hung(self: cl.LinkHelper) -> None:
            try:
                _time.sleep(5)
            except cl.LinkError:
                ended.append("unwound")
                raise

        monkeypatch.setattr(cl.LinkHelper, "route", hung)
        started = _time.monotonic()
        assert cl.main(["route", "--env-file", str(tmp_path / "env")]) == 1
        assert _time.monotonic() - started < 4
        assert ended == ["unwound"]
        assert capsys.readouterr().err.splitlines()[0] == "timeout"

    def test_the_deadline_is_disarmed_after_a_verb(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if not hasattr(cl.signal, "alarm"):
            pytest.skip("POSIX only")
        armed: list[int] = []
        monkeypatch.setattr(
            cl.signal, "alarm", lambda seconds: armed.append(seconds) or 0, raising=False
        )
        cl._arm_deadline(5)
        cl._disarm_deadline()
        assert armed == [5, 0]
