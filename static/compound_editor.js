/* Compound peak editor: place a compound's two MS2 peaks by hand and set the
   spectrum window around each one. Main app, chromatogram step.

   Choose peak 1 or 2 on the right, then click its peak on the chart: the click
   walks uphill on the raw MS2 TIC to the top, as the server will; Shift-click
   puts it on the clicked scan. A placed peak gets the automatic N x FWHM window
   again, measured on the smoothed trace exactly as the server measures it.

   A window can also be moved: drag its middle to slide it, or its edges (or type
   its times) to resize it. A window set that way decides its peak, which is the
   tallest raw scan inside it, so the peak travels with the window. Drag outside
   the windows to zoom.

   The chart is drawn on the standard ladder's time axis when there is one, but
   every time sent to the server is the run's own, so the windows pick the same
   scans whatever the fit.

   Uses nearestScan, climbUphill, niceStep, PE and SVG sizes from peak_editor.js.

   opts: trace() -> Promise of the trace JSON; save(body) -> Promise of the reply;
         close(reply|null) puts the figure back; alert(title, items). */

const CE_COLORS = { 1: "#6A3D9A", 2: "#B4441F" };   // as PEAK_COLORS in plotting.py
let CE_OPEN = null;           // compound number being edited; one at a time

function compoundEditorOpen() { return CE_OPEN; }
function resetCompoundEditor() { CE_OPEN = null; }

function openCompoundEditor(holder, n, opts) {
  if (CE_OPEN !== null && CE_OPEN !== n) {
    opts.alert("Finish the open peak edit first",
      ["Save or cancel the changes on the compound being edited before editing another."]);
    return;
  }
  const button = holder.querySelector("[data-role=edit-compound]");
  if (button) { button.disabled = true; button.textContent = "Loading the chromatogram…"; }
  opts.trace().then(t => {
    if (t.error) throw new Error(t.error);
    CE_OPEN = n;
    startCompoundEditor(holder, n, t, opts);
  }).catch(e => {
    if (button) { button.disabled = false; button.textContent = "Edit peaks and windows"; }
    opts.alert("Could not open the peak editor", [e.message]);
  });
}

/* ---------------------------------------------------------------- numbers */
/* Same walk as ltqindex.analysis.fwhm_at: down each side to half the top,
   stopping at a valley. */
function fwhmAt(rt, ys, apex) {
  const half = ys[apex] / 2;
  const edge = (step) => {
    let i = apex;
    while (i + step >= 0 && i + step < ys.length) {
      const j = i + step;
      if (ys[j] <= half) {
        const drop = ys[i] - ys[j];
        const f = drop > 0 ? (ys[i] - half) / drop : 0;
        return rt[i] + f * (rt[j] - rt[i]);
      }
      if (ys[j] > ys[i]) return rt[i];
      i = j;
    }
    return rt[i];
  };
  return edge(1) - edge(-1);
}

function interp(x, xs, ys) {
  const n = xs.length;
  if (x <= xs[0]) return ys[0] + (x - xs[0]) * (ys[1] - ys[0]) / (xs[1] - xs[0]);
  if (x >= xs[n - 1]) return ys[n - 1] + (x - xs[n - 1]) * (ys[n - 1] - ys[n - 2]) / (xs[n - 1] - xs[n - 2]);
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] <= x) lo = mid; else hi = mid;
  }
  return ys[lo] + (x - xs[lo]) * (ys[hi] - ys[lo]) / ((xs[hi] - xs[lo]) || 1);
}

/* ----------------------------------------------------------------- editor */
function startCompoundEditor(holder, n, t, opts) {
  const ed = {
    holder, n, t, opts,
    picks: {},                          // slot -> own rt, or null for no peak
    exact: new Set((t.exact || []).map(Number)),
    windows: {},                        // slot -> [start, end], own rt
    active: 1,
    shift: false,
    view: [t.axis[0], t.axis[t.axis.length - 1]],
    hoverX: null,
    drag: null,
  };
  for (const [k, v] of Object.entries(t.manual || {})) ed.picks[Number(k)] = v;
  for (const k of t.absent || []) ed.picks[Number(k)] = null;
  for (const [k, v] of Object.entries(t.windows || {})) ed.windows[Number(k)] = v.slice();
  // own time <-> chart time
  ed.toAxis = (r) => interp(r, t.rt, t.axis);
  ed.toOwn = (a) => interp(a, t.axis, t.rt);

  holder.classList.add("ce-editing");
  holder.innerHTML = `
    <div class="ce">
      <div class="ce-main peak-editor">
        <div class="pe-bar">
          <span class="pe-hint" data-role="hint"></span>
          <span class="pe-zoom">
            <button type="button" class="ghost small" data-role="zoom-peaks">Zoom to peaks</button>
            <button type="button" class="ghost small" data-role="zoom-all">Whole run</button>
          </span>
        </div>
        <svg viewBox="0 0 ${PE.W} ${PE.H}" class="pe-svg" data-role="svg"
          aria-label="MS2 chromatogram of ${escapeHtml(t.name)}; click to place the selected peak"></svg>
        <div class="pe-actions">
          <button type="button" class="ghost small" data-role="auto-all">Undo all hand edits</button>
          <span class="note" data-role="status"></span>
          <button type="button" class="ghost" data-role="cancel">Cancel</button>
          <button type="button" class="primary" data-role="save">Save peaks and windows</button>
        </div>
      </div>
      <div class="ce-side">
        <h4>${escapeHtml(t.name)}: choose a peak, then click it</h4>
        <p class="note">◆ placed by hand, ◇ placed exactly (Shift-click). Windows start
          at ${t.window_fwhm} × FWHM. Drag a window's middle to move it, its edges or the
          times below to resize it; the peak follows to the tallest point inside.
          Times are ${t.on_standard_axis ? "on the standard axis" : "the instrument's own"}.</p>
        <div data-role="rows"></div>
      </div>
    </div>`;

  ed.svg = holder.querySelector("[data-role=svg]");
  holder.querySelector("[data-role=zoom-all]").onclick = () => {
    ed.view = [t.axis[0], t.axis.at(-1)]; ceRender(ed);
  };
  holder.querySelector("[data-role=zoom-peaks]").onclick = () => { zoomToPeaks(ed); ceRender(ed); };
  holder.querySelector("[data-role=auto-all]").onclick = () => {
    ed.picks = {}; ed.windows = {}; ed.exact.clear();
    ceStatus(ed, "Both peaks and windows back to automatic (not saved yet)");
    ceRenderAll(ed);
  };
  holder.querySelector("[data-role=cancel]").onclick = () => closeCompoundEditor(ed, null);
  holder.querySelector("[data-role=save]").onclick = () => saveCompound(ed);

  ed.onKey = (ev) => {
    if (!document.body.contains(ed.svg)) { ceUnbind(ed); return; }
    if (ev.key === "Shift") { ed.shift = ev.type === "keydown"; ceRender(ed); return; }
    if (ev.type !== "keydown" || (ev.target.closest && ev.target.closest("input"))) return;
    if (ev.key === "1" || ev.key === "2") { ed.active = Number(ev.key); ceRenderAll(ed); }
    if (ev.key === "Escape") { ed.active = null; ceRenderAll(ed); }
  };
  document.addEventListener("keydown", ed.onKey);
  document.addEventListener("keyup", ed.onKey);
  ceBindPointer(ed);
  zoomToPeaks(ed);
  ceRenderAll(ed);
  holder.scrollIntoView({ behavior: "smooth", block: "start" });
}

function ceUnbind(ed) {
  document.removeEventListener("keydown", ed.onKey);
  document.removeEventListener("keyup", ed.onKey);
}

function closeCompoundEditor(ed, reply) {
  ceUnbind(ed);
  CE_OPEN = null;
  ed.opts.close(reply);
}

function ceStatus(ed, text) {
  const s = ed.holder.querySelector("[data-role=status]");
  if (s) s.textContent = text || "";
}

/* Each present peak right now: its top scan and its window, in own time. */
/* The tallest raw scan inside a window (own times), as the server picks it. */
function tallestIn(t, w) {
  let best = null;
  for (let i = Math.max(0, nearestScan(t.rt, w[0]) - 1);
       i < t.rt.length && t.rt[i] <= w[1]; i++) {
    if (t.rt[i] < w[0]) continue;
    if (best === null || t.tic[i] > t.tic[best]) best = i;
  }
  return best;
}

function cePeaks(ed) {
  const t = ed.t, out = {};
  for (const slot of [1, 2]) {
    if (slot in ed.picks && ed.picks[slot] === null) continue;
    const autoTop = t.auto[slot] !== undefined ? nearestScan(t.rt, t.auto[slot]) : null;
    let top, manualPeak;
    if (ed.windows[slot]) {
      // a window set by hand carries its peak: the tallest scan inside it
      top = tallestIn(t, ed.windows[slot]);
      if (top === null) continue;
      manualPeak = top !== autoTop;
    } else if (slot in ed.picks) {
      top = nearestScan(t.rt, ed.picks[slot]);
      manualPeak = true;
    } else if (autoTop !== null) {
      top = autoTop;
      manualPeak = false;
    } else continue;
    const width = fwhmAt(t.rt, t.smooth, climbUphill(t.smooth, top)) ||
      2 * (t.rt[1] - t.rt[0]);
    const half = t.window_fwhm * width / 2;
    const auto = [Math.max(t.rt[0], t.rt[top] - half), Math.min(t.rt.at(-1), t.rt[top] + half)];
    out[slot] = {
      top, rt: t.rt[top], fwhm: width, auto,
      window: ed.windows[slot] || auto,
      manualWindow: !!ed.windows[slot],
      manualPeak,
    };
  }
  return out;
}

function ceIndex(ed, slot, pk) {
  const sc = ed.t.index_scale;
  if (sc) return interp(ed.toAxis(pk.rt), sc[0], sc[1]);
  // no standard: the server's ladder index, known only for an unmoved peak
  const was = ed.t.peaks[slot];
  return was && Math.abs(was.rt - pk.rt) < 1e-6 ? was.index : null;
}

function zoomToPeaks(ed) {
  const pk = Object.values(cePeaks(ed));
  if (!pk.length) return;
  const a = ed.toAxis(Math.min(...pk.map(p => p.window[0])));
  const b = ed.toAxis(Math.max(...pk.map(p => p.window[1])));
  const pad = Math.max((b - a) * 1.5, 1);
  ed.view = [Math.max(ed.t.axis[0], a - pad), Math.min(ed.t.axis.at(-1), b + pad)];
}

function ceRenderAll(ed) { ceRenderRows(ed); ceRender(ed); }

function ceRenderRows(ed) {
  const peaks = cePeaks(ed), box = ed.holder.querySelector("[data-role=rows]");
  box.innerHTML = "";
  for (const slot of [1, 2]) {
    const pk = peaks[slot];
    const absent = (slot in ed.picks) && ed.picks[slot] === null;
    const row = document.createElement("div");
    row.className = "ce-row" + (ed.active === slot ? " active" : "") + (absent ? " absent" : "");
    const idx = pk ? ceIndex(ed, slot, pk) : null;
    const mark = pk && pk.manualPeak
      ? (ed.exact.has(slot) && !pk.manualWindow ? "◇" : "◆") : "";
    const fmt = (v) => ed.toAxis(v).toFixed(2);
    row.innerHTML = `
      <div class="ce-row-head">
        <i style="background:${CE_COLORS[slot]}"></i><b>Peak ${slot}</b>
        <span class="mark">${mark}</span>
        <span class="ce-rt">${absent ? "no peak" : pk ? `${fmt(pk.rt)} min` : "none found"}</span>
        <button type="button" class="pe-x" data-act="absent" title="${absent
          ? "Use a peak here again" : "This compound has no peak " + slot}">${absent ? "+" : "×"}</button>
        <button type="button" class="pe-x" data-act="auto" title="Back to the automatic peak"
          ${slot in ed.picks || ed.windows[slot] ? "" : "disabled"}>↺</button>
      </div>
      ${pk ? `
      <div class="ce-row-body">
        <span class="ce-k">index</span>
        <span class="mono">${idx === null || idx === undefined ? "on save" : idx.toFixed(2)}</span>
        <span class="ce-k">window</span>
        <span class="ce-win">
          <input type="number" step="0.01" class="mono" data-edge="0" value="${fmt(pk.window[0])}">
          –
          <input type="number" step="0.01" class="mono" data-edge="1" value="${fmt(pk.window[1])}">
        </span>
        <span class="ce-k">width</span>
        <span class="mono">${((pk.window[1] - pk.window[0]) / pk.fwhm).toFixed(2)} × FWHM
          ${pk.manualWindow ? `<button type="button" class="ce-reset" data-act="window"
            title="Back to ${ed.t.window_fwhm} × FWHM">↺ ${ed.t.window_fwhm}×</button>` : ""}</span>
      </div>` : ""}`;
    row.onclick = (ev) => {
      if (ev.target.closest("input, button")) return;
      ed.active = ed.active === slot ? null : slot;
      ceRenderAll(ed);
    };
    row.querySelector("[data-act=absent]").onclick = () => {
      ceForget(ed, slot);
      if (!absent) ed.picks[slot] = null;
      ceStatus(ed, absent ? `Peak ${slot} uses its automatic peak again` : `Peak ${slot} marked as absent`);
      ceRenderAll(ed);
    };
    row.querySelector("[data-act=auto]").onclick = () => {
      ceForget(ed, slot);
      ceStatus(ed, `Peak ${slot} back to its automatic peak`);
      ceRenderAll(ed);
    };
    const reset = row.querySelector("[data-act=window]");
    if (reset) reset.onclick = () => {
      // the peak stays where the window put it; only the width goes back
      delete ed.windows[slot];
      if (pk.manualPeak) {
        ed.picks[slot] = Math.round(ed.t.rt[pk.top] * 1e5) / 1e5;
        ed.exact.add(slot);
      }
      ceStatus(ed, `Window ${slot} back to ${ed.t.window_fwhm} × FWHM`);
      ceRenderAll(ed);
    };
    for (const input of row.querySelectorAll("input[data-edge]")) {
      input.onchange = () => {
        const v = Number(input.value);
        if (!Number.isFinite(v)) return ceRenderAll(ed);
        setEdge(ed, slot, pk, Number(input.dataset.edge), ed.toOwn(v));
        ceRenderAll(ed);
      };
    }
    box.appendChild(row);
  }

  const hint = ed.holder.querySelector("[data-role=hint]");
  hint.innerHTML = ed.active
    ? `Placing <b>peak ${ed.active}</b>. Click its top; Shift-click to place it exactly.
       Drag a window's middle to move it, an edge to resize it; drag outside to zoom.`
    : `Choose peak 1 or 2 on the right (or press 1 / 2), then click on the chart.`;
}

function ceForget(ed, slot) {
  delete ed.picks[slot];
  delete ed.windows[slot];
  ed.exact.delete(slot);
}

/* A window set by hand: from now on it decides where its peak is. */
function setWindow(ed, slot, w) {
  ed.windows[slot] = w.map(v => Math.round(v * 1e5) / 1e5);
  delete ed.picks[slot];
  ed.exact.delete(slot);
}

/* Move one window edge; the window stays at least a couple of scans wide. */
function setEdge(ed, slot, pk, side, own) {
  const w = (ed.windows[slot] || pk.window).slice();
  const step = 2 * ((ed.t.rt[1] - ed.t.rt[0]) || 0.01);
  own = Math.min(Math.max(own, ed.t.rt[0]), ed.t.rt.at(-1));
  w[side] = side === 0 ? Math.min(own, w[1] - step) : Math.max(own, w[0] + step);
  setWindow(ed, slot, w);
}

/* Slide a whole window by `d` minutes on the chart's axis, from where the
   drag started; it keeps its width and stays on the trace. */
function moveWindow(ed, slot, base, d) {
  const ax = ed.t.axis, a = ed.toAxis(base[0]), b = ed.toAxis(base[1]);
  d = Math.min(Math.max(d, ax[0] - a), ax.at(-1) - b);
  setWindow(ed, slot, [ed.toOwn(a + d), ed.toOwn(b + d)]);
}

function ceRender(ed) {
  const { L, R, T, B } = PE;
  const W = Math.max(420, Math.round(ed.svg.getBoundingClientRect().width || PE.W));
  const H = PE.H;
  if (ed.W !== W) { ed.W = W; ed.svg.setAttribute("viewBox", `0 0 ${W} ${H}`); }
  const t = ed.t, ax = t.axis, y = t.tic, [x0, x1] = ed.view;
  const i0 = Math.max(0, nearestScan(ax, x0) - 1), i1 = Math.min(ax.length - 1, nearestScan(ax, x1) + 1);
  let top = 0;
  for (let i = i0; i <= i1; i++) top = Math.max(top, y[i]);
  top = (top || 1) * 1.18;
  const sx = (v) => L + (v - x0) / (x1 - x0) * (W - L - R);
  const sy = (v) => T + (1 - v / top) * (H - T - B);
  ed.invX = (px) => x0 + (px - L) / (W - L - R) * (x1 - x0);

  const parts = [`<defs><clipPath id="ce-clip"><rect x="${L}" y="${T - 14}"
    width="${W - L - R}" height="${H - T - B + 14}"/></clipPath></defs>`];
  const xs = niceStep(x1 - x0, 8);
  for (let v = Math.ceil(x0 / xs) * xs; v <= x1 + 1e-9; v += xs) {
    parts.push(`<line x1="${sx(v)}" x2="${sx(v)}" y1="${T}" y2="${H - B}" class="pe-grid"/>`,
      `<text x="${sx(v)}" y="${H - B + 15}" class="pe-tick" text-anchor="middle">${+v.toFixed(2)}</text>`);
  }
  const exp = top > 0 ? 3 * Math.floor(Math.log10(top) / 3) : 0;
  const ys = niceStep(top, 4);
  for (let v = 0; v <= top; v += ys) {
    parts.push(`<line x1="${L}" x2="${W - R}" y1="${sy(v)}" y2="${sy(v)}" class="pe-grid"/>`,
      `<text x="${L - 6}" y="${sy(v) + 3.5}" class="pe-tick" text-anchor="end">${+(v / 10 ** exp).toFixed(2)}</text>`);
  }
  parts.push(`<text x="${(L + W - R) / 2}" y="${H - 4}" class="pe-label" text-anchor="middle">${escapeHtml(t.axis_label)}</text>`,
    `<text x="12" y="${(T + H - B) / 2}" class="pe-label" text-anchor="middle"
      transform="rotate(-90 12 ${(T + H - B) / 2})">MS2 TIC${exp ? ` (×10^${exp})` : ""}</text>`,
    `<line x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}" class="pe-axis"/>`);

  const g = [`<g clip-path="url(#ce-clip)">`];
  // windows under the trace, with grab handles on their edges
  const peaks = cePeaks(ed);
  ed.edges = [];
  ed.bands = [];
  for (const [k, pk] of Object.entries(peaks)) {
    const slot = Number(k), c = CE_COLORS[slot];
    const a = sx(ed.toAxis(pk.window[0])), b = sx(ed.toAxis(pk.window[1]));
    const on = ed.drag && ed.drag.edge && ed.drag.edge.slot === slot;
    ed.bands.push({ slot, a, b });
    g.push(`<rect x="${a}" y="${T}" width="${Math.max(1, b - a)}" height="${H - T - B}"
      fill="${c}" fill-opacity="${pk.manualWindow ? 0.17 : 0.1}"/>`);
    for (const [side, px] of [[0, a], [1, b]]) {
      g.push(`<line x1="${px}" x2="${px}" y1="${T}" y2="${H - B}" stroke="${c}"
        stroke-width="${on && ed.drag.edge.side === side ? 2.4 : 1.4}"
        stroke-dasharray="${pk.manualWindow ? "none" : "4 3"}"/>`,
        `<rect x="${px - 4}" y="${(T + H - B) / 2 - 12}" width="8" height="24" rx="2"
          fill="#fff" stroke="${c}" stroke-width="1.2" class="ce-handle"/>`);
      ed.edges.push({ slot, side, px });
    }
  }

  // the trace, thinned to at most two points per SVG unit
  const pts = [];
  let bucket = null, loI = 0, hiI = 0;
  const flush = () => {
    if (bucket === null) return;
    for (const i of new Set([loI, hiI].sort((p, q) => p - q)))
      pts.push(`${sx(ax[i]).toFixed(1)},${sy(y[i]).toFixed(1)}`);
  };
  for (let i = i0; i <= i1; i++) {
    const b = Math.floor(sx(ax[i]));
    if (b !== bucket) { flush(); bucket = b; loI = hiI = i; }
    else { if (y[i] < y[loI]) loI = i; if (y[i] > y[hiI]) hiI = i; }
  }
  flush();
  g.push(`<polyline points="${pts.join(" ")}" fill="none" stroke="#33415C" stroke-width="1.2"/>`);

  // peak markers
  ed.markers = [];
  for (const [k, pk] of Object.entries(peaks)) {
    const slot = Number(k), c = CE_COLORS[slot];
    const px = sx(ed.toAxis(pk.rt)), py = Math.max(sy(y[pk.top]), T);
    const shape = pk.manualPeak
      ? `<path d="M${px} ${py - 9} l4.5 4.5 -4.5 4.5 -4.5 -4.5z" fill="${c}"/>`
      : `<path d="M${px - 4} ${py - 8} h8 l-4 7z" fill="${c}"/>`;
    g.push(`<g>${slot === ed.active ? `<circle cx="${px}" cy="${py - 4.5}" r="11" class="pe-ring"/>` : ""}
      ${shape}<text x="${px}" y="${py - 13}" text-anchor="middle" fill="${c}" class="pe-num">${slot}</text></g>`);
    ed.markers.push({ slot, px, py: py - 10 });
  }

  // hover guide and where a click would land
  if (ed.hoverX !== null && !ed.drag) {
    const time = ed.invX(ed.hoverX);
    const edge = edgeAt(ed, ed.hoverX);
    g.push(`<line x1="${ed.hoverX}" x2="${ed.hoverX}" y1="${T}" y2="${H - B}" class="pe-hover"/>`);
    // one readout, top right, so the two never overlap
    if (!edge && !ed.active && !bandAt(ed, ed.hoverX)) {
      parts.push(`<text x="${W - R}" y="${T - 8}" text-anchor="end" class="pe-readout">${time.toFixed(2)} min</text>`);
    }
    if (edge) {
      parts.push(`<text x="${W - R}" y="${T - 8}" text-anchor="end" class="pe-readout">
        ${time.toFixed(2)} min: drag to move the ${edge.side ? "end" : "start"} of window ${edge.slot}</text>`);
    } else if (bandAt(ed, ed.hoverX) && !ed.active) {
      parts.push(`<text x="${W - R}" y="${T - 8}" text-anchor="end" class="pe-readout">
        ${time.toFixed(2)} min: drag to move window ${bandAt(ed, ed.hoverX).slot}</text>`);
    } else if (ed.active) {
      const s = ceSnap(ed, time, ed.shift);
      g.push(`<circle cx="${sx(ax[s])}" cy="${sy(y[s])}" r="5.5" class="pe-snap"/>`);
      parts.push(`<text x="${W - R}" y="${T - 8}" text-anchor="end" class="pe-readout">
        ${ed.shift ? "shift-click puts" : "click puts"} peak ${ed.active} at ${ax[s].toFixed(2)} min${ed.shift ? " exactly" : ""}</text>`);
    }
  }
  if (ed.drag && ed.drag.moved && !ed.drag.edge) {
    const a = Math.min(ed.drag.start, ed.drag.now), b = Math.max(ed.drag.start, ed.drag.now);
    g.push(`<rect x="${a}" y="${T}" width="${b - a}" height="${H - T - B}" class="pe-zoombox"/>`);
  }
  g.push(`</g>`);
  ed.svg.innerHTML = parts.join("") + g.join("");
  const hx = ed.hoverX;
  ed.svg.style.cursor = ed.drag && ed.drag.band && ed.drag.moved ? "grabbing"
    : hx !== null && edgeAt(ed, hx) ? "ew-resize"
    : hx !== null && bandAt(ed, hx) ? "grab" : "";
}

/* The window whose middle is under x: the one with the nearest centre. */
function bandAt(ed, x) {
  const hit = (ed.bands || []).filter(b => x > b.a && x < b.b)
    .sort((p, q) => Math.abs(x - (p.a + p.b) / 2) - Math.abs(x - (q.a + q.b) / 2))[0];
  return hit || null;
}

function edgeAt(ed, x) {
  const hit = (ed.edges || []).map(e => ({ e, d: Math.abs(e.px - x) }))
    .filter(h => h.d <= 6).sort((a, b) => a.d - b.d)[0];
  return hit ? hit.e : null;
}

function ceSnap(ed, axisTime, exact) {
  const start = nearestScan(ed.t.axis, axisTime);
  return exact ? start : climbUphill(ed.t.tic, start);
}

function ceSvgX(ed, ev) {
  const box = ed.svg.getBoundingClientRect();
  return (ev.clientX - box.left) * (ed.W || PE.W) / box.width;
}
function ceSvgY(ed, ev) {
  const box = ed.svg.getBoundingClientRect();
  return (ev.clientY - box.top) * PE.H / box.height;
}

function ceBindPointer(ed) {
  const svg = ed.svg;
  svg.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    svg.setPointerCapture(ev.pointerId);
    const x = ceSvgX(ed, ev);
    const edge = edgeAt(ed, x), band = edge ? null : bandAt(ed, x);
    const pk = band ? cePeaks(ed)[band.slot] : null;
    ed.drag = { start: x, now: x, y: ceSvgY(ed, ev), moved: false, edge,
                band: pk ? band : null, base: pk ? pk.window.slice() : null };
  });
  svg.addEventListener("pointermove", (ev) => {
    const x = ceSvgX(ed, ev);
    ed.shift = ev.shiftKey;
    ed.hoverX = x >= PE.L && x <= ed.W - PE.R ? x : null;
    const d = ed.drag;
    if (d) {
      d.now = x;
      if (Math.abs(x - d.start) > (d.edge ? 1 : d.band ? 3 : 6)) d.moved = true;
      if (d.band && d.moved) {
        moveWindow(ed, d.band.slot, d.base, ed.invX(x) - ed.invX(d.start));
        ceRenderAll(ed);
        return;
      }
      if (d.edge && d.moved) {
        const pk = cePeaks(ed)[d.edge.slot];
        if (pk) setEdge(ed, d.edge.slot, pk, d.edge.side, ed.toOwn(ed.invX(x)));
        ceRenderAll(ed);
        return;
      }
    }
    ceRender(ed);
  });
  svg.addEventListener("pointerleave", () => { ed.hoverX = null; ceRender(ed); });
  svg.addEventListener("pointerup", (ev) => {
    const d = ed.drag;
    ed.drag = null;
    if (!d) return;
    if (d.edge) {
      if (d.moved) ceStatus(ed, `Window ${d.edge.slot} changed (not saved yet)`);
      ceRenderAll(ed);
      return;
    }
    if (d.band && d.moved) {
      const pk = cePeaks(ed)[d.band.slot];
      ceStatus(ed, pk ? `Window ${d.band.slot} moved; peak ${d.band.slot} at `
        + `${ed.toAxis(pk.rt).toFixed(2)} min (not saved yet)` : "");
      ceRenderAll(ed);
      return;
    }
    if (d.moved) {
      const a = ed.invX(Math.min(d.start, d.now)), b = ed.invX(Math.max(d.start, d.now));
      if (b - a > 0.02) ed.view = [Math.max(a, ed.t.axis[0]), Math.min(b, ed.t.axis.at(-1))];
      ceRender(ed);
      return;
    }
    ceClick(ed, d.start, d.y, ev.shiftKey);
  });
}

function ceClick(ed, x, y, exact) {
  // clicking a peak's number chooses that peak
  const hit = (ed.markers || []).map(mk => ({ mk, d: Math.hypot(mk.px - x, mk.py - y) }))
    .filter(h => h.d < 14).sort((a, b) => a.d - b.d)[0];
  if (hit && hit.mk.slot !== ed.active) { ed.active = hit.mk.slot; ceRenderAll(ed); return; }
  if (!ed.active || x < PE.L || x > ed.W - PE.R) return;
  const s = ceSnap(ed, ed.invX(x), exact);
  const own = ed.t.rt[s], slot = ed.active, auto = ed.t.auto[slot];
  ceForget(ed, slot);           // a moved peak gets the automatic window again
  if (exact || auto === undefined || Math.abs(auto - own) >= 1e-4) {
    ed.picks[slot] = Math.round(own * 1e5) / 1e5;
    if (exact) ed.exact.add(slot);
  }
  ceStatus(ed, `Peak ${slot} at ${ed.t.axis[s].toFixed(2)} min${exact ? ", exactly" : ""} (not saved yet)`);
  ceRenderAll(ed);
}

function saveCompound(ed) {
  const button = ed.holder.querySelector("[data-role=save]");
  button.disabled = true;
  ceStatus(ed, "Saving and re-plotting…");
  const peaks = Object.fromEntries(Object.entries(ed.picks).map(([k, v]) =>
    [k, v !== null && ed.exact.has(Number(k)) ? { rt: v, exact: true } : v]));
  ed.opts.save({ peaks, windows: ed.windows }).then(d => {
    if (d.error) throw new Error(d.error);
    closeCompoundEditor(ed, d);
  }).catch(e => {
    button.disabled = false;
    ceStatus(ed, "");
    ed.opts.alert(`Could not save the peaks for ${ed.t.name}`, [e.message]);
  });
}
