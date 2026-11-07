const $ = (id) => document.getElementById(id);
const state = {
  ladders: [], standard: null, reached: 1,
  selections: {},   // file name -> array of indexes, or undefined for "all"
  last: null,       // last /api/calibration response
};

// three curves on one axes need three hues that cannot be confused
const MODEL_COLORS = { linear: "#B4441F", quadratic: "#0E7C86", cubic: "#5F5AA2" };
const TITLE_CASE = { linear: "Linear", quadratic: "Quadratic", cubic: "Cubic" };
const fmt = (v, places = 6) =>
  (Math.abs(v) >= 1e-4 || v === 0) ? Number(v).toFixed(places) : Number(v).toExponential(4);

const esc = (s) => String(s).replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ------------------------------------------------------------ navigation */
function paintRail(active) {
  for (const li of document.querySelectorAll(".rail li")) {
    const i = Number(li.dataset.step);
    li.classList.toggle("on", i === active);
    li.classList.toggle("done", i < state.reached && i !== active);
    li.classList.toggle("clickable", i <= state.reached);
    li.setAttribute("aria-disabled", i > state.reached);
  }
}
function goStep(n) {
  paintRail(n);
  $("working").classList.add("hidden");
  for (const s of document.querySelectorAll(".panel.step")) s.classList.add("hidden");
  $("step-" + n).classList.remove("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
}
function showWorking(title) {
  paintRail(null);
  for (const s of document.querySelectorAll(".panel.step")) s.classList.add("hidden");
  $("working").classList.remove("hidden");
  $("working-title").textContent = title;
  setProgress(0.02, "Starting");
}
function setProgress(p, msg) {
  $("working-bar").style.width = Math.round(p * 100) + "%";
  $("working-pct").textContent = Math.round(p * 100) + "%";
  $("working-message").textContent = msg;
}
for (const li of document.querySelectorAll(".rail li")) {
  li.addEventListener("click", () => {
    const n = Number(li.dataset.step);
    if (n <= state.reached) n === 2 ? runFits() : goStep(1);
  });
}
function showAlert(title, items) {
  const box = $("alert");
  if (!items || !items.length) return box.classList.add("hidden");
  box.innerHTML = `<h3>${esc(title)}</h3><ul>${items.map(t => `<li>${esc(t)}</li>`).join("")}</ul>`;
  box.classList.remove("hidden");
}
function pollJob(id) {
  return new Promise((resolve, reject) => {
    const tick = () => fetch("/api/job/" + id).then(r => r.json()).then(j => {
      setProgress(j.progress, j.message);
      if (j.error) return reject(new Error(j.error));
      if (j.done) return resolve(j);
      setTimeout(tick, 600);
    }).catch(reject);
    tick();
  });
}

/* ================================================== page 1: the ladders */
function loadConfig() {
  return fetch("/api/config").then(r => r.json()).then(cfg => {
    const dot = $("parser-dot"), text = $("parser-text");
    if (cfg.parser && cfg.parser.available) {
      dot.classList.add("ok");
      text.textContent = "Reading .raw with " + cfg.parser.detail.split(" (")[0];
    } else {
      dot.classList.add("bad");
      text.textContent = "No .raw reader — run python tools/get_parser.py";
    }
    if (cfg.error) {
      $("dir-line").textContent = "config.json could not be used.";
      showAlert("Configuration problem", [cfg.error]);
      $("rescan").disabled = true;
      return null;
    }
    showAlert("", []);
    $("dir-line").innerHTML =
      `ladder_dir <span class="mono">${esc(cfg.ladder_dir)}</span> — masses ` +
      `<span class="mono">${cfg.masses.map(m => m).join(", ")}</span>`;
    $("file-count").textContent =
      `${cfg.files.length} .raw file${cfg.files.length === 1 ? "" : "s"} found`;
    state.config = cfg;
    $("save-standard-path").textContent = cfg.standard_dir || "standard_ladder/";
    showSavedStandard(cfg.saved_standard);
    if (cfg.standard_error) showAlert("Saved standard", [cfg.standard_error]);
    return cfg;
  });
}

function scan() {
  showWorking("Reading ladders");
  return fetch("/api/scan", { method: "POST" }).then(r => r.json())
    .then(d => pollJob(d.job))
    .then(() => fetch("/api/ladders").then(r => r.json()))
    .then(showLadders)
    .catch(e => { goStep(1); showAlert("Could not read the ladders", [e.message]); });
}

function showLadders(d) {
  state.ladders = d.ladders;
  const box = $("ladder-list");
  box.innerHTML = "";
  const warnings = [];
  for (const l of d.ladders) warnings.push(...l.warnings.map(w => `${l.name}: ${w}`));

  for (const l of d.ladders) {
    const card = document.createElement("div");
    card.className = "ladder";
    card.dataset.name = l.name;
    const badge = l.monotonic ? "" :
      `<span class="badge">retention times not increasing</span>`;
    card.id = "ladder-card-" + cardId(l.name);
    card.innerHTML = `
      <div class="ladder-head">
        <label><input type="radio" name="standard" value="${esc(l.name)}"> ${esc(l.name)}</label>
        ${badge}
        <span class="meta" data-role="count"></span>
      </div>
      <div class="ladder-body">
        <div class="ladder-plot">
          <img src="/api/figure/${encodeURIComponent(l.figure)}?t=${Date.now()}" alt="${esc(l.name)} chromatogram">
        </div>
        <div class="ladder-indexes">
          <h4>Indexes to fit with</h4>
          <p class="note">Unticked indexes are left out of every fit this ladder
          takes part in, including when it is the standard.</p>
          <div class="index-list" data-role="list"></div>
          <div class="index-actions">
            <button type="button" class="ghost small" data-role="all">All</button>
            <button type="button" class="ghost small" data-role="none">None</button>
          </div>
        </div>
      </div>`;
    card.querySelector("input[name=standard]").onchange = () => chooseStandard(l.name);
    buildIndexList(card, l);
    box.appendChild(card);
  }
  if (d.mixed_peak_counts) {
    warnings.push("The ladders did not all yield the same number of peaks. " +
      "Only indexes found in both runs take part in a fit.");
  }
  showAlert("Worth checking", warnings);

  $("standard-gate").classList.remove("hidden");
  if (state.standard && d.ladders.some(l => l.name === state.standard)) {
    // matched by value rather than a selector, so odd characters in file names are safe
    const radio = [...box.querySelectorAll('input[name=standard]')]
      .find(r => r.value === state.standard);
    if (radio) { radio.checked = true; chooseStandard(state.standard); }
  } else {
    state.standard = null;
    $("to-fits").disabled = true;
    state.reached = 1;
  }
  goStep(1);
}

const cardId = (name) => name.replace(/[^A-Za-z0-9_-]/g, "_");

function indexesOf(l) {
  return Object.keys(l.peaks).map(Number).sort((a, b) => a - b);
}

function buildIndexList(card, l) {
  const all = indexesOf(l);
  const known = new Set(all);
  // keep an earlier choice only where it still refers to indexes this run has
  const previous = state.selections[l.name];
  const chosen = new Set(previous ? previous.filter(i => known.has(i)) : all);
  state.selections[l.name] = [...chosen].sort((a, b) => a - b);

  const list = card.querySelector('[data-role=list]');
  list.innerHTML = "";
  for (const i of all) {
    const row = document.createElement("label");
    row.className = chosen.has(i) ? "" : "off";
    row.innerHTML = `<input type="checkbox" ${chosen.has(i) ? "checked" : ""}>
      <span class="idx">${i}</span><span class="rt">${l.peaks[i].toFixed(2)}</span>`;
    row.querySelector("input").onchange = (ev) => {
      const set = new Set(state.selections[l.name]);
      ev.target.checked ? set.add(i) : set.delete(i);
      state.selections[l.name] = [...set].sort((a, b) => a - b);
      row.classList.toggle("off", !ev.target.checked);
      updateCount(card, l);
    };
    list.appendChild(row);
  }
  card.querySelector('[data-role=all]').onclick = () => setAll(card, l, all);
  card.querySelector('[data-role=none]').onclick = () => setAll(card, l, []);
  updateCount(card, l);
}

function setAll(card, l, indexes) {
  state.selections[l.name] = [...indexes];
  const wanted = new Set(indexes);
  const rows = [...card.querySelectorAll('[data-role=list] label')];
  indexesOf(l).forEach((i, n) => {
    rows[n].querySelector("input").checked = wanted.has(i);
    rows[n].classList.toggle("off", !wanted.has(i));
  });
  updateCount(card, l);
}

function updateCount(card, l) {
  const n = (state.selections[l.name] || []).length;
  const total = indexesOf(l).length;
  card.querySelector('[data-role=count]').textContent = `${n} of ${total} indexes`;
  card.classList.toggle("too-few", n < 3);
}

function chooseStandard(name) {
  state.standard = name;
  for (const card of document.querySelectorAll(".ladder")) {
    card.classList.toggle("chosen", card.dataset.name === name);
  }
  $("standard-line").innerHTML =
    `Everything else will be fitted onto <b>${esc(name)}</b>. ` +
    `Come back here any time to try a different one.`;
  $("to-fits").disabled = false;
}

$("rescan").onclick = scan;
$("reset-indexes").onclick = () => {
  for (const l of state.ladders) {
    state.selections[l.name] = indexesOf(l);
    const card = $("ladder-card-" + cardId(l.name));
    if (card) setAll(card, l, state.selections[l.name]);
  }
};
$("to-fits").onclick = runFits;

/* ================================================ page 2: the fits */
function runFits() {
  if (!state.standard) return;
  return fetch("/api/calibration", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ standard: state.standard, selections: state.selections }),
  }).then(r => r.json()).then(d => {
    if (d.error) throw new Error(d.error);
    state.last = d;
    renderFits(d);
    state.reached = 2;
    goStep(2);
  }).catch(e => { goStep(1); showAlert("Could not fit", [e.message]); });
}

function renderFits(d) {
  $("fit-caption").innerHTML =
    `Standard is <b>${esc(d.standard)}</b>. ` +
    `x is the retention time in the run being calibrated, y the retention time of ` +
    `the same ladder index in the standard.`;
  const box = $("fits");
  box.innerHTML = "";
  for (const f of d.fits) box.appendChild(fitCard(f));
  renderSummary(d);
}

function fitCard(f) {
  const el = document.createElement("div");
  el.className = "fit";

  const used = new Set(f.used_indexes);
  const chips = f.available_indexes.map(i =>
    `<span class="chip${used.has(i) ? "" : " off"}">${i}</span>`).join("");
  const dropped = [];
  if (f.excluded_by_file && f.excluded_by_file.length) {
    dropped.push(`unticked on ${f.name}: ${f.excluded_by_file.join(", ")}`);
  }
  if (f.excluded_by_standard && f.excluded_by_standard.length) {
    dropped.push(`unticked on the standard: ${f.excluded_by_standard.join(", ")}`);
  }
  if (f.missing_from_one_run && f.missing_from_one_run.length) {
    dropped.push(`not found in both runs: ${f.missing_from_one_run.join(", ")}`);
  }

  const models = (f.models || []).map(m => {
    const coefs = m.names.map((n, i) =>
      `<span><em>${n}</em> ${fmt(m.coefficients[i])}</span>`).join("");
    return `
      <div class="model${m.key === f.best ? " best" : ""}" style="--c:${MODEL_COLORS[m.key]}">
        <div class="model-head">
          <span class="swatch"></span>${TITLE_CASE[m.key]}
          <em>${m.label}</em>
          ${m.key === f.best ? '<span class="tag">best fit</span>' : ""}
        </div>
        <div class="eq">${esc(m.equation)}</div>
        <div class="coefs">${coefs}</div>
        <div class="metrics">
          <div><span>RMSE</span><b>${m.rmse.toFixed(4)} min</b></div>
          <div><span>Std. error</span><b>${m.std_error === null ? "—" : m.std_error.toFixed(4) + " min"}</b></div>
          <div><span>Largest residual</span><b>${m.max_abs_residual.toFixed(4)} min</b></div>
          <div><span>R²</span><b>${m.r2.toFixed(6)}</b></div>
          <div><span>Adjusted R²</span><b>${m.adjusted_r2 === null ? "—" : m.adjusted_r2.toFixed(6)}</b></div>
          ${m.exact ? '<div class="exact">runs through every point — no residual to judge</div>' : ""}
        </div>
      </div>`;
  }).join("");

  const before = f.uncalibrated ? `
    <div class="baseline">
      <span>Before any fit</span>
      <b>RMSE ${f.uncalibrated.rmse.toFixed(4)} min</b>
    </div>` : "";
  const note = (f.models || []).length > 1 ? `
    <p class="note model-note">These models are nested, so RMSE can only fall as the
    degree rises. "Best fit" is the simplest model that nothing beats by more than 5%
    on standard error, which divides by the degrees of freedom left rather than by the
    number of points${
      f.best_by_rmse && f.best_by_rmse !== f.best
        ? `. RMSE alone would have chosen the ${TITLE_CASE[f.best_by_rmse].toLowerCase()} here`
        : ""}.</p>` : "";

  el.innerHTML = `
    <div class="fit-head">
      <h3>${esc(f.name)}</h3>
      <span class="sub">${f.n_used} of ${f.available_indexes.length} indexes used</span>
    </div>
    <div class="fit-body">
      <div class="fit-plot">${scatterSvg(f)}${residualSvg(f)}</div>
      <div class="fit-side">
        <h4>Indexes chosen on page 1</h4>
        <div class="chips">${chips}</div>
        ${dropped.length ? `<p class="note dropped">${dropped.map(esc).join("<br>")}</p>` : ""}
        <button type="button" class="ghost small" data-role="edit">Change on page 1</button>
        ${f.error ? `<p class="fit-error">${esc(f.error)}</p>` : ""}
        ${before}${models}${note}
      </div>
    </div>`;
  el.querySelector('[data-role=edit]').onclick = () => editIndexes(f.name);
  return el;
}

/* Scatter of the shared indexes with the fitted curve through them. */
function scatterSvg(f) {
  const W = 560, H = 360, L = 58, R = 16, T = 14, B = 44;
  const pts = f.points;
  if (!pts.length) return `<p class="note">No shared indexes between these two runs.</p>`;

  const curves = f.models || [];
  const xs = pts.map(p => p.x).concat(...curves.map(m => m.curve.map(c => c[0])));
  const ys = pts.map(p => p.y).concat(...curves.map(m => m.curve.map(c => c[1])));
  const pad = (v) => (Math.max(...v) - Math.min(...v)) * 0.08 || 1;
  const x0 = Math.min(...xs) - pad(xs), x1 = Math.max(...xs) + pad(xs);
  const y0 = Math.min(...ys) - pad(ys), y1 = Math.max(...ys) + pad(ys);
  const px = (v) => L + (W - L - R) * (v - x0) / (x1 - x0);
  const py = (v) => H - B - (H - T - B) * (v - y0) / (y1 - y0);

  const ticks = (lo, hi) => {
    const step = Math.pow(10, Math.floor(Math.log10((hi - lo) / 4)));
    const nice = [1, 2, 5, 10].map(m => m * step).find(s => (hi - lo) / s <= 6) || step;
    const out = [];
    for (let v = Math.ceil(lo / nice) * nice; v <= hi; v += nice) out.push(v);
    return out;
  };

  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Retention time of ${esc(f.name)} against the standard">`;
  for (const t of ticks(x0, x1)) {
    s += `<line x1="${px(t)}" y1="${T}" x2="${px(t)}" y2="${H - B}" stroke="var(--rule-soft)"/>`;
    s += `<text x="${px(t)}" y="${H - B + 15}" text-anchor="middle" font-size="10" fill="var(--muted)">${t.toFixed(0)}</text>`;
  }
  for (const t of ticks(y0, y1)) {
    s += `<line x1="${L}" y1="${py(t)}" x2="${W - R}" y2="${py(t)}" stroke="var(--rule-soft)"/>`;
    s += `<text x="${L - 8}" y="${py(t) + 3}" text-anchor="end" font-size="10" fill="var(--muted)">${t.toFixed(0)}</text>`;
  }
  s += `<line x1="${L}" y1="${H - B}" x2="${W - R}" y2="${H - B}" stroke="var(--rule)"/>`;
  s += `<line x1="${L}" y1="${T}" x2="${L}" y2="${H - B}" stroke="var(--rule)"/>`;
  s += `<text x="${(L + W - R) / 2}" y="${H - 6}" text-anchor="middle" font-size="11" fill="var(--ink)">retention time in ${esc(f.name)} (min)</text>`;
  s += `<text x="14" y="${(T + H - B) / 2}" text-anchor="middle" font-size="11" fill="var(--ink)" transform="rotate(-90 14 ${(T + H - B) / 2})">retention time in the standard (min)</text>`;

  // dashes as well as colour, so the curves stay apart where they nearly coincide
  const dashes = { linear: "6 3", quadratic: "", cubic: "2 3" };
  for (const m of curves) {
    const path = m.curve.map((c, i) =>
      `${i ? "L" : "M"}${px(c[0]).toFixed(1)},${py(c[1]).toFixed(1)}`).join("");
    s += `<path d="${path}" fill="none" stroke="${MODEL_COLORS[m.key]}" stroke-width="1.6"` +
      (dashes[m.key] ? ` stroke-dasharray="${dashes[m.key]}"` : "") + `/>`;
  }
  curves.forEach((m, i) => {
    const ly = T + 12 + i * 16, lx = L + 14;
    s += `<line x1="${lx}" y1="${ly}" x2="${lx + 22}" y2="${ly}" stroke="${MODEL_COLORS[m.key]}"
      stroke-width="1.6"${dashes[m.key] ? ` stroke-dasharray="${dashes[m.key]}"` : ""}/>`;
    s += `<text x="${lx + 28}" y="${ly + 3.5}" font-size="10" fill="${MODEL_COLORS[m.key]}">${TITLE_CASE[m.key]}</text>`;
  });
  for (const p of pts) {
    const fill = p.used ? "var(--accent-deep)" : "none";
    s += `<circle cx="${px(p.x)}" cy="${py(p.y)}" r="4" fill="${fill}" stroke="${p.used ? "var(--accent-deep)" : "var(--muted)"}" stroke-width="1.2"${p.used ? "" : ' stroke-dasharray="2 2"'}/>`;
    s += `<text x="${px(p.x) + 7}" y="${py(p.y) - 6}" font-size="9.5" fill="${p.used ? "var(--accent-deep)" : "var(--muted)"}">${p.index}</text>`;
  }
  return s + `</svg>`;
}

/* The three curves nearly coincide on a 40-minute axis, so the residuals are where
   the models are actually told apart. Same x margins as the scatter, so they line up. */
function residualSvg(f) {
  const models = (f.models || []).filter(m => m.residuals);
  const pts = f.points.filter(p => p.used);
  if (!models.length || pts.length < 2) return "";

  const W = 560, H = 150, L = 58, R = 16, T = 20, B = 40;
  const xs = pts.map(p => p.x);
  const xpad = (Math.max(...xs) - Math.min(...xs)) * 0.08 || 1;
  const x0 = Math.min(...xs) - xpad, x1 = Math.max(...xs) + xpad;
  const all = models.flatMap(m => Object.values(m.residuals));
  const worst = Math.max(...all.map(Math.abs)) || 1e-6;
  const px = (v) => L + (W - L - R) * (v - x0) / (x1 - x0);
  const py = (v) => (T + H - B) / 2 - ((H - T - B) / 2) * (v / (worst * 1.25));

  const unit = worst < 0.01 ? 1000 : 1;             // switch to seconds when tiny
  const label = unit === 1000 ? "residual (s)" : "residual (min)";
  const tickVals = [worst, 0, -worst];

  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Residuals by model for ${esc(f.name)}">`;
  s += `<text x="${L}" y="${T - 8}" font-size="10" fill="var(--muted)">residual = standard − fitted</text>`;
  for (const t of tickVals) {
    const zero = t === 0;
    s += `<line x1="${L}" y1="${py(t)}" x2="${W - R}" y2="${py(t)}"
      stroke="${zero ? "var(--rule)" : "var(--rule-soft)"}"/>`;
    s += `<text x="${L - 8}" y="${py(t) + 3}" text-anchor="end" font-size="9" fill="var(--muted)">${
      (t * unit).toFixed(unit === 1000 ? 1 : 3)}</text>`;
  }
  s += `<text x="14" y="${(T + H - B) / 2}" text-anchor="middle" font-size="10" fill="var(--ink)"
    transform="rotate(-90 14 ${(T + H - B) / 2})">${label}</text>`;

  for (const m of models) {
    const seq = pts
      .filter(p => m.residuals[String(p.index)] !== undefined)
      .map(p => [px(p.x), py(m.residuals[String(p.index)])]);
    if (seq.length > 1) {
      s += `<path d="${seq.map((c, i) => `${i ? "L" : "M"}${c[0].toFixed(1)},${c[1].toFixed(1)}`).join("")}"
        fill="none" stroke="${MODEL_COLORS[m.key]}" stroke-width="1" opacity="0.55"/>`;
    }
    for (const [cx, cy] of seq) {
      s += `<circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="2.6"
        fill="${MODEL_COLORS[m.key]}"/>`;
    }
  }
  s += `<text x="${(L + W - R) / 2}" y="${H - 6}" text-anchor="middle" font-size="10"
    fill="var(--muted)">retention time in ${esc(f.name)} (min)</text>`;
  return s + `</svg>`;
}

function renderSummary(d) {
  const sm = d.summary;
  if (!sm.n_files) {
    $("summary-body").innerHTML = `<p class="note">No ladder could be fitted yet.</p>`;
    return;
  }
  const keys = Object.keys(sm.models);

  let html = `<p class="summary-lead">${sm.n_files} ladder${sm.n_files === 1 ? "" : "s"} fitted onto
    <b>${esc(d.standard)}</b>. Before any fit they sat
    <b>${sm.mean_uncalibrated_rmse.toFixed(4)} min</b> RMSE from it on average.</p>`;

  // per-model averages, the headline numbers
  html += `<h4 class="sub-head">Average coefficients and error, by model</h4>`;
  html += `<table><thead><tr><th>Model</th><th>Form</th>`;
  html += `<th>a</th><th>b</th><th>c</th><th>d</th>`;
  html += `<th>mean RMSE</th><th>mean std. error</th><th>worst RMSE</th>
    <th>worst residual</th><th>mean R²</th><th>best for</th></tr></thead><tbody>`;
  for (const key of keys) {
    const m = sm.models[key];
    const cell = (n) => {
      if (!(n in m.mean_coefficients)) return `<td class="void">—</td>`;
      const sd = m.sd_coefficients[n];
      return `<td>${fmt(m.mean_coefficients[n])}${
        sd ? `<em class="sd">±${Number(sd).toExponential(1)}</em>` : ""}</td>`;
    };
    html += `<tr${key === sm.overall_best ? ' class="win"' : ""}>
      <td><span class="swatch" style="--c:${MODEL_COLORS[key]}"></span>${TITLE_CASE[key]}</td>
      <td class="form">${m.label}</td>
      ${cell("a")}${cell("b")}${cell("c")}${cell("d")}
      <td>${m.mean_rmse.toFixed(4)}</td>
      <td>${m.mean_std_error === null ? "—" : m.mean_std_error.toFixed(4)}</td>
      <td>${m.worst_rmse.toFixed(4)}</td>
      <td>${m.worst_residual.toFixed(4)}</td><td>${m.mean_r2.toFixed(6)}</td>
      <td>${sm.best_counts[key] || 0} of ${sm.n_files}</td></tr>`;
  }
  html += `</tbody></table>`;
  html += `<p class="note">Mean coefficients carry ± one standard deviation across the
    ladders. "best for" counts the ladders where that model had the lowest standard
    error, and only when it beat the simpler model by more than 5% — the three models
    are nested, so RMSE alone would hand the contest to the cubic almost every time.
    The highlighted row is the model that rule picks on the averages.</p>`;

  // per-ladder comparison, so one bad run is visible rather than averaged away
  html += `<h4 class="sub-head">RMSE per ladder (min)</h4>`;
  html += `<table><thead><tr><th>Ladder</th><th>Indexes</th><th>before fit</th>`;
  for (const key of keys) html += `<th>${TITLE_CASE[key]}</th>`;
  html += `<th>best</th></tr></thead><tbody>`;
  for (const f of d.fits) {
    if (!f.models || !f.models.length) {
      html += `<tr><td>${esc(f.name)}</td><td>${f.n_used}</td>
        <td colspan="${keys.length + 2}">${esc(f.error || "not fitted")}</td></tr>`;
      continue;
    }
    const byKey = {};
    for (const m of f.models) byKey[m.key] = m;
    html += `<tr><td>${esc(f.name)}</td>
      <td>${f.n_used}/${f.available_indexes.length}</td>
      <td>${f.uncalibrated ? f.uncalibrated.rmse.toFixed(4) : "—"}</td>`;
    for (const key of keys) {
      const m = byKey[key];
      html += m
        ? `<td class="${key === f.best_by_rmse ? "win" : ""}">${m.rmse.toFixed(4)}${m.exact ? "*" : ""}</td>`
        : `<td class="void">—</td>`;
    }
    html += `<td><span class="swatch" style="--c:${MODEL_COLORS[f.best]}"></span>${TITLE_CASE[f.best] || "—"}</td></tr>`;
  }
  html += `</tbody></table>`;
  html += `<p class="note">RMSE in minutes, lowest per row highlighted. * the model
    passes through every selected point, so its zero residual is arithmetic rather
    than agreement. The "best" column is decided on standard error, so it need not be
    the lowest RMSE in the row.</p>`;
  if (sm.n_skipped) {
    html += `<p class="note">${sm.n_skipped} ladder(s) not fitted; they are listed above.</p>`;
  }
  $("summary-body").innerHTML = html;
}

function showSavedStandard(saved) {
  const note = $("saved-note");
  if (!saved) { note.classList.add("hidden"); return; }
  const when = String(saved.saved_at || "").replace("T", " ").slice(0, 16);
  const all = saved.usable_indexes && saved.n_peaks &&
    saved.usable_indexes.length === saved.n_peaks;
  note.textContent = `current: ${saved.name}` +
    (when ? `, saved ${when}` : "") +
    (all ? "" : ` (${(saved.usable_indexes || []).length} indexes)`);
  note.classList.remove("hidden");
  note.title = `Indexes usable for fitting: ${(saved.usable_indexes || []).join(", ")}`;
}

$("save-standard").onclick = () => {
  if (!state.standard) return;
  const button = $("save-standard");
  button.disabled = true;
  fetch("/api/standard/save", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ standard: state.standard, selections: state.selections }),
  }).then(r => r.json()).then(d => {
    button.disabled = false;
    if (d.error) return showAlert("Could not save the standard", [d.error]);
    showAlert("", []);
    showSavedStandard(d);
    $("save-standard-line").innerHTML =
      `Saved to <span class="mono">${esc(d.path)}</span>. Compounds normalized in the
       main app will be placed on <b>${esc(d.name)}</b>'s time axis.`;
  }).catch(e => { button.disabled = false; showAlert("Could not save the standard", [e.message]); });
};

$("back-to-1").onclick = () => goStep(1);

function editIndexes(name) {
  goStep(1);
  const card = $("ladder-card-" + cardId(name));
  if (card) setTimeout(() => card.scrollIntoView({ behavior: "smooth", block: "center" }), 60);
}
$("download-summary").onclick = () => {
  if (!state.last) return;
  const d = state.last, sm = d.summary;
  const rows = [["standard", "ladder", "indexes_used", "indexes_available", "model",
    "degree", "n_params", "a", "b", "c", "d", "rmse_min", "std_error_min",
    "max_abs_residual_min", "r2", "adjusted_r2", "exact_fit", "is_best",
    "rmse_before_min"].join(",")];
  for (const f of d.fits) {
    for (const m of f.models || []) {
      const coef = {};
      m.names.forEach((n, i) => { coef[n] = m.coefficients[i]; });
      rows.push([d.standard, f.name, `"${f.used_indexes.join(" ")}"`,
        f.available_indexes.length, m.key, m.degree, m.n_params,
        coef.a ?? "", coef.b ?? "", coef.c ?? "", coef.d ?? "",
        m.rmse, m.std_error ?? "", m.max_abs_residual, m.r2, m.adjusted_r2 ?? "",
        m.exact, m.key === f.best,
        f.uncalibrated ? f.uncalibrated.rmse : ""].join(","));
    }
  }
  rows.push("");
  rows.push(["model", "n_files", "mean_a", "mean_b", "mean_c", "mean_d",
    "mean_rmse_min", "mean_std_error_min", "worst_rmse_min", "mean_r2",
    "best_for_n_ladders"].join(","));
  for (const [key, m] of Object.entries(sm.models)) {
    rows.push([key, m.n_files,
      m.mean_coefficients.a ?? "", m.mean_coefficients.b ?? "",
      m.mean_coefficients.c ?? "", m.mean_coefficients.d ?? "",
      m.mean_rmse, m.mean_std_error ?? "", m.worst_rmse, m.mean_r2,
      sm.best_counts[key] || 0].join(","));
  }
  rows.push("", `mean_rmse_before_any_fit_min,${sm.mean_uncalibrated_rmse}`,
    `overall_best_model,${sm.overall_best}`);

  const blob = new Blob([rows.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `calibration_${d.standard}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
};

/* --------------------------------------------------------------- start */
goStep(1);
loadConfig().then(cfg => { if (cfg && cfg.files.length) scan(); });
