"""Generate the small test models in this folder.

Each model has a known right answer, so the tests can check exact numbers.
The generated files are committed, so you only need this script (and
`pip install manifold3d` for the boolean cuts) if you want to change them.

    python tests/fixtures/make_fixtures.py
"""
from pathlib import Path

import numpy as np
import trimesh

HERE = Path(__file__).parent


def box(size, corner=(0, 0, 0)):
    m = trimesh.creation.box(size)
    m.apply_translation(np.array(size) / 2 + np.array(corner))
    return m


def cylinder(r, h, x=0.0, y=0.0, sections=64):
    m = trimesh.creation.cylinder(radius=r, height=h, sections=sections)
    m.apply_translation((x, y, h / 2))
    return m


def save(mesh, name):
    mesh.export(HERE / name)
    print(f"{name:22s} {len(mesh.faces):6d} faces  extents {np.round(mesh.extents, 2)}")


def main():
    # 20 x 30 x 5 mm block: the simplest possible answer.
    save(box((20, 30, 5)), "box.stl")

    # 60 x 40 x 6 plate: an 8 mm through-hole, a 20 x 15 pocket 2 mm deep,
    # and an 8 x 6 spot inside it 4 mm deep.
    plate = box((60, 40, 6))
    hole = cylinder(4, 20, 10, 20)
    hole.apply_translation((0, 0, -7))
    pocket = box((20, 15, 10), (30, 12.5, 4))
    deep = box((8, 6, 10), (36, 17, 2))
    save(plate.difference(hole).difference(pocket).difference(deep), "plate.stl")

    # Overlapping solids, like the mountain: a 50 x 30 x 3 slab with four
    # separate 10 mm tall walls standing on (and overlapping) it.
    parts = [box((50, 30, 3)),
             box((3, 30, 10)), box((3, 30, 10), (47, 0, 0)),
             box((50, 3, 10)), box((50, 3, 10), (0, 27, 0))]
    save(trimesh.util.concatenate(parts), "overlap.stl")

    # Sphere, radius 20, sitting on z = 0: slices have a known radius.
    s = trimesh.creation.icosphere(subdivisions=4, radius=20)
    s.apply_translation((0, 0, 20))
    save(s, "sphere.stl")

    # Torus: every slice is a ring (a hole inside one solid).
    t = trimesh.creation.torus(major_radius=18, minor_radius=6)
    t.apply_translation((0, 0, 6))
    save(t, "torus.stl")

    # Two separate posts: each layer has two pieces.
    save(trimesh.util.concatenate([cylinder(8, 12), cylinder(8, 12, 40)]), "two_posts.stl")

    # A tower too thin to fit a layer number.
    save(cylinder(1.5, 9, sections=32), "thin_tower.stl")

    # Same block as box.stl, written as ASCII STL.
    (HERE / "box_ascii.stl").write_bytes(trimesh.exchange.stl.export_stl_ascii(box((20, 30, 5))).encode())
    print("box_ascii.stl          ASCII copy of box.stl")

    # Same block as 3MF (the Python version only; needs networkx + lxml).
    box((20, 30, 5)).export(HERE / "box.3mf")
    print("box.3mf                3MF copy of box.stl")

    # Same block as an OBJ with four-sided faces, to test splitting quads.
    v = [(x, y, z) for z in (0, 5) for y in (0, 30) for x in (0, 20)]
    quads = [(1, 3, 4, 2), (5, 6, 8, 7), (1, 2, 6, 5), (3, 7, 8, 4), (1, 5, 7, 3), (2, 4, 8, 6)]
    lines = ["# 20 x 30 x 5 block with quad faces"] + [f"v {x} {y} {z}" for x, y, z in v]
    lines += ["f " + " ".join(f"{i}/{i}/{i}" for i in q) for q in quads]
    (HERE / "box_quads.obj").write_text("\n".join(lines) + "\n")
    print("box_quads.obj          OBJ copy of box.stl with quad faces")

    # 20 x 20 x 5 block whose top is split around a point 0.00005 mm from a
    # corner, making two extremely thin (but real) triangles. Regression test:
    # the web version once merged nearby corners and threw such triangles away.
    b = box((20, 20, 5))
    keep = [f for f in b.faces if not np.all(b.vertices[f][:, 2] > 4.99)]
    verts = [tuple(p) for p in b.vertices]
    idx = {(round(x, 6), round(y, 6), round(z, 6)): i for i, (x, y, z) in enumerate(verts)}
    A, B, C, D = (idx[(0, 0, 5)], idx[(20, 0, 5)], idx[(20, 20, 5)], idx[(0, 20, 5)])
    verts.append((0.00005, 0.00005, 5.0))
    E = len(verts) - 1
    faces = [list(f) for f in keep] + [[A, B, E], [B, C, E], [C, D, E], [D, A, E]]
    sliver = trimesh.Trimesh(np.array(verts), np.array(faces), process=False)
    trimesh.repair.fix_normals(sliver)
    save(sliver, "sliver.stl")

    # Not a model at all, and a valid STL header with zero triangles.
    (HERE / "garbage.stl").write_text("this is not a 3D model\n")
    (HERE / "empty.stl").write_bytes(b"\0" * 80 + (0).to_bytes(4, "little"))
    print("garbage.stl, empty.stl  invalid inputs")


if __name__ == "__main__":
    main()
