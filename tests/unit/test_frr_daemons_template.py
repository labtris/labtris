"""The generated /etc/frr/daemons has to be valid shell.

frrinit.sh sources this file. `VAR=  "x"` assigns VAR empty and then runs
`  "x"` as a command, so a stray space after `=` silently drops the option
and FRR starts anyway — which is why it survived unnoticed:

    /etc/frr/daemons: line 18:   -A 127.0.0.1 -s 90000000: command not found
    /etc/frr/daemons: line 19:   -A 127.0.0.1: command not found

Losing `-s 90000000` leaves zebra on the default netlink receive buffer,
which is the one thing a fabric with tens of thousands of interfaces cannot
afford.
"""

from __future__ import annotations

import re
import subprocess

from labtris_api.topology_gen import _FRR_DAEMONS_EVPN


def test_the_daemons_file_is_valid_shell() -> None:
    r = subprocess.run(["sh", "-n"], input=_FRR_DAEMONS_EVPN,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_sourcing_it_sets_the_options_rather_than_running_them() -> None:
    """`sh -n` passes on the broken form too — it is syntactically fine and
    semantically wrong. This is the test that actually catches it."""
    script = _FRR_DAEMONS_EVPN + '\nprintf "%s|%s" "$zebra_options" "$bgpd_options"\n'
    r = subprocess.run(["sh"], input=script, capture_output=True, text=True)

    assert "command not found" not in r.stderr, r.stderr
    zebra, bgpd = r.stdout.split("|")
    assert "-s 90000000" in zebra, f"zebra lost its netlink buffer size: {zebra!r}"
    assert "-A 127.0.0.1" in zebra, f"zebra VTY not bound to loopback: {zebra!r}"
    assert "-A 127.0.0.1" in bgpd, f"bgpd VTY not bound to loopback: {bgpd!r}"


def test_no_assignment_has_a_space_after_the_equals() -> None:
    for n, line in enumerate(_FRR_DAEMONS_EVPN.splitlines(), 1):
        if line.startswith("#") or not line.strip():
            continue
        assert re.match(r"^[A-Za-z_][A-Za-z0-9_]*=\S", line), f"line {n}: {line!r}"


def test_bgpd_is_actually_enabled() -> None:
    """The whole point of the EVPN variant — the stock image ships bgpd=no."""
    assert "bgpd=yes" in _FRR_DAEMONS_EVPN
    assert "bgpd=no" not in _FRR_DAEMONS_EVPN
