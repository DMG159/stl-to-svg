"""The web version (docs/core.js) must give the same SVG as stl_to_svg.py.

Runs both on every test model in both modes and compares what they drew:
sheet size, which colour groups exist, how many shapes each has, and how
closely the shapes overlap. Needs Node.js; skipped if `node` isn't installed.
"""
import gzip
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from shapely.ops import unary_union

import stl_to_svg
from conftest import FIXTURES, REAL, ROOT
from svg_utils import parse_svg

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

# (model, options) - options use the web version's names.
SYNTHETIC = [
    ("box.stl", {}), ("box.stl", {"view": "front"}), ("box.stl", {"mode": "slice", "thickness": 2}),
    ("plate.stl", {}), ("plate.stl", {"pockets": False}), ("plate.stl", {"mode": "slice", "thickness": 2}),
    ("overlap.stl", {}), ("overlap.stl", {"mode": "slice", "thickness": 3}),
    ("sphere.stl", {}), ("sphere.stl", {"mode": "slice", "thickness": 4}),
    ("torus.stl", {"mode": "slice", "thickness": 3}),
    ("two_posts.stl", {"mode": "slice", "thickness": 3, "sheetWidth": 60}),
    ("thin_tower.stl", {"mode": "slice", "thickness": 3}),
    ("sliver.stl", {}), ("box_quads.obj", {}), ("box_ascii.stl", {}),
    ("sphere.stl", {"mode": "slice", "thickness": 4, "guides": False, "numbers": False}),
]
REAL_MODELS = [
    ("xyz_cube.stl", {}), ("xyz_cube.stl", {"mode": "slice", "thickness": 3}),
    ("benchy.stl.gz", {}), ("benchy.stl.gz", {"mode": "slice", "thickness": 3}),
    ("benchy_17_parts.stl.gz", {}), ("benchy_17_parts.stl.gz", {"mode": "slice", "thickness": 3}),
]


def run_python(model, opts, out):
    if opts.get("mode") == "slice":
        stl_to_svg.convert_slice(model, out, opts.get("view", "auto"), opts["thickness"],
                                 opts.get("sheetWidth", 300.0), opts.get("guides", True),
                                 opts.get("numbers", True), 1.0, 0.01)
    else:
        stl_to_svg.convert_outline(model, out, opts.get("view", "auto"), opts.get("pockets", True),
                                   1.0, 0.01, 2.0)
    return parse_svg(out.read_text())


def run_js(model, opts, out):
    r = subprocess.run([NODE, str(ROOT / "tests" / "js_convert.js"), str(model), str(out), json.dumps(opts)],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr
    return parse_svg(out.read_text())


def unpack(path, tmp_path):
    if path.suffix != ".gz":
        return path
    dest = tmp_path / path.stem
    dest.write_bytes(gzip.decompress(path.read_bytes()))
    return dest


def numbers_per_piece(svg, reference):
    """Total length of number strokes inside each cut piece of `reference`."""
    pieces = reference.polygons("cut")
    totals = [0.0] * len(pieces)
    for ln in svg.lines("layer_numbers"):
        mid = ln.interpolate(0.5, normalized=True)
        for i, p in enumerate(pieces):
            if p.buffer(0.01).contains(mid):
                totals[i] += ln.length
                break
    return totals


def assert_same(py, js, label):
    assert py.width == pytest.approx(js.width, abs=0.1), f"{label}: sheet width"
    assert py.height == pytest.approx(js.height, abs=0.1), f"{label}: sheet height"
    assert set(py.groups) == set(js.groups), f"{label}: colour groups differ"
    for gid in py.groups:
        if gid == "layer_numbers":
            # A number may sit anywhere its piece has room (a rectangle has a
            # whole line of equally good spots), so check that every piece
            # carries the same amount of number strokes, wherever they are.
            # Number size comes from the room in the piece, which the two
            # versions measure slightly differently, so allow 15%.
            got, want = numbers_per_piece(js, py), numbers_per_piece(py, py)
            assert [g > 0 for g in got] == [w > 0 for w in want], f"{label}: different pieces are numbered"
            assert got == pytest.approx(want, rel=0.15, abs=0.5), f"{label}: layer number sizes differ"
            continue
        if gid == "glue_guides":
            # Lines, not areas: same total length, drawn in the same places.
            # (How a line is split into pieces doesn't matter.)
            assert js.length(gid) == pytest.approx(py.length(gid), rel=0.02, abs=0.5), f"{label}: {gid} length"
            near_py = unary_union(py.lines(gid)).buffer(0.1)
            off = unary_union(js.lines(gid)).difference(near_py).length
            assert off <= max(0.02 * py.length(gid), 0.5), f"{label}: {gid} drawn in different places ({off:.1f} mm)"
            continue
        assert len(py.get(gid)) == len(js.get(gid)), f"{label}: number of shapes in {gid}"
        pa, ja = py.area(gid), js.area(gid)
        assert ja == pytest.approx(pa, rel=0.01, abs=1.0), f"{label}: {gid} area {pa:.1f} vs {ja:.1f}"
        # The shapes must sit in the same place, not just have the same size.
        p_union = unary_union(py.polygons(gid)).buffer(0)
        j_union = unary_union(js.polygons(gid)).buffer(0)
        mismatch = p_union.symmetric_difference(j_union).area
        assert mismatch <= max(0.01 * pa, 1.0), f"{label}: {gid} shapes don't line up ({mismatch:.2f} mm2 differ)"


@pytest.mark.parametrize("name,opts", SYNTHETIC, ids=[f"{n}-{json.dumps(o)}" for n, o in SYNTHETIC])
def test_web_matches_python(name, opts, tmp_path):
    model = FIXTURES / name
    py = run_python(model, opts, tmp_path / "py.svg")
    js = run_js(model, opts, tmp_path / "js.svg")
    assert_same(py, js, f"{name} {opts}")


@pytest.mark.slow
@pytest.mark.parametrize("name,opts", REAL_MODELS, ids=[f"{n}-{json.dumps(o)}" for n, o in REAL_MODELS])
def test_web_matches_python_on_real_models(name, opts, tmp_path):
    path = REAL / name
    if not path.exists():
        pytest.skip(f"{name} not present")
    model = unpack(path, tmp_path)
    py = run_python(model, opts, tmp_path / "py.svg")
    js = run_js(model, opts, tmp_path / "js.svg")
    assert_same(py, js, f"{name} {opts}")


def test_web_rejects_bad_files(tmp_path):
    for name in ("garbage.stl", "empty.stl"):
        r = subprocess.run([NODE, str(ROOT / "tests" / "js_convert.js"), str(FIXTURES / name),
                            str(tmp_path / "x.svg"), "{}"], capture_output=True, text=True)
        assert r.returncode == 1, name
        assert "no triangles" in r.stderr
