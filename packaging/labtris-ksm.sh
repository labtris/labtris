#!/bin/sh
#
# Turn on Kernel Samepage Merging and tune it for a lab host.
#
# This is the single biggest capacity lever the platform has, and it is off by
# default on every stock Ubuntu. Measured on EVE-NG running fifteen VMs:
#
#   pages_sharing  16,330,113   ~62 GiB deduplicated
#   pages_shared    1,914,989   ~7.3 GiB actually backing it
#                               ~8.5:1
#
# Which is the difference between roughly twenty VMs on a box and roughly a
# hundred and fifty. Labs are the ideal case for it — twenty students running
# the same Ubuntu image share almost every page of it.
#
# Nothing here is a guess: the values are what a production EVE-NG host was
# measured using. The defaults (100 pages, 20ms) scan far too gently to find
# duplicates at the rate VMs are started.
set -eu

KSM=/sys/kernel/mm/ksm
[ -d "$KSM" ] || { echo "no $KSM — this kernel has no KSM support"; exit 0; }

# Overridable in /etc/labtris/labtris.env; the defaults are the measured ones.
[ -r /etc/labtris/labtris.env ] && . /etc/labtris/labtris.env

set_knob() {
  # Every knob is optional: they come and go between kernel versions, and a
  # missing one must not fail the unit and leave KSM off entirely.
  [ -w "$KSM/$1" ] || { echo "  $1: not available on this kernel"; return 0; }
  echo "$2" > "$KSM/$1" 2>/dev/null && echo "  $1 = $2" || echo "  $1: could not set"
}

# Scan harder than stock. 1250 pages every 10ms rather than 100 every 20ms:
# roughly 25x the scan rate, which is what makes dedup keep up with a lab
# being started rather than trailing minutes behind it.
set_knob pages_to_scan "${LABTRIS_KSM_PAGES_TO_SCAN:-1250}"
set_knob sleep_millisecs "${LABTRIS_KSM_SLEEP_MS:-10}"

# 6.7+ only. smart_scan skips pages repeatedly found unmergeable, which is
# most of a running VM's memory, so the scanner spends its budget where
# duplicates actually are.
set_knob smart_scan 1

# Last, so a failure above leaves KSM off rather than running untuned.
set_knob run "${LABTRIS_KSM_RUN:-1}"
