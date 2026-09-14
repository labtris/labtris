#!/usr/bin/env bash
#
# Boot a built ISO under QEMU against a throwaway disk, so the install can be
# watched end to end without hardware and without risking one.
#
#   ./packaging/iso/test-boot.sh dist/labtris-0.1.0-amd64.iso
#
# Headless by default: it serves VNC on :1 and the serial console on stdio,
# which is what you want on a remote build box. DISPLAY=1 opens a window.
#
set -euo pipefail

ISO=${1:?usage: test-boot.sh <iso> [disk-gb]}
DISK_GB=${2:-40}
DISK=${DISK:-${ISO%.iso}-test.qcow2}
RAM=${RAM:-4096}
CPUS=${CPUS:-2}

[ -f "$ISO" ] || { echo "no such ISO: $ISO" >&2; exit 1; }

if [ ! -f "$DISK" ]; then
  echo "==> Creating $DISK_GB GB scratch disk at $DISK"
  qemu-img create -f qcow2 "$DISK" "${DISK_GB}G" >/dev/null
fi

ACCEL=tcg
if [ -e /dev/kvm ] && [ -r /dev/kvm ]; then
  ACCEL=kvm
else
  echo "==> No /dev/kvm: emulating. An unattended install takes hours this way," >&2
  echo "    which is long enough to be worth knowing before you start." >&2
fi

# UEFI, because that is what the machines this targets actually boot with, and
# because a BIOS-only test would not exercise the half of the boot path that
# most often breaks after a repack.
OVMF=""
for c in /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd \
         /usr/share/edk2/x64/OVMF_CODE.4m.fd; do
  [ -f "$c" ] && { OVMF=$c; break; }
done

# The monitor gets its own socket rather than sharing stdio. Multiplexed onto
# stdio it is unreachable once output is redirected to a log, which is exactly
# the situation a headless test runs in — and a screendump is the only way to
# see an installer that has stopped talking to the serial port.
MONITOR=${MONITOR:-${DISK%.qcow2}-monitor.sock}
rm -f "$MONITOR"

# Where the console and the forwarded UI listen. Loopback by default, because
# the obvious `-vnc :1` binds to every interface — on a public host that is an
# unauthenticated console on the internet, which is what it was for an hour of
# testing before anyone noticed. Set BIND to a VPN address to reach it
# remotely; set it to 0.0.0.0 only if you mean it.
BIND=${BIND:-127.0.0.1}
# QEMU needs to be told at start-up that a password is possible; it cannot be
# added later. Without one macOS Screen Sharing refuses to connect at all,
# since it will not speak to a VNC server offering no authentication.
VNC_PASSWORD=${VNC_PASSWORD:-}
# dc = CD first, which is what installs. Once the disk holds an installed
# system, BOOT=c boots that instead — otherwise every run reinstalls over the
# thing you were trying to look at.
BOOT=${BOOT:-dc}
# NET=none cuts the guest off entirely, which is the only honest way to test
# that an ISO really installs without a network. Anything less proves nothing:
# a machine that can reach the internet will quietly use it.
NET=${NET:-user}
# Where the guest's serial console goes. "stdio" writes it to whatever the
# script's output is redirected to — fine for automation, useless if you are
# somewhere else and want to watch a machine boot.
#
# "telnet" publishes it on a port instead: the real serial console, from the
# first kernel line through to a login prompt, and typeable. The chardev form
# is used rather than `-serial telnet:` because it also takes logfile=, so the
# output is still captured for anything that boots before you connect.
SERIAL=${SERIAL:-stdio}
SERIAL_PORT=${SERIAL_PORT:-4555}
SERIAL_LOG=${SERIAL_LOG:-${DISK%.qcow2}-serial.log}
if [ "$NET" = "none" ]; then
  NETWORK="-nic none"
  echo "==> No guest network: a genuinely offline install"
else
  NETWORK="-netdev user,id=n0,hostfwd=tcp:$BIND:8443-:8081 -device virtio-net-pci,netdev=n0"
fi

set -- \
  -machine q35,accel=$ACCEL -cpu max -smp "$CPUS" -m "$RAM" \
  -drive file="$DISK",if=virtio,format=qcow2 \
  -drive file="$ISO",media=cdrom,readonly=on \
  -boot order=$BOOT \
  $NETWORK \
  -monitor "unix:$MONITOR,server,nowait"

if [ -n "$OVMF" ]; then
  VARS="${DISK%.qcow2}-ovmf-vars.fd"
  for v in /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/OVMF/OVMF_VARS.fd; do
    [ -f "$v" ] && [ ! -f "$VARS" ] && cp "$v" "$VARS" && break
  done
  [ -f "$VARS" ] && set -- "$@" \
    -drive if=pflash,format=raw,readonly=on,file="$OVMF" \
    -drive if=pflash,format=raw,file="$VARS"
else
  echo "==> No OVMF firmware found; booting BIOS instead of UEFI." >&2
  echo "    apt-get install ovmf to test the UEFI path." >&2
fi

if [ "$SERIAL" = "telnet" ]; then
  set -- "$@" \
    -chardev "socket,id=ser0,host=$BIND,port=$SERIAL_PORT,telnet=on,server=on,wait=off,logfile=$SERIAL_LOG" \
    -serial chardev:ser0
  echo "==> Serial console on $BIND:$SERIAL_PORT — connect with:"
  echo "      nc $BIND $SERIAL_PORT          (or telnet)"
  echo "    Boot output is also written to $SERIAL_LOG"
else
  set -- "$@" -serial stdio
fi

if [ "${DISPLAY_QEMU:-0}" = "1" ]; then
  set -- "$@" -display gtk
else
  echo "==> VNC on $BIND:5901, serial console below."
  echo "    UI forwarded to http://$BIND:8443 once the install finishes"
  echo "    Screenshot at any time:"
  echo "      echo screendump /tmp/shot.ppm | socat - unix-connect:$MONITOR"
  if [ -n "$VNC_PASSWORD" ]; then
    # Passed as a secret read from a file, not typed on the command line where
    # every user on the host could read it out of ps, and not poked in through
    # the monitor afterwards — that needed socat, and when socat was missing
    # the step was skipped in silence, leaving a VNC server that demanded a
    # password nobody had set and refused every connection.
    PWFILE=${PWFILE:-${DISK%.qcow2}-vncpw}
    ( umask 077; printf '%s' "$VNC_PASSWORD" > "$PWFILE" )
    set -- "$@" \
      -object "secret,id=vncsec,file=$PWFILE,format=raw" \
      -display none -vnc "$BIND:1,password-secret=vncsec"
  else
    echo "    (no VNC_PASSWORD set — the console has no authentication)"
    set -- "$@" -display none -vnc "$BIND:1"
  fi
fi

exec qemu-system-x86_64 "$@"
