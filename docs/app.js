(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = { mode: 'outline', mesh: null, name: '', svg: '', outName: '' };

  const HELP = {
    outline: 'Flattens the model like a scanner and traces its outline, holes and pockets.',
    slice: 'Cuts the model into layers as thick as your sheet, to cut out and glue into a stack.',
  };
  const VIEW_HINT = {
    outline: 'Automatic looks at the flattest side.',
    slice: 'Layers stack in this direction. Automatic stacks from the top, like a 3D printer.',
  };

  // ---- mode switch ------------------------------------------------------
  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll('.modes button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
    document.querySelectorAll('[data-for]').forEach(el => { el.hidden = el.dataset.for !== mode; });
    $('mode-help').textContent = HELP[mode];
    $('view-hint').textContent = VIEW_HINT[mode];
    scheduleRun();
  }
  document.querySelectorAll('.modes button').forEach(b => b.addEventListener('click', () => setMode(b.dataset.mode)));

  // ---- loading files ----------------------------------------------------
  const drop = $('drop'), fileInput = $('file');
  const openPicker = () => fileInput.click();
  drop.addEventListener('click', e => { if (!e.target.closest('.examples')) openPicker(); });
  drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPicker(); } });
  $('choose').addEventListener('click', openPicker);
  fileInput.addEventListener('change', () => { if (fileInput.files[0]) loadFile(fileInput.files[0]); fileInput.value = ''; });

  ['dragenter', 'dragover'].forEach(t => document.addEventListener(t, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(t => document.addEventListener(t, e => { e.preventDefault(); if (t === 'drop' || !e.relatedTarget) drop.classList.remove('over'); }));
  document.addEventListener('drop', e => { const f = e.dataTransfer && e.dataTransfer.files[0]; if (f) loadFile(f); });

  document.querySelectorAll('[data-example]').forEach(b => b.addEventListener('click', async () => {
    setMode(b.dataset.mode);
    state.sourceImg = b.dataset.img;
    state.sourceAlt = b.querySelector('img').alt;
    try {
      const res = await fetch('examples/' + b.dataset.example);
      if (!res.ok) throw new Error('could not load the example');
      load(await res.arrayBuffer(), b.dataset.example);
    } catch (err) { showError(err.message); }
  }));

  function loadFile(file) {
    state.sourceImg = null;   // only the examples come with a picture of the model
    if (!/\.(stl|obj)$/i.test(file.name)) { showError(`${file.name} isn't an STL or OBJ file. Export your model as STL and try again.`); return; }
    const reader = new FileReader();
    reader.onload = () => load(reader.result, file.name);
    reader.onerror = () => showError(`Couldn't read ${file.name}.`);
    reader.readAsArrayBuffer(file);
  }

  function load(buffer, name) {
    busy(true);
    setTimeout(() => {
      try {
        state.mesh = StlToSvg.parseModel(buffer, name);
        state.name = name;
        run();
      } catch (err) {
        state.mesh = null;
        busy(false);
        showError(`Couldn't open ${name}: ${err.message}.`);
      }
    }, 30);
  }

  // ---- converting -------------------------------------------------------
  let timer = null;
  function scheduleRun() {
    if (!state.mesh) return;
    clearTimeout(timer);
    timer = setTimeout(() => { busy(true); setTimeout(run, 30); }, 250);
  }
  ['view', 'pockets', 'thickness', 'sheet-width', 'guides', 'numbers'].forEach(id =>
    $(id).addEventListener(id === 'thickness' || id === 'sheet-width' ? 'input' : 'change', scheduleRun));

  function run() {
    hideError();
    const base = state.name.replace(/\.[^.]+$/, '');
    try {
      let r;
      if (state.mode === 'slice') {
        const thickness = parseFloat($('thickness').value);
        const sheetWidth = parseFloat($('sheet-width').value);
        if (!(thickness >= 0.5)) throw new Error('Enter a sheet thickness of at least 0.5 mm');
        if (!(sheetWidth >= 20)) throw new Error('Enter a sheet width of at least 20 mm');
        r = StlToSvg.convertSlice(state.mesh, {
          view: $('view').value, thickness, sheetWidth,
          guides: $('guides').checked, numbers: $('numbers').checked,
        });
        state.outName = base + '_layers.svg';
      } else {
        r = StlToSvg.convertOutline(state.mesh, { view: $('view').value, pockets: $('pockets').checked });
        state.outName = base + '.svg';
      }
      show(r);
    } catch (err) {
      showError(err.message.endsWith('.') ? err.message : err.message + '.');
      $('download').disabled = true;
    } finally {
      busy(false);
    }
  }

  function show(r) {
    state.svg = r.svg;
    drop.hidden = true;
    $('result').hidden = false;
    $('filename').textContent = state.outName;
    const hasSource = Boolean(state.sourceImg);
    $('source').hidden = $('arrow').hidden = !hasSource;
    if (hasSource) { $('source-img').src = state.sourceImg; $('source-img').alt = state.sourceAlt; }
    $('summary').textContent = r.summary;
    const preview = $('preview');
    preview.innerHTML = r.svg;
    const svg = preview.querySelector('svg');
    svg.removeAttribute('width'); svg.removeAttribute('height');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Preview of ' + state.outName);
    $('legend').replaceChildren(...r.legend.map(l => {
      const li = document.createElement('li');
      const sw = document.createElement('i'); sw.style.background = l.color;
      li.append(sw, document.createTextNode(l.text));
      return li;
    }));
    $('warnings').replaceChildren(...r.warnings.map(w => { const li = document.createElement('li'); li.textContent = w; return li; }));
    $('download').disabled = false;
  }

  $('download').addEventListener('click', () => {
    if (!state.svg) return;
    const url = URL.createObjectURL(new Blob([state.svg], { type: 'image/svg+xml' }));
    const a = document.createElement('a');
    a.href = url; a.download = state.outName;
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  // ---- status -----------------------------------------------------------
  function busy(on) { $('status').hidden = !on; }
  function showError(msg) { const e = $('error'); e.textContent = msg; e.hidden = false; }
  function hideError() { $('error').hidden = true; }

  setMode('outline');
})();
