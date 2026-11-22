#!/usr/bin/env python3
"""
stl_to_svg.py - Turn a 3D model (STL, OBJ, 3MF) into a 1:1 laser-ready SVG.

It works like putting the part on a scanner: the model is flattened along one
direction and the outline is traced at real size (millimetres).

What you get in the SVG:
  red   - outer cut line, plus any through-holes
  other - one colour per pocket / step depth (flat surfaces lower than the top),
          so you can set each colour to a different engrave depth or cut layer

Setup (once):
    pip install trimesh shapely numpy

Usage:
    python stl_to_svg.py part.stl                 # writes part.svg next to it
    python stl_to_svg.py a.stl b.stl -o out/      # several files, output folder
    python stl_to_svg.py part.stl --view bottom   # part was modelled face-down
    python stl_to_svg.py part.stl --no-pockets    # outline only

Views: auto (default: looks at the flattest side), top, bottom, front, back,
left, right.
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import shapely
import trimesh
from shapely.geometry import MultiPolygon, Polygon

# Each view is (right, up, toward-viewer) in the model's own X/Y/Z.
VIEWS = {
    "top":    ([1, 0, 0],  [0, 1, 0],  [0, 0, 1]),
    "bottom": ([1, 0, 0],  [0, -1, 0], [0, 0, -1]),
    "front":  ([1, 0, 0],  [0, 0, 1],  [0, -1, 0]),
    "back":   ([-1, 0, 0], [0, 0, 1],  [0, 1, 0]),
    "right":  ([0, 1, 0],  [0, 0, 1],  [1, 0, 0]),
    "left":   ([0, -1, 0], [0, 0, 1],  [-1, 0, 0]),
}
AUTO_VIEW = {0: "right", 1: "front", 2: "top"}  # thinnest axis -> view

CUT_COLOR = "#ff0000"
LEVEL_COLORS = ["#0000ff", "#00a000", "#ff00ff", "#ff8000", "#00c0c0",
                "#808000", "#8000ff", "#c00060"]


def as_polygons(geom, min_area):
    """Return the list of polygons in geom that are bigger than min_area."""
    if geom.is_empty:
        return []
    parts = geom.geoms if hasattr(geom, "geoms") else [geom]
    out = []
    for p in parts:
        if isinstance(p, Polygon) and p.area >= min_area:
            holes = [h for h in p.interiors if Polygon(h).area >= min_area]
            out.append(Polygon(p.exterior, holes))
    return out


def flatten(tris_xy, close):
    """Union a stack of 2D triangles into one clean shape."""
    polys = shapely.polygons(tris_xy)
    polys = polys[shapely.area(polys) > 1e-9]
    if len(polys) == 0:
        return Polygon()
    shape = shapely.union_all(polys)
    # Close the hairline seams that floating-point error leaves between triangles.
    return shape.buffer(close, join_style="mitre").buffer(-close, join_style="mitre")


def convert(path, out_path, view, pockets, min_area, simplify, margin):
    mesh = trimesh.load(path, force="mesh")
    if len(mesh.faces) == 0:
        raise ValueError("no triangles found in file")

    if view == "auto":
        view = AUTO_VIEW[int(np.argmin(mesh.extents))]
    basis = np.array(VIEWS[view], dtype=float)
    verts = mesh.vertices @ basis.T          # now x=right, y=up, z=toward viewer
    tris = verts[mesh.faces]                  # (n, 3, 3)
    normals_z = mesh.face_normals @ basis[2]

    outline = flatten(tris[:, :, :2], close=0.02).simplify(simplify)
    parts = as_polygons(outline, min_area)
    if not parts:
        raise ValueError("outline came out empty")

    levels = []
    if pockets:
        z = tris[:, :, 2]
        flat_up = (normals_z > 0.99) & (np.ptp(z, axis=1) < 0.02)
        heights = np.round(z[flat_up].mean(axis=1) / 0.05) * 0.05
        flat_tris = tris[flat_up][:, :, :2]
        top = verts[:, 2].max()
        tri_top = z.max(axis=1)
        for h in sorted(set(heights), reverse=True):
            depth = top - h
            if depth < 0.05:
                continue  # that's the top surface itself
            region = flatten(flat_tris[heights == h], close=0.02)
            # Only keep the part of this surface you can actually see from above:
            # models built from overlapping solids can hide a floor under a wall.
            above = flatten(tris[tri_top > h + 0.05][:, :, :2], close=0.02)
            region = region.difference(above).buffer(-0.05).buffer(0.05).simplify(simplify)
            polys = as_polygons(region, min_area)
            if polys:
                levels.append((depth, polys))

    # SVG coordinates: origin at top-left, y pointing down, units = mm.
    x0, y0, x1, y1 = MultiPolygon(parts).bounds
    w, h = x1 - x0 + 2 * margin, y1 - y0 + 2 * margin

    def ring_d(ring):
        pts = [(x - x0 + margin, y1 - y + margin) for x, y in ring.coords]
        return "M" + " L".join(f"{px:.3f},{py:.3f}" for px, py in pts) + " Z"

    def poly_d(p):
        return " ".join([ring_d(p.exterior)] + [ring_d(r) for r in p.interiors])

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.3f}mm" height="{h:.3f}mm" '
        f'viewBox="0 0 {w:.3f} {h:.3f}">',
        f'<g id="cut" fill="none" stroke="{CUT_COLOR}" stroke-width="0.1">',
        *[f'<path d="{poly_d(p)}"/>' for p in parts],
        "</g>",
    ]
    for i, (depth, polys) in enumerate(levels):
        color = LEVEL_COLORS[i % len(LEVEL_COLORS)]
        lines.append(f'<g id="depth_{depth:.1f}mm" fill="none" stroke="{color}" stroke-width="0.1">')
        lines += [f'<path d="{poly_d(p)}"/>' for p in polys]
        lines.append("</g>")
    lines.append("</svg>")
    out_path.write_text("\n".join(lines))

    holes = sum(len(p.interiors) for p in parts)
    print(f"{path.name} -> {out_path}")
    print(f"  view: {view}   size: {x1 - x0:.1f} x {y1 - y0:.1f} mm   "
          f"thickness: {np.ptp(verts[:, 2]):.1f} mm")
    print(f"  red: {len(parts)} outline(s), {holes} through-hole(s)")
    for i, (depth, polys) in enumerate(levels):
        print(f"  {LEVEL_COLORS[i % len(LEVEL_COLORS)]}: {len(polys)} area(s) "
              f"{depth:.1f} mm below the top")


def main():
    ap = argparse.ArgumentParser(description="Turn STL/OBJ/3MF models into 1:1 laser SVG outlines.")
    ap.add_argument("files", nargs="+", type=Path, help="model file(s)")
    ap.add_argument("-o", "--out", type=Path, help="output folder (default: next to each input)")
    ap.add_argument("--view", default="auto", choices=["auto", *VIEWS], help="side to look at (default: auto)")
    ap.add_argument("--no-pockets", action="store_true", help="only trace the outline and through-holes")
    ap.add_argument("--min-area", type=float, default=1.0, help="ignore shapes smaller than this, mm^2 (default 1)")
    ap.add_argument("--simplify", type=float, default=0.01, help="smoothing tolerance in mm (default 0.01)")
    ap.add_argument("--margin", type=float, default=2.0, help="blank border around the drawing, mm (default 2)")
    args = ap.parse_args()

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    failed = 0
    for f in args.files:
        out = (args.out or f.parent) / (f.stem + ".svg")
        try:
            convert(f, out, args.view, not args.no_pockets, args.min_area, args.simplify, args.margin)
        except Exception as e:  # keep going with the other files
            failed += 1
            print(f"{f.name}: failed - {e}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
