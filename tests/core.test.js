// Unit tests for the web version's converter (docs/core.js).
// Run with:  node --test tests/
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const core = require('../docs/core.js');

const FIX = path.join(__dirname, 'fixtures');

const zlib = require('zlib');

function load(name) {
  let buf = fs.readFileSync(path.join(FIX, name));
  if (name.endsWith('.gz')) buf = zlib.gunzipSync(buf);
  return core.parseModel(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.length), name);
}

// --- reading the SVG back -------------------------------------------------

function parse(svg) {
  const vb = svg.match(/viewBox="0 0 ([\d.]+) ([\d.]+)"/);
  const groups = {}, colors = {};
  for (const m of svg.matchAll(/<g id="([^"]+)" fill="none" stroke="([^"]+)"[^>]*>([\s\S]*?)<\/g>/g)) {
    colors[m[1]] = m[2];
    groups[m[1]] = [...m[3].matchAll(/<path d="([^"]+)"\/>/g)].map(p => p[1].split('M').slice(1).map(sub => {
      const nums = sub.match(/-?\d+(?:\.\d+)?/g).map(Number);
      const pts = [];
      for (let i = 0; i < nums.length; i += 2) pts.push([nums[i], nums[i + 1]]);
      return { pts, closed: /Z\s*$/.test(sub) };
    }));
  }
  return { width: +vb[1], height: +vb[2], widthAttr: svg.match(/width="([^"]+)"/)[1], groups, colors };
}

const ringArea = pts => Math.abs(pts.reduce((s, p, i) => {
  const q = pts[(i + 1) % pts.length];
  return s + p[0] * q[1] - q[0] * p[1];
}, 0)) / 2;
const shapeArea = sub => ringArea(sub[0].pts) - sub.slice(1).reduce((s, r) => s + ringArea(r.pts), 0);
const groupArea = (svg, g) => (svg.groups[g] || []).reduce((s, sh) => s + shapeArea(sh), 0);
const lineLen = sub => sub.reduce((s, r) => {
  const pts = r.closed ? r.pts.concat([r.pts[0]]) : r.pts;
  let l = 0;
  for (let i = 1; i < pts.length; i++) l += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  return s + l;
}, 0);
const groupLen = (svg, g) => (svg.groups[g] || []).reduce((s, sh) => s + lineLen(sh), 0);
const bbox = sub => {
  const xs = sub[0].pts.map(p => p[0]), ys = sub[0].pts.map(p => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
};
const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, `${msg || ''} expected ${b} ± ${tol}, got ${a}`);
const piecesInRow = svg => svg.groups.cut.slice().sort((a, b) => bbox(a)[0] - bbox(b)[0]);

const outline = (name, opts = {}) => parse(core.convertOutline(load(name), opts).svg);
const slice = (name, opts) => {
  const r = core.convertSlice(load(name), opts);
  return Object.assign(parse(r.svg), { result: r });
};

// --- loading ---------------------------------------------------------------

test('reads binary STL, ASCII STL and OBJ with quads the same way', () => {
  for (const name of ['box.stl', 'box_ascii.stl', 'box_quads.obj']) {
    const m = load(name);
    assert.equal(m.faces.length / 3, 12, name);
    assert.equal(m.positions.length / 3, 8, name);
    near(groupArea(outline(name), 'cut'), 600, 1e-6, name);
  }
});

test('keeps extremely thin triangles (regression: nearby corners were merged)', () => {
  assert.equal(load('sliver.stl').faces.length / 3, 14);
  near(groupArea(outline('sliver.stl'), 'cut'), 400, 1e-6);
});

test('rejects files that are not models', () => {
  assert.throws(() => load('garbage.stl'), /no triangles/);
  assert.throws(() => load('empty.stl'), /no triangles/);
});

// --- outline mode ----------------------------------------------------------

test('traces each view at true size', () => {
  const sizes = { top: [20, 30], bottom: [20, 30], front: [20, 5], back: [20, 5], left: [30, 5], right: [30, 5] };
  for (const [view, [w, h]] of Object.entries(sizes)) {
    const [x0, y0, x1, y1] = bbox(outline('box.stl', { view }).groups.cut[0]);
    near(x1 - x0, w, 1e-6, view); near(y1 - y0, h, 1e-6, view);
  }
});

test('views are rotations, not mirror images', () => {
  for (const [r, u, t] of Object.values(core.VIEWS)) {
    const det = r[0] * (u[1] * t[2] - u[2] * t[1]) - r[1] * (u[0] * t[2] - u[2] * t[0]) + r[2] * (u[0] * t[1] - u[1] * t[0]);
    assert.equal(det, 1);
  }
});

test('auto view looks at the flattest side', () => {
  assert.equal(core.convertOutline(load('box.stl'), {}).view, 'top');
  assert.equal(core.convertSlice(load('box.stl'), { thickness: 1 }).view, 'top');
});

test('box: size in millimetres, one outline, no pockets', () => {
  const svg = outline('box.stl');
  assert.equal(svg.widthAttr, '24.000mm');
  assert.deepEqual([svg.width, svg.height], [24, 34]);
  assert.equal(svg.groups.cut.length, 1);
  assert.deepEqual(Object.keys(svg.groups), ['cut']);
});

test('plate: through-hole and two pocket depths', () => {
  const svg = outline('plate.stl');
  assert.equal(svg.groups.cut.length, 1);
  assert.equal(svg.groups.cut[0].length, 2, 'outline + one hole');
  near(ringArea(svg.groups.cut[0][1].pts), Math.PI * 16, 0.5);
  assert.deepEqual(Object.keys(svg.groups).sort(), ['cut', 'depth_2.0mm', 'depth_4.0mm']);
  near(groupArea(svg, 'depth_2.0mm'), 300 - 48, 0.5);
  near(groupArea(svg, 'depth_4.0mm'), 48, 0.5);
  assert.notEqual(svg.colors['depth_2.0mm'], svg.colors['depth_4.0mm']);
});

test('y axis points down in the SVG', () => {
  const hole = outline('plate.stl').groups.cut[0][1].pts;
  const cx = hole.reduce((s, p) => s + p[0], 0) / hole.length;
  const cy = hole.reduce((s, p) => s + p[1], 0) / hole.length;
  near(cx, 12, 0.1); near(cy, 22, 0.1);
});

test('pockets can be turned off', () => {
  assert.deepEqual(Object.keys(outline('plate.stl', { pockets: false }).groups), ['cut']);
});

test('floor hidden under overlapping walls only shows between them', () => {
  const svg = outline('overlap.stl');
  near(groupArea(svg, 'cut'), 1500, 1e-6);
  assert.deepEqual(Object.keys(svg.groups).sort(), ['cut', 'depth_7.0mm']);
  near(groupArea(svg, 'depth_7.0mm'), 44 * 24, 0.5);
});

test('min area and margin options', () => {
  assert.equal(outline('plate.stl', { minArea: 60 }).groups.cut[0].length, 1, 'small hole dropped');
  const svg = outline('box.stl', { margin: 5 });
  assert.deepEqual([svg.width, svg.height], [30, 40]);
});

test('separate parts come out separately', () => {
  assert.equal(outline('two_posts.stl').groups.cut.length, 2);
});

test('many flat depths stay fast (regression: 147 depths on Benchy took minutes)', () => {
  const t = Date.now();
  outline('sphere.stl', { view: 'front' });
  assert.ok(Date.now() - t < 10000);
});

// --- slice mode ------------------------------------------------------------

test('layer count rounds to whole sheets', () => {
  for (const [t, n] of [[1, 5], [2, 3], [3, 2], [5, 1], [10, 1], [20, 1]]) {
    const svg = slice('box.stl', { thickness: t, sheetWidth: 1000, numbers: false });
    assert.equal(svg.groups.cut.length, n, `thickness ${t}`);
  }
});

test('rejects a thickness of zero or less', () => {
  assert.throws(() => core.convertSlice(load('box.stl'), { thickness: 0 }));
  assert.throws(() => core.convertSlice(load('box.stl'), { thickness: -2 }));
});

test('sphere slices have the true radius', () => {
  const pieces = piecesInRow(slice('sphere.stl', { thickness: 4, sheetWidth: 10000 }));
  assert.equal(pieces.length, 10);
  pieces.forEach((p, i) => {
    const z = (i + 0.5) * 4;
    near(Math.sqrt(shapeArea(p) / Math.PI), Math.sqrt(400 - (z - 20) ** 2), 0.25, `layer ${i + 1}`);
  });
});

test('a hole inside one solid stays a hole (regression: rings came out as discs)', () => {
  const svg = slice('torus.stl', { thickness: 3 });
  assert.equal(svg.groups.cut.length, 4);
  for (const p of svg.groups.cut) assert.equal(p.length, 2);
});

test('overlapping solids add up (regression: overlaps cancelled into holes)', () => {
  const pieces = piecesInRow(slice('overlap.stl', { thickness: 3, sheetWidth: 1000 }));
  assert.deepEqual(pieces.map(p => Math.round(shapeArea(p))), [1500, 444, 444]);
  assert.deepEqual(pieces.map(p => p.length), [1, 2, 2]);
});

test('stacking direction follows the view', () => {
  const svg = slice('box.stl', { thickness: 3, sheetWidth: 10000, view: 'front', numbers: false });
  assert.equal(svg.groups.cut.length, 10);
  const [x0, y0, x1, y1] = bbox(svg.groups.cut[0]);
  near(x1 - x0, 20, 1e-6); near(y1 - y0, 5, 1e-6);
});

test('glue guide marks where the smaller layer above sits', () => {
  const svg = slice('overlap.stl', { thickness: 3, sheetWidth: 1000 });
  near(groupLen(svg, 'glue_guides'), 2 * (44 + 24), 0.5);
});

test('no glue guide when the layer above is bigger; guides are single lines', () => {
  const svg = slice('sphere.stl', { thickness: 4, sheetWidth: 10000 });
  const pieces = piecesInRow(svg).map(bbox);
  const guided = new Set(svg.groups.glue_guides.map(g => {
    const x = g[0].pts[0][0];
    return pieces.findIndex(b => x >= b[0] && x <= b[2]);
  }));
  assert.deepEqual([...guided].sort(), [5, 6, 7, 8]);
  assert.equal(svg.groups.glue_guides.length, 4);
});

test('every piece is numbered, numbers centred on their strokes', () => {
  const svg = slice('two_posts.stl', { thickness: 3, sheetWidth: 1000 });
  assert.equal(svg.groups.layer_numbers.length, 2 * (1 + 5 + 5 + 4));
  for (const p of svg.groups.cut) {
    const [x0, y0, x1, y1] = bbox(p);
    assert.ok(svg.groups.layer_numbers.some(n => n[0].pts.every(([x, y]) => x > x0 && x < x1 && y > y0 && y < y1)));
  }
});

test('pieces too small for a number are reported', () => {
  const svg = slice('thin_tower.stl', { thickness: 3 });
  assert.equal(svg.groups.layer_numbers, undefined);
  assert.match(svg.result.warnings.join(' '), /too small to number/);
});

test('guides and numbers can be turned off', () => {
  assert.deepEqual(Object.keys(slice('overlap.stl', { thickness: 3, guides: false, numbers: false }).groups), ['cut']);
});

test('colours: red cut, blue guides, green numbers', () => {
  assert.deepEqual(slice('sphere.stl', { thickness: 4 }).colors,
    { cut: '#ff0000', glue_guides: '#0000ff', layer_numbers: '#00a000' });
});

test('layers wrap at the sheet width and never overlap', () => {
  near(slice('two_posts.stl', { thickness: 3, sheetWidth: 300 }).height, 20, 1e-6);
  const narrow = slice('two_posts.stl', { thickness: 3, sheetWidth: 60 });
  near(narrow.height, 2 + 4 * 16 + 3 * 4 + 2, 1e-6);
  assert.ok(narrow.width <= 60);
  const boxes = slice('sphere.stl', { thickness: 4, sheetWidth: 150 }).groups.cut.map(bbox);
  boxes.forEach((a, i) => boxes.slice(i + 1).forEach(b => {
    const apart = a[2] + 3.9 <= b[0] || b[2] + 3.9 <= a[0] || a[3] + 3.9 <= b[1] || b[3] + 3.9 <= a[1];
    assert.ok(apart, 'pieces overlap or are closer than the 4 mm gap');
  }));
});

test('warns when a layer is wider than the sheet', () => {
  const r = core.convertSlice(load('two_posts.stl'), { thickness: 3, sheetWidth: 50 });
  assert.match(r.warnings.join(' '), /wider than your 50 mm sheet/);
});

test('summary reports the stack height', () => {
  const r = core.convertSlice(load('overlap.stl'), { thickness: 3 });
  assert.match(r.summary, /10\.0 mm tall → 3 layers of 3 mm = 9\.0 mm stack/);
});

test('numbers are centred on the strokes actually drawn', () => {
  const lines = core.numberLines('10', 50, 20, 6);
  const xs = lines.flatMap(l => [l[0][0], l[1][0]]), ys = lines.flatMap(l => [l[0][1], l[1][1]]);
  near((Math.min(...xs) + Math.max(...xs)) / 2, 50, 1e-9);
  near(Math.min(...ys), 17, 1e-9); near(Math.max(...ys), 23, 1e-9);
  const strokes = Object.fromEntries([...'0123456789'].map(d => [d, core.numberLines(d, 0, 0, 5).length]));
  assert.deepEqual(strokes, { 0: 6, 1: 1, 2: 5, 3: 5, 4: 4, 5: 5, 6: 6, 7: 3, 8: 7, 9: 6 });
});

// --- real-world models (tests/fixtures/real) --------------------------------

const real = name => fs.existsSync(path.join(FIX, 'real', name));

test('XYZ cube: glue guides come out as joined lines', { skip: !real('xyz_cube.stl') }, () => {
  const svg = slice('real/xyz_cube.stl', { thickness: 3 });
  const ends = svg.groups.glue_guides.flatMap(g => {
    const p = g[0].pts, a = p[0].join(','), b = p[p.length - 1].join(',');
    return g[0].closed || a === b ? [] : [a, b];       // loops have no loose ends
  });
  // A point where one guide ends and another starts means they should have been one line.
  const counts = ends.reduce((m, k) => m.set(k, (m.get(k) || 0) + 1), new Map());
  assert.ok([...counts.values()].every(c => c === 1), 'guide pieces left unjoined');
});

test('Benchy (225,000 triangles): outline in reasonable time', { skip: !real('benchy.stl.gz') }, () => {
  const t = Date.now();
  const svg = outline('real/benchy.stl.gz');
  assert.ok(Date.now() - t < 60000, `took ${Date.now() - t} ms`);
  assert.equal(svg.groups.cut.length, 1);
  assert.equal(svg.groups.cut[0].length, 3, 'outline + 2 holes (the cabin windows)');
});

test('17-part Benchy: outline in reasonable time (regression: edge-on slivers hung the merge)',
  { skip: !real('benchy_17_parts.stl.gz') }, () => {
    const t = Date.now();
    const svg = outline('real/benchy_17_parts.stl.gz');
    assert.ok(Date.now() - t < 60000, `took ${Date.now() - t} ms`);
    assert.equal(svg.groups.cut.length, 1);
  });
