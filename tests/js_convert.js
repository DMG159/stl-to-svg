#!/usr/bin/env node
// Run the web version's converter (docs/core.js) from the command line.
// Used by the parity tests to compare it with stl_to_svg.py.
//
//   node tests/js_convert.js model.stl out.svg '{"mode":"slice","thickness":3}'
//
// Prints the summary and warnings as JSON on stdout.
const fs = require('fs');
const path = require('path');
const core = require(path.join(__dirname, '..', 'docs', 'core.js'));

const [input, output, optsJson] = process.argv.slice(2);
const opts = JSON.parse(optsJson || '{}');
const buf = fs.readFileSync(input);
try {
  const mesh = core.parseModel(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.length), input);
  const r = opts.mode === 'slice' ? core.convertSlice(mesh, opts) : core.convertOutline(mesh, opts);
  fs.writeFileSync(output, r.svg);
  process.stdout.write(JSON.stringify({ summary: r.summary, warnings: r.warnings, view: r.view }));
} catch (err) {
  process.stderr.write(err.message + '\n');
  process.exit(1);
}
