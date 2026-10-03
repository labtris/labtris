"""What survives when a lab's node ids are minted afresh — clone, import, pod load."""

from __future__ import annotations

from labtris_api.routers.labs import _remap_geometry


def test_node_positions_follow_the_new_ids() -> None:
    out = _remap_geometry({"nodes": {"OLD1": {"x": 10, "y": 20}}}, {"OLD1": "NEW1"})
    assert out["nodes"] == {"NEW1": {"x": 10, "y": 20}}


def test_annotations_are_carried_over_untouched() -> None:
    """A box drawn round a pod's spines is part of what the pod is. These used
    to be dropped here, so a clone or a pod load arrived with every box and
    label gone and no error to say so."""
    annos = {
        "a1": {
            "kind": "box", "x": 0, "y": 0, "w": 300, "h": 200,
            "color": "var(--accent)", "text": "spines",
        },
        "a2": {
            "kind": "text", "x": 50, "y": 400, "color": "var(--warn)", "text": "DCI", "size": 14,
        },
    }
    out = _remap_geometry({"nodes": {}, "annotations": annos}, {})
    assert out["annotations"] == annos


def test_absent_annotations_become_an_empty_dict_not_a_crash() -> None:
    out = _remap_geometry({"nodes": {}}, {})
    assert out["annotations"] == {}
    #: Links are rebuilt from topology on the way in, so their geometry is
    #: deliberately not carried.
    assert out["links"] == {}
