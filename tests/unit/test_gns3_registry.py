"""Reading GNS3 appliance definitions.

Two things make this worth testing rather than eyeballing. The registry spans
schema versions 3 to 8, and the newest entries — SR Linux, XRd, Cisco IOL — use
a shape the old blocks do not cover, so a parser that only knows `qemu` and
`docker` silently drops the devices people actually came for.

And the disk bus matters more than it looks: 67 of the 182 QEMU appliances need
IDE rather than virtio. A guest handed the wrong bus does not boot, and the
failure presents as a hung host rather than a wrong setting.
"""

from __future__ import annotations

from labtris_api.gns3_registry import parse, summarise

SCHEMA_4_QEMU = {
    "appliance_id": "a1",
    "name": "Arista vEOS",
    "category": "multilayer_switch",
    "vendor_name": "Arista",
    "registry_version": 4,
    "qemu": {
        "ram": 2048,
        "adapters": 13,
        "adapter_type": "e1000",
        "console_type": "telnet",
        "hda_disk_interface": "ide",
    },
    "images": [
        {
            "filename": "vEOS64-lab-4.35.3F.qcow2",
            "md5sum": "00d11e33dc4288f509441a4ab6319ca0",
            "download_url": "https://www.arista.com/en/support/software-download",
        }
    ],
}

SCHEMA_8_DOCKER = {
    "appliance_id": "b2",
    "name": "SRLinux",
    "category": "router",
    "vendor_name": "Nokia",
    "registry_version": 8,
    "settings": [
        {
            "name": "Default template settings",
            "default": True,
            "template_type": "docker",
            "template_properties": {
                "adapters": 35,
                "image": "ghcr.io/nokia/srlinux:latest",
                "console_type": "docker_exec",
            },
        }
    ],
}


def test_the_old_schema_still_reads() -> None:
    a = parse(SCHEMA_4_QEMU)

    assert a.runtime == "qemu"
    assert a.ram_mb == 2048
    assert a.nic_model == "e1000"
    # The one that breaks a boot rather than a feature.
    assert a.disk_bus == "ide"
    assert a.obtainable == "account", "a vendor download page is not a direct link"
    assert a.supported


def test_schema_eight_is_not_silently_dropped() -> None:
    """SR Linux, XRd and Cisco IOL live here. A parser that only knows the
    `docker` block reports them as unrunnable, which is the opposite of true —
    they are containers, which is the runtime Labtris handles best."""
    a = parse(SCHEMA_8_DOCKER)

    assert a.runtime == "docker"
    assert a.docker_image == "ghcr.io/nokia/srlinux:latest"
    assert a.obtainable == "registry", "a container is pulled, not downloaded"
    assert a.supported


def test_an_unknown_disk_bus_falls_back_rather_than_propagating() -> None:
    """Passing a bus QEMU does not recognise fails at start time with a message
    about command-line syntax, which says nothing about the image."""
    doc = {**SCHEMA_4_QEMU, "qemu": {**SCHEMA_4_QEMU["qemu"], "hda_disk_interface": "nonsense"}}

    assert parse(doc).disk_bus == "virtio"


def test_a_direct_link_is_distinguished_from_a_vendor_portal() -> None:
    """"Downloadable" and "you will need an account" are different promises, and
    conflating them is how a catalogue becomes untrustworthy."""
    doc = {
        **SCHEMA_4_QEMU,
        "images": [{"filename": "x.qcow2", "direct_download_url": "https://example.invalid/x.qcow2"}],
    }

    assert parse(doc).obtainable == "direct"


def test_the_summary_counts_what_a_person_would_act_on() -> None:
    """"228 appliances" is not the useful number; how many can be booted today
    is."""
    items = [parse(SCHEMA_4_QEMU), parse(SCHEMA_8_DOCKER)]
    s = summarise(items)

    assert s["total"] == 2
    assert s["supported"] == 2
    # vEOS needs an Arista account; SR Linux is a public container.
    assert s["ready_to_use"] == 1
