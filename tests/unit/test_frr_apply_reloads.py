"""Applying config must not restart FRR unless a restart is required.

An unconditional restart is correct on a cold node and catastrophic on a
converged fabric: it drops every session, each drop is seen by every peer,
and each peer re-advertises to its own. On a k=22 fat tree one edge restart
touches roughly 253 adjacencies, so applying config to 242 edges sixteen at a
time put ~4000 adjacency events in flight, pinned all 24 cores, and starved
sshd off the box entirely.

A restart is only needed when the daemon SET changed — true on the first
apply, false on every one after.
"""

from __future__ import annotations

import subprocess
import textwrap

import labtris_api.topology_gen as tg


def _run(script: str, *, running: bool, same_daemons: bool) -> str:
    """Execute the installer's decision logic against fakes.

    Only the tail matters, so the heredocs and chown are replaced with a
    harness that records which verb was chosen.
    """
    harness = textwrap.dedent(f"""
        set -e
        mkdir -p etc/frr usr/lib/frr
        cat > usr/lib/frr/frrinit.sh <<'EOS'
        #!/bin/sh
        case "$1" in
          status) exit {0 if running else 3} ;;
          *) echo "VERB=$1" ;;
        esac
        EOS
        chmod +x usr/lib/frr/frrinit.sh
        printf 'bgpd=yes\\n' > etc/frr/daemons.new
        printf '{'bgpd=yes' if same_daemons else 'bgpd=no'}\\n' > etc/frr/daemons
    """)
    tail = script[script.index("NEED_RESTART=no"):]
    tail = tail.replace("/usr/lib/frr/frrinit.sh", "usr/lib/frr/frrinit.sh")
    tail = tail.replace("/etc/frr/", "etc/frr/")
    r = subprocess.run(["sh", "-c", harness + tail], capture_output=True, text=True,
                       cwd=subprocess.run(["mktemp", "-d"], capture_output=True,
                                          text=True).stdout.strip())
    return (r.stdout + r.stderr).strip()


def _installer() -> str:
    return tg._frr_installer(tg._FRR_DAEMONS_EVPN, "hostname x")


def test_first_apply_restarts_because_the_daemon_set_changed() -> None:
    """The image entrypoint starts FRR from a daemons file saying bgpd=no, so
    `start` is a no-op and bgpd never launches. Only a restart re-reads it."""
    out = _run(_installer(), running=True, same_daemons=False)

    assert "VERB=restart" in out, out
    assert "VERB=reload" not in out


def test_second_apply_reloads_because_nothing_about_the_daemons_changed() -> None:
    """This is the case that caused the storm — it used to restart anyway."""
    out = _run(_installer(), running=True, same_daemons=True)

    assert "VERB=reload" in out, out
    assert "VERB=restart" not in out, "a converged fabric must not be restarted"


def test_a_node_with_no_frr_running_restarts_rather_than_reloading() -> None:
    """There is nothing to reload into, and reload would simply fail."""
    out = _run(_installer(), running=False, same_daemons=True)

    assert "VERB=restart" in out, out


def test_the_daemons_file_is_only_replaced_after_the_comparison() -> None:
    """Writing it first would make every comparison report 'unchanged' and the
    first apply would reload into a bgpd that is not running."""
    script = _installer()

    assert script.index("daemons.new") < script.index("cmp -s")
    assert script.index("cmp -s") < script.index("mv -f /etc/frr/daemons.new")


def test_a_declined_reload_is_announced_not_silent() -> None:
    """frr-reload.py refuses diffs it cannot express incrementally. Falling
    back to a restart is right, but it is expensive enough to say so."""
    script = _installer()
    i = script.index("elif ! /usr/lib/frr/frrinit.sh reload")

    assert "reload declined the diff" in script[i:]
    assert ">&2" in script[i:], "the warning belongs on stderr"
