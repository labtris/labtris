"""Privilege is a property of an image, and it has to stay opt-in.

The catalog exists so that "this image needs host privilege" is a statement
someone made deliberately, in code, rather than a default every container
inherits. These tests guard that asymmetry: the escape hatch stays narrow, and
nothing acquires it by accident.
"""

from __future__ import annotations

from labtris_api.runtime.containers import (
    BASE_CAPS,
    CONTAINER_CATALOG,
    ContainerImage,
    needs_privilege,
    profile_for,
)


def test_an_unknown_image_gets_no_profile_and_no_privilege() -> None:
    """Bring-your-own images are the common case and must stay unprivileged."""
    assert profile_for("ghcr.io/someone/unreviewed:latest") is None
    assert needs_privilege("ghcr.io/someone/unreviewed:latest") is False


def test_privilege_is_rare_and_always_explains_itself() -> None:
    """A reader should never have to guess why an entry got host privilege."""
    for img in CONTAINER_CATALOG.values():
        if img.privileged:
            assert img.notes, f"{img.id} is privileged but says nothing about why"


def test_the_ordinary_images_are_unprivileged() -> None:
    plain = [i for i in CONTAINER_CATALOG.values() if not i.privileged]

    assert len(plain) >= 8
    assert all(i.cap_add == () for i in plain)


def test_srlinux_is_privileged_because_it_cannot_boot_otherwise() -> None:
    """Measured on 26.7.2: it writes sysctls during boot, and docker keeps
    /proc/sys read-only for every unprivileged container. Targeted caps and
    unconfined seccomp were both tried and both still exited."""
    srl = CONTAINER_CATALOG["srlinux"]

    assert srl.privileged is True
    assert srl.user == "0"
    assert srl.cmd is not None


def test_lookup_is_by_image_reference_not_catalog_id() -> None:
    """Nodes store the reference, so that is what the runtime has to match."""
    assert profile_for("ghcr.io/nokia/srlinux:latest") is CONTAINER_CATALOG["srlinux"]
    assert profile_for("srlinux") is None


def test_base_caps_are_the_two_that_make_a_container_a_network_device() -> None:
    assert set(BASE_CAPS) == {"NET_ADMIN", "NET_RAW"}


def test_a_new_entry_defaults_to_unprivileged() -> None:
    """The safe answer has to be the one you get by not thinking about it."""
    fresh = ContainerImage(id="x", label="X", image="x:1")

    assert fresh.privileged is False
    assert fresh.cap_add == ()
    assert fresh.user is None
