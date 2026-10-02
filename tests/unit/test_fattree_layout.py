"""A fat tree is k pods, not four rows.

Laid out as four centred rows, k=22 puts 2662 hosts across one of them:
532,400 px wide against 800 px tall, a 665:1 aspect ratio. Fit that to a
screen and every node is sub-pixel, nothing can be selected, and the 7986
links cross everything. The complaint that produced this was literally "to
goto edge-11-11 it is not even visible due to wirings".
"""

from __future__ import annotations

import math

GRID = 200


def _extent(geom: dict[str, dict[str, int]]) -> tuple[int, int]:
    xs = [p["x"] for p in geom.values()]
    ys = [p["y"] for p in geom.values()]
    return max(xs) - min(xs) + GRID, max(ys) - min(ys) + GRID


async def _generate(k: int) -> dict[str, dict[str, int]]:
    """Run the generator's fat-tree against a builder that records geometry
    and nothing else — no database, no containers."""
    from labtris_api import topology_gen as tg

    class FakeNode:
        def __init__(self, name: str) -> None:
            self.id = name
            self.name = name

    class FakeBuilder:
        def __init__(self) -> None:
            self.geometry_nodes: dict[str, dict[str, int]] = {}
            self._ifaces: dict[str, int] = {}

        async def add_node(self, name, kind, row, col, row_wide, opts=None,
                           startup_config=None, at=None):  # noqa: ANN001, ANN201
            n = FakeNode(name)
            self.geometry_nodes[name] = (
                {"x": at[0] * GRID, "y": at[1] * GRID}
                if at is not None
                else tg._grid_pos(row, col, row_wide)  # noqa: SLF001
            )
            return n

        async def add_interface(self, node):  # noqa: ANN001, ANN201
            self._ifaces[node.name] = self._ifaces.get(node.name, 0) + 1
            return object()

        async def wire(self, a, b):  # noqa: ANN001, ANN201
            return None

        async def flush(self):  # noqa: ANN201
            return None

    body = tg.GenerateIn(
        pattern="fat-tree", k=k,
        spine_kind="frr", leaf_kind="frr", host_kind="frr",
    )
    b = FakeBuilder()
    await tg._fat_tree(b, body)  # noqa: SLF001
    return b.geometry_nodes


async def test_a_3267_node_fat_tree_is_not_half_a_million_pixels_wide() -> None:
    geom = await _generate(22)

    assert len(geom) == 5 * 22 * 22 // 4 + 22**3 // 4 == 3267
    w, h = _extent(geom)

    # The old layout was 532,400 x 800. Anything near that is the bug back.
    assert w < 20_000, f"{w} px wide"
    aspect = w / h
    assert 0.25 < aspect < 4, f"aspect {aspect:.1f}:1 is not navigable"


async def test_a_host_sits_directly_under_its_own_edge_switch() -> None:
    """Which is what turns 2662 edge-to-host links — a third of every link in
    the lab — into short vertical segments that cross nothing."""
    geom = await _generate(8)

    for pod in (1, 4, 8):
        for e in (1, 4):
            edge = geom[f"edge-{pod}-{e}"]
            for h in range(1, 5):
                host = geom[f"h-{pod}-{e}-{h}"]
                assert host["x"] == edge["x"], f"h-{pod}-{e}-{h} is not under its edge"
                assert host["y"] > edge["y"], "hosts belong below the switch"


async def test_each_pod_is_its_own_block() -> None:
    """Two pods must not overlap, or the tiling is cosmetic only."""
    geom = await _generate(8)
    half = 4

    def pod_box(pod: int) -> tuple[int, int, int, int]:
        pts = [p for name, p in geom.items() if _pod_of(name) == pod]
        xs = [p["x"] for p in pts]
        ys = [p["y"] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def _pod_of(name: str) -> int | None:
        if name.startswith(("agg-", "edge-")):
            return int(name.split("-")[1])
        if name.startswith("h-"):
            return int(name.split("-")[1])
        return None

    boxes = {p: pod_box(p) for p in range(1, 9)}
    for a in range(1, 9):
        for bb in range(a + 1, 9):
            ax0, ay0, ax1, ay1 = boxes[a]
            bx0, by0, bx1, by1 = boxes[bb]
            disjoint = ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0
            assert disjoint, f"pod {a} and pod {bb} overlap"
    assert half == 4


async def test_the_cores_wrap_instead_of_stretching() -> None:
    """121 cores in one line is 24,200 px of header above everything else."""
    geom = await _generate(22)
    cores = {n: p for n, p in geom.items() if n.startswith("core-")}

    assert len(cores) == 121
    rows = {p["y"] for p in cores.values()}
    assert len(rows) > 1, "cores still on a single line"
    width = max(p["x"] for p in cores.values()) - min(p["x"] for p in cores.values())
    assert width < 20_000, f"core band is {width} px wide"


def test_the_pod_grid_is_roughly_square() -> None:
    for k in (4, 8, 16, 22, 24):
        across = max(1, math.ceil(math.sqrt(k)))
        assert across * across >= k
