/* Peak editor: place ladder peaks by hand on one ladder card. Used by both the
   calibration app (page 1) and the main app (ladder step).

   Pick an index in the list, then click its peak on the chart. A click walks
   uphill on that index's own raw mass trace, the line drawn, to the top of the
   peak it landed on; the server does the same walk when the picks are saved, so
   the peak goes where it is shown. Shift-click puts the peak on the clicked scan
   with no walk at all. Drag across the chart to zoom; "Whole run" zooms back out.

   The page supplies what differs between the two apps as `opts`:
     traces()        -> Promise of the ladder's traces JSON
     save(picks)     -> Promise of the server's reply after saving the picks
     rebuild(l)      -> a fresh card element for ladder entry `l` (on Cancel)
     onSaved(card, d) after a save: swap in the re-analysed ladder
     alert(title, items) to show a problem */

const LADDER_COLORS = [
  "#0B5D6E", "#0E7C86", "#2E9B8F", "#6BB187",
  "#A8C06B", "#D9BE55", "#E08A3C", "#C6532C",
];
// margins in SVG units; the width follows the card so text stays at its real size
const PE = { W: 900, H: 340, L: 58, R: 14, T: 22, B: 36 };
const SVG_NS = "http://www.w3.org/2000/svg";

let PE_OPEN = null;        // name of the ladder being edited; one at a time

function peakEditorOpen() { return PE_OPEN; }
// a page that rebuilds every card at once forgets the open editor with it
function resetPeakEditor() { PE_OPEN = null; }

function openPeakEditor(card, l, opts) {
  if (PE_OPEN && PE_OPEN !== l.name) {
    opts.alert("Finish the open peak edit first",
      [`Save or cancel the peak changes on ${PE_OPEN} before editing another ladder.`]);
    return;
  }
  const button = card.querySelector("[data-role=edit-peaks]");
  if (button) { button.disabled = true; button.textContent = "Loading traces…"; }
  opts.traces().then(t => {
    if (t.error) throw new Error(t.error);
    PE_OPEN = l.name;
    startEditor(card, l, t, opts);
  }).catch(e => {
    if (button) { button.disabled = false; button.textContent = "Edit peaks"; }
    opts.alert(`Could not open the peak editor for ${l.name}`, [e.message]);
  });
}

/* --------------------------------------------------------------- numbers */
function nearestScan(rt, t) {
  let lo = 0, hi = rt.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (rt[mid] < t) lo = mid; else hi = mid;
  }
  return Math.abs(rt[lo] - t) <= Math.abs(rt[hi] - t) ? lo : hi;
}

function climbUphill(ys, start) {
  let i = Math.min(Math.max(start, 0), ys.length - 1);
  for (;;) {
    const left = i > 0 ? ys[i - 1] : -Infinity;
    const right = i + 1 < ys.length ? ys[i + 1] : -Infinity;
    if (right > ys[i] && right >= left) i += 1;
    else if (left > ys[i]) i -= 1;
    else return i;
  }
}

function niceStep(span, target) {
  const raw = span / target, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / mag;
  return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * mag;
}

/* ---------------------------------------------------------------- editor */
function startEditor(card, l, t, opts) {
  const n = t.rt.length;
  const ed = {
    card, l, t, opts,
    expected: l.expected,
    picks: {},            // index -> rt placed by hand, or null for "no peak"
    exact: new Set(l.exact || []),   // picks put on the clicked scan, unsnapped
    shift: false,
    active: null,
    view: [t.rt[0], t.rt[n - 1]],
    hoverX: null,
    drag: null,
  };
  for (const [k, v] of Object.entries(l.manual || {})) ed.picks[Number(k)] = v;
  for (const i of l.absent || []) ed.picks[Number(i)] = null;

  const plot = card.querySelector(".ladder-plot");
  plot.innerHTML = `
    <div class="peak-editor">
      <div class="pe-bar">
        <span class="pe-hint" data-role="hint"></span>
        <span class="pe-zoom">
          <button type="button" class="ghost small" data-role="zoom-out">Zoom out</button>
          <button type="button" class="ghost small" data-role="zoom-all">Whole run</button>
        </span>
      </div>
      <svg viewBox="0 0 ${PE.W} ${PE.H}" class="pe-svg" data-role="svg"
        aria-label="Ladder traces; click to place the selected index"></svg>
      <div class="pe-legend" data-role="legend"></div>
      <div class="pe-actions">
        <button type="button" class="ghost small" data-role="auto-all">Undo all hand placement</button>
        <span class="note" data-role="status"></span>
        <button type="button" class="ghost" data-role="cancel">Cancel</button>
        <button type="button" class="primary" data-role="save">Save peaks</button>
      </div>
    </div>`;
  const side = card.querySelector(".ladder-indexes");
  side.innerHTML = `
    <h4>Choose an index, then click its peak</h4>
    <p class="note">◆ placed by hand, ◇ placed exactly (Shift-click). Red means not
    later than the index before it.</p>
    <div class="pe-list" data-role="pe-list"></div>`;
  card.classList.add("editing");

  ed.svg = plot.querySelector("[data-role=svg]");
  plot.querySelector("[data-role=legend]").innerHTML = t.traces.map((tr, m) =>
    `<span><i style="background:${LADDER_COLORS[m % 8]}"></i>m/z ${tr.mass}
     <em>${2 * m + 1}–${2 * m + 2}</em></span>`).join("");
  plot.querySelector("[data-role=zoom-out]").onclick = () => zoomOut(ed);
  plot.querySelector("[data-role=zoom-all]").onclick = () => {
    ed.view = [t.rt[0], t.rt[n - 1]]; render(ed);
  };
  plot.querySelector("[data-role=auto-all]").onclick = () => {
    ed.picks = {}; ed.exact.clear(); status(ed, "Every index back to its automatic peak (not saved yet)");
    renderAll(ed);
  };
  plot.querySelector("[data-role=cancel]").onclick = () => closeEditor(ed);
  plot.querySelector("[data-role=save]").onclick = () => savePeaks(ed);
  ed.onKey = (ev) => {
    if (!document.body.contains(ed.svg)) {
      unbindKeys(ed);
      return;
    }
    if (ev.key === "Shift") {
      // the hover ring follows Shift at once, without waiting for the mouse
      ed.shift = ev.type === "keydown";
      render(ed);
      return;
    }
    if (ev.type !== "keydown") return;
    if (ev.target.closest && ev.target.closest("input, textarea")) return;
    if (ev.key === "Escape") { ed.active = null; renderAll(ed); }
    if ((ev.key === "ArrowDown" || ev.key === "ArrowUp") && ed.active) {
      ev.preventDefault();
      const next = ed.active + (ev.key === "ArrowDown" ? 1 : -1);
      if (next >= 1 && next <= ed.expected) { ed.active = next; renderAll(ed); }
    }
  };
  document.addEventListener("keydown", ed.onKey);
  document.addEventListener("keyup", ed.onKey);
  bindPointer(ed);
  renderAll(ed);
  card.scrollIntoView({ behavior: "smooth", block: "start" });
}

function unbindKeys(ed) {
  document.removeEventListener("keydown", ed.onKey);
  document.removeEventListener("keyup", ed.onKey);
}

function closeEditor(ed, entry) {
  unbindKeys(ed);
  PE_OPEN = null;
  const fresh = ed.opts.rebuild(entry || ed.l);
  ed.card.replaceWith(fresh);
  return fresh;
}

function status(ed, text) {
  const s = ed.card.querySelector("[data-role=status]");
  if (s) s.textContent = text || "";
}

/* Where each index sits right now: a hand pick, else the automatic peak. */
function positions(ed) {
  const pos = {};
  for (let i = 1; i <= ed.expected; i++) {
    if (i in ed.picks) {
      if (ed.picks[i] !== null) pos[i] = ed.picks[i];
    } else if (ed.l.auto_peaks && ed.l.auto_peaks[i] !== undefined) {
      pos[i] = ed.l.auto_peaks[i];
    }
  }
  return pos;
}

function outOfOrder(pos) {
  const bad = new Set();
  const idx = Object.keys(pos).map(Number).sort((a, b) => a - b);
  for (let k = 1; k < idx.length; k++) {
    if (pos[idx[k]] <= pos[idx[k - 1]]) bad.add(idx[k]);
  }
  return bad;
}

const massOf = (index) => Math.floor((index - 1) / 2);
const isManual = (ed, i) => (i in ed.picks) && ed.picks[i] !== null;

/* The scan a click at `time` puts `index` on: the top of the raw peak under the
   click, or with `exact` the clicked scan itself. */
function snapTo(ed, index, time, exact) {
  const rt = ed.t.rt, start = nearestScan(rt, time);
  return exact ? start : climbUphill(ed.t.traces[massOf(index)].intensity, start);
}

const forget = (ed, i) => { delete ed.picks[i]; ed.exact.delete(i); };

function renderAll(ed) { renderList(ed); render(ed); }

function renderList(ed) {
  const pos = positions(ed), bad = outOfOrder(pos);
  const list = ed.card.querySelector("[data-role=pe-list]");
  list.innerHTML = "";
  for (let i = 1; i <= ed.expected; i++) {
    const m = massOf(i);
    const row = document.createElement("div");
    const absent = (i in ed.picks) && ed.picks[i] === null;
    row.className = "pe-row" + (ed.active === i ? " active" : "") +
      (bad.has(i) ? " bad" : "") + (absent ? " absent" : "");
    row.innerHTML = `
      <i style="background:${LADDER_COLORS[m % 8]}"></i>
      <span class="idx">${i}</span>
      <span class="rt">${absent ? "no peak" : pos[i] !== undefined ? pos[i].toFixed(2) : "—"}</span>
      <span class="mark">${isManual(ed, i) ? (ed.exact.has(i) ? "◇" : "◆") : ""}</span>
      <button type="button" class="pe-x" title="${absent ? "Use a peak for this index again"
        : "This index has no peak"}">${absent ? "+" : "×"}</button>
      <button type="button" class="pe-x" title="Back to the automatic peak"
        ${i in ed.picks ? "" : "disabled"}>↺</button>`;
    row.title = `Index ${i}, m/z ${ed.t.traces[m].mass}`;
    row.onclick = () => { ed.active = ed.active === i ? null : i; renderAll(ed); };
    const [absentBtn, autoBtn] = row.querySelectorAll("button");
    absentBtn.onclick = (ev) => {
      ev.stopPropagation();
      forget(ed, i);
      if (!absent) ed.picks[i] = null;
      status(ed, absent ? `Index ${i} uses its automatic peak again`
                        : `Index ${i} marked as having no peak`);
      renderAll(ed);
    };
    autoBtn.onclick = (ev) => {
      ev.stopPropagation();
      forget(ed, i);
      status(ed, `Index ${i} back to its automatic peak`);
      renderAll(ed);
    };
    list.appendChild(row);
  }

  const hint = ed.card.querySelector("[data-role=hint]");
  if (ed.active) {
    hint.innerHTML = `Placing <b>index ${ed.active}</b> on m/z ${ed.t.traces[massOf(ed.active)].mass}.
      Click its peak; Shift-click to place it exactly. Drag to zoom. Esc to stop.`;
  } else {
    const first = [...bad][0];
    hint.innerHTML = first
      ? `<span class="pe-bad">Index ${first} is not later than the index before it.</span>
         Choose an index on the right, or click a number on the chart.`
      : `Retention times rise with index. Choose an index on the right, or click a number on the chart.`;
  }
}

function visibleRange(ed) {
  const rt = ed.t.rt;
  return [Math.max(0, nearestScan(rt, ed.view[0]) - 1),
          Math.min(rt.length - 1, nearestScan(rt, ed.view[1]) + 1)];
}

function render(ed) {
  const { L, R, T, B } = PE;
  const W = Math.max(420, Math.round(ed.svg.getBoundingClientRect().width || PE.W));
  const H = PE.H;
  if (ed.W !== W) { ed.W = W; ed.svg.setAttribute("viewBox", `0 0 ${W} ${H}`); }
  const rt = ed.t.rt, [x0, x1] = ed.view;
  const [i0, i1] = visibleRange(ed);
  const activeMass = ed.active ? massOf(ed.active) : null;

  // y axis fits the selected trace, so a weak late mass is not drawn flat
  let top = 0;
  ed.t.traces.forEach((tr, m) => {
    if (activeMass !== null && m !== activeMass) return;
    for (let i = i0; i <= i1; i++) top = Math.max(top, tr.intensity[i]);
  });
  top = (top || 1) * 1.15;
  const sx = (v) => L + (v - x0) / (x1 - x0) * (W - L - R);
  const sy = (v) => T + (1 - v / top) * (H - T - B);
  ed.sx = sx; ed.invX = (px) => x0 + (px - L) / (W - L - R) * (x1 - x0);

  const parts = [];
  parts.push(`<defs><clipPath id="pe-clip"><rect x="${L}" y="${T - 14}"
    width="${W - L - R}" height="${H - T - B + 14}"/></clipPath></defs>`);

  // axes
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
  parts.push(`<text x="${(L + W - R) / 2}" y="${H - 4}" class="pe-label" text-anchor="middle">Retention time (min)</text>`,
    `<text x="12" y="${(T + H - B) / 2}" class="pe-label" text-anchor="middle"
      transform="rotate(-90 12 ${(T + H - B) / 2})">Intensity${exp ? ` (×10^${exp})` : ""}</text>`,
    `<line x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}" class="pe-axis"/>`);

  // traces, thinned to at most two points per SVG unit
  const g = [`<g clip-path="url(#pe-clip)">`];
  const order = ed.t.traces.map((_, m) => m).filter(m => m !== activeMass);
  if (activeMass !== null) order.push(activeMass);
  for (const m of order) {
    const y = ed.t.traces[m].intensity, pts = [];
    let bucket = null, lo = 0, hi = 0, loI = 0, hiI = 0;
    const flush = () => {
      if (bucket === null) return;
      const a = [loI, hiI].sort((p, q) => p - q);
      for (const i of new Set(a)) pts.push(`${sx(rt[i]).toFixed(1)},${sy(y[i]).toFixed(1)}`);
    };
    for (let i = i0; i <= i1; i++) {
      const b = Math.floor(sx(rt[i]));
      if (b !== bucket) { flush(); bucket = b; lo = hi = y[i]; loI = hiI = i; }
      else {
        if (y[i] < lo) { lo = y[i]; loI = i; }
        if (y[i] > hi) { hi = y[i]; hiI = i; }
      }
    }
    flush();
    const faded = activeMass !== null && m !== activeMass;
    g.push(`<polyline points="${pts.join(" ")}" fill="none" stroke="${LADDER_COLORS[m % 8]}"
      stroke-width="${m === activeMass ? 1.6 : 1}" stroke-opacity="${faded ? 0.14 : 1}"/>`);
  }

  // candidate peaks on the selected trace
  if (activeMass !== null) {
    for (const [ct, ch] of ed.t.traces[activeMass].candidates) {
      if (ct < x0 || ct > x1) continue;
      g.push(`<circle cx="${sx(ct)}" cy="${sy(ch)}" r="4.5" class="pe-cand"/>`);
    }
  }

  // index markers
  const pos = positions(ed), bad = outOfOrder(pos);
  ed.markers = [];
  for (const [k, time] of Object.entries(pos)) {
    const i = Number(k), m = massOf(i);
    if (time < x0 || time > x1) continue;
    const y = ed.t.traces[m].intensity[nearestScan(rt, time)];
    const px = sx(time), py = Math.max(sy(y), T);
    const faded = activeMass !== null && m !== activeMass;
    const color = bad.has(i) ? "#B4441F" : LADDER_COLORS[m % 8];
    const shape = isManual(ed, i)
      ? `<path d="M${px} ${py - 9} l4.5 4.5 -4.5 4.5 -4.5 -4.5z" fill="${color}"/>`
      : `<path d="M${px - 4} ${py - 8} h8 l-4 7z" fill="${color}"/>`;
    g.push(`<g opacity="${faded ? 0.3 : 1}" class="pe-marker${i === ed.active ? " on" : ""}">
      ${i === ed.active ? `<circle cx="${px}" cy="${py - 4.5}" r="11" class="pe-ring"/>` : ""}
      ${shape}
      <text x="${px}" y="${py - 13}" text-anchor="middle" fill="${color}"
        class="pe-num">${i}</text></g>`);
    ed.markers.push({ i, px, py: py - 10 });
  }

  // hover guide and where a click would land
  if (ed.hoverX !== null && !ed.drag) {
    const time = ed.invX(ed.hoverX);
    if (time >= x0 && time <= x1) {
      g.push(`<line x1="${ed.hoverX}" x2="${ed.hoverX}" y1="${T}" y2="${H - B}" class="pe-hover"/>`);
      parts.push(`<text x="${Math.min(ed.hoverX + 6, W - R - 70)}" y="${T - 8}"
        class="pe-readout">${time.toFixed(2)} min</text>`);
      if (ed.active) {
        const s = snapTo(ed, ed.active, time, ed.shift);
        const y = ed.t.traces[activeMass].intensity[s];
        g.push(`<circle cx="${sx(rt[s])}" cy="${sy(y)}" r="5.5" class="pe-snap"/>`);
        parts.push(`<text x="${W - R}" y="${T - 8}" text-anchor="end" class="pe-readout">
          ${ed.shift ? "shift-click puts" : "click puts"} index ${ed.active}
          at ${rt[s].toFixed(2)} min${ed.shift ? " exactly" : ""}</text>`);
      }
    }
  }
  if (ed.drag && ed.drag.moved) {
    const a = Math.min(ed.drag.start, ed.drag.now), b = Math.max(ed.drag.start, ed.drag.now);
    g.push(`<rect x="${a}" y="${T}" width="${b - a}" height="${H - T - B}" class="pe-zoombox"/>`);
  }
  g.push(`</g>`);
  ed.svg.innerHTML = parts.join("") + g.join("");
}

function svgX(ed, ev) {
  const box = ed.svg.getBoundingClientRect();
  return (ev.clientX - box.left) * (ed.W || PE.W) / box.width;
}
function svgY(ed, ev) {
  const box = ed.svg.getBoundingClientRect();
  return (ev.clientY - box.top) * PE.H / box.height;
}

function bindPointer(ed) {
  const svg = ed.svg;
  svg.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    svg.setPointerCapture(ev.pointerId);
    ed.drag = { start: svgX(ed, ev), now: svgX(ed, ev), y: svgY(ed, ev), moved: false };
  });
  svg.addEventListener("pointermove", (ev) => {
    const x = svgX(ed, ev);
    ed.shift = ev.shiftKey;
    ed.hoverX = x >= PE.L && x <= ed.W - PE.R ? x : null;
    if (ed.drag) {
      ed.drag.now = x;
      if (Math.abs(x - ed.drag.start) > 6) ed.drag.moved = true;
    }
    render(ed);
  });
  svg.addEventListener("pointerleave", () => { ed.hoverX = null; render(ed); });
  svg.addEventListener("pointerup", (ev) => {
    const d = ed.drag;
    ed.drag = null;
    if (!d) return;
    if (d.moved) {
      const a = ed.invX(Math.min(d.start, d.now)), b = ed.invX(Math.max(d.start, d.now));
      if (b - a > 0.05) ed.view = [Math.max(a, ed.t.rt[0]), Math.min(b, ed.t.rt.at(-1))];
      render(ed);
      return;
    }
    clickAt(ed, d.start, d.y, ev.shiftKey);
  });
}

function clickAt(ed, x, y, exact) {
  // clicking a number on the chart chooses that index
  const hit = (ed.markers || [])
    .map(mk => ({ mk, d: Math.hypot(mk.px - x, mk.py - y) }))
    .filter(h => h.d < 14).sort((a, b) => a.d - b.d)[0];
  if (hit && hit.mk.i !== ed.active) {
    ed.active = hit.mk.i;
    renderAll(ed);
    return;
  }
  if (!ed.active || x < PE.L || x > ed.W - PE.R) return;
  const s = snapTo(ed, ed.active, ed.invX(x), exact);
  const time = ed.t.rt[s], i = ed.active;
  const auto = ed.l.auto_peaks ? ed.l.auto_peaks[i] : undefined;
  forget(ed, i);
  // landing back on the automatic peak is not a hand placement
  if (exact || auto === undefined || Math.abs(auto - time) >= 1e-4) {
    ed.picks[i] = Math.round(time * 1e5) / 1e5;
    if (exact) ed.exact.add(i);
  }
  status(ed, `Index ${i} at ${time.toFixed(2)} min${exact ? ", exactly" : ""} (not saved yet)`);
  renderAll(ed);
}

function zoomOut(ed) {
  const rt = ed.t.rt, [a, b] = ed.view, mid = (a + b) / 2, half = b - a;
  ed.view = [Math.max(rt[0], mid - half), Math.min(rt.at(-1), mid + half)];
  render(ed);
}

function savePeaks(ed) {
  const button = ed.card.querySelector("[data-role=save]");
  button.disabled = true;
  status(ed, "Saving and re-plotting…");
  const picks = Object.fromEntries(Object.entries(ed.picks).map(([k, v]) =>
    [k, v !== null && ed.exact.has(Number(k)) ? { rt: v, exact: true } : v]));
  ed.opts.save(picks).then(d => {
    if (d.error) throw new Error(d.error);
    unbindKeys(ed);
    PE_OPEN = null;
    ed.opts.onSaved(ed.card, d);
  }).catch(e => {
    button.disabled = false;
    status(ed, "");
    ed.opts.alert(`Could not save the peaks for ${ed.l.name}`, [e.message]);
  });
}
