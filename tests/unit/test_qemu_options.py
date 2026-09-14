"""How the machine is built, and the one volume that outlives it.

The data volume's guarantee is structural, not procedural: it lives outside
the directory Wipe deletes, so nobody has to remember to spare it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from labtris_api.runtime.qemu import (
    QEMU_OPTIONS,
    _data_volume_path,
    _vm_dir,
    ensure_data_volume,
    option_defaults,
    remove_data_volume,
    resolved_options,
)


def test_an_untouched_node_behaves_as_it_did_before_options_existed() -> None:
    """Defaults are what QEMU nodes already did, so adding this changed nothing
    for every lab that already exists."""
    d = option_defaults()

    assert d["machine"] == "pc"
    assert d["usb_tablet"] is True
    assert d["mgmt_nic"] is False
    assert d["data_volume"] is False
    assert d["extra_args"] == []


def test_unknown_options_are_ignored_rather_than_passed_through() -> None:
    """Options reach a command line. Anything not in the schema is not a knob
    this build has, and quietly dropping it beats forwarding it to QEMU."""
    merged = resolved_options({"machine": "q35", "not_a_real_option": "rm -rf /"})

    assert merged["machine"] == "q35"
    assert "not_a_real_option" not in merged


def test_every_option_carries_help_the_ui_can_render() -> None:
    """The form is generated from this schema — an option with no explanation
    is a checkbox nobody can reason about."""
    for name, opt in QEMU_OPTIONS.items():
        assert opt.get("label"), f"{name} has no label"
        assert opt.get("help"), f"{name} has no help"
        assert opt.get("type") in {"bool", "int", "choice", "list"}


def test_the_data_volume_lives_outside_what_wipe_deletes() -> None:
    """This is the entire guarantee. Wipe removes the VM directory wholesale;
    if the volume were inside it, no amount of care elsewhere would save it."""
    node_id = "01TESTVOLUME00000000000000"
    vm_dir = _vm_dir(node_id).resolve()
    volume = _data_volume_path(node_id).resolve()

    assert vm_dir not in volume.parents
    assert not str(volume).startswith(str(vm_dir))


@pytest.mark.skipif(
    shutil.which("mkfs.ext4") is None, reason="mkfs.ext4 not available"
)
def test_the_volume_is_created_once_formatted_and_labelled() -> None:
    """Labelled because the guest should not have to care whether it landed on
    /dev/vdb or /dev/vdc, and created once because a second start must not
    reformat the thing whose point is to persist."""
    node_id = "01TESTVOLUME00000000000001"
    remove_data_volume(node_id)
    try:
        path = ensure_data_volume(node_id, 16)
        assert path is not None and path.exists()

        out = subprocess.run(
            ["blkid", "-o", "value", "-s", "LABEL", str(path)],
            capture_output=True, check=False,
        )
        if out.returncode == 0:
            assert out.stdout.decode().strip() == "LABTRIS"

        marker = path.stat().st_mtime
        again = ensure_data_volume(node_id, 16)
        assert again == path
        assert path.stat().st_mtime == marker, "a second call reformatted the volume"
    finally:
        remove_data_volume(node_id)


def test_removing_the_volume_takes_the_directory_with_it() -> None:
    node_id = "01TESTVOLUME00000000000002"
    path = _data_volume_path(node_id)
    path.write_bytes(b"")
    assert path.exists()

    remove_data_volume(node_id)

    assert not path.exists()
    assert not path.parent.exists()


def test_vm_dir_is_cleaned_up_after_the_path_test() -> None:
    """_vm_dir creates directories as a side effect; do not leave them behind."""
    for node_id in ("01TESTVOLUME00000000000000",):
        d = Path(_vm_dir(node_id))
        shutil.rmtree(d, ignore_errors=True)
        assert not d.exists()
