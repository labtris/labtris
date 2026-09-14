"""Images of the same OS at different releases are one choice, not several.

And the catalog has to be honest about what a given image needs: a cloud image
has no password until cloud-init sets one, which is a different kind of entry
from a desktop install that ships with credentials baked in.
"""

from __future__ import annotations

from labtris_api.runtime.qemu import CLOUD_USER, QEMU_CATALOG


def _family(name: str):
    return [i for i in QEMU_CATALOG.values() if i.family == name]


def test_ubuntu_server_offers_2404_and_2604() -> None:
    versions = {i.version for i in _family("ubuntu-server")}

    assert "26.04 LTS" in versions
    assert "24.04 LTS" in versions


def test_every_family_names_exactly_one_default() -> None:
    """Otherwise the palette has to guess which release to show first."""
    families = {i.family for i in QEMU_CATALOG.values() if i.family}

    for family in families:
        defaults = [i for i in _family(family) if i.default_version]
        assert len(defaults) == 1, f"{family} has {len(defaults)} defaults"


def test_the_default_is_an_lts_not_merely_the_highest_number() -> None:
    for family in ("ubuntu-server", "ubuntu-desktop"):
        default = next(i for i in _family(family) if i.default_version)
        assert "LTS" in default.version


def test_cloud_images_advertise_the_credentials_the_seed_will_set() -> None:
    """Canonical's images have no password at all. If the catalog showed the
    image's own credentials it would be showing nothing, so it has to show
    what our generated seed puts there — and they must agree."""
    for img in QEMU_CATALOG.values():
        if img.cloud_init:
            assert img.credentials, f"{img.id} claims no credentials"
            assert CLOUD_USER in img.credentials


def test_ids_stay_stable_across_the_family_change() -> None:
    """Node rows store the image id, so renaming one orphans existing labs."""
    for expected in ("ubuntu-24.04", "ubuntu-22.04", "cirros", "kali-2025.2"):
        assert expected in QEMU_CATALOG
