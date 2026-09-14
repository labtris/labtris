"""Argparse wiring for `labtris-image`.

Just proves the subcommand tree and defaults land where the code expects.
The heavy lifting (hashing, DB inserts) is covered by test_image_upload.py
and the templates unit + acceptance suites."""

from __future__ import annotations

import pytest

from labtris_api.cli import image as cli


def test_add_defaults_match_the_qemu_image_defaults() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(["add", "/some/path.qcow2", "--name", "pfSense 24.03"])
    assert args.path == "/some/path.qcow2"
    assert args.name == "pfSense 24.03"
    # These defaults have to line up with QemuImage's defaults so a bare
    # `add` matches "the average Linux/QEMU appliance" rather than something
    # surprising.
    assert args.ram_mb == 256
    assert args.cpus == 1
    assert args.nic_model == "virtio-net-pci"
    assert args.disk_bus == "virtio"
    assert args.iface_scheme == "ens"
    assert args.graphical is False
    assert args.func is cli._add


def test_add_accepts_full_sizing_flags() -> None:
    parser = cli._build_parser()
    args = parser.parse_args([
        "add", "/x.qcow2",
        "--name", "big-appliance",
        "--ram-mb", "8192",
        "--cpus", "4",
        "--nic-model", "e1000",
        "--disk-bus", "ide",
        "--iface-scheme", "eth",
        "--graphical",
        "--description", "vendor blob",
    ])
    assert args.ram_mb == 8192
    assert args.cpus == 4
    assert args.nic_model == "e1000"
    assert args.disk_bus == "ide"
    assert args.iface_scheme == "eth"
    assert args.graphical is True
    assert args.description == "vendor blob"


def test_list_takes_no_args() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(["list"])
    assert args.func is cli._list


def test_rm_needs_a_name() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(["rm", "pfSense 24.03"])
    assert args.name == "pfSense 24.03"
    assert args.func is cli._rm


def test_missing_subcommand_is_a_parse_error(capsys) -> None:
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
