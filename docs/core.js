/*
 * stl-to-svg core: turn a 3D model (STL or OBJ) into 1:1 laser-ready SVG.
 * Browser port of stl_to_svg.py. Runs entirely on your computer; nothing is uploaded.
 * Depends on ClipperLib (clipper.js, Boost Software License).
 */
(function (root) {
  'use strict';
  const CL = (typeof ClipperLib !== 'undefined') ? ClipperLib
    : (typeof require !== 'undefined' ? require('./clipper.js') : null);

  const S = 1000;                 // Clipper works in integers: 1 unit = 1 micrometre
  const SLIVER = 0.005;           // mm: triangles seen edge-on flatten thinner than this
  const NZ = CL.PolyFillType.pftNonZero;

  // Each view is (right, up, toward-viewer) in the model's own X/Y/Z.
  const VIEWS = {
    top:    [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
    bottom: [[1, 0, 0], [0, -1, 0], [0, 0, -1]],
    front:  [[1, 0, 0], [0, 0, 1], [0, -1, 0]],
    back:   [[-1, 0, 0], [0, 0, 1], [0, 1, 0]],
    right:  [[0, 1, 0], [0, 0, 1], [1, 0, 0]],
    left:   [[0, -1, 0], [0, 0, 1], [-1, 0, 0]],
  };
  const AUTO_VIEW = ['right', 'front', 'top'];   // thinnest axis -> view

  const CUT_COLOR = '#ff0000', GUIDE_COLOR = '#0000ff', NUMBER_COLOR = '#00a000';
  const LEVEL_COLORS = ['#0000ff', '#00a000', '#ff00ff', '#ff8000', '#00c0c0',
                        '#808000', '#8000ff', '#c00060'];

  // ------------------------------------------------------------------------
  // Loading
  // ------------------------------------------------------------------------

  function parseModel(buffer, name) {
    const lower = (name || '').toLowerCase();
    const raw = lower.endsWith('.obj') ? parseOBJ(buffer) : parseSTL(buffer);
    if (raw.length === 0) throw new Error('no triangles found in file');
    return indexMesh(raw);
  }

  function parseSTL(buffer) {
    const dv = new DataView(buffer);
    if (buffer.byteLength >= 84) {
      const n = dv.getUint32(80, true);
      if (84 + n * 50 === buffer.byteLength) {
        const out = new Float64Array(n * 9);
        for (let i = 0; i < n; i++) {
          const off = 84 + i * 50 + 12;           // skip the stored normal
          for (let k = 0; k < 9; k++) out[i * 9 + k] = dv.getFloat32(off + k * 4, true);
        }
        return out;
      }
    }
    const text = new TextDecoder().decode(buffer);
    const re = /vertex\s+(\S+)\s+(\S+)\s+(\S+)/g;
    const vals = [];
    let m;
    while ((m = re.exec(text))) vals.push(+m[1], +m[2], +m[3]);
    return new Float64Array(vals.slice(0, vals.length - (vals.length % 9)));
  }

  function parseOBJ(buffer) {
    const text = new TextDecoder().decode(buffer);
    const v = [], out = [];
    for (const line of text.split(/\r?\n/)) {
      const p = line.trim().split(/\s+/);
      if (p[0] === 'v') v.push([+p[1], +p[2], +p[3]]);
      else if (p[0] === 'f') {
        const idx = p.slice(1).map(t => {
          const i = parseInt(t.split('/')[0], 10);
          return i < 0 ? v.length + i : i - 1;
        });
        for (let k = 1; k + 1 < idx.length; k++) {
          for (const j of [idx[0], idx[k], idx[k + 1]]) out.push(v[j][0], v[j][1], v[j][2]);
        }
      }
    }
    return new Float64Array(out);
  }

  // Merge duplicate corners so neighbouring triangles share vertices.
  function indexMesh(raw) {
    const map = new Map();
    const pos = [];
    const faces = [];
    const nt = raw.length / 9;
    const tri = [0, 0, 0];
    for (let t = 0; t < nt; t++) {
      for (let k = 0; k < 3; k++) {
        const x = raw[t * 9 + k * 3], y = raw[t * 9 + k * 3 + 1], z = raw[t * 9 + k * 3 + 2];
        const key = x + ',' + y + ',' + z;   // exact match: shared corners are bit-identical
        let id = map.get(key);
        if (id === undefined) { id = pos.length / 3; map.set(key, id); pos.push(x, y, z); }
        tri[k] = id;
      }
      if (tri[0] !== tri[1] && tri[1] !== tri[2] && tri[0] !== tri[2]) faces.push(tri[0], tri[1], tri[2]);
    }
    if (faces.length === 0) throw new Error('no triangles found in file');
    return { positions: new Float64Array(pos), faces: new Uint32Array(faces) };
  }

  // Turn the model so x = right, y = up, z = toward the viewer.
  function orient(mesh, view, sliceMode) {
    const p = mesh.positions, n = p.length / 3;
    if (view === 'auto') {
      const ext = [0, 1, 2].map(a => {
        let lo = Infinity, hi = -Infinity;
        for (let i = a; i < p.length; i += 3) { if (p[i] < lo) lo = p[i]; if (p[i] > hi) hi = p[i]; }
        return hi - lo;
      });
      view = sliceMode ? 'top' : AUTO_VIEW[ext.indexOf(Math.min(...ext))];
    }
    const B = VIEWS[view];
    const vx = new Float64Array(n), vy = new Float64Array(n), vz = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const x = p[i * 3], y = p[i * 3 + 1], z = p[i * 3 + 2];
      vx[i] = B[0][0] * x + B[0][1] * y + B[0][2] * z;
      vy[i] = B[1][0] * x + B[1][1] * y + B[1][2] * z;
      vz[i] = B[2][0] * x + B[2][1] * y + B[2][2] * z;
    }
    return { vx, vy, vz, faces: mesh.faces, view };
  }

  // ------------------------------------------------------------------------
  // 2D geometry helpers (Clipper paths in micrometres)
  // ------------------------------------------------------------------------

  function clip(type, subj, clipPaths, fill) {
    const c = new CL.Clipper();
    c.AddPaths(subj, CL.PolyType.ptSubject, true);
    if (clipPaths) c.AddPaths(clipPaths, CL.PolyType.ptClip, true);
    const out = new CL.Paths();
    const ft = fill === undefined ? NZ : fill;   // pftEvenOdd is 0, so no `||` here
    c.Execute(type, out, ft, ft);
    return out;
  }
  const union = (a, fill) => clip(CL.ClipType.ctUnion, a, null, fill);
  const difference = (a, b) => clip(CL.ClipType.ctDifference, a, b);
  const intersection = (a, b) => clip(CL.ClipType.ctIntersection, a, b);

  function offset(paths, mm) {
    const co = new CL.ClipperOffset(2, 0.25 * S / 100);
    co.AddPaths(paths, CL.JoinType.jtMiter, CL.EndType.etClosedPolygon);
    const out = new CL.Paths();
    co.Execute(out, mm * S);
    return out;
  }

  const closeSeams = (paths, mm) => offset(offset(paths, mm), -mm);

  // Union a set of triangles, flattened onto the page.
  function flatten(o, faceIdx) {
    const { vx, vy, faces } = o;
    const paths = [];
    for (const f of faceIdx) {
      const a = faces[f * 3], b = faces[f * 3 + 1], c = faces[f * 3 + 2];
      const pts = [a, b, c].map(i => ({ X: Math.round(vx[i] * S), Y: Math.round(vy[i] * S) }));
      const area2 = (pts[1].X - pts[0].X) * (pts[2].Y - pts[0].Y) - (pts[2].X - pts[0].X) * (pts[1].Y - pts[0].Y);
      // Walls seen edge-on flatten into slivers with no real width. They add
      // nothing to the drawing (the seam closing below fills far wider gaps)
      // but make Clipper very slow, so leave them out.
      const longest = Math.max(Math.hypot(pts[1].X - pts[0].X, pts[1].Y - pts[0].Y),
        Math.hypot(pts[2].X - pts[1].X, pts[2].Y - pts[1].Y), Math.hypot(pts[0].X - pts[2].X, pts[0].Y - pts[2].Y));
      if (longest === 0 || Math.abs(area2) / longest < SLIVER * S) continue;
      if (area2 < 0) pts.reverse();
      paths.push(pts);
    }
    if (!paths.length) return [];
    return closeSeams(unionMany(paths), 0.02);
  }

  // Clipper slows down sharply on huge inputs, so merge neighbouring triangles
  // in small batches first, then merge the batch results pairwise.
  function unionMany(paths) {
    const BATCH = 1500;
    if (paths.length <= BATCH) return union(paths);
    const cx = p => (p[0].X + p[1].X + p[2].X);
    const sorted = paths.slice().sort((a, b) => cx(a) - cx(b));
    let parts = [];
    for (let i = 0; i < sorted.length; i += BATCH) parts.push(union(sorted.slice(i, i + BATCH)));
    while (parts.length > 1) {
      const next = [];
      for (let i = 0; i < parts.length; i += 2) next.push(i + 1 < parts.length ? union(parts[i].concat(parts[i + 1])) : parts[i]);
      parts = next;
    }
    return parts[0];
  }

  // Douglas-Peucker on a closed ring.
  function simplifyRing(ring, tol) {
    if (tol <= 0 || ring.length < 4) return ring;
    const pts = ring.concat([ring[0]]);
    const keep = new Uint8Array(pts.length);
    keep[0] = keep[pts.length - 1] = 1;
    // split at the point farthest from the start so the closed ring is handled well
    let far = 1, fd = -1;
    for (let i = 1; i < pts.length - 1; i++) {
      const d = (pts[i].X - pts[0].X) ** 2 + (pts[i].Y - pts[0].Y) ** 2;
      if (d > fd) { fd = d; far = i; }
    }
    keep[far] = 1;
    const stack = [[0, far], [far, pts.length - 1]];
    const t2 = tol * tol;
    while (stack.length) {
      const [s, e] = stack.pop();
      const ax = pts[s].X, ay = pts[s].Y, dx = pts[e].X - ax, dy = pts[e].Y - ay;
      const len2 = dx * dx + dy * dy;
      let best = -1, bi = -1;
      for (let i = s + 1; i < e; i++) {
        let px = pts[i].X - ax, py = pts[i].Y - ay, d;
        if (len2 === 0) d = px * px + py * py;
        else {
          const t = Math.max(0, Math.min(1, (px * dx + py * dy) / len2));
          const qx = px - t * dx, qy = py - t * dy;
          d = qx * qx + qy * qy;
        }
        if (d > best) { best = d; bi = i; }
      }
      if (best > t2) { keep[bi] = 1; stack.push([s, bi], [bi, e]); }
    }
    const out = [];
    for (let i = 0; i < pts.length - 1; i++) if (keep[i]) out.push(pts[i]);
    return out.length >= 3 ? out : ring;
  }

  const simplifyPaths = (paths, mm) => paths.map(r => simplifyRing(r, mm * S));

  // Group paths into polygons with holes.
  function toPolys(paths) {
    const c = new CL.Clipper();
    c.AddPaths(paths, CL.PolyType.ptSubject, true);
    const tree = new CL.PolyTree();
    c.Execute(CL.ClipType.ctUnion, tree, NZ, NZ);
    const polys = [];
    (function walk(node) {
      for (const outer of node.Childs()) {
        const poly = { outer: outer.Contour(), holes: [] };
        for (const h of outer.Childs()) { poly.holes.push(h.Contour()); walk(h); }
        polys.push(poly);
      }
    })(tree);
    return polys;
  }

  const ringArea = r => Math.abs(CL.Clipper.Area(r)) / (S * S);   // mm^2
  const polyPaths = p => [p.outer].concat(p.holes);
  const polysPaths = ps => ps.reduce((acc, p) => acc.concat(polyPaths(p)), []);

  function asPolygons(paths, minArea) {
    const out = [];
    for (const p of toPolys(paths)) {
      const area = ringArea(p.outer) - p.holes.reduce((s, h) => s + ringArea(h), 0);
      if (area >= minArea) out.push({ outer: p.outer, holes: p.holes.filter(h => ringArea(h) >= minArea) });
    }
    return out;
  }

  function bounds(polys) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const p of polys) for (const pt of p.outer) {
      if (pt.X < x0) x0 = pt.X; if (pt.X > x1) x1 = pt.X;
      if (pt.Y < y0) y0 = pt.Y; if (pt.Y > y1) y1 = pt.Y;
    }
    return [x0 / S, y0 / S, x1 / S, y1 / S];
  }

  // ------------------------------------------------------------------------
  // SVG output
  // ------------------------------------------------------------------------

  const f3 = v => v.toFixed(3);

  function ringD(ring, tf, closed) {
    const pts = ring.map(p => tf(p.X / S, p.Y / S));
    return 'M' + pts.map(q => f3(q[0]) + ',' + f3(q[1])).join(' L') + (closed ? ' Z' : '');
  }
  const polyD = (p, tf) => polyPaths(p).map(r => ringD(r, tf, true)).join(' ');

  function svgDoc(width, height, groups) {
    const lines = [`<svg xmlns="http://www.w3.org/2000/svg" width="${f3(width)}mm" height="${f3(height)}mm" viewBox="0 0 ${f3(width)} ${f3(height)}">`];
    for (const g of groups) {
      if (!g.paths.length) continue;
      lines.push(`<g id="${g.id}" fill="none" stroke="${g.color}" stroke-width="0.1">`);
      for (const d of g.paths) lines.push(`<path d="${d}"/>`);
      lines.push('</g>');
    }
    lines.push('</svg>');
    return lines.join('\n');
  }

  // ------------------------------------------------------------------------
  // Outline mode
  // ------------------------------------------------------------------------

  function convertOutline(mesh, opts) {
    const o = orient(mesh, opts.view || 'auto', false);
    const { vz, faces } = o;
    const nf = faces.length / 3;
    const all = Array.from({ length: nf }, (_, i) => i);
    const minArea = opts.minArea ?? 1, simp = opts.simplify ?? 0.01, margin = opts.margin ?? 2;

    const parts = asPolygons(simplifyPaths(flatten(o, all), simp), minArea);
    if (!parts.length) throw new Error('outline came out empty');

    const levels = [];
    let top = -Infinity, bottom = Infinity;
    for (let i = 0; i < vz.length; i++) { if (vz[i] > top) top = vz[i]; if (vz[i] < bottom) bottom = vz[i]; }
    if (opts.pockets !== false) {
      const triTop = new Float64Array(nf);
      const byLevel = new Map();
      for (let f = 0; f < nf; f++) {
        const a = faces[f * 3], b = faces[f * 3 + 1], c = faces[f * 3 + 2];
        const za = vz[a], zb = vz[b], zc = vz[c];
        triTop[f] = Math.max(za, zb, zc);
        if (triTop[f] - Math.min(za, zb, zc) >= 0.02) continue;
        if (faceNormalZ(o, a, b, c) <= 0.99) continue;
        const k = Math.round((za + zb + zc) / 3 / 0.05);
        if (!byLevel.has(k)) byLevel.set(k, []);
        byLevel.get(k).push(f);
      }
      // Each triangle's flat footprint box, to find which ones sit over a floor.
      const { vx, vy } = o;
      const bx = new Float64Array(nf * 4);
      for (let f = 0; f < nf; f++) {
        const a = faces[f * 3], b = faces[f * 3 + 1], c = faces[f * 3 + 2];
        bx[f * 4] = Math.min(vx[a], vx[b], vx[c]); bx[f * 4 + 1] = Math.max(vx[a], vx[b], vx[c]);
        bx[f * 4 + 2] = Math.min(vy[a], vy[b], vy[c]); bx[f * 4 + 3] = Math.max(vy[a], vy[b], vy[c]);
      }
      for (const k of [...byLevel.keys()].sort((a, b) => b - a)) {
        const h = k * 0.05, depth = top - h;
        if (depth < 0.05) continue;
        let region = flatten(o, byLevel.get(k));
        const regionPolys = toPolys(region);
        const regionArea = regionPolys.reduce((s, p) => s + ringArea(p.outer) - p.holes.reduce((t, r) => t + ringArea(r), 0), 0);
        if (regionArea < minArea) continue;   // too small to keep, so skip the expensive part
        // Only triangles over this floor can hide it, so only merge those.
        const [rx0, ry0, rx1, ry1] = bounds(regionPolys);
        const above = flatten(o, all.filter(f => triTop[f] > h + 0.05
          && bx[f * 4 + 1] >= rx0 - 0.1 && bx[f * 4] <= rx1 + 0.1
          && bx[f * 4 + 3] >= ry0 - 0.1 && bx[f * 4 + 2] <= ry1 + 0.1));
        region = offset(offset(difference(region, above), -0.05), 0.05);
        const polys = asPolygons(simplifyPaths(region, simp), minArea);
        if (polys.length) levels.push({ depth, polys });
      }
    }

    const [x0, , x1, y1] = bounds(parts);
    const y0 = bounds(parts)[1];
    const tf = (x, y) => [x - x0 + margin, y1 - y + margin];
    const groups = [{ id: 'cut', color: CUT_COLOR, paths: parts.map(p => polyD(p, tf)) }];
    levels.forEach((lv, i) => groups.push({
      id: `depth_${lv.depth.toFixed(1)}mm`, color: LEVEL_COLORS[i % LEVEL_COLORS.length],
      paths: lv.polys.map(p => polyD(p, tf)),
    }));
    const svg = svgDoc(x1 - x0 + 2 * margin, y1 - y0 + 2 * margin, groups);

    const holes = parts.reduce((s, p) => s + p.holes.length, 0);
    const legend = [{ color: CUT_COLOR, text: `Cut: ${parts.length} outline${parts.length === 1 ? '' : 's'}, ${holes} through-hole${holes === 1 ? '' : 's'}` }];
    levels.forEach((lv, i) => legend.push({
      color: LEVEL_COLORS[i % LEVEL_COLORS.length],
      text: `${lv.polys.length} area${lv.polys.length === 1 ? '' : 's'} ${lv.depth.toFixed(1)} mm below the top`,
    }));
    return {
      svg, legend, view: o.view,
      summary: `View: ${o.view}. Size ${(x1 - x0).toFixed(1)} × ${(y1 - y0).toFixed(1)} mm, ${(top - bottom).toFixed(1)} mm thick.`,
      warnings: [],
    };
  }

  function faceNormalZ(o, a, b, c) {
    const { vx, vy, vz } = o;
    const ux = vx[b] - vx[a], uy = vy[b] - vy[a], uz = vz[b] - vz[a];
    const wx = vx[c] - vx[a], wy = vy[c] - vy[a], wz = vz[c] - vz[a];
    const nx = uy * wz - uz * wy, ny = uz * wx - ux * wz, nz = ux * wy - uy * wx;
    const len = Math.hypot(nx, ny, nz);
    return len ? nz / len : 0;
  }

  // ------------------------------------------------------------------------
  // Slice mode
  // ------------------------------------------------------------------------

  // Cross-section of the model at height z, as Clipper paths.
  // Each triangle's facing direction says which side of its slice line is
  // inside the material. The outlines are oriented that way and filled by
  // winding number, so overlapping solids add up instead of cancelling, and
  // a hole inside a solid stays a hole.
  function section(o, z) {
    const { vx, vy, vz, faces } = o;
    const n = vx.length, nf = faces.length / 3;
    const segKeys = [], segDir = [], pointOf = new Map(), adj = new Map();
    for (let f = 0; f < nf; f++) {
      const ids = [faces[f * 3], faces[f * 3 + 1], faces[f * 3 + 2]];
      const d0 = vz[ids[0]] - z, d1 = vz[ids[1]] - z, d2 = vz[ids[2]] - z;
      if (!(Math.min(d0, d1, d2) < 0 && Math.max(d0, d1, d2) > 0)) continue;
      const keys = [];
      for (const [i, j] of [[0, 1], [1, 2], [2, 0]]) {
        const lo = Math.min(ids[i], ids[j]), hi = Math.max(ids[i], ids[j]);
        const dlo = vz[lo] - z, dhi = vz[hi] - z;
        if ((dlo < 0) === (dhi < 0)) continue;
        const t = dlo / (dlo - dhi);
        const key = lo * n + hi;
        if (!pointOf.has(key)) pointOf.set(key, [vx[lo] + t * (vx[hi] - vx[lo]), vy[lo] + t * (vy[hi] - vy[lo])]);
        keys.push(key);
      }
      if (keys.length !== 2) continue;
      // Face normal from the corner order; the slice line should run along
      // (-ny, nx) so the material is on its left.
      const [a, b, c] = ids;
      const ux = vx[b] - vx[a], uy = vy[b] - vy[a], uz = vz[b] - vz[a];
      const wx = vx[c] - vx[a], wy = vy[c] - vy[a], wz = vz[c] - vz[a];
      const nx = uy * wz - uz * wy, ny = uz * wx - ux * wz;
      const s = segKeys.length;
      segKeys.push(keys);
      segDir.push([-ny, nx]);
      for (const k of keys) { if (!adj.has(k)) adj.set(k, []); adj.get(k).push(s); }
    }
    const used = new Uint8Array(segKeys.length);
    const rings = [];
    for (let s0 = 0; s0 < segKeys.length; s0++) {
      if (used[s0]) continue;
      used[s0] = 1;
      const start = segKeys[s0][0];
      let cur = segKeys[s0][1];
      const chain = [start, cur];
      const step = (from, to, s) => {
        const p = pointOf.get(from), q = pointOf.get(to);
        return (q[0] - p[0]) * segDir[s][0] + (q[1] - p[1]) * segDir[s][1];
      };
      let vote = step(start, cur, s0);
      while (cur !== start) {
        const nxt = adj.get(cur).find(s => !used[s]);
        if (nxt === undefined) break;
        used[nxt] = 1;
        const [k1, k2] = segKeys[nxt];
        const prev = cur;
        cur = k1 === cur ? k2 : k1;
        vote += step(prev, cur, nxt);
        chain.push(cur);
      }
      const closed = chain[chain.length - 1] === start;
      if (closed) chain.pop();
      const pts = chain.map(k => pointOf.get(k));
      if (!closed) {
        const a = pts[0], b = pts[pts.length - 1];
        if (Math.hypot(a[0] - b[0], a[1] - b[1]) > 0.5) continue;   // gap too big to bridge
      }
      if (pts.length >= 3) {
        const ring = pts.map(p => ({ X: Math.round(p[0] * S), Y: Math.round(p[1] * S) }));
        rings.push(vote >= 0 ? ring : ring.reverse());
      }
    }
    return rings.length ? union(rings, NZ) : [];
  }

  // Single-stroke 7-segment digits in a 1 x 1 box (y up).
  const SEG = { a: [[0, 1], [1, 1]], b: [[1, 1], [1, .5]], c: [[1, .5], [1, 0]], d: [[0, 0], [1, 0]],
                e: [[0, 0], [0, .5]], f: [[0, .5], [0, 1]], g: [[0, .5], [1, .5]], '|': [[.5, 0], [.5, 1]] };
  const DIGITS = { 0: 'abcdef', 1: '|', 2: 'abged', 3: 'abgcd', 4: 'fgbc', 5: 'afgcd',
                   6: 'afedcg', 7: 'abc', 8: 'abcdefg', 9: 'abcdfg' };
  const CHAR_W = 0.55, CHAR_GAP = 0.3;

  function numberLines(text, cx, cy, h) {
    const n = text.length;
    const width = (n * CHAR_W + (n - 1) * CHAR_GAP) * h;
    const x0 = cx - width / 2, y0 = cy - h / 2;
    const lines = [];
    [...text].forEach((ch, i) => {
      const ox = x0 + i * (CHAR_W + CHAR_GAP) * h;
      for (const s of DIGITS[ch]) {
        const [[ax, ay], [bx, by]] = SEG[s];
        lines.push([[ox + ax * CHAR_W * h, y0 + ay * h], [ox + bx * CHAR_W * h, y0 + by * h]]);
      }
    });
    // Centre on the strokes actually drawn ("1" is a single line mid-slot).
    const xs = lines.flatMap(l => [l[0][0], l[1][0]]);
    const shift = cx - (Math.min(...xs) + Math.max(...xs)) / 2;
    return lines.map(([[ax, ay], [bx, by]]) => [[ax + shift, ay], [bx + shift, by]]);
  }

  // "Pole of inaccessibility": the point inside a polygon farthest from its edges.
  function polylabel(rings, precision) {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const [x, y] of rings[0]) {
      if (x < minX) minX = x; if (y < minY) minY = y; if (x > maxX) maxX = x; if (y > maxY) maxY = y;
    }
    const w = maxX - minX, h = maxY - minY, cellSize = Math.min(w, h);
    if (cellSize === 0) return { x: minX, y: minY, d: 0 };
    const cell = (x, y, half) => { const d = pointToPolygonDist(x, y, rings); return { x, y, h: half, d, max: d + half * Math.SQRT2 }; };
    const heap = [];
    const push = c => { heap.push(c); let i = heap.length - 1; while (i > 0) { const p = (i - 1) >> 1; if (heap[p].max >= heap[i].max) break; [heap[p], heap[i]] = [heap[i], heap[p]]; i = p; } };
    const pop = () => {
      const top = heap[0], last = heap.pop();
      if (heap.length) {
        heap[0] = last; let i = 0;
        for (;;) {
          const l = 2 * i + 1, r = l + 1; let m = i;
          if (l < heap.length && heap[l].max > heap[m].max) m = l;
          if (r < heap.length && heap[r].max > heap[m].max) m = r;
          if (m === i) break; [heap[m], heap[i]] = [heap[i], heap[m]]; i = m;
        }
      }
      return top;
    };
    const half = cellSize / 2;
    for (let x = minX; x < maxX; x += cellSize) for (let y = minY; y < maxY; y += cellSize) push(cell(x + half, y + half, half));
    let best = cell(minX + w / 2, minY + h / 2, 0);
    while (heap.length) {
      const c = pop();
      if (c.d > best.d) best = c;
      if (c.max - best.d <= precision) continue;
      const hh = c.h / 2;
      push(cell(c.x - hh, c.y - hh, hh)); push(cell(c.x + hh, c.y - hh, hh));
      push(cell(c.x - hh, c.y + hh, hh)); push(cell(c.x + hh, c.y + hh, hh));
    }
    return best;
  }

  function pointToPolygonDist(x, y, rings) {
    let inside = false, minSq = Infinity;
    for (const ring of rings) {
      for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const a = ring[i], b = ring[j];
        if ((a[1] > y) !== (b[1] > y) && x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]) inside = !inside;
        let px = a[0], py = a[1], dx = b[0] - px, dy = b[1] - py;
        if (dx !== 0 || dy !== 0) {
          const t = ((x - px) * dx + (y - py) * dy) / (dx * dx + dy * dy);
          if (t > 1) { px = b[0]; py = b[1]; } else if (t > 0) { px += dx * t; py += dy * t; }
        }
        const ddx = x - px, ddy = y - py;
        minSq = Math.min(minSq, ddx * ddx + ddy * ddy);
      }
    }
    return (inside ? 1 : -1) * Math.sqrt(minSq);
  }

  // Spot and size for a layer number; prefers the area the next layer covers.
  function placeNumber(piecePaths, abovePaths, text, maxH = 6, minH = 2) {
    const ratio = text.length * CHAR_W + (text.length - 1) * CHAR_GAP;
    const halfDiag = Math.hypot(ratio, 1) / 2;
    const candidates = [];
    if (abovePaths && abovePaths.length) candidates.push(intersection(piecePaths, abovePaths));
    candidates.push(piecePaths);
    for (const region of candidates) {
      let best = null;
      for (const poly of asPolygons(offset(region, -1.0), 0.5)) {
        const rings = polyPaths(poly).map(r => r.map(p => [p.X / S, p.Y / S]));
        const c = polylabel(rings, 0.1);
        if (!best || c.d > best.d) best = c;
      }
      if (best) {
        const h = Math.min(maxH, best.d / halfDiag);
        if (h >= minH) return { x: best.x, y: best.y, h };
      }
    }
    return null;
  }

  // Join line pieces that continue one another into single lines.
  function joinLines(lines) {
    const same = (a, b) => a.X === b.X && a.Y === b.Y;
    let pieces = lines.filter(l => l.length > 1).map(l => l.slice());
    let joined = true;
    while (joined) {
      joined = false;
      outer: for (let i = 0; i < pieces.length; i++) {
        for (let j = 0; j < pieces.length; j++) {
          if (i === j) continue;
          const a = pieces[i], b = pieces[j];
          if (same(a[0], a[a.length - 1]) || same(b[0], b[b.length - 1])) continue;
          let merged = null;
          if (same(a[a.length - 1], b[0])) merged = a.concat(b.slice(1));
          else if (same(a[a.length - 1], b[b.length - 1])) merged = a.concat(b.slice(0, -1).reverse());
          else if (same(a[0], b[0])) merged = a.slice().reverse().concat(b.slice(1));
          else if (same(a[0], b[b.length - 1])) merged = b.concat(a.slice(1));
          if (merged) {
            pieces = pieces.filter((_, k) => k !== i && k !== j);
            pieces.push(merged);
            joined = true;
            break outer;
          }
        }
      }
    }
    return pieces;
  }

  function convertSlice(mesh, opts) {
    const t = opts.thickness;
    if (!(t > 0)) throw new Error('layer thickness must be more than 0');
    const sheetW = opts.sheetWidth ?? 300, minArea = opts.minArea ?? 1;
    const simp = opts.simplify ?? 0.01, margin = opts.margin ?? 2, gap = 4;
    const guides = opts.guides !== false, numbers = opts.numbers !== false;

    const o = orient(mesh, opts.view || 'auto', true);
    const { vz } = o;
    let zmin = Infinity, zmax = -Infinity;
    for (let i = 0; i < vz.length; i++) { if (vz[i] < zmin) zmin = vz[i]; if (vz[i] > zmax) zmax = vz[i]; }
    const height = zmax - zmin;
    // One layer per sheet, traced through its middle; a sliver under half a sheet is dropped.
    const nLayers = Math.max(1, Math.floor(height / t + 0.5));
    if (nLayers > 500) throw new Error(`that would be ${nLayers} layers - check the thickness`);

    const layers = [];
    for (let i = 0; i < nLayers; i++) {
      let z = Math.min(zmin + (i + 0.5) * t, zmax - 1e-3);
      for (let guard = 0; guard < 100 && vz.some(v => Math.abs(v - z) < 1e-7); guard++) z += 1e-5;
      let shape = section(o, z);
      if (shape.length) shape = closeSeams(shape, 0.02);
      layers.push(asPolygons(simplifyPaths(shape, simp), minArea));
    }
    const kept = layers.filter(l => l.length);
    if (!kept.length) throw new Error('no layers came out - is the model closed (watertight)?');

    const cut = [], guideD = [], numD = [];
    let xCur = margin, yCur = margin, rowH = 0, usedW = 0;
    const tooWide = [], unnumbered = new Set();
    kept.forEach((pieces, idx) => {
      const label = String(idx + 1);
      const above = idx + 1 < kept.length ? polysPaths(kept[idx + 1]) : null;
      const [bx0, by0, bx1, by1] = bounds(pieces);
      const w = bx1 - bx0, h = by1 - by0;
      if (w + 2 * margin > sheetW) tooWide.push(label);
      if (xCur > margin && xCur + w > sheetW - margin) { xCur = margin; yCur += rowH + gap; rowH = 0; }
      const ox = xCur - bx0, oy = yCur + by1;
      const tf = (x, y) => [x + ox, oy - y];

      for (const p of pieces) {
        cut.push(polyD(p, tf));
        const pp = polyPaths(p);
        if (guides && above) {
          const c = new CL.Clipper();
          for (const r of above) c.AddPath(r.concat([r[0]]), CL.PolyType.ptSubject, false);
          c.AddPaths(offset(pp, -0.3), CL.PolyType.ptClip, true);
          const tree = new CL.PolyTree();
          c.Execute(CL.ClipType.ctIntersection, tree, NZ, NZ);
          for (const ln of joinLines(CL.Clipper.OpenPathsFromPolyTree(tree))) {
            let len = 0;
            for (let k = 1; k < ln.length; k++) len += Math.hypot(ln[k].X - ln[k - 1].X, ln[k].Y - ln[k - 1].Y);
            if (len / S > 0.5) guideD.push(ringD(ln, tf, false));
          }
        }
        if (numbers) {
          const spot = placeNumber(pp, above, label);
          if (spot) {
            for (const [[ax, ay], [bx, by]] of numberLines(label, spot.x, spot.y, spot.h)) {
              const a = tf(ax, ay), b = tf(bx, by);
              numD.push(`M${f3(a[0])},${f3(a[1])} L${f3(b[0])},${f3(b[1])}`);
            }
          } else unnumbered.add(label);
        }
      }
      xCur += w + gap;
      rowH = Math.max(rowH, h);
      usedW = Math.max(usedW, xCur - gap + margin);
    });
    const totalH = yCur + rowH + margin;
    const svg = svgDoc(usedW, totalH, [
      { id: 'cut', color: CUT_COLOR, paths: cut },
      { id: 'glue_guides', color: GUIDE_COLOR, paths: guideD },
      { id: 'layer_numbers', color: NUMBER_COLOR, paths: numD },
    ]);

    const legend = [{ color: CUT_COLOR, text: 'Cut lines' }];
    if (guides) legend.push({ color: GUIDE_COLOR, text: 'Glue guides: where the next layer sits (score)' });
    if (numbers) legend.push({ color: NUMBER_COLOR, text: 'Layer numbers, 1 = bottom (score)' });
    const warnings = [];
    if (kept.length < nLayers) warnings.push(`${nLayers - kept.length} empty layer(s) were skipped.`);
    if (tooWide.length) warnings.push(`Layer(s) ${tooWide.join(', ')} are wider than your ${sheetW} mm sheet.`);
    if (unnumbered.size) warnings.push(`Piece(s) in layer(s) ${[...unnumbered].join(', ')} were too small to number.`);
    return {
      svg, legend, warnings, view: o.view,
      summary: `View: ${o.view}. Model ${height.toFixed(1)} mm tall → ${kept.length} layer${kept.length === 1 ? '' : 's'} of ${t} mm = ${(kept.length * t).toFixed(1)} mm stack. Sheet ${usedW.toFixed(0)} × ${totalH.toFixed(0)} mm.`,
    };
  }

  const api = { parseModel, convertOutline, convertSlice, VIEWS, numberLines };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.StlToSvg = api;
})(typeof self !== 'undefined' ? self : this);
