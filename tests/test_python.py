"""Unit tests for stl_to_svg.py, using small test models with known answers."""
import math
import subprocess
import sys

import numpy as np
import pytest
from shapely.geometry import box as rect

import stl_to_svg as s
from conftest import ROOT
from svg_utils import parse_svg


def outline(path, tmp_path, view="auto", pockets=True, min_area=1.0, simplify=0.01, margin=2.0):
    out = tmp_path / "out.svg"
    s.convert_outline(path, out, view, pockets, min_area, simplify, margin)
    return parse_svg(out.read_text())


def sliced(path, tmp_path, thickness, sheet=300.0, guides=True, numbers=True, view="auto"):
    out = tmp_path / "layers.svg"
    s.convert_slice(path, out, view, thickness, sheet, guides, numbers, 1.0, 0.01)
    return parse_svg(out.read_text())


def in_row_order(svg):
    """Cut pieces sorted left to right (for sheets laid out in one row)."""
    return sorted(svg.polygons("cut"), key=lambda p: p.bounds[0])


# --------------------------------------------------------------------------
# Loading and orientation
# --------------------------------------------------------------------------

class TestLoading:
    @pytest.mark.parametrize("view,size", [("top", (20, 30)), ("bottom", (20, 30)), ("front", (20, 5)),
                                           ("back", (20, 5)), ("left", (30, 5)), ("right", (30, 5))])
    def test_each_view_sees_the_right_faces(self, fixture, tmp_path, view, size):
        svg = outline(fixture("box.stl"), tmp_path, view=view)
        x0, y0, x1, y1 = svg.polygons("cut")[0].bounds
        assert (x1 - x0, y1 - y0) == pytest.approx(size, abs=0.01)

    def test_views_are_rotations_not_mirror_images(self):
        for right, up, toward in s.VIEWS.values():
            assert np.linalg.det(np.array([right, up, toward])) == pytest.approx(1)

    def test_auto_view_looks_at_the_flattest_side(self, fixture):
        _, _, _, _, view = s.load_oriented(fixture("box.stl"), "auto", False)
        assert view == "top"                    # box is 20 x 30 x 5: thinnest along Z
        _, _, _, _, view = s.load_oriented(fixture("two_posts.stl"), "auto", False)
        assert view == "top"

    def test_auto_view_when_slicing_stacks_from_the_top(self, fixture):
        _, _, _, _, view = s.load_oriented(fixture("two_posts.stl"), "auto", True)
        assert view == "top"

    @pytest.mark.parametrize("name", ["box_ascii.stl", "box_quads.obj", "box.3mf"])
    def test_ascii_stl_obj_and_3mf_match_binary_stl(self, fixture, tmp_path, name):
        ref = outline(fixture("box.stl"), tmp_path)
        other = outline(fixture(name), tmp_path)
        assert other.polygons("cut")[0].equals_exact(ref.polygons("cut")[0], 1e-6) or \
            other.polygons("cut")[0].symmetric_difference(ref.polygons("cut")[0]).area < 1e-6

    def test_thin_triangles_are_kept(self, fixture, tmp_path):
        # Regression: corners 0.00005 mm apart must not be merged away.
        mesh, _, faces, _, _ = s.load_oriented(fixture("sliver.stl"), "top", False)
        assert len(faces) == 14
        assert outline(fixture("sliver.stl"), tmp_path).area("cut") == pytest.approx(400)


# --------------------------------------------------------------------------
# Outline mode
# --------------------------------------------------------------------------

class TestRealModels:
    """Real models from Thingiverse (see tests/fixtures/real/CREDITS.md)."""

    def real(self, name, tmp_path):
        import gzip
        from conftest import REAL
        path = REAL / name
        if not path.exists():
            pytest.skip(f"{name} not present")
        if path.suffix == ".gz":
            out = tmp_path / path.stem
            out.write_bytes(gzip.decompress(path.read_bytes()))
            return out
        return path

    def test_xyz_cube_is_20mm_with_engraved_letter(self, tmp_path):
        svg = outline(self.real("xyz_cube.stl", tmp_path), tmp_path, view="top")
        x0, y0, x1, y1 = svg.polygons("cut")[0].bounds
        assert (x1 - x0, y1 - y0) == pytest.approx((20, 20), abs=0.01)
        assert len(svg.depth_groups()) == 1                    # the engraved "Z" on top

    @pytest.mark.slow
    @pytest.mark.parametrize("name", ["benchy.stl.gz", "benchy_17_parts.stl.gz"])
    def test_benchy_outline_is_robust_and_fast(self, name, tmp_path):
        # Regressions: minutes per pocket depth, and a geometry error on a
        # slightly self-touching floor shape.
        import time
        t = time.time()
        svg = outline(self.real(name, tmp_path), tmp_path)
        assert time.time() - t < 60
        assert len(svg.get("cut")) == 1
        assert len(svg.polygons("cut")[0].interiors) == 2       # the cabin's side windows

    @pytest.mark.slow
    def test_benchy_slices(self, tmp_path):
        svg = sliced(self.real("benchy.stl.gz", tmp_path), tmp_path, 3)
        assert len(svg.get("cut")) > 16                           # 16 layers, some in several pieces


class TestOutline:
    def test_box_is_traced_at_true_size(self, fixture, tmp_path):
        svg = outline(fixture("box.stl"), tmp_path)
        assert svg.width_attr == "24.000mm"           # 20 mm + 2 mm margin each side
        assert (svg.width, svg.height) == (24, 34)
        assert len(svg.get("cut")) == 1
        assert svg.area("cut") == pytest.approx(600)
        assert svg.depth_groups() == {}

    def test_svg_y_axis_points_down(self, fixture, tmp_path):
        # The plate's hole is near the left; its pocket is right of centre.
        svg = outline(fixture("plate.stl"), tmp_path)
        hole = svg.polygons("cut")[0].interiors[0]
        assert hole.centroid.x == pytest.approx(12, abs=0.1)   # 10 mm + 2 mm margin
        assert hole.centroid.y == pytest.approx(22, abs=0.1)   # 40 - 20 + 2

    def test_plate_hole_and_two_pocket_depths(self, fixture, tmp_path):
        svg = outline(fixture("plate.stl"), tmp_path)
        cut = svg.polygons("cut")
        assert len(cut) == 1 and len(cut[0].interiors) == 1
        hole_area = cut[0].interiors[0].convex_hull.area
        assert hole_area == pytest.approx(math.pi * 16, rel=0.01)
        depths = svg.depth_groups()
        assert set(depths) == {"depth_2.0mm", "depth_4.0mm"}
        assert svg.area("depth_2.0mm") == pytest.approx(20 * 15 - 8 * 6, abs=0.5)
        assert svg.area("depth_4.0mm") == pytest.approx(8 * 6, abs=0.5)
        assert svg.colors["depth_2.0mm"] != svg.colors["depth_4.0mm"] != svg.colors["cut"]

    def test_no_pockets_option(self, fixture, tmp_path):
        svg = outline(fixture("plate.stl"), tmp_path, pockets=False)
        assert svg.depth_groups() == {}
        assert len(svg.polygons("cut")[0].interiors) == 1       # holes still traced

    def test_floor_hidden_under_overlapping_walls(self, fixture, tmp_path):
        # Slab top at 3 mm, walls to 10 mm: only the 44 x 24 floor between the walls shows.
        svg = outline(fixture("overlap.stl"), tmp_path)
        assert svg.area("cut") == pytest.approx(1500)
        assert set(svg.depth_groups()) == {"depth_7.0mm"}
        assert svg.area("depth_7.0mm") == pytest.approx(44 * 24, abs=0.5)

    def test_min_area_drops_small_holes(self, fixture, tmp_path):
        svg = outline(fixture("plate.stl"), tmp_path, min_area=60)   # hole is ~50 mm2
        assert len(svg.polygons("cut")[0].interiors) == 0
        assert "depth_4.0mm" not in svg.groups                         # 48 mm2 spot dropped too

    def test_margin(self, fixture, tmp_path):
        svg = outline(fixture("box.stl"), tmp_path, margin=5)
        assert (svg.width, svg.height) == (30, 40)

    def test_simplify_reduces_points_but_keeps_shape(self, fixture, tmp_path):
        fine = outline(fixture("sphere.stl"), tmp_path, simplify=0.01).get("cut")[0]
        coarse = outline(fixture("sphere.stl"), tmp_path, simplify=0.5).get("cut")[0]
        assert len(coarse.subpaths[0]) < len(fine.subpaths[0])
        # Smoothing may move the edge by up to the tolerance, no more.
        assert abs(coarse.polygon.area - fine.polygon.area) <= fine.polygon.length * 0.5

    def test_two_separate_parts(self, fixture, tmp_path):
        svg = outline(fixture("two_posts.stl"), tmp_path)
        assert len(svg.get("cut")) == 2

    def test_pocket_search_is_fast_on_many_depths(self, fixture, tmp_path):
        # Regression: the sphere seen from the side has hundreds of tiny flat
        # facets; each must not trigger a full merge of the model.
        import time
        t = time.time()
        outline(fixture("sphere.stl"), tmp_path, view="front")
        assert time.time() - t < 10


# --------------------------------------------------------------------------
# Slice mode
# --------------------------------------------------------------------------

class TestSliceLayers:
    @pytest.mark.parametrize("thickness,layers", [(1, 5), (2, 3), (3, 2), (5, 1), (10, 1), (20, 1)])
    def test_layer_count_rounds_to_whole_sheets(self, fixture, tmp_path, thickness, layers):
        # Box is 5 mm tall: 2 mm sheets -> 2.5 -> 3 layers; 3 mm -> 1.67 -> 2 layers.
        svg = sliced(fixture("box.stl"), tmp_path, thickness, sheet=1000, numbers=False)
        assert len(svg.get("cut")) == layers
        assert all(p.area == pytest.approx(600) for p in svg.polygons("cut"))

    def test_sphere_slices_have_the_true_radius(self, fixture, tmp_path):
        svg = sliced(fixture("sphere.stl"), tmp_path, 4, sheet=10000)
        pieces = in_row_order(svg)
        assert len(pieces) == 10
        for i, p in enumerate(pieces):
            z = (i + 0.5) * 4
            expected = math.sqrt(400 - (z - 20) ** 2)
            assert math.sqrt(p.area / math.pi) == pytest.approx(expected, abs=0.25), f"layer {i + 1}"

    def test_hole_inside_one_solid_stays_a_hole(self, fixture, tmp_path):
        # Regression: every torus slice is a ring, not a disc.
        svg = sliced(fixture("torus.stl"), tmp_path, 3)
        pieces = svg.polygons("cut")
        assert len(pieces) == 4
        assert all(len(p.interiors) == 1 for p in pieces)

    def test_overlapping_solids_add_up(self, fixture, tmp_path):
        # Regression: walls overlapping the slab must not cancel out into holes.
        svg = sliced(fixture("overlap.stl"), tmp_path, 3, sheet=1000)
        pieces = in_row_order(svg)
        assert [round(p.area) for p in pieces] == [1500, 444, 444]
        assert [len(p.interiors) for p in pieces] == [0, 1, 1]

    def test_separate_pieces_in_one_layer(self, fixture, tmp_path):
        svg = sliced(fixture("two_posts.stl"), tmp_path, 3, sheet=1000)
        assert len(svg.get("cut")) == 8                           # 4 layers x 2 posts

    def test_stack_direction_follows_view(self, fixture, tmp_path):
        # Stacking the 20 x 30 x 5 box from the front: layers are 20 x 5, 30 mm of them.
        svg = sliced(fixture("box.stl"), tmp_path, 3, sheet=10000, view="front", numbers=False)
        assert len(svg.get("cut")) == 10
        x0, y0, x1, y1 = svg.polygons("cut")[0].bounds
        assert (x1 - x0, y1 - y0) == pytest.approx((20, 5), abs=0.01)


class TestSliceExtras:
    def test_guides_show_where_the_smaller_layer_above_sits(self, fixture, tmp_path):
        svg = sliced(fixture("overlap.stl"), tmp_path, 3, sheet=1000)
        # Only the slab gets a guide: the frame's inner edge, 44 x 24.
        assert svg.length("glue_guides") == pytest.approx(2 * (44 + 24), abs=0.5)
        slab = in_row_order(svg)[0]
        assert all(slab.contains(ln) for ln in svg.lines("glue_guides"))

    def test_no_guide_when_the_layer_above_is_bigger(self, fixture, tmp_path):
        svg = sliced(fixture("sphere.stl"), tmp_path, 4, sheet=10000)
        pieces = in_row_order(svg)
        guided = {i for i, p in enumerate(pieces) for ln in svg.lines("glue_guides") if p.contains(ln)}
        # Widening bottom half (layers 1-5) and the top layer have no guide.
        assert guided == {5, 6, 7, 8}

    def test_guides_are_continuous_lines(self, fixture, tmp_path):
        svg = sliced(fixture("sphere.stl"), tmp_path, 4, sheet=10000)
        assert len(svg.get("glue_guides")) == 4                    # one closed line per guided layer

    def test_every_piece_is_numbered(self, fixture, tmp_path):
        svg = sliced(fixture("two_posts.stl"), tmp_path, 3, sheet=1000)
        # Strokes for "1","2","3","4" = 1 + 5 + 5 + 4, once per post.
        assert len(svg.get("layer_numbers")) == 2 * (1 + 5 + 5 + 4)
        for p in svg.polygons("cut"):
            assert any(p.contains(ln) for ln in svg.lines("layer_numbers"))

    def test_number_hides_under_the_next_layer(self, fixture, tmp_path):
        svg = sliced(fixture("sphere.stl"), tmp_path, 4, sheet=10000)
        pieces = in_row_order(svg)
        offset = pieces[7].bounds[0] - pieces[6].bounds[0]
        # Layer 7's number sits where layer 8 will cover it.
        nums = [ln for ln in svg.lines("layer_numbers") if pieces[6].contains(ln)]
        layer8_centre = pieces[7].centroid
        for ln in nums:
            x, y = ln.centroid.x + offset, ln.centroid.y
            assert pieces[7].contains(type(layer8_centre)(x, y))

    def test_too_small_to_number(self, fixture, tmp_path, capsys):
        svg = sliced(fixture("thin_tower.stl"), tmp_path, 3)
        assert "layer_numbers" not in svg.groups
        assert "too small to number" in capsys.readouterr().out

    def test_options_turn_extras_off(self, fixture, tmp_path):
        svg = sliced(fixture("overlap.stl"), tmp_path, 3, guides=False, numbers=False)
        assert set(svg.groups) == {"cut"}

    def test_colours(self, fixture, tmp_path):
        svg = sliced(fixture("sphere.stl"), tmp_path, 4)
        assert svg.colors == {"cut": "#ff0000", "glue_guides": "#0000ff", "layer_numbers": "#00a000"}


class TestSliceLayout:
    def test_layers_wrap_at_sheet_width(self, fixture, tmp_path):
        wide = sliced(fixture("two_posts.stl"), tmp_path, 3, sheet=300)
        narrow = sliced(fixture("two_posts.stl"), tmp_path, 3, sheet=60)
        assert wide.height == pytest.approx(2 + 16 + 2)               # one row
        assert narrow.height == pytest.approx(2 + 4 * 16 + 3 * 4 + 2)  # one layer per row
        assert narrow.width <= 60

    def test_pieces_never_overlap(self, fixture, tmp_path):
        svg = sliced(fixture("sphere.stl"), tmp_path, 4, sheet=150)
        pieces = svg.polygons("cut")
        for i, a in enumerate(pieces):
            for b in pieces[i + 1:]:
                assert a.distance(b) >= 3.9                             # 4 mm gap

    def test_everything_fits_on_the_sheet(self, fixture, tmp_path):
        svg = sliced(fixture("sphere.stl"), tmp_path, 4, sheet=150)
        page = rect(0, 0, svg.width, svg.height)
        assert all(page.contains(p) for p in svg.polygons("cut"))

    def test_warns_when_a_layer_is_wider_than_the_sheet(self, fixture, tmp_path, capsys):
        sliced(fixture("two_posts.stl"), tmp_path, 3, sheet=50)
        assert "wider than the 50 mm sheet" in capsys.readouterr().out


class TestDigits:
    def test_every_digit_is_drawn(self):
        strokes = {d: len(s.number_lines(d, (0, 0), 5)) for d in "0123456789"}
        assert strokes == {"0": 6, "1": 1, "2": 5, "3": 5, "4": 4, "5": 5, "6": 6, "7": 3, "8": 7, "9": 6}

    def test_number_is_centred_and_sized(self):
        lines = s.number_lines("10", (50, 20), 6)
        xs = [x for ln in lines for x, _ in ln.coords]
        ys = [y for ln in lines for _, y in ln.coords]
        assert (min(ys), max(ys)) == pytest.approx((17, 23))
        assert (min(xs) + max(xs)) / 2 == pytest.approx(50)

    def test_place_number_prefers_hidden_area_and_gives_up_when_tiny(self):
        piece = rect(0, 0, 40, 20)
        cover = rect(25, 0, 40, 20)
        (x, y), h = s.place_number(piece, cover, "3")
        assert x > 25 and 2 <= h <= 6
        assert s.place_number(rect(0, 0, 2, 2), None, "3") is None


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def cli(*args):
    return subprocess.run([sys.executable, str(ROOT / "stl_to_svg.py"), *map(str, args)],
                          capture_output=True, text=True)


class TestCommandLine:
    def test_output_names_and_folder(self, fixture, tmp_path):
        out = tmp_path / "new" / "folder"
        r = cli(fixture("box.stl"), fixture("plate.stl"), "-o", out)
        assert r.returncode == 0, r.stderr
        assert (out / "box.svg").exists() and (out / "plate.svg").exists()
        r = cli(fixture("box.stl"), "--slice", 2, "-o", out)
        assert (out / "box_layers.svg").exists()

    def test_bad_files_fail_but_others_still_convert(self, fixture, tmp_path):
        r = cli(fixture("garbage.stl"), fixture("empty.stl"), fixture("box.stl"), "-o", tmp_path)
        assert r.returncode == 1
        assert "garbage.stl: failed" in r.stderr and "empty.stl: failed" in r.stderr
        assert (tmp_path / "box.svg").exists()

    @pytest.mark.parametrize("bad", ["0", "-3"])
    def test_rejects_bad_thickness(self, fixture, bad):
        r = cli(fixture("box.stl"), "--slice", bad)
        assert r.returncode == 2 and "positive thickness" in r.stderr

    def test_help_lists_every_option(self):
        r = cli("--help")
        for opt in ["--view", "--no-pockets", "--slice", "--sheet-width", "--no-guides",
                    "--no-numbers", "--min-area", "--simplify", "--margin", "--out"]:
            assert opt in r.stdout
