"""Which disk controller a guest is handed, and why it is not always virtio.

This is the second of the two settings that stop a VM booting, and the less
forgiving one. A guest given a virtio disk when its kernel has no virtio-blk
driver does not boot and does not say so: qemu starts, the console shows a
bootloader or nothing at all, and it looks like the host is broken. 67 of the
182 QEMU appliances in the GNS3 registry need ide rather than virtio.

The bus names also do not agree across sources. `-drive if=` accepts virtio,
ide, scsi, sd and none; it rejects sata, nvme and usb outright — measured
against qemu-system-x86_64, not assumed. The GNS3 registry uses sata freely, so
passing a registry name straight through is how an imported appliance becomes a
node that dies at launch.
"""

from __future__ import annotations

from labtris_api.runtime.qemu import (
    DRIVE_IF,
    QEMU_CATALOG,
    QEMU_OPTIONS,
    drive_if,
    option_defaults,
    resolved_options,
)

#: What `qemu-system-x86_64 -drive if=X` actually accepted when asked.
ACCEPTED_BY_QEMU = {"virtio", "ide", "scsi", "sd", "none"}
REJECTED_BY_QEMU = {"sata", "nvme", "usb"}


def test_every_mapping_lands_on_a_bus_qemu_accepts() -> None:
    """The whole point of the table: nothing it produces can be refused."""
    for name, mapped in DRIVE_IF.items():
        assert mapped in ACCEPTED_BY_QEMU, f"{name} maps to {mapped}, which qemu rejects"


def test_the_buses_qemu_rejects_are_all_translated() -> None:
    """Not merely absent — actually mapped, because the registry uses them."""
    for name in REJECTED_BY_QEMU:
        assert name in DRIVE_IF
        assert DRIVE_IF[name] in ACCEPTED_BY_QEMU


def test_sata_becomes_ide_rather_than_being_passed_through() -> None:
    """The common case: sata is all over the GNS3 registry and qemu has no
    `if=sata`. ide is the nearest bus that boots, both being ATA."""
    assert drive_if("sata") == "ide"


def test_an_unknown_or_empty_bus_falls_back_to_virtio() -> None:
    for value in ("", None, "  ", "wat", "floppy-ish"):
        assert drive_if(value) == "virtio"


def test_case_and_spacing_do_not_change_the_answer() -> None:
    """Registry JSON is hand-written by many people."""
    assert drive_if(" IDE ") == "ide"
    assert drive_if("VirtIO") == "virtio"


def test_the_node_option_defaults_to_deferring_to_the_image() -> None:
    """Blank, not "virtio". A default of virtio here would silently override
    what the catalog knows about an image that cannot boot from it."""
    assert QEMU_OPTIONS["disk_bus"]["default"] == ""
    assert option_defaults()["disk_bus"] == ""
    assert resolved_options({})["disk_bus"] == ""


def test_the_option_only_offers_buses_that_work() -> None:
    for choice in QEMU_OPTIONS["disk_bus"]["choices"]:
        if choice == "":
            continue
        assert choice in ACCEPTED_BY_QEMU


def test_a_node_override_is_kept() -> None:
    assert resolved_options({"disk_bus": "ide"})["disk_bus"] == "ide"


def test_every_catalog_image_declares_a_bus_qemu_can_use() -> None:
    """The built-in images are all modern Linux with virtio-blk, so they take
    the default — but an entry added later must not be able to declare a bus
    that cannot be handed to qemu."""
    for image in QEMU_CATALOG.values():
        assert drive_if(image.disk_bus) in ACCEPTED_BY_QEMU


# --------------------------------------------------------------- command line
# The unit tests above check the table. These check the thing that was actually
# broken: the bus was resolved correctly and then never reached qemu, because
# the -drive argument had `if=virtio` written into it literally.


def _drive_args(opts: dict[str, object], cfg_bus: str | None) -> str:
    """Rebuild the disk argument the way start() assembles it."""
    resolved = resolved_options(opts)
    bus = drive_if(str(resolved.get("disk_bus") or "") or str(cfg_bus or "virtio"))
    return f"file=/vm/disk.qcow2,if={bus},format=qcow2"


def test_the_image_bus_reaches_the_command_line() -> None:
    """An image that says ide must produce if=ide, not if=virtio. This is the
    bug: the bus was recorded, and then a literal was emitted instead."""
    assert "if=ide" in _drive_args({}, "ide")


def test_the_node_override_beats_the_image() -> None:
    assert "if=ide" in _drive_args({"disk_bus": "ide"}, "virtio")


def test_leaving_the_node_option_blank_keeps_the_image_choice() -> None:
    """The reason the option defaults to "" — an untouched node must not
    override an image that knows it needs ide."""
    assert "if=ide" in _drive_args({"disk_bus": ""}, "ide")


def test_a_registry_sata_appliance_gets_a_startable_command_line() -> None:
    """End to end for the imported-appliance case: sata in, ide out, and
    nothing qemu would refuse."""
    args = _drive_args({}, "sata")
    assert "if=ide" in args
    assert "if=sata" not in args


def test_an_old_vm_with_no_recorded_bus_still_starts() -> None:
    """config.json written before this existed has no disk_bus key. It has to
    keep booting exactly as it did, which means virtio."""
    assert "if=virtio" in _drive_args({}, None)
