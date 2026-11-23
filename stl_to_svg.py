#!/usr/bin/env python3
"""
stl_to_svg.py - Turn a 3D model (STL, OBJ, 3MF) into 1:1 laser-ready SVG.

Two modes:

OUTLINE (default) - like putting the part on a scanner. The model is flattened
and traced at real size (millimetres).
  red   - outer cut line, plus any through-holes
  other - one colour per pocket / step depth (flat surfaces lower than the top)

SLICE (--slice 3) - cuts the model into layers as thick as your sheet material,
so you can laser-cut each layer and glue the stack back into the 3D shape.
All layers are laid out on one sheet.
  red   - cut line for each layer
  blue  - glue guide: where the next layer up sits (score or light engrave)
  green - layer number, single-stroke (score or light engrave)

Setup (once):
    pip install trimesh shapely numpy

Usage:
    python stl_to_svg.py part.stl                     # outline -> part.svg
    python stl_to_svg.py a.stl b.stl -o out/          # several files, output folder
    python stl_to_svg.py part.stl --view bottom       # part was modelled face-down
    python stl_to_svg.py part.stl --slice 3           # 3 mm layers -> part_layers.svg
    python stl_to_svg.py part.stl --slice 3 --sheet-width 400
"""
import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import shapely
import shapely.affinity
import shapely.errors
import trimesh
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import polygonize, polylabel

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
GUIDE_COLOR = "#0000ff"
NUMBER_COLOR = "#00a000"
LEVEL_COLORS = ["#0000ff", "#00a000", "#ff00ff", "#ff8000", "#00c0c0",
                "#808000", "#8000ff", "#c00060"]


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def load_oriented(path, view, slice_mode):
    """Load a model and turn it so x=right, y=up, z=toward the viewer."""
    mesh = trimesh.load(path, force="mesh")
    if len(mesh.faces) == 0:
        raise ValueError("no triangles found in file")
    if view == "auto":
        view = "top" if slice_mode else AUTO_VIEW[int(np.argmin(mesh.extents))]
    basis = np.array(VIEWS[view], dtype=float)
    verts = mesh.vertices @ basis.T
    normals = mesh.face_normals @ basis.T
    return mesh, verts, np.asarray(mesh.faces), normals, view


def as_polygons(geom, min_area):
    """Return the polygons in geom bigger than min_area (small holes dropped)."""
    if geom is None or geom.is_empty:
        return []
    parts = geom.geoms if hasattr(geom, "geoms") else [geom]
    out = []
    for p in parts:
        if isinstance(p, Polygon) and p.area >= min_area:
            holes = [h for h in p.interiors if Polygon(h).area >= min_area]
            out.append(Polygon(p.exterior, holes))
    return out


def as_lines(geom):
    """Return the line pieces in geom (ignores points)."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if hasattr(geom, "geoms"):
        return [g for sub in geom.geoms for g in as_lines(sub)]
    if geom.geom_type in ("Polygon", "LinearRing"):
        return [LineString(geom.exterior.coords if hasattr(geom, "exterior") else geom.coords)]
    return []


SLIVER = 0.005  # mm: triangles seen edge-on flatten thinner than this
GRID = 0.001    # mm: fallback precision, the same 1 micron grid the web version uses


def robust(op, *args):
    """Run a shapely operation; if near-touching lines confuse it, retry on a 1 micron grid."""
    args = [np.asarray(a) if isinstance(a, list) else a for a in args]
    try:
        return op(*args)
    except shapely.errors.GEOSException:
        pass
    fixed = [shapely.make_valid(a) for a in args]          # repair the shapes themselves
    try:
        return op(*fixed)
    except shapely.errors.GEOSException:
        return op(*fixed, grid_size=GRID)


def flatten(tris_xy, close):
    """Union a stack of 2D triangles into one clean shape."""
    # Walls seen edge-on flatten into slivers with no real width. They add
    # nothing to the drawing (the seam closing below fills far wider gaps)
    # but can make the merge very slow, so leave them out.
    a, b, c = tris_xy[:, 0], tris_xy[:, 1], tris_xy[:, 2]
    area2 = np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
    longest = np.maximum.reduce([np.hypot(*(b - a).T), np.hypot(*(c - b).T), np.hypot(*(a - c).T)])
    width = np.divide(area2, longest, out=np.zeros_like(area2), where=longest > 0)
    polys = shapely.polygons(tris_xy[width >= SLIVER])
    if len(polys) == 0:
        return Polygon()
    shape = robust(shapely.union_all, polys)
    # Close the hairline seams that floating-point error leaves between triangles.
    shape = shape.buffer(close, join_style="mitre").buffer(-close, join_style="mitre")
    return shape if shape.is_valid else shapely.make_valid(shape)


class SvgWriter:
    """Collects coloured groups of paths in sheet coordinates (mm, y down)."""

    def __init__(self):
        self.groups = []  # (id, colour, [d strings])

    def group(self, gid, color):
        paths = []
        self.groups.append((gid, color, paths))
        return paths

    def save(self, out_path, width, height):
        lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.3f}mm" '
                 f'height="{height:.3f}mm" viewBox="0 0 {width:.3f} {height:.3f}">']
        for gid, color, paths in self.groups:
            if not paths:
                continue
            lines.append(f'<g id="{gid}" fill="none" stroke="{color}" stroke-width="0.1">')
            lines += [f'<path d="{d}"/>' for d in paths]
            lines.append("</g>")
        lines.append("</svg>")
        Path(out_path).write_text("\n".join(lines))


def poly_d(poly, tf):
    def ring(r):
        pts = [tf(x, y) for x, y in r.coords]
        return "M" + " L".join(f"{a:.3f},{b:.3f}" for a, b in pts) + " Z"
    return " ".join([ring(poly.exterior)] + [ring(r) for r in poly.interiors])


def line_d(line, tf):
    pts = [tf(x, y) for x, y in line.coords]
    return "M" + " L".join(f"{a:.3f},{b:.3f}" for a, b in pts)


# --------------------------------------------------------------------------
# Outline mode
# --------------------------------------------------------------------------

def convert_outline(path, out_path, view, pockets, min_area, simplify, margin):
    _, verts, faces, normals, view = load_oriented(path, view, False)
    normals_z = normals[:, 2]
    tris = verts[faces]

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
        tri_x, tri_y = tris[:, :, 0], tris[:, :, 1]
        tx0, tx1, ty0, ty1 = tri_x.min(1), tri_x.max(1), tri_y.min(1), tri_y.max(1)
        for h in sorted(set(heights), reverse=True):
            depth = top - h
            if depth < 0.05:
                continue  # that's the top surface itself
            region = flatten(flat_tris[heights == h], close=0.02)
            if region.area < min_area:
                continue  # too small to keep, so skip the expensive part
            # Only keep the part of this surface you can actually see from above:
            # models built from overlapping solids can hide a floor under a wall.
            # Only triangles over this surface can hide it, so only merge those.
            rx0, ry0, rx1, ry1 = region.bounds
            near = ((tri_top > h + 0.05) & (tx1 >= rx0 - 0.1) & (tx0 <= rx1 + 0.1)
                    & (ty1 >= ry0 - 0.1) & (ty0 <= ry1 + 0.1))
            above = flatten(tris[near][:, :, :2], close=0.02)
            region = robust(shapely.difference, region, above).buffer(-0.05).buffer(0.05).simplify(simplify)
            polys = as_polygons(region, min_area)
            if polys:
                levels.append((depth, polys))

    x0, y0, x1, y1 = MultiPolygon(parts).bounds
    w, h = x1 - x0 + 2 * margin, y1 - y0 + 2 * margin

    def tf(x, y):
        return x - x0 + margin, y1 - y + margin

    svg = SvgWriter()
    svg.group("cut", CUT_COLOR).extend(poly_d(p, tf) for p in parts)
    for i, (depth, polys) in enumerate(levels):
        svg.group(f"depth_{depth:.1f}mm", LEVEL_COLORS[i % len(LEVEL_COLORS)]).extend(
            poly_d(p, tf) for p in polys)
    svg.save(out_path, w, h)

    holes = sum(len(p.interiors) for p in parts)
    print(f"{path.name} -> {out_path}")
    print(f"  view: {view}   size: {x1 - x0:.1f} x {y1 - y0:.1f} mm   "
          f"thickness: {np.ptp(verts[:, 2]):.1f} mm")
    print(f"  red: {len(parts)} outline(s), {holes} through-hole(s)")
    for i, (depth, polys) in enumerate(levels):
        print(f"  {LEVEL_COLORS[i % len(LEVEL_COLORS)]}: {len(polys)} area(s) "
              f"{depth:.1f} mm below the top")


# --------------------------------------------------------------------------
# Slice mode
# --------------------------------------------------------------------------

def section(verts, faces, normals, z):
    """Cross-section of the model at height z, as a shapely shape.

    Each triangle's facing direction says which side of its slice line is
    inside the material. The slice outlines are oriented that way and filled
    by winding number, so overlapping solids add up instead of cancelling,
    and a hole inside a solid stays a hole.
    """
    while np.any(np.abs(verts[:, 2] - z) < 1e-7):
        z += 1e-5  # avoid slicing exactly through a vertex
    d = verts[faces][:, :, 2] - z
    cross = (d.min(1) < 0) & (d.max(1) > 0)
    if not cross.any():
        return Polygon()
    f = faces[cross]
    nrm = normals[cross]
    key_cols, pt_cols, mask_cols = [], [], []
    n = len(verts)
    for i, j in ((0, 1), (1, 2), (2, 0)):
        a, b = f[:, i], f[:, j]
        lo, hi = np.minimum(a, b), np.maximum(a, b)   # same edge -> same maths
        dlo, dhi = verts[lo, 2] - z, verts[hi, 2] - z
        crosses = (dlo < 0) != (dhi < 0)
        t = np.where(crosses, dlo / np.where(crosses, dlo - dhi, 1), 0)
        p = verts[lo, :2] + t[:, None] * (verts[hi, :2] - verts[lo, :2])
        key_cols.append(lo.astype(np.int64) * n + hi)
        pt_cols.append(p)
        mask_cols.append(crosses)
    keys = np.stack(key_cols, 1)
    pts = np.stack(pt_cols, 1)
    mask = np.stack(mask_cols, 1)
    ok = mask.sum(1) == 2
    seg_keys = keys[ok][mask[ok]].reshape(-1, 2)
    seg_pts = pts[ok][mask[ok]].reshape(-1, 2, 2)
    # Direction a slice line should run so the material is on its left.
    seg_dir = np.stack([-nrm[ok][:, 1], nrm[ok][:, 0]], 1)

    point_of = {}
    adj = defaultdict(list)
    for s, ((k1, k2), (p1, p2)) in enumerate(zip(seg_keys, seg_pts)):
        point_of[k1] = p1
        point_of[k2] = p2
        adj[k1].append(s)
        adj[k2].append(s)

    used = np.zeros(len(seg_keys), bool)
    rings = []
    for s0 in range(len(seg_keys)):
        if used[s0]:
            continue
        used[s0] = True
        start, cur = seg_keys[s0]
        chain = [start, cur]
        vote = float(np.dot(point_of[cur] - point_of[start], seg_dir[s0]))
        while cur != start:
            nxt = [s for s in adj[cur] if not used[s]]
            if not nxt:
                break
            s = nxt[0]
            used[s] = True
            k1, k2 = seg_keys[s]
            prev, cur = cur, (k2 if k1 == cur else k1)
            vote += float(np.dot(point_of[cur] - point_of[prev], seg_dir[s]))
            chain.append(cur)
        coords = [tuple(point_of[k]) for k in chain]
        closed = chain[-1] == start
        if closed:
            coords.pop()
        elif math.dist(coords[0], coords[-1]) > 0.5:
            continue  # gap in the mesh too big to bridge
        if len(coords) >= 3:
            rings.append(coords if vote >= 0 else coords[::-1])
    return winding_fill(rings)


def winding_fill(rings):
    """Fill closed outlines by the non-zero winding rule.

    Outlines running anticlockwise add material and clockwise ones remove it.
    A spot is solid if the count of outlines around it is anything but zero.
    """
    if not rings:
        return Polygon()
    lines = [LineString(r + [r[0]]) for r in rings]
    signed = []
    for r in rings:
        poly = Polygon(r)
        area2 = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(r, r[1:] + r[:1]))
        if area2 != 0:
            signed.append((poly if poly.is_valid else poly.buffer(0), 1 if area2 > 0 else -1))
    faces = list(polygonize(shapely.union_all(lines)))
    if not faces:
        return Polygon()
    pts = [face.representative_point() for face in faces]
    winding = np.zeros(len(faces), int)
    for poly, sign in signed:
        shapely.prepare(poly)
        winding += sign * shapely.contains(poly, pts).astype(int)
    keep = [face for face, w in zip(faces, winding) if w != 0]
    return shapely.union_all(keep) if keep else Polygon()


# Single-stroke 7-segment digits in a 1 x 1 box (y up).
_SEG = {"a": ((0, 1), (1, 1)), "b": ((1, 1), (1, .5)), "c": ((1, .5), (1, 0)),
        "d": ((0, 0), (1, 0)), "e": ((0, 0), (0, .5)), "f": ((0, .5), (0, 1)),
        "g": ((0, .5), (1, .5)), "|": ((.5, 0), (.5, 1))}
_DIGITS = {"0": "abcdef", "1": "|", "2": "abged", "3": "abgcd", "4": "fgbc",
           "5": "afgcd", "6": "afedcg", "7": "abc", "8": "abcdefg", "9": "abcdfg"}
CHAR_W, CHAR_GAP = 0.55, 0.3   # as a fraction of text height


def number_lines(text, center, height):
    """Strokes for a number centred on `center`, `height` mm tall."""
    n = len(text)
    width = (n * CHAR_W + (n - 1) * CHAR_GAP) * height
    x0 = center[0] - width / 2
    y0 = center[1] - height / 2
    lines = []
    for i, ch in enumerate(text):
        cx = x0 + i * (CHAR_W + CHAR_GAP) * height
        for seg in _DIGITS[ch]:
            (ax, ay), (bx, by) = _SEG[seg]
            lines.append(LineString([(cx + ax * CHAR_W * height, y0 + ay * height),
                                     (cx + bx * CHAR_W * height, y0 + by * height)]))
    # Centre on the strokes actually drawn ("1" is a single line mid-slot).
    xs = [x for ln in lines for x, _ in ln.coords]
    shift = center[0] - (min(xs) + max(xs)) / 2
    return [shapely.affinity.translate(ln, shift, 0) for ln in lines]


def place_number(piece, cover, text, max_h=6.0, min_h=2.0):
    """Find a spot and size for the layer number inside a piece.

    Prefers the area the next layer will cover, so the number is hidden once
    the stack is glued.
    """
    ratio = len(text) * CHAR_W + (len(text) - 1) * CHAR_GAP
    half_diag = math.hypot(ratio, 1) / 2
    candidates = []
    if cover is not None and not cover.is_empty:
        candidates.append(piece.intersection(cover))
    candidates.append(piece)
    for region in candidates:
        best = None
        for poly in as_polygons(region.buffer(-1.0), 0.5):
            c = polylabel(poly, tolerance=0.1)
            r = poly.boundary.distance(c)
            if best is None or r > best[1]:
                best = (c, r)
        if best:
            h = min(max_h, best[1] / half_diag)
            if h >= min_h:
                return (best[0].x, best[0].y), h
    return None


def convert_slice(path, out_path, view, thickness, sheet_w, guides, numbers,
                  min_area, simplify, gap=4.0, margin=2.0):
    _, verts, faces, normals, view = load_oriented(path, view, True)
    zmin, zmax = verts[:, 2].min(), verts[:, 2].max()
    height = zmax - zmin
    # One layer per sheet thickness, sampled through the middle of each layer.
    # A leftover sliver thinner than half a sheet is dropped.
    n_layers = max(1, int(math.floor(height / thickness + 0.5)))

    layers = []
    for i in range(n_layers):
        z = zmin + (i + 0.5) * thickness
        z = min(z, zmax - 1e-3)
        shape = section(verts, faces, normals, z)
        shape = shape.buffer(0.02, join_style="mitre").buffer(-0.02, join_style="mitre")
        layers.append(as_polygons(shape.simplify(simplify), min_area))

    kept = [(i, pieces) for i, pieces in enumerate(layers) if pieces]
    if not kept:
        raise ValueError("no layers came out - is the model closed (watertight)?")

    svg = SvgWriter()
    cut_g = svg.group("cut", CUT_COLOR)
    guide_g = svg.group("glue_guides", GUIDE_COLOR)
    num_g = svg.group("layer_numbers", NUMBER_COLOR)

    # Shelf layout: left to right, wrapping into new rows at the sheet width.
    x_cur, y_cur, row_h, used_w = margin, margin, 0.0, 0.0
    too_wide, unnumbered = [], []
    for idx, (i, pieces) in enumerate(kept):
        label = str(idx + 1)
        layer = MultiPolygon(pieces)
        above = MultiPolygon(kept[idx + 1][1]) if idx + 1 < len(kept) else None
        bx0, by0, bx1, by1 = layer.bounds
        w, h = bx1 - bx0, by1 - by0
        if w + 2 * margin > sheet_w:
            too_wide.append(label)
        if x_cur > margin and x_cur + w > sheet_w - margin:
            x_cur, y_cur, row_h = margin, y_cur + row_h + gap, 0.0

        def tf(x, y, ox=x_cur - bx0, oy=y_cur + by1):
            return x + ox, oy - y

        for p in pieces:
            cut_g.append(poly_d(p, tf))
            if guides and above is not None:
                inner = p.buffer(-0.3)
                guide = shapely.line_merge(shapely.union_all(as_lines(above.boundary.intersection(inner)) or [LineString()]))
                for ln in as_lines(guide):
                    if ln.length > 0.5:
                        guide_g.append(line_d(ln, tf))
            if numbers:
                spot = place_number(p, above, label)
                if spot:
                    for ln in number_lines(label, *spot):
                        num_g.append(line_d(ln, tf))
                else:
                    unnumbered.append(label)
        x_cur += w + gap
        row_h = max(row_h, h)
        used_w = max(used_w, x_cur - gap + margin)

    total_h = y_cur + row_h + margin
    svg.save(out_path, used_w, total_h)

    stack = len(kept) * thickness
    print(f"{path.name} -> {out_path}")
    print(f"  view: {view}   model height: {height:.1f} mm   "
          f"{len(kept)} layer(s) of {thickness:g} mm = {stack:.1f} mm stack")
    print(f"  sheet: {used_w:.0f} x {total_h:.0f} mm")
    legend = ["red: cut lines"]
    if guides:
        legend.append("blue: glue guides")
    if numbers:
        legend.append("green: layer numbers (1 = bottom)")
    print("  " + "   ".join(legend))
    if len(kept) < n_layers:
        print(f"  note: {n_layers - len(kept)} layer(s) had nothing in them and were skipped")
    if too_wide:
        print(f"  warning: layer(s) {', '.join(too_wide)} are wider than the "
              f"{sheet_w:g} mm sheet")
    if unnumbered:
        print(f"  note: piece(s) in layer(s) {', '.join(sorted(set(unnumbered), key=int))} "
              f"were too small to number")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Turn STL/OBJ/3MF models into 1:1 laser SVGs.")
    ap.add_argument("files", nargs="+", type=Path, help="model file(s)")
    ap.add_argument("-o", "--out", type=Path, help="output folder (default: next to each input)")
    ap.add_argument("--view", default="auto", choices=["auto", *VIEWS],
                    help="side to look at; in slice mode, the stacking direction "
                         "(default: auto = flattest side, or top when slicing)")
    ap.add_argument("--no-pockets", action="store_true", help="outline mode: only trace the outline and through-holes")
    ap.add_argument("--slice", type=float, metavar="MM",
                    help="slice into layers this thick (your sheet thickness, e.g. 3)")
    ap.add_argument("--sheet-width", type=float, default=300.0,
                    help="slice mode: width of your sheet / laser bed in mm (default 300)")
    ap.add_argument("--no-guides", action="store_true", help="slice mode: leave out the glue guides")
    ap.add_argument("--no-numbers", action="store_true", help="slice mode: leave out the layer numbers")
    ap.add_argument("--min-area", type=float, default=1.0, help="ignore shapes smaller than this, mm^2 (default 1)")
    ap.add_argument("--simplify", type=float, default=0.01, help="smoothing tolerance in mm (default 0.01)")
    ap.add_argument("--margin", type=float, default=2.0, help="blank border around the drawing, mm (default 2)")
    args = ap.parse_args()

    if args.slice is not None and args.slice <= 0:
        ap.error("--slice must be a positive thickness in mm")
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    failed = 0
    for f in args.files:
        folder = args.out or f.parent
        try:
            if args.slice:
                convert_slice(f, folder / (f.stem + "_layers.svg"), args.view, args.slice,
                              args.sheet_width, not args.no_guides, not args.no_numbers,
                              args.min_area, args.simplify, margin=args.margin)
            else:
                convert_outline(f, folder / (f.stem + ".svg"), args.view, not args.no_pockets,
                                args.min_area, args.simplify, args.margin)
        except Exception as e:  # keep going with the other files
            failed += 1
            print(f"{f.name}: failed - {e}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
