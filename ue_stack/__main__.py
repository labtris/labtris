"""`uestack` — thin CLI around the daemon.

Two verbs today:

    uestack listen [--port N]              # bind + print every packet
    uestack send <host>:<port> <message>   # send one packet, exit

Enough to demo the wire format between two Labtris nodes on a
common bridge; a fuller CLI (open a context, hold it, run a
benchmark) is future work."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from ue_stack.daemon import DEFAULT_PORT, UeStackDaemon
from ue_stack.packet import OpCode, UePacket
from ue_stack.pdc import Ipdc


def _cmd_listen(args: argparse.Namespace) -> int:
    async def go() -> None:
        d = UeStackDaemon(port=args.port)
        seen = 0

        def on_rx(addr: tuple[str, int], pkt: UePacket) -> None:
            nonlocal seen
            seen += 1
            print(f"[{seen}] {addr[0]}:{addr[1]}  {pkt!r}")
            if pkt.payload:
                print(f"    payload: {pkt.payload!r}")
            sys.stdout.flush()

        await d.start(on_rx)
        print(f"uestack listening on port {args.port}. Ctrl-C to stop.")
        try:
            await asyncio.Event().wait()   # sleep forever
        except asyncio.CancelledError:
            pass
        d.close()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    host, _, port = args.target.partition(":")
    port_num = int(port) if port else DEFAULT_PORT

    async def go() -> None:
        d = UeStackDaemon(port=0)  # ephemeral local port
        received: list[str] = []
        def on_rx(addr: tuple[str, int], pkt: UePacket) -> None:
            received.append(repr(pkt))
        await d.start(on_rx)
        ctx = Ipdc(pdc_id=1, local_endpoint=1, dst_endpoint=args.dst)
        pkt = await ctx.send(
            d.sock_send_to(host, port_num),
            OpCode.SEND,
            args.message.encode(),
        )
        print(f"sent → {pkt!r}")
        d.close()

    asyncio.run(go())
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    p = argparse.ArgumentParser(prog="uestack", description="UET wire-format PoC")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_listen = sub.add_parser("listen", help="bind a UDP port + print every received packet")
    p_listen.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_listen.set_defaults(func=_cmd_listen)

    p_send = sub.add_parser("send", help="send one UET SEND packet to host:port")
    p_send.add_argument("target", help="host:port")
    p_send.add_argument("message", help="ASCII payload")
    p_send.add_argument("--dst", type=int, default=1, help="dst endpoint id (default 1)")
    p_send.set_defaults(func=_cmd_send)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
