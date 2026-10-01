#!/usr/bin/env python3
"""Manage Labtris accounts from the shell.

    sudo -u labtris labtris-user list
    sudo -u labtris labtris-user add alice --admin
    sudo -u labtris labtris-user passwd alice
    sudo -u labtris labtris-user disable alice
    sudo -u labtris labtris-user enable alice

WHY THIS EXISTS

Until this, an instance whose admin password was lost had no supported way
back. There was no way to add a user, reset a password or even list accounts
without hand-editing Postgres — which is what both of us ended up doing, and
it is not something to ask an operator to do on their own install.

`/auth/setup` creates the first administrator and then closes forever, so the
web interface cannot help either: no wizard appears once a row exists, and the
login page is the only thing left.

PASSWORDS ARE NEVER TAKEN AS ARGUMENTS. They are prompted for, twice, with
echo off. A password on the command line lands in shell history and is visible
in `ps` to every other user on the box for as long as the process runs, which
is a bad trade for saving one line of typing.

Runs against the database directly, like packaging/seed-demo-pods.py: these
are the operations you need precisely when you cannot log in, so going through
the API would be circular.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _reexec_in_venv() -> None:
    """Hand ourselves to the interpreter that actually has the dependencies.

    The installer puts this on PATH as a symlink, so the shebang picks the
    system `python3` — which has no sqlalchemy and no asyncpg, because those
    live in /opt/labtris/.venv. Installed, the command therefore died on
    `ModuleNotFoundError: No module named 'sqlalchemy'` the moment it touched
    the database. That it is the documented way back into an instance you are
    locked out of makes failing at the import the worst possible place.

    A wrapper script in /usr/local/bin would fix the symlink case only; doing
    it here also covers running the file directly, or from a copy.
    """
    if (ROOT / ".venv" / "bin" / "python3").exists():
        try:
            import sqlalchemy  # noqa: F401
        except ModuleNotFoundError:
            venv = str(ROOT / ".venv" / "bin" / "python3")
            # execv, not a subprocess: the caller's exit status, stdin (which
            # --stdin reads a password from) and tty all carry straight over.
            os.execv(venv, [venv, str(Path(__file__).resolve()), *sys.argv[1:]])


_reexec_in_venv()

G, R, Y, D, N = "\033[0;32m", "\033[1;31m", "\033[1;33m", "\033[0;90m", "\033[0m"


def ask_password(username: str, from_stdin: bool = False) -> str | None:
    """Prompt twice with echo off, or read one line from stdin.

    --stdin exists for provisioning: a first-boot script or a CI job has no
    terminal, and `getpass` either fails or silently echoes there. It reads
    exactly one line, so the password still never appears in argv or in `ps`
    the way a `--password` flag would.
    """
    if from_stdin:
        first = sys.stdin.readline().rstrip("\n")
        if not first:
            print(f"{R}nothing on stdin — nothing changed{N}", file=sys.stderr)
            return None
        return first
    first = getpass.getpass(f"  new password for {username}: ")
    if not first:
        print(f"{R}empty password — nothing changed{N}", file=sys.stderr)
        return None
    if len(first) < 8:
        print(f"{Y}  that is under 8 characters{N}")
    again = getpass.getpass("  again: ")
    if first != again:
        print(f"{R}they do not match — nothing changed{N}", file=sys.stderr)
        return None
    return first


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every account, with role and state")
    a = sub.add_parser("add", help="create an account")
    a.add_argument("username")
    a.add_argument("--admin", action="store_true", help="give it the admin role")
    a.add_argument("--display-name", default="")
    a.add_argument("--stdin", action="store_true",
                   help="read the password from stdin instead of prompting")
    p = sub.add_parser("passwd", help="set a new password")
    p.add_argument("username")
    p.add_argument("--stdin", action="store_true",
                   help="read the password from stdin instead of prompting "
                        "(for provisioning; still never in argv)")
    for name, helptext in (("disable", "block sign-in"), ("enable", "allow sign-in again")):
        d = sub.add_parser(name, help=helptext)
        d.add_argument("username")
    args = ap.parse_args()

    from sqlalchemy import select

    from labtris_api.auth import create_user, hash_password
    from labtris_api.db import SessionLocal
    from labtris_api.models import User

    async with SessionLocal() as session:
        if args.cmd == "list":
            rows = (await session.execute(
                select(User).order_by(User.created_at))).scalars().all()
            if not rows:
                print("no accounts — open the web interface and the first-run "
                      "wizard will ask you to make one")
                return 0
            print(f"\n  {'username':<24} {'role':<7} {'state':<9} created")
            for u in rows:
                state = f"{R}disabled{N}" if u.disabled else f"{G}active{N}  "
                print(f"  {u.username:<24} {u.role:<7} {state} "
                      f"{u.created_at:%Y-%m-%d}")
            print()
            return 0

        name = args.username.strip().lower()
        existing = (await session.execute(
            select(User).where(User.username == name))).scalar_one_or_none()

        if args.cmd == "add":
            if existing:
                print(f"{R}{name} already exists{N} — use `passwd` to change its "
                      f"password", file=sys.stderr)
                return 1
            pw = ask_password(name, getattr(args, "stdin", False))
            if pw is None:
                return 1
            await create_user(session, name, pw,
                              role="admin" if args.admin else "user",
                              display_name=args.display_name)
            await session.commit()
            print(f"{G}created{N} {name} ({'admin' if args.admin else 'user'})")
            return 0

        if not existing:
            print(f"{R}no account called {name}{N} — `labtris-user list` shows "
                  f"what there is", file=sys.stderr)
            return 1

        if args.cmd == "passwd":
            pw = ask_password(name, getattr(args, "stdin", False))
            if pw is None:
                return 1
            existing.password_hash = hash_password(pw)
            await session.commit()
            print(f"{G}password changed{N} for {name}")
            return 0

        if args.cmd in ("disable", "enable"):
            # Refusing to disable the last active admin: an instance nobody can
            # administer is the state this whole command exists to get out of.
            if args.cmd == "disable" and existing.role == "admin":
                others = (await session.execute(
                    select(User).where(User.role == "admin",
                                       User.disabled.is_(False),
                                       User.username != name))).scalars().all()
                if not others:
                    print(f"{R}{name} is the only active admin{N} — disabling it "
                          f"would leave nobody able to administer this instance.\n"
                          f"       Add another admin first.", file=sys.stderr)
                    return 1
            existing.disabled = args.cmd == "disable"
            await session.commit()
            print(f"{G}{name} {'disabled' if existing.disabled else 'enabled'}{N}")
            return 0

    return 0


def load_env(confdir: str = "/etc/labtris") -> None:
    """Read LABTRIS_* out of labtris.env before anything imports settings.

    Without this the CLI falls back to the default database URL in
    labtris_api/config.py — postgresql+asyncpg://pnl:pnl@localhost/pnl — and
    dies with `password authentication failed for user "pnl"`. That is a
    confusing failure for a command whose whole job is fixing a login, and it
    is the same mistake the EVE-NG migrator shipped with: a tool that talks to
    the database has to be told which database.

    Existing variables win, so LABTRIS_DATABASE_URL=... in front of the
    command still overrides the file.
    """
    try:
        for line in Path(confdir, "labtris.env").read_text().splitlines():
            line = line.strip()
            if line.startswith("LABTRIS_") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip().strip('"').strip("'"))
    except OSError:
        pass


if __name__ == "__main__":
    os.environ.setdefault("LABTRIS_PLUGINS_DISABLED", "aws")
    load_env(os.environ.get("LABTRIS_CONFDIR", "/etc/labtris"))
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\ninterrupted — nothing changed")
        raise SystemExit(130) from None
