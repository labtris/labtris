from __future__ import annotations

import argparse
import asyncio
import grp
import os
from pathlib import Path

from labtris_netd.net import PyrouteNet
from labtris_netd.protocol import FrameDecoder, encode
from labtris_netd.verbs import dispatch


async def handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    net: PyrouteNet,
    token: str | None = None,
) -> None:
    """Unix-socket callers are trusted (file perms already gate access); a
    TCP listener is reachable off-host, so it's how a *second* host's netd is
    meant to be driven for multi-host — and it requires the first frame to be
    `auth` with the shared token before anything else is dispatched."""
    decoder = FrameDecoder()
    authenticated = token is None
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            for frame in decoder.feed(data):
                if not authenticated:
                    req_id = frame.get("id")
                    ok = frame.get("verb") == "auth" and (frame.get("params") or {}).get(
                        "token"
                    ) == token
                    if ok:
                        authenticated = True
                        writer.write(encode({"id": req_id, "ok": True, "result": {}}))
                    else:
                        writer.write(
                            encode(
                                {
                                    "id": req_id,
                                    "ok": False,
                                    "error": {"code": "EAUTH", "message": "auth required"},
                                }
                            )
                        )
                        await writer.drain()
                        return
                    await writer.drain()
                    continue
                reply = dispatch(frame, net)
                writer.write(encode(reply))
                await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def log_startup_warning(exc: Exception) -> None:
    print(f"netd: group_fwd reconcile skipped: {exc}", flush=True)


def _relax_bridge_netfilter() -> None:
    """Linux bridges used as a lab dataplane must not be hijacked by iptables."""
    for name in (
        "bridge-nf-call-iptables",
        "bridge-nf-call-ip6tables",
        "bridge-nf-call-arptables",
    ):
        path = Path(f"/proc/sys/net/bridge/{name}")
        if path.exists():
            path.write_text("0")


async def serve(
    path: str, tcp_listen: str | None, token: str | None, group: str | None = None
) -> None:
    sock = Path(path)
    sock.parent.mkdir(parents=True, exist_ok=True)
    if sock.exists():
        sock.unlink()
    _relax_bridge_netfilter()
    net = PyrouteNet()
    # Repair bridges that predate the group_fwd_mask change, so upgrading
    # fixes a running lab instead of requiring it to be rebuilt.
    try:
        net.reconcile_group_fwd()
    except Exception as exc:  # noqa: BLE001 - never block startup on this
        log_startup_warning(exc)
    servers = [await asyncio.start_unix_server(lambda r, w: handle(r, w, net), path=path)]
    # netd runs as root; the API does not. 0660 root:root means the API cannot
    # connect at all, which shows up as a permission error on every call rather
    # than anything pointing at the socket — so name the group that may reach it.
    os.chmod(path, 0o660)
    if group:
        try:
            os.chown(path, -1, grp.getgrnam(group).gr_gid)
        except KeyError:
            raise SystemExit(f"--socket-group: no such group {group!r}") from None
        except PermissionError:
            raise SystemExit(f"cannot chown {path} to group {group!r}; netd needs root") from None
    if tcp_listen:
        host, _, port_s = tcp_listen.rpartition(":")
        tcp = await asyncio.start_server(
            lambda r, w: handle(r, w, net, token=token), host=host or "0.0.0.0", port=int(port_s)
        )
        servers.append(tcp)
    await asyncio.gather(*(s.serve_forever() for s in servers))


def main() -> None:
    parser = argparse.ArgumentParser(prog="labtris-netd")
    parser.add_argument("--socket", default="/run/labtris/netd.sock")
    parser.add_argument(
        "--tcp-listen",
        default=os.environ.get("LABTRIS_NETD_TCP"),
        help="HOST:PORT — makes this netd reachable for multi-host (requires --token)",
    )
    parser.add_argument("--token", default=os.environ.get("LABTRIS_NETD_TOKEN"))
    parser.add_argument(
        "--socket-group",
        default=os.environ.get("LABTRIS_NETD_SOCKET_GROUP"),
        help="group allowed to reach the unix socket — the group the API runs as",
    )
    args = parser.parse_args()
    if args.tcp_listen and not args.token:
        parser.error(
            "--tcp-listen requires --token (or LABTRIS_NETD_TOKEN) — it is network-reachable"
        )
    asyncio.run(serve(args.socket, args.tcp_listen, args.token, args.socket_group))


if __name__ == "__main__":
    main()
