#!/usr/bin/env bash
#
# Register VyOS as a Labtris QEMU template, from the installer ISO.
#
# VyOS ships only a live+installer ISO, and Labtris's built-in
# QEMU_CATALOG downloads ready-to-boot disks — it does not drive an
# installer. So the way in is the same as any other appliance that
# needs first-boot setup: install once, register the resulting qcow2
# with `labtris-image add`. This script automates all of that except
# the four keystrokes VyOS's `install image` wizard insists on
# (yes, defaults, admin password, yes).
#
#   sudo ./packaging/recipes/vyos.sh                 # 2026.03, default
#   sudo VYOS_ISO_URL=... VYOS_NAME=... ./vyos.sh    # any version
#
# What it does:
#   1. Downloads the ISO into /var/cache/labtris/iso/ (skips if cached).
#   2. Creates a scratch 10 GB qcow2 in a workdir.
#   3. Boots QEMU with the ISO as CD-ROM plus a serial console on VNC
#      :10 — attach a VNC viewer to <host>:5910 and run `install image`
#      inside the guest, choosing defaults and setting the admin
#      password. `poweroff` when done.
#   4. Waits for QEMU to exit, then re-uses the resulting disk to
#      register a Labtris template via `labtris-image add`.
#   5. Removes the workdir; the disk lives on inside the image cache
#      under its content hash.
#
# The interactive step is deliberate: VyOS's installer takes a
# password from the operator and there is no defensible non-prompt
# path. What this script removes is everything around that step —
# the download, the qemu flags, the register-with-labtris.
#
set -euo pipefail

VYOS_ISO_URL=${VYOS_ISO_URL:-https://community-downloads.vyos.dev/stream/2026.03/vyos-2026.03-generic-amd64.iso}
VYOS_NAME=${VYOS_NAME:-VyOS 2026.03}
VYOS_DISK_GB=${VYOS_DISK_GB:-10}
VYOS_RAM_MB=${VYOS_RAM_MB:-1024}
VYOS_CPUS=${VYOS_CPUS:-1}
# How you drive the installer: "vnc" (a graphical console, the default)
# or "serial" (a text console on the terminal this script runs in).
# Serial needs nothing but the terminal — no VNC port, no viewer, no
# firewall hole — and works because VyOS's live ISO already boots with
# `console=ttyS0` in its kernel command line. Run this script inside
# tmux/screen for serial so you can attach and detach at will.
VYOS_CONSOLE=${VYOS_CONSOLE:-vnc}          # vnc | serial
VYOS_VNC_DISPLAY=${VYOS_VNC_DISPLAY:-10}   # attach viewer to <host>:5900+display
# Where the installer's VNC listens. Localhost by default — the safe
# choice, since the install shows a root shell. Reach it with an SSH
# tunnel:
#     ssh -L 5910:localhost:5910 <box>
# then point a viewer at localhost:5910. Set VYOS_VNC_HOST=0.0.0.0 to
# expose it on the network directly (only on a network you trust, and
# only for the few minutes the install takes) — pair that with a
# password.
VYOS_VNC_HOST=${VYOS_VNC_HOST:-127.0.0.1}
# Optional VNC password. Empty means no auth. The VNC protocol truncates
# to 8 characters, so anything longer is silently shortened. Strongly
# advised whenever VYOS_VNC_HOST is not localhost. Some VNC clients
# (macOS Screen Sharing among them) also connect more reliably to a
# password-protected server than to an open one.
VYOS_VNC_PASSWORD=${VYOS_VNC_PASSWORD:-}

# Where the ISO is cached. Under /var/cache so a re-run of this script
# for a new VyOS release lands next to the last one, not in /tmp where
# a reboot would wipe it and force a re-download.
ISO_DIR=${ISO_DIR:-/var/cache/labtris/iso}
WORK_DIR=${WORK_DIR:-/var/tmp/labtris-vyos-install}

LABTRIS_USER=${LABTRIS_USER:-labtris}
LABTRIS_IMAGE=${LABTRIS_IMAGE:-/opt/labtris/.venv/bin/labtris-image}
# The CLI talks to the same Postgres the services do, and it reads the
# connection string from LABTRIS_DATABASE_URL — which lives in this
# EnvironmentFile, not the ambient shell. Without it the `add` at the
# end fails with "password authentication failed". Sourced into the
# labtris user's shell (which can read the root:labtris file) at
# register time, so the credential never reaches a command line.
LABTRIS_ENV=${LABTRIS_ENV:-/etc/labtris/labtris.env}

if [[ $EUID -ne 0 ]]; then
    echo "vyos.sh: run as root (uses /var/cache and sudo -u labtris)." >&2
    exit 2
fi

if [[ ! -x "$LABTRIS_IMAGE" ]]; then
    echo "vyos.sh: $LABTRIS_IMAGE not found. Is Labtris installed on this host?" >&2
    exit 2
fi

if ! command -v qemu-system-x86_64 >/dev/null; then
    echo "vyos.sh: qemu-system-x86_64 is not installed. apt install qemu-system-x86 qemu-utils" >&2
    exit 2
fi

mkdir -p "$ISO_DIR" "$WORK_DIR"

iso_name=$(basename "$VYOS_ISO_URL")
iso_path="$ISO_DIR/$iso_name"
if [[ ! -f "$iso_path" ]]; then
    echo "vyos.sh: downloading $iso_name (~600 MiB)…"
    # -L follows the CDN redirect; -f fails on HTTP error so we don't
    # end up with a 404 body masquerading as an ISO.
    curl -fL --progress-bar -o "$iso_path.partial" "$VYOS_ISO_URL"
    mv "$iso_path.partial" "$iso_path"
else
    echo "vyos.sh: reusing cached $iso_path"
fi

disk_path="$WORK_DIR/vyos.qcow2"
if [[ -f "$disk_path" ]]; then
    echo "vyos.sh: removing stale $disk_path from a previous aborted run"
    rm -f "$disk_path"
fi
qemu-img create -f qcow2 "$disk_path" "${VYOS_DISK_GB}G" >/dev/null

# KVM if the host has it, TCG if not. VyOS's installer is fast enough
# under TCG that this is not a blocker if /dev/kvm is missing.
accel=tcg
if [[ -r /dev/kvm && -w /dev/kvm ]]; then
    accel=kvm
fi

# The console-specific half of the qemu command line. Serial puts the
# guest on this terminal's stdio (-nographic); VNC puts it on a display,
# optionally password-protected. A VNC password is handed to qemu
# through a file-backed `secret` object rather than on the command line,
# so it never appears in the process list.
console_args=()
if [[ "$VYOS_CONSOLE" == "serial" ]]; then
    console_args=(-nographic)
else
    vnc_opts="$VYOS_VNC_HOST:$VYOS_VNC_DISPLAY"
    if [[ -n "$VYOS_VNC_PASSWORD" ]]; then
        pw_file="$WORK_DIR/vnc-secret"
        printf '%s' "$VYOS_VNC_PASSWORD" > "$pw_file"
        chmod 600 "$pw_file"
        console_args=(-object "secret,id=vncsec,file=$pw_file")
        vnc_opts="$vnc_opts,password-secret=vncsec"
    fi
    console_args+=(-vnc "$vnc_opts" -display none)
fi

echo
echo "vyos.sh: booting the installer."
echo
if [[ "$VYOS_CONSOLE" == "serial" ]]; then
    echo "  1. the guest's console is right here on this terminal"
    echo "     (run this script inside tmux/screen so you can detach)"
else
    if [[ "$VYOS_VNC_HOST" == "127.0.0.1" || "$VYOS_VNC_HOST" == "localhost" ]]; then
        echo "  1. from your workstation:  ssh -L $((5900 + VYOS_VNC_DISPLAY)):localhost:$((5900 + VYOS_VNC_DISPLAY)) $(whoami)@$(hostname -I | awk '{print $1}')"
        echo "     then attach a VNC viewer to  localhost:$((5900 + VYOS_VNC_DISPLAY))"
    else
        echo "  1. attach a VNC viewer to $(hostname -I | awk '{print $1}'):$((5900 + VYOS_VNC_DISPLAY))"
    fi
    [[ -n "$VYOS_VNC_PASSWORD" ]] && echo "     VNC password: $VYOS_VNC_PASSWORD"
fi
echo "  2. wait for the vyos login prompt (username: vyos, password: vyos)"
echo "  3. run:  install image"
echo "     - accept defaults, choose the whole disk, set an admin password"
echo "  4. when it says 'Installation successful', run:  poweroff"
if [[ "$VYOS_CONSOLE" == "serial" ]]; then
    echo
    echo "     (serial console escape is Ctrl-a x to force-quit qemu; you"
    echo "      should not need it — 'poweroff' exits cleanly.)"
fi
echo
echo "vyos.sh: qemu will exit on its own once the guest halts."
echo

# -boot d = boot the CD-ROM first, this once. On the reboot after
# install completes there is no more `-boot d` because we would not
# get that far — the guest is expected to `poweroff`.
qemu-system-x86_64 \
    -m "$VYOS_RAM_MB" -smp "$VYOS_CPUS" -accel "$accel" -cpu qemu64 \
    -drive "file=$disk_path,if=virtio,format=qcow2" \
    -cdrom "$iso_path" -boot d \
    -netdev user,id=n1 -device virtio-net-pci,netdev=n1 \
    "${console_args[@]}" \
    -no-reboot

# -no-reboot turns `poweroff` inside the guest into a real qemu exit,
# so this line only reaches after the operator has completed the
# install. If they Ctrl-C QEMU without installing, we exit non-zero
# above and the disk is left in $WORK_DIR for a re-attempt.

if [[ ! -s "$disk_path" ]]; then
    echo "vyos.sh: no disk to register — did the install complete?" >&2
    exit 1
fi

echo
echo "vyos.sh: registering '$VYOS_NAME' with Labtris."
echo

# VyOS is Debian-derived, uses systemd, so predictable interface names
# are ens3/ens4/... under the pc machine type — hence `--iface-scheme
# ens`, the Labtris default that matches most modern Linuxes. If you
# ever hand VyOS a q35 chipset the scheme is still ens; if you go back
# to an older CLI-only guest without systemd, switch to `--iface-scheme
# eth`.
if [[ ! -r "$LABTRIS_ENV" ]]; then
    echo "vyos.sh: warning: $LABTRIS_ENV not readable; the register step needs" >&2
    echo "  LABTRIS_DATABASE_URL from it and will likely fail to reach the DB." >&2
fi

# The helper shell runs as the labtris user, sources the EnvironmentFile
# so LABTRIS_DATABASE_URL is set, then execs the CLI. Positional args:
# $1 = env file, $2.. = the command to run after sourcing.
sudo -u "$LABTRIS_USER" bash -c '
    set -a
    [ -r "$1" ] && . "$1"
    set +a
    shift
    exec "$@"
' _ "$LABTRIS_ENV" \
    "$LABTRIS_IMAGE" add "$disk_path" \
    --name "$VYOS_NAME" \
    --ram-mb "$VYOS_RAM_MB" \
    --cpus "$VYOS_CPUS" \
    --nic-model virtio-net-pci \
    --disk-bus virtio \
    --iface-scheme ens \
    --description "VyOS router, installed from $iso_name via packaging/recipes/vyos.sh"

# The disk has been copied into the image cache under its content
# hash by `labtris-image add`; the workdir copy is redundant.
rm -f "$disk_path"
rmdir "$WORK_DIR" 2>/dev/null || true

echo
echo "vyos.sh: done. '$VYOS_NAME' is now in the palette under Your templates."
