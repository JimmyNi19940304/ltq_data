const $ = (id) => document.getElementById(id);
const state = {
  session: null, compounds: 0, summary: null, spectra: [], standard: null,
  reached: 1,          // highest step whose content exists
  spectraFresh: false, // false once the chromatograms are replotted
  exportFresh: false,
};

/* ---------------------------------------------------------------- health */
fetch("/api/health").then(r => r.json()).then(h => {
  state.standard = h.standard;
  const card = $("standard-card"), line = $("standard-line");
  if (h.standard && h.standard.available) {
    card.classList.add("present");
    const when = String(h.standard.saved_at || "").replace("T", " ").slice(0, 16);
    line.innerHTML = `Compounds will be placed on <b>${escapeHtml(h.standard.name)}</b>'s
      time axis (saved ${escapeHtml(when)}, ${h.standard.usable_indexes.length} usable indexes).`;
  } else {
    card.classList.add("absent");
    $("model-field").classList.add("hidden");
    line.textContent = (h.standard && h.standard.detail) ||
      "No standard ladder saved. Times will be the instrument's own.";
  }
  const dot = $("parser-dot"), text = $("parser-text");
  if (h.parser.available) {
    dot.classList.add("ok");
    text.textContent = "Reading .raw with " + h.parser.detail.split(" (")[0];
  } else {
    dot.classList.add("bad");
    text.textContent = "No .raw reader — run python tools/get_parser.py";
  }
}).catch(() => { $("parser-text").textContent = "Server not reachable"; });

/* ----------------------------------------------------------- step rail */
function reach(n) {
  state.reached = Math.max(state.reached, n);
  paintRail(currentStep());
}
function currentStep() {
  for (const n of [1, 2, 3, 4]) if (!$("step-" + n).classList.contains("hidden")) return n;
  return null;
}
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
function showWorking(title, hint) {
  paintRail(null);
  for (const s of document.querySelectorAll(".panel.step")) s.classList.add("hidden");
  $("working").classList.remove("hidden");
  $("working-title").textContent = title;
  $("working-hint").textContent = hint || "";
  setProgress(0.02, "Starting");
  window.scrollTo({ top: 0, behavior: "smooth" });
}
function setProgress(p, message) {
  $("working-bar").style.width = Math.round(p * 100) + "%";
  $("working-pct").textContent = Math.round(p * 100) + "%";
  $("working-message").textContent = message;
}
/* The rail is a control, not decoration: any step already built can be revisited,
   and revisiting never recomputes anything. */
for (const li of document.querySelectorAll(".rail li")) {
  li.addEventListener("click", () => {
    const n = Number(li.dataset.step);
    if (n <= state.reached) enterStep(n);
  });
}
goStep(1);

/* Moving to a step either shows what is already there, or builds it once. */
function enterStep(n) {
  if (n === 3 && !(state.spectraFresh && state.spectra.length)) return buildSpectra();
  if (n === 4) fillManifest();
  goStep(n);
}

/* --------------------------------------------------------------- helpers */
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function showAlert(title, items) {
  const box = $("alert");
  if (!items || !items.length) { box.classList.add("hidden"); return; }
  box.innerHTML = `<h3>${escapeHtml(title)}</h3><ul>${items.map(t => `<li>${escapeHtml(t)}</li>`).join("")}</ul>`;
  box.classList.remove("hidden");
}
function failed(err, backTo) {
  showAlert("Something went wrong", [err.message]);
  goStep(backTo || 1);
}
function pollJob(jobId) {
  return new Promise((resolve, reject) => {
    const tick = () => {
      fetch("/api/job/" + jobId).then(r => r.json()).then(j => {
        setProgress(j.progress, j.message);
        if (j.error) return reject(new Error(j.error));
        if (j.done) return resolve(j);
        setTimeout(tick, 600);
      }).catch(reject);
    };
    tick();
  });
}
function plate(container, fig, sub) {
  const f = document.createElement("figure");
  f.className = "plate";
  const src = `/api/session/${state.session}/figure/${encodeURIComponent(fig.file)}?t=${Date.now()}`;
  f.innerHTML = `<img src="${src}" alt="${escapeHtml(fig.title)}">
    <figcaption><b>${escapeHtml(fig.title)}</b><span>${escapeHtml(sub || fig.subtitle || "")}</span></figcaption>`;
  f.querySelector("img").onclick = () => {
    $("lightbox-img").src = src;
    $("lightbox").classList.remove("hidden");
  };
  container.appendChild(f);
}
$("lightbox").onclick = () => $("lightbox").classList.add("hidden");
document.addEventListener("keydown", e => {
  if (e.key === "Escape") $("lightbox").classList.add("hidden");
});

/* ================================================== step 1: input data */
function addCompound() {
  if (state.compounds >= 5) return;
  const i = state.compounds++;
  const box = document.createElement("div");
  box.className = "compound";
  box.innerHTML = `
    <div class="compound-head"><b>Compound ${i + 1}</b>
      <button type="button" class="remove">Remove</button></div>
    <div class="pair">
      <label class="field"><span>Name</span>
        <input type="text" data-role="name" placeholder="6E1"></label>
      <label class="field"><span>Nominal mass</span>
        <input type="number" step="1" class="mono" data-role="mass" placeholder="1419"></label>
    </div>
    <label class="field"><span>.raw file</span>
      <input type="file" data-role="file" accept=".raw"></label>
    <label class="field"><span>or a path on this computer</span>
      <input type="text" data-role="path" spellcheck="false" placeholder="/data/6E1.raw"></label>`;
  box.querySelector(".remove").onclick = () => { box.remove(); renumber(); };
  $("compounds").appendChild(box);
  renumber();
}
function renumber() {
  const boxes = [...document.querySelectorAll(".compound")];
  state.compounds = boxes.length;
  boxes.forEach((b, i) => {
    b.querySelector(".compound-head b").textContent = `Compound ${i + 1}`;
    b.querySelector(".remove").style.visibility = boxes.length > 1 ? "visible" : "hidden";
  });
  $("add-compound").disabled = boxes.length >= 5;
}
$("add-compound").onclick = addCompound;
addCompound();

$("setup").onsubmit = (e) => {
  e.preventDefault();
  const form = $("setup");
  const boxes = [...document.querySelectorAll(".compound")];

  // the requirement is one ladder and at least one compound, so check here first
  const ladderGiven = form.querySelector('input[name=ladder_file]').files[0] ||
    form.querySelector('input[name=ladder_path]').value.trim();
  if (!ladderGiven) return showAlert("Add a ladder run", ["Choose a ladder .raw file, or give its path."]);
  const missing = boxes.filter(b =>
    !(b.querySelector('[data-role=file]').files[0] || b.querySelector('[data-role=path]').value.trim()));
  if (!boxes.length) return showAlert("Add a compound", ["At least one compound file is required."]);
  if (missing.length) return showAlert("A compound has no file", ["Every compound needs a .raw file or a path."]);
  showAlert("", []);

  const fd = new FormData();
  const ladderFile = form.querySelector('input[name=ladder_file]').files[0];
  if (ladderFile) fd.append("ladder_file", ladderFile);
  fd.append("ladder_path", form.querySelector('input[name=ladder_path]').value);
  fd.append("ladder_masses", form.querySelector('input[name=ladder_masses]').value);
  fd.append("compound_count", boxes.length);
  boxes.forEach((b, i) => {
    const file = b.querySelector('[data-role=file]').files[0];
    if (file) fd.append(`compound_file_${i}`, file);
    fd.append(`compound_path_${i}`, b.querySelector('[data-role=path]').value);
    fd.append(`compound_name_${i}`, b.querySelector('[data-role=name]').value);
    fd.append(`compound_mass_${i}`, b.querySelector('[data-role=mass]').value);
  });
  for (const el of form.querySelectorAll(".settings input")) fd.append(el.name, el.value);
  fd.append("transform_model", $("transform-model").value);

  showWorking("Uploading", "Large .raw files take a moment. Giving a path instead skips the upload entirely.");
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/session");
  xhr.upload.onprogress = (ev) => {
    if (ev.lengthComputable) setProgress(0.9 * ev.loaded / ev.total, "Sending files");
  };
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (_) { }
    if (xhr.status !== 200) {
      goStep(1);
      return showAlert("The run could not start", [data.error || `Server returned ${xhr.status}`]);
    }
    state.session = data.session;
    $("working-title").textContent = "Reading scans";
    $("working-hint").textContent =
      "The first read of a .raw file takes roughly half a minute. The same file is reused after that.";
    pollJob(data.job).then(loadChromatograms).catch(e => failed(e, 1));
  };
  xhr.onerror = () => failed(new Error("Upload failed"), 1);
  xhr.send(fd);
};

/* ============================================== step 2: chromatograms */
function loadChromatograms() {
  return fetch(`/api/session/${state.session}/chromatograms`).then(r => r.json()).then(d => {
    state.summary = d.summary;
    const lad = d.summary.ladder;

    $("chrom-caption").textContent =
      `Ladder ${lad.run}: ${lad.peaks.length} peaks numbered by retention time. ` +
      `${d.summary.compounds.length} compound${d.summary.compounds.length === 1 ? "" : "s"} ` +
      `placed on that scale by interpolation.`;

    const box = $("chrom-figures");
    box.innerHTML = "";
    renderAxisBanner(d.summary);
    drawRuler(d.summary);
    for (const fig of d.figures) {
      let sub = "click to enlarge";
      if (fig.kind === "compound") {
        const c = d.summary.compounds.find(x => `Chromatogram of ${x.name}` === fig.title);
        if (c && c.on_standard_axis) {
          sub = c.peaks.map(p => `peak ${p.peak_no}: ${p.rt.toFixed(2)} min`).join("   ·   ");
        } else if (c) {
          sub = c.peaks.map(p =>
            `${p.rt.toFixed(2)} min = index ${p.index === null ? "n/a" : p.index.toFixed(2)}`).join("   ·   ");
        }
      }
      plate(box, fig, sub);
    }

    const sel = $("ladder-limit");
    sel.innerHTML = "";
    const all = document.createElement("option");
    all.value = "all";
    all.textContent = `All ${lad.n_points} indexes`;
    sel.appendChild(all);
    for (let i = lad.n_points; i >= 2; i--) {
      const o = document.createElement("option");
      o.value = i;
      o.textContent = `1 to ${i}`;
      sel.appendChild(o);
    }
    sel.value = lad.limit >= lad.n_points ? "all" : String(lad.limit);

    buildTables(d.summary);
    showAlert("Check these before you continue", d.summary.warnings);
    // the scans behind the spectra just changed, so anything downstream is stale
    state.spectraFresh = false;
    state.exportFresh = false;
    state.reached = 2;
    reach(2);
    goStep(2);
  });
}

function renderAxisBanner(summary) {
  let el = $("axis-banner");
  if (!el) {
    el = document.createElement("div");
    el.id = "axis-banner";
    $("step-2").querySelector(".panel-head").after(el);
  }
  if (summary.standard) {
    const t = summary.standard.transform;
    const coefs = Object.entries(t.coefficients)
      .map(([n, v]) => `${n}=${Math.abs(v) >= 1e-4 ? v.toFixed(6) : v.toExponential(3)}`)
      .join("  ");
    el.className = "axis-banner";
    el.innerHTML = `Compound times are on <b>${escapeHtml(summary.standard.name)}</b>'s axis,
      via a <b>${escapeHtml(t.model)}</b> fit of this run's ladder over
      ${t.n_indexes} shared indexes
      (RMSE ${t.metrics.rmse.toFixed(4)} min).
      <span class="mono">${escapeHtml(coefs)}</span>`;
  } else {
    el.className = "axis-banner absent";
    el.innerHTML = `Times are the instrument's own.
      ${escapeHtml(summary.standard_error || "No standard ladder available.")}`;
  }
}

/* The index ruler: the ladder's retention-time scale with the compounds on it. */
function drawRuler(summary) {
  const lad = summary.ladder.peaks;
  if (!lad.length) { $("ruler-wrap").innerHTML = ""; return; }
  const W = 1000, H = 132, L = 24, R = 24, y = 74;
  const rts = lad.map(p => p.rt);
  let lo = Math.min(...rts), hi = Math.max(...rts);
  for (const c of summary.compounds) for (const p of c.peaks) {
    lo = Math.min(lo, p.rt); hi = Math.max(hi, p.rt);
  }
  const pad = (hi - lo) * 0.06 || 1;
  lo -= pad; hi += pad;
  const x = (rt) => L + (W - L - R) * (rt - lo) / (hi - lo);

  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Ladder index scale with compound peaks">`;
  s += `<line x1="${L}" y1="${y}" x2="${W - R}" y2="${y}" stroke="var(--rule)" stroke-width="1"/>`;
  for (const p of lad) {
    const px = x(p.rt);
    const col = p.calibrating ? "var(--accent-deep)" : "var(--rule)";
    s += `<line x1="${px}" y1="${y - 7}" x2="${px}" y2="${y + 7}" stroke="${col}" stroke-width="1.4"/>`;
    s += `<text x="${px}" y="${y + 22}" text-anchor="middle" font-size="11" fill="${col}">${p.index}</text>`;
    s += `<text x="${px}" y="${y + 36}" text-anchor="middle" font-size="9" fill="var(--muted)">${p.rt.toFixed(1)}</text>`;
  }
  const lanes = [];
  for (const c of summary.compounds) {
    for (const p of c.peaks) {
      const px = x(p.rt);
      let lane = 0;
      while (lanes[lane] !== undefined && px - lanes[lane] < 96) lane++;
      lanes[lane] = px;
      const ty = y - 20 - lane * 17;
      s += `<line x1="${px}" y1="${y - 7}" x2="${px}" y2="${ty + 4}" stroke="var(--mark)" stroke-width="1"/>`;
      s += `<circle cx="${px}" cy="${y}" r="3" fill="var(--mark)"/>`;
      s += `<text x="${px}" y="${ty}" text-anchor="middle" font-size="10.5" fill="var(--mark)">${escapeHtml(c.name)} ${p.index === null ? "?" : p.index.toFixed(2)}</text>`;
    }
  }
  s += `</svg>`;
  $("ruler-wrap").innerHTML = `<div class="ruler"><h3>Index scale</h3>
    <p>Ladder peaks below the line, compound peaks above, both on retention time in minutes.
    Grey ladder ticks are excluded from the calibration.</p>${s}</div>`;
}

function buildTables(summary) {
  let html = `<div class="table-wrap"><table><caption>Ladder index</caption>
    <thead><tr><th>Index</th><th>Ladder m/z</th><th>RT (min)</th><th>Height</th>
    <th>Area</th><th>Calibrating</th></tr></thead><tbody>`;
  for (const r of summary.ladder.peaks) {
    html += `<tr class="${r.calibrating ? "" : "dim"}"><td>${r.index}</td><td>${r.mass}</td>
      <td>${r.rt.toFixed(2)}</td><td>${r.height.toExponential(2)}</td>
      <td>${r.area.toExponential(2)}</td><td>${r.calibrating ? "yes" : "no"}</td></tr>`;
  }
  html += `</tbody></table></div>`;

  const onStd = summary.axis === "standard";
  const rtHead = onStd ? `RT on ${escapeHtml(summary.standard.name)} (min)` : "RT (min)";
  html += `<div class="table-wrap"><table><caption>Compound peaks</caption>
    <thead><tr><th>Compound</th><th>Nominal mass</th><th>MS2 precursor</th><th>Peak</th>
    <th>${rtHead}</th>${onStd ? "" : "<th>Index</th>"}
    <th>Window (min)</th><th>MS levels</th></tr></thead><tbody>`;
  for (const c of summary.compounds) {
    for (const p of c.peaks) {
      html += `<tr class="${p.extrapolated ? "flag" : ""}">
        <td>${escapeHtml(c.name)}</td><td>${c.nominal_mass ?? ""}</td><td>${c.ms2_precursor ?? ""}</td>
        <td>${p.peak_no}</td><td>${p.rt.toFixed(2)}${p.extrapolated ? " *" : ""}</td>
        ${onStd ? "" : `<td>${p.index === null ? "n/a" : p.index.toFixed(2)}</td>`}
        <td>${p.rt_start.toFixed(2)}–${p.rt_end.toFixed(2)}</td>
        <td>${c.ms_levels.join(", ")}</td></tr>`;
    }
  }
  html += `</tbody></table></div>`;
  $("index-table").innerHTML = html;
}

$("apply-limit").onclick = () => {
  const params = {};
  for (const el of document.querySelectorAll(".settings input")) params[el.name] = el.value;
  showWorking("Replotting", "The scans are already read, so this only takes a moment.");
  fetch(`/api/session/${state.session}/reanalyse`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      params,
      transform_model: $("transform-model").value,
      ladder_limit: $("ladder-limit").value,
      ladder_masses: document.querySelector('input[name=ladder_masses]').value
    })
  }).then(r => r.json()).then(d => pollJob(d.job)).then(loadChromatograms).catch(e => failed(e, 2));
};

$("say-no").onclick = () => {
  showAlert("What to check", [
    "Confirm the ladder file is the ladder run and its masses match the list.",
    "Check every ladder mass shows two peaks and that the index increases left to right.",
    "Adjust the m/z tolerance if a ladder trace looks noisy or empty.",
    "Raise the minimum peak height to skip shoulders, or lower it if a real peak was missed.",
    "Check each compound's nominal mass against the MS2 precursor found in the file.",
    "Adjust the peak window height if the shaded region misses part of a peak.",
  ]);
  goStep(1);
};

/* ================================================ step 3: mass spectra */
$("say-yes").onclick = () => enterStep(3);

function buildSpectra() {
  showWorking("Building mass spectra",
    "Scans from both chromatogram peaks are pooled and summed, one spectrum per MS level and CID channel.");
  return fetch(`/api/session/${state.session}/confirm`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ correct: true })
  }).then(r => r.json()).then(d => pollJob(d.job))
    .then(() => fetch(`/api/session/${state.session}/spectra`).then(r => r.json()))
    .then(showSpectra).catch(e => failed(e, 2));
}

function showSpectra(d) {
  state.spectra = d.figures;
  const box = $("spec-figures");
  box.innerHTML = "";
  const levels = [...new Set(d.figures.map(f => f.level))].sort().join(", MS");
  $("spec-caption").textContent =
    `MS${levels} for each compound. Every spectrum sums the scans from both chromatogram peaks.`;

  const byCompound = {};
  for (const f of d.figures) (byCompound[f.compound] ||= []).push(f);
  for (const [name, figs] of Object.entries(byCompound)) {
    const head = document.createElement("p");
    head.className = "group-head";
    head.innerHTML = `<b>${escapeHtml(name)}</b> — ${figs.length} spectra`;
    box.appendChild(head);
    figs.sort((a, b) => a.level - b.level || a.file.localeCompare(b.file));
    for (const f of figs) plate(box, f, `${f.subtitle} · ${f.n_scans} scans`);
  }
  state.spectraFresh = true;
  reach(3);
  goStep(3);
}

$("back-to-chrom").onclick = () => enterStep(2);
$("to-download").onclick = () => enterStep(4);

function fillManifest() {
  const counts = {};
  for (const f of state.spectra) counts[`MS${f.level}`] = (counts[`MS${f.level}`] || 0) + 1;
  const n = state.summary.compounds.length;
  const levels = Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(", ");
  $("manifest").innerHTML = `
    <li>Chromatogram figures<span>${n + 1} figures, PNG and PDF</span></li>
    <li>Mass spectrum figures<span>${state.spectra.length} figures (${levels}), PNG and PDF</span></li>
    <li>Chromatogram traces<span>every retention time and intensity, ladder and compounds</span></li>
    <li>Peak tables<span>index, height, area, and peak 1 vs peak 2 ratios</span></li>
    <li>Peak lists<span>m/z, intensity and % base peak — pooled and per peak</span></li>
    <li>results.json<span>the whole lot in one machine-readable file</span></li>`;
  if (!state.exportFresh) {
    $("download").classList.add("hidden");
    $("build-export").classList.remove("hidden");
    $("build-export").disabled = false;
  }
  reach(4);
}

/* ==================================================== step 4: download */
$("back-to-spectra").onclick = () => enterStep(3);
$("build-export").onclick = () => {
  $("build-export").disabled = true;
  showWorking("Creating the results file", "Figures are re-rendered at print resolution.");
  fetch(`/api/session/${state.session}/export`, { method: "POST" })
    .then(r => r.json()).then(d => {
      if (d.error) throw new Error(d.error);
      return pollJob(d.job);
    }).then(() => {
      state.exportFresh = true;
      goStep(4);
      $("build-export").classList.add("hidden");
      const a = $("download");
      a.href = `/api/session/${state.session}/download`;
      a.classList.remove("hidden");
    }).catch(e => { $("build-export").disabled = false; failed(e, 4); });
};
