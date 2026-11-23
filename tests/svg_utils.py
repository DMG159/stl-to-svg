"""Read the SVGs this project writes back into measurable shapes (for tests)."""
import math
import re
from dataclasses import dataclass, field

from shapely.geometry import LineString, Polygon

PATH_RE = re.compile(r'<path d="([^"]+)"/>')
GROUP_RE = re.compile(r'<g id="([^"]+)" fill="none" stroke="([^"]+)"[^>]*>(.*?)</g>', re.S)
NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class Element:
    """One <path>: a list of subpaths, each a list of (x, y) points."""
    subpaths: list
    closed: list

    @property
    def polygon(self):
        """First closed subpath is the outline, the rest are holes."""
        rings = [p for p, c in zip(self.subpaths, self.closed) if c and len(p) >= 3]
        return Polygon(rings[0], rings[1:]) if rings else Polygon()

    @property
    def lines(self):
        out = []
        for pts, closed in zip(self.subpaths, self.closed):
            seq = pts + [pts[0]] if closed else pts
            if len(seq) >= 2:
                out.append(LineString(seq))
        return out

    @property
    def length(self):
        total = 0.0
        for pts, closed in zip(self.subpaths, self.closed):
            seq = pts + [pts[0]] if closed else pts
            total += sum(math.dist(a, b) for a, b in zip(seq, seq[1:]))
        return total


@dataclass
class Svg:
    width: float
    height: float
    width_attr: str
    groups: dict = field(default_factory=dict)    # id -> list[Element]
    colors: dict = field(default_factory=dict)    # id -> stroke colour

    def get(self, gid):
        return self.groups.get(gid, [])

    def polygons(self, gid):
        return [e.polygon for e in self.get(gid)]

    def lines(self, gid):
        return [ln for e in self.get(gid) for ln in e.lines]

    def area(self, gid):
        return sum(p.area for p in self.polygons(gid))

    def length(self, gid):
        return sum(e.length for e in self.get(gid))

    def depth_groups(self):
        return {g: self.get(g) for g in self.groups if g.startswith("depth_")}


def parse_path(d):
    subpaths, closed = [], []
    for chunk in d.split("M")[1:]:
        is_closed = chunk.rstrip().endswith("Z")
        nums = [float(n) for n in NUM_RE.findall(chunk)]
        pts = list(zip(nums[0::2], nums[1::2]))
        subpaths.append(pts)
        closed.append(is_closed)
    return Element(subpaths, closed)


def parse_svg(text):
    vb = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', text)
    width_attr = re.search(r'width="([^"]+)"', text).group(1)
    svg = Svg(float(vb.group(1)), float(vb.group(2)), width_attr)
    for gid, color, body in GROUP_RE.findall(text):
        svg.groups[gid] = [parse_path(d) for d in PATH_RE.findall(body)]
        svg.colors[gid] = color
    return svg
