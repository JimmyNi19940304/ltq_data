const $ = (id) => document.getElementById(id);
const state = {
  session: null, compounds: 0, summary: null, spectra: [], standard: null,
  reached: 1,          // highest step whose content exists
  model: "cubic",      // the fit model chosen on step 2
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
    $("standard-path").textContent = h.standard.standard_file || "";
    showStandard(h.standard);
  } else {
    card.classList.add("absent");
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

/* The saved standard ladder (procedure 1): read from standard_ladder/, shown
   with its index table; the chromatogram is drawn on request since it means
   reading the standard's own .raw file. */
function showStandard(std) {
  $("standard-view").classList.remove("hidden");
  $("standard-view-title").textContent = `Standard ladder — ${std.name}`;
  const usable = new Set((std.usable_indexes || []).map(Number));
  const byHand = new Set((std.manual_indexes || []).map(Number));
  const absent = new Set((std.absent_indexes || []).map(Number));
  const total = (std.ladder_masses || []).length * 2 || 16;
  let chips = "";
  for (let i = 1; i <= total; i++) {
    const rt = std.peaks[i];
    if (rt === undefined) {
      chips += `<span class="chip none" title="${absent.has(i) ? "marked as having no peak" : "no peak found"}">${i} —</span>`;
    } else {
      chips += `<span class="chip${usable.has(i) ? "" : " off"}"
        title="${usable.has(i) ? "used for fitting" : "unticked on the standard"}">${i}
        <em>${Number(rt).toFixed(2)}</em>${byHand.has(i) ? " ◆" : ""}</span>`;
    }
  }
  $("standard-chips").innerHTML = chips;
  $("standard-toggle").onclick = () => {
    const box = $("standard-figure");
    const opening = box.classList.contains("hidden");
    box.classList.toggle("hidden", !opening);
    $("standard-toggle").textContent = opening ? "Hide chromatogram" : "Show chromatogram";
    if (!opening || box.dataset.loaded) return;
    box.dataset.loaded = "1";
    box.innerHTML = `<p class="note">Drawing the standard from its .raw file…</p>`;
    fetch("/api/standard/figure?t=" + Date.now()).then(r => {
      if (r.ok) return r.blob().then(b => {
        box.innerHTML = "";
        const img = document.createElement("img");
        img.src = URL.createObjectURL(b);
        img.alt = `Standard ladder ${std.name}`;
        box.appendChild(img);
      });
      return r.json().then(d => { throw new Error(d.error || `Server returned ${r.status}`); });
    }).catch(e => {
      delete box.dataset.loaded;
      box.innerHTML = `<p class="note">${escapeHtml(e.message)}</p>`;
    });
  };
}

/* ----------------------------------------------------------- step rail */
function reach(n) {
  state.reached = Math.max(state.reached, n);
  paintRail(currentStep());
}
function currentStep() {
  for (const n of [1, 2, 3, 4, 5]) if (!$("step-" + n).classList.contains("hidden")) return n;
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
  if (n === 4 && !(state.spectraFresh && state.spectra.length)) return buildSpectra();
  if (n === 5) fillManifest();
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
    $("working-title").textContent = "Reading the ladder";
    $("working-hint").textContent =
      "The first read of a .raw file takes roughly half a minute. The same file is reused after that.";
    pollJob(data.job).then(loadLadder).catch(e => failed(e, 1));
  };
  xhr.onerror = () => failed(new Error("Upload failed"), 1);
  xhr.send(fd);
};

/* ============================================ step 2: ladder and fit */
const api = (path) => `/api/session/${state.session}${path}`;
const postJson = (path, body) => fetch(api(path), {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
}).then(r => r.json());

function loadLadder() {
  return fetch(api("/ladder")).then(r => r.json()).then(d => {
    if (d.error) throw new Error(d.error);
    showLadderPage(d);
    state.reached = 2;
    reach(2);
    goStep(2);
  });
}

/* Draw step 2 from the server's view of it. Anything after it is now stale. */
function showLadderPage(d, message) {
  resetPeakEditor();
  state.ladderPage = d;
  state.selected = (d.selected || []).map(Number);
  state.compoundsFresh = false;
  state.spectraFresh = false;
  state.exportFresh = false;
  state.reached = Math.min(state.reached, 2);
  const l = d.ladder;
  $("ladder-caption").innerHTML =
    `Ladder <b>${escapeHtml(l.name)}</b>: ${l.n_peaks} of ${l.expected} peaks, numbered 1–${l.expected}
     by ladder mass. Fix the peaks if needed, tick the indexes to fit with, then fit.`;
  const slot = $("ladder-slot");
  slot.innerHTML = "";
  slot.appendChild(mainLadderCard(l));
  if (message) slot.querySelector("[data-role=rt-max-status]").textContent = message;
  if (d.model) state.model = d.model;
  renderComparison(d.comparison);
  renderFit(d);
  showAlert("Worth checking", l.warnings || []);
  paintRail(2);
}

function mainLadderCard(l) {
  const d = state.ladderPage;
  const usable = d.standard ? new Set(d.standard.usable_indexes.map(Number)) : null;
  const card = document.createElement("div");
  card.className = "ladder";
  const badge = l.monotonic ? "" : `<span class="badge">retention times not increasing</span>`;
  const custom = l.rt_max_custom ? `<span class="badge custom">own rt_max</span>` : "";
  const warn = (l.warnings || []).length
    ? `<ul class="card-warnings">${l.warnings.map(w => `<li>${escapeHtml(w)}</li>`).join("")}</ul>` : "";
  card.innerHTML = `
    <div class="ladder-head">
      <b>${escapeHtml(l.name)}</b>${badge}${custom}
      <span class="meta" data-role="count"></span>
    </div>
    <div class="ladder-body">
      <div class="ladder-plot">
        <img src="${api("/figure/" + encodeURIComponent(l.figure))}?t=${Date.now()}" alt="${escapeHtml(l.name)} chromatogram">
        <form class="rt-max" data-role="rt-max">
          <label>Retention time limit
            <input type="number" class="mono" min="0.1" step="0.1" value="${l.rt_max}"
              data-role="rt-max-input"> min</label>
          <button type="submit" class="ghost small">Apply</button>
          ${l.rt_max_custom ? `<button type="button" class="ghost small" data-role="rt-max-reset">
            Back to ${l.rt_max_default}</button>` : `<span class="note">run default</span>`}
          <span class="note" data-role="rt-max-status"></span>
          <button type="button" class="ghost small edit-peaks" data-role="edit-peaks">Edit peaks</button>
        </form>
        ${warn}
      </div>
      <div class="ladder-indexes">
        <h4>Indexes to fit with</h4>
        <p class="note">${usable ? "Greyed-out indexes are not usable in the standard." : ""}</p>
        <div class="index-list" data-role="list"></div>
        <div class="index-actions">
          <button type="button" class="ghost small" data-role="all">All</button>
          <button type="button" class="ghost small" data-role="none">None</button>
        </div>
      </div>
    </div>`;

  const present = Object.keys(l.peaks).map(Number).sort((a, b) => a - b);
  const list = card.querySelector("[data-role=list]");
  const exact = new Set((l.exact || []).map(Number));
  let built = false;
  const count = () => {
    card.querySelector("[data-role=count]").textContent =
      `${state.selected.length} of ${present.length} indexes ticked`;
    // new ticks: the three fits are worked out again for them
    if (built) scheduleComparison();
    fitStale();
  };
  const paint = () => {
    const chosen = new Set(state.selected);
    for (const row of list.children) {
      const i = Number(row.dataset.i);
      row.querySelector("input").checked = chosen.has(i);
      row.classList.toggle("off", !chosen.has(i));
    }
    count();
  };
  for (const i of present) {
    const row = document.createElement("label");
    row.dataset.i = i;
    const blocked = usable && !usable.has(i);
    const mark = l.manual && l.manual[i] !== undefined
      ? `<span class="by-hand" title="${exact.has(i) ? "placed exactly by hand" : "placed by hand"}">${exact.has(i) ? "◇" : "◆"}</span>` : "";
    row.innerHTML = `<input type="checkbox" ${blocked ? "disabled" : ""}>
      <span class="idx">${i}</span><span class="rt">${l.peaks[i].toFixed(2)}</span>${mark}`;
    if (blocked) row.title = "Not usable in the standard ladder";
    row.querySelector("input").onchange = (ev) => {
      const set = new Set(state.selected);
      ev.target.checked ? set.add(i) : set.delete(i);
      state.selected = [...set].sort((a, b) => a - b);
      paint();
    };
    list.appendChild(row);
  }
  const allowed = present.filter(i => !usable || usable.has(i));
  card.querySelector("[data-role=all]").onclick = () => { state.selected = [...allowed]; paint(); };
  card.querySelector("[data-role=none]").onclick = () => { state.selected = []; paint(); };
  paint();
  built = true;

  card.querySelector("[data-role=rt-max]").onsubmit = (ev) => {
    ev.preventDefault();
    applyLadderRtMax(card, card.querySelector("[data-role=rt-max-input]").value);
  };
  const reset = card.querySelector("[data-role=rt-max-reset]");
  if (reset) reset.onclick = () => applyLadderRtMax(card, null);
  card.querySelector("[data-role=edit-peaks]").onclick = () => openPeakEditor(card, l, {
    traces: () => fetch(api("/ladder/traces")).then(r => r.json()),
    save: (picks) => postJson("/ladder/peaks", { picks }),
    rebuild: (entry) => mainLadderCard(entry),
    onSaved: (_card, d2) => showLadderPage(d2, "Peaks saved; fit again"),
    alert: showAlert,
  });
  return card;
}

function applyLadderRtMax(card, value) {
  const status = card.querySelector("[data-role=rt-max-status]");
  card.querySelectorAll("[data-role=rt-max] button, [data-role=rt-max] input")
    .forEach(el => { el.disabled = true; });
  status.textContent = "Re-picking peaks…";
  postJson("/ladder/rt_max", { rt_max: value }).then(d => {
    if (d.error) throw new Error(d.error);
    showLadderPage(d, `${d.ladder.n_peaks} peaks; ticks reset, fit again`);
  }).catch(e => {
    card.querySelectorAll("[data-role=rt-max] button, [data-role=rt-max] input")
      .forEach(el => { el.disabled = false; });
    status.textContent = "";
    showAlert("Could not apply the limit", [e.message]);
  });
}

const sameSet = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);

/* A fit is only good for the ticks and model it was made with. */
function fitStale() {
  const d = state.ladderPage;
  if (!d) return;
  const go = $("to-compounds");
  if (!d.standard) { go.disabled = false; return; }
  const fit = d.fit;
  const current = fit && sameSet(fit.transform.indexes.map(Number),
    state.selected.filter(i => d.standard.usable_indexes.includes(i)))
    && fit.model === state.model;
  go.disabled = !current;
  $("fit-status").textContent = fit && !current
    ? "Ticks or model changed since this fit — fit again to continue." : "";
}

const MODEL_NAMES = { linear: "Linear", quadratic: "Quadratic", cubic: "Cubic" };
const modelName = (key) => MODEL_NAMES[key] || key;

/* The three models side by side for the ticked indexes, worked out before any
   is chosen. Click a row to choose; the button below fits with it. */
function renderComparison(c) {
  const box = $("fit-compare");
  state.comparison = c;
  $("run-fit").textContent = `Fit with ${modelName(state.model).toLowerCase()}`;
  if (!c) { box.innerHTML = ""; return; }
  if (c.error || !c.models.length) {
    box.innerHTML = `<p class="note fit-error">${escapeHtml(c.error || "Nothing could be fitted.")}</p>`;
    $("run-fit").disabled = true;
    return;
  }
  $("run-fit").disabled = !c.models.some(m => m.key === state.model);
  const fit = state.ladderPage && state.ladderPage.fit;
  const inUse = fit && sameSet(fit.transform.indexes.map(Number), c.indexes) ? fit.model : null;
  const best = (k) => Math.min(...c.models.map(m => m[k] ?? Infinity));
  const num = (v, k, digits) => v === null || v === undefined ? "—"
    : `<span class="${c.models.length > 1 && v === best(k) ? "best" : ""}">${v.toFixed(digits)}</span>`;
  const rows = c.models.map(m => `
    <tr data-model="${m.key}" class="${m.key === state.model ? "chosen" : ""}">
      <td><input type="radio" name="fit-choice" ${m.key === state.model ? "checked" : ""}
        aria-label="${modelName(m.key)}"></td>
      <td>${modelName(m.key)}${m.key === c.suggested ? ' <span class="tag">suggested</span>' : ""}${m.key === inUse ? ' <span class="tag used">in use</span>' : ""}</td>
      <td class="mono">${num(m.rmse, "rmse", 4)}</td>
      <td class="mono">${num(m.std_error, "std_error", 4)}</td>
      <td class="mono">${num(m.max_abs_residual, "max_abs_residual", 4)}</td>
      <td class="mono">${m.r2.toFixed(6)}</td>
      <td class="mono">${m.adjusted_r2 === null || m.adjusted_r2 === undefined ? "—" : m.adjusted_r2.toFixed(6)}</td>
      <td class="${m.turns_at === null ? "" : "warn"}">${m.turns_at === null ? "no" : `yes, at ${m.turns_at} min`}</td>
    </tr>`).join("");
  const chosen = c.models.find(m => m.key === state.model);
  const notes = [];
  if (c.excluded_by_standard.length) {
    notes.push(`Index ${c.excluded_by_standard.join(", ")} ticked but not usable in the standard, so left out.`);
  }
  if (c.uncalibrated) notes.push(`With no fit at all, RMSE ${c.uncalibrated.rmse.toFixed(4)} min.`);
  notes.push(`"Suggested" is the simplest model no other beats by more than 5% on standard error.
    Lowest value in each column in bold. "Turns over" means the curve stops rising inside
    this run, which would put later times out of order.`);
  box.innerHTML = `
    <div class="table-wrap"><table class="fit-table compare">
      <caption>All three models on ${c.indexes.length} indexes (${c.indexes.join(", ")}). Choose one.</caption>
      <thead><tr><th></th><th>Model</th><th>RMSE (min)</th><th>Std. error</th>
      <th>Largest residual</th><th>R²</th><th>Adjusted R²</th><th>Turns over</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    ${chosen ? `<p class="fit-equation"><b>${modelName(chosen.key)}:</b>
      <span class="mono">${escapeHtml(chosen.equation)}</span></p>` : ""}
    <p class="note">${notes.join(" ")}</p>`;
  for (const tr of box.querySelectorAll("tr[data-model]")) {
    tr.onclick = () => {
      state.model = tr.dataset.model;
      renderComparison(state.comparison);
      fitStale();
    };
  }
}

let compareTimer = null;
function scheduleComparison() {
  clearTimeout(compareTimer);
  compareTimer = setTimeout(() => {
    if (!state.ladderPage || !state.ladderPage.standard) return;
    const asked = state.selected.slice();
    postJson("/fit/compare", { indexes: asked }).then(c => {
      if (!sameSet(asked, state.selected)) return;     // ticks moved on meanwhile
      if (c.error && !c.models) throw new Error(c.error);
      renderComparison(c);
    }).catch(e => showAlert("Could not compare the fits", [e.message]));
  }, 250);
}

function renderFit(d) {
  const box = $("fit-result");
  if (!d.standard) {
    $("fit-line").textContent = (d.standard_error || "No standard ladder saved.") +
      " Compound times will be the instrument's own.";
    $("fit-controls").classList.add("hidden");
    box.innerHTML = "";
    fitStale();
    return;
  }
  $("fit-controls").classList.remove("hidden");
  $("fit-line").innerHTML = `x = this run's ladder retention time, y = the same index on
    <b>${escapeHtml(d.standard.name)}</b>. Only ticked indexes that are usable in the standard
    take part. The three models below are already worked out for the ticked indexes and
    update as you tick.`;
  const f = d.fit;
  if (!f) {
    box.innerHTML = `<p class="note">Choose a model in the table, then fit to see its plot.</p>`;
    fitStale();
    return;
  }
  const notes = [];
  if (f.monotonic && !f.monotonic.monotonic) {
    notes.push(`The ${f.model} curve turns over at ${f.monotonic.turns_at} min, so later
      times would be mapped out of order. Try a simpler model or other indexes.`);
  }
  if (f.excluded_by_standard.length) {
    notes.push(`Index ${f.excluded_by_standard.join(", ")} ticked here but not usable in the standard, so left out.`);
  }
  box.innerHTML = `
    <figure class="plate fit-plate"><img src="${api("/figure/" + encodeURIComponent(f.figure))}?t=${Date.now()}"
      alt="Ladder fit"><figcaption><b>${escapeHtml(f.transform.model)} fit over
      ${f.transform.n_indexes} indexes</b><span>${f.transform.indexes.join(", ")}</span></figcaption></figure>
    ${notes.length ? `<ul class="card-warnings fit-notes">${notes.map(n => `<li>${n}</li>`).join("")}</ul>` : ""}`;
  box.querySelector("img").onclick = () => {
    $("lightbox-img").src = box.querySelector("img").src;
    $("lightbox").classList.remove("hidden");
  };
  fitStale();
}

$("run-fit").onclick = () => {
  const button = $("run-fit");
  button.disabled = true;
  $("fit-status").textContent = "Fitting…";
  postJson("/fit", { model: state.model, indexes: state.selected }).then(d => {
    button.disabled = false;
    if (d.error) throw new Error(d.error);
    showLadderPage(d);
    $("fit-card").scrollIntoView({ behavior: "smooth", block: "start" });
  }).catch(e => {
    button.disabled = false;
    $("fit-status").textContent = "";
    showAlert("Could not fit", [e.message]);
  });
};

$("back-to-input").onclick = () => goStep(1);
$("to-compounds").onclick = () => {
  if (peakEditorOpen()) return showAlert("Finish the peak edit first", ["Save or cancel the open peak editor."]);
  showWorking("Reading the compounds",
    "Each compound is put on the standard's time axis through the fit you approved.");
  postJson("/compounds").then(d => {
    if (d.error) throw new Error(d.error);
    return pollJob(d.job);
  }).then(loadChromatograms).catch(e => failed(e, 2));
};

/* ============================================== step 3: chromatograms */
function loadChromatograms() {
  return fetch(`/api/session/${state.session}/chromatograms`).then(r => r.json()).then(d => {
    renderChromatograms(d);
    showAlert("Check these before you continue", d.summary.warnings);
    state.reached = 3;
    reach(3);
    goStep(3);
  });
}

/* Step 3's figures, scale and tables. Also called after a compound's peaks are
   edited, which makes any spectra built before stale. */
function renderChromatograms(d) {
    state.chrom = d;
    state.summary = d.summary;
    resetCompoundEditor();
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
      const c = fig.kind === "compound" ? d.summary.compounds[fig.compound] : null;
      if (c) {
        sub = c.peaks.map(p =>
          `peak ${p.peak_no}: ${p.rt.toFixed(2)} min = index ${p.index === null ? "n/a" : p.index.toFixed(2)}`
          + (p.manual || p.window_manual ? " ◆" : "")).join("   ·   ");
      }
      if (!c) { plate(box, fig, sub); continue; }
      const holder = document.createElement("div");
      holder.className = "ce-holder";
      box.appendChild(holder);
      plate(holder, fig, sub);
      const bar = document.createElement("div");
      bar.className = "ce-open";
      bar.innerHTML = `<span class="note">Spectrum windows: ${c.window_fwhm} × FWHM unless set by
        hand. Index ${c.index_from === "standard" ? "read off the standard ladder" : "from this run's ladder"}.</span>
        <button type="button" class="ghost small" data-role="edit-compound">Edit peaks and windows</button>`;
      holder.appendChild(bar);
      bar.querySelector("button").onclick = () => openCompoundEditor(holder, fig.compound, {
        trace: () => fetch(api(`/compound/${fig.compound}/trace`)).then(r => r.json()),
        save: (body) => fetch(api(`/compound/${fig.compound}/peaks`), {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }).then(r => r.json()),
        close: (reply) => {
          if (!reply) return renderChromatograms(state.chrom);
          renderChromatograms(reply);
          showAlert("Check these before you continue", reply.summary.warnings);
          // the spectra and the download were built from the old windows
          state.reached = Math.min(state.reached, 3);
          reach(3);
        },
        alert: (title, items) => showAlert(title, items),
      });
    }

    buildTables(d.summary);
    // the scans behind the spectra just changed, so anything downstream is stale
    state.spectraFresh = false;
    state.exportFresh = false;
}

function renderAxisBanner(summary) {
  let el = $("axis-banner");
  if (!el) {
    el = document.createElement("div");
    el.id = "axis-banner";
    $("step-3").querySelector(".panel-head").after(el);
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
    lo = Math.min(lo, p.rt_raw); hi = Math.max(hi, p.rt_raw);
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
      // this run's own time, the scale the ladder ticks are drawn on
      const px = x(p.rt_raw);
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
    <th>${rtHead}</th><th>Index</th>
    <th>Window (min)</th><th>MS levels</th></tr></thead><tbody>`;
  for (const c of summary.compounds) {
    for (const p of c.peaks) {
      html += `<tr class="${p.extrapolated ? "flag" : ""}">
        <td>${escapeHtml(c.name)}</td><td>${c.nominal_mass ?? ""}</td><td>${c.ms2_precursor ?? ""}</td>
        <td>${p.peak_no}</td><td>${p.rt.toFixed(2)}${p.extrapolated ? " *" : ""}</td>
        <td>${p.index === null ? "n/a" : p.index.toFixed(2)}${p.manual ? " ◆" : ""}</td>
        <td>${p.rt_start.toFixed(2)}–${p.rt_end.toFixed(2)}${p.window_manual ? " ◆" : ""}</td>
        <td>${c.ms_levels.join(", ")}</td></tr>`;
    }
  }
  html += `</tbody></table></div>`;
  $("index-table").innerHTML = html;
}

$("say-no").onclick = () => {
  showAlert("What to check", [
    "Confirm the ladder file is the ladder run and its masses match the list.",
    "Check every ladder mass shows two peaks and that the index increases left to right.",
    "Adjust the m/z tolerance if a ladder trace looks noisy or empty.",
    "Raise the minimum peak height to skip shoulders, or lower it if a real peak was missed.",
    "Check each compound's nominal mass against the MS2 precursor found in the file.",
    "Use Edit peaks and windows on a chromatogram to move a peak or resize its shaded window.",
  ]);
  goStep(1);
};

/* ================================================ step 4: mass spectra */
$("say-yes").onclick = () => enterStep(4);

function buildSpectra() {
  showWorking("Building mass spectra",
    "Scans from both chromatogram peaks are pooled and summed, one spectrum per MS level and CID channel.");
  return fetch(`/api/session/${state.session}/confirm`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ correct: true })
  }).then(r => r.json()).then(d => pollJob(d.job))
    .then(() => fetch(`/api/session/${state.session}/spectra`).then(r => r.json()))
    .then(showSpectra).catch(e => failed(e, 3));
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
  reach(4);
  goStep(4);
}

$("back-to-chrom").onclick = () => enterStep(3);
$("to-download").onclick = () => enterStep(5);

function fillManifest() {
  const counts = {};
  for (const f of state.spectra) counts[`MS${f.level}`] = (counts[`MS${f.level}`] || 0) + 1;
  const n = state.summary.compounds.length;
  const levels = Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(", ");
  $("manifest").innerHTML = `
    <li>Chromatogram figures<span>the ladder${state.ladderPage && state.ladderPage.fit ? ", its fit" : ""}
      and ${n} compound${n === 1 ? "" : "s"}, PNG and PDF</span></li>
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
  reach(5);
}

/* ==================================================== step 5: download */
$("back-to-spectra").onclick = () => enterStep(4);
$("build-export").onclick = () => {
  $("build-export").disabled = true;
  showWorking("Creating the results file", "Figures are re-rendered at print resolution.");
  fetch(`/api/session/${state.session}/export`, { method: "POST" })
    .then(r => r.json()).then(d => {
      if (d.error) throw new Error(d.error);
      return pollJob(d.job);
    }).then(() => {
      state.exportFresh = true;
      goStep(5);
      $("build-export").classList.add("hidden");
      const a = $("download");
      a.href = `/api/session/${state.session}/download`;
      a.classList.remove("hidden");
    }).catch(e => { $("build-export").disabled = false; failed(e, 5); });
};
