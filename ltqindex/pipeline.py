"""Ties the pieces together: one Session per set of uploaded files."""

from __future__ import annotations

import csv
import json
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .analysis import Compound, Params, analyse_ladder, DEFAULT_LADDER_MASSES
from .calibration import calibrate
from .compoundedit import load_overrides as load_compound_edits, parse_edits, store_edits
from .ladderedit import (ladder_entry, load_overrides, parse_picks, parse_rt_max,
                         saved_picks, store_override, with_rt_max)
from .msdata import load_run
from .standards import (StandardError_, build_transform, compound_output_dir, index_scale,
                        load_standard)
from .plotting import plot_compound, plot_fit, plot_ladder, plot_spectrum

EXPORT_FORMATS = ("png", "pdf")


def spectrum_slug(spec):
    """MS1, MS2_1419.5, MS3_1419.5_1013.5 - the CID chain identifies the channel."""
    chain = "_".join(f"{m:g}" for m in spec["chain_mz"])
    return f"MS{spec['level']}" + (f"_{chain}" if chain else "")


def safe_name(text, fallback="file"):
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(text)).strip("_")
    return cleaned or fallback


@dataclass
class Job:
    id: str
    stage: str = "queued"
    message: str = "Waiting to start"
    progress: float = 0.0
    done: bool = False
    error: str = ""
    log: list = field(default_factory=list)

    def update(self, message, progress=None, stage=None):
        self.message = message
        if progress is not None:
            self.progress = max(0.0, min(1.0, progress))
        if stage:
            self.stage = stage
        self.log.append({"t": time.time(), "message": message})
        del self.log[:-200]

    def to_dict(self):
        return {
            "id": self.id,
            "stage": self.stage,
            "message": self.message,
            "progress": round(self.progress, 3),
            "done": self.done,
            "error": self.error,
            "log": self.log[-25:],
        }


class Session:
    """Holds the uploaded files, the analysis results and the generated figures.

    The work runs in the order the operator sees it: the standard ladder is read,
    this run's own ladder is peak-picked and corrected by hand (step 2), it is
    fitted onto the standard with the indexes ticked there, and only then are the
    compounds read and placed on the standard's time axis (step 3 onwards).
    """

    def __init__(self, root: Path, overrides_folder=None):
        self.id = uuid.uuid4().hex[:12]
        self.root = Path(root) / self.id
        self.uploads = self.root / "uploads"
        # shared across sessions: the same ladder file is read once, not once per run
        self.cache = Path(root) / "scan-cache"
        self.figures = self.root / "figures"
        for d in (self.uploads, self.cache, self.figures):
            d.mkdir(parents=True, exist_ok=True)

        self.ladder_path = None
        self.ladder_file = ""         # the ladder's own file name: the key for hand edits
        self.ladder_name = ""
        self.ladder_masses = list(DEFAULT_LADDER_MASSES)
        self.ladder_dir = None        # from config.json; locates standard_ladder/
        # ladder_overrides.json lives here, shared with the calibration app
        self.overrides_folder = overrides_folder
        self.ladder_rt_max = None     # this ladder's own limit; None = params.rt_max
        self.ladder_picks = {}        # index -> hand-placed pick, or None for "no peak"
        self.selected_indexes = None  # ticked on the ladder page; None = all
        self.ladder_run = None
        self.ladder_entry = None      # what the ladder page draws
        self.fit = None               # every model's fit, for the ladder page
        self.fit_figure = None
        self.transform_model = "cubic"
        self.standard = None          # the saved standard ladder record
        self.transform = None         # this run's ladder onto that standard
        self.transform_error = None
        self.compound_specs = []   # [{path, name, nominal_mass}]
        self.params = Params()

        self.ladder = None
        self.compounds = []
        self.chromatogram_figures = []
        self.spectrum_figures = []
        self.confirmed = False
        self.lock = threading.Lock()

    # ------------------------------------------------------- standard ladder
    def _load_standard(self):
        self.standard = None
        self.transform_error = None
        if not self.ladder_dir:
            self.transform_error = (
                "No config.json, so no standard ladder folder to look in. Times are "
                "the instrument's own."
            )
            return
        try:
            self.standard = load_standard(self.ladder_dir)
        except StandardError_ as exc:
            self.transform_error = str(exc)
            return
        if self.standard is None:
            self.transform_error = (
                "No standard ladder has been saved yet. Save one in the calibration "
                "app to put compounds on a common time axis."
            )

    # ----------------------------------------------------------- ladder page
    def run_ladder(self, job: Job):
        with self.lock:
            self._run_ladder(job)

    def _run_ladder(self, job: Job):
        job.update("Reading the standard ladder", 0.02, "ladder")
        self._load_standard()

        # start from whatever was set for this ladder file before, in either app
        if self.overrides_folder and self.ladder_file:
            saved = load_overrides(self.overrides_folder).get(self.ladder_file, {})
            self.ladder_rt_max = saved.get("rt_max")
            self.ladder_picks = dict(saved.get("peaks") or {})

        job.update("Reading the ladder file", 0.05, "ladder")
        self.ladder_run = load_run(
            self.ladder_path, self.cache, progress=lambda m: job.update(m, 0.5),
        )
        self.ladder_name = self.ladder_run.source_name
        job.update("Extracting ladder ion traces", 0.9, "ladder")
        try:
            self._analyse_ladder()
        except ValueError:
            if self.ladder_rt_max is None:
                raise
            # a saved limit that no longer works should not block the run
            self.ladder_rt_max = None
            self._analyse_ladder()
        job.update("Ladder ready for review", 1.0, "ladder_review")
        job.done = True

    @property
    def ladder_rt_limit(self):
        return self.ladder_rt_max if self.ladder_rt_max is not None else self.params.rt_max

    def _analyse_ladder(self):
        """Peak-pick this run's ladder with its own limit and hand placements.

        Indexes are numbered by their place in the full ladder, so one marked as
        having no peak leaves a gap rather than renumbering the rest. Any fit made
        before is dropped: it was made on the old peaks.
        """
        params = with_rt_max(self.params, self.ladder_rt_limit)
        self.ladder = analyse_ladder(self.ladder_run, self.ladder_masses, params,
                                     manual=self.ladder_picks, fixed_slots=True)
        self._plot_ladder()
        present = set(int(k) for k in self.ladder_entry["peaks"])
        if self.selected_indexes is not None:
            self.selected_indexes = sorted(set(self.selected_indexes) & present)
        self.fit = None
        self.fit_figure = None
        self.transform = None
        self.compounds = []

    def _plot_ladder(self):
        for f in self.figures.glob("ladder_*"):
            f.unlink()
        stem = self.figures / f"ladder_{safe_name(self.ladder_name)}_{int(time.time() * 1000)}"
        plot_ladder(self.ladder, self.ladder_name, stem, formats=("png",))
        self.ladder_entry = ladder_entry(
            self.ladder, name=self.ladder_name, file=self.ladder_file,
            figure=stem.name + ".png", rt_max=self.ladder_rt_limit,
            default_rt_max=self.params.rt_max, masses=self.ladder_masses,
        )

    def _remember_ladder(self):
        if self.overrides_folder and self.ladder_file:
            store_override(self.overrides_folder, self.ladder_file,
                           rt_max=self.ladder_rt_limit, default_rt_max=self.params.rt_max,
                           peaks=saved_picks(self.ladder_entry))

    def set_ladder_rt_max(self, value):
        """Raises ValueError with a message for the operator."""
        with self.lock:
            rt_max = parse_rt_max(value, self.params.rt_max)
            before = (self.ladder_rt_max, self.ladder_picks)
            self.ladder_rt_max = None if rt_max == self.params.rt_max else rt_max
            self.ladder_picks = saved_picks(self.ladder_entry)
            try:
                self._analyse_ladder()
            except ValueError:
                self.ladder_rt_max, self.ladder_picks = before
                raise
            # a hand-placed peak now past the limit is let go
            self.ladder_picks = saved_picks(self.ladder_entry)
            self._remember_ladder()

    def set_ladder_picks(self, raw):
        """Raises ValueError with a message for the operator."""
        with self.lock:
            picks = parse_picks(raw, 2 * len(self.ladder_masses))
            before = self.ladder_picks
            self.ladder_picks = picks
            self._analyse_ladder()
            if self.ladder.dropped:
                dropped = self.ladder.dropped
                self.ladder_picks = before
                self._analyse_ladder()
                raise ValueError("Index " + ", ".join(map(str, dropped))
                                 + " was placed past the retention time limit.")
            self._remember_ladder()

    def ladder_page(self):
        """Everything step 2 draws: the ladder, the standard, and the fit if made."""
        std = self.standard
        usable = (sorted(int(i) for i in (std.get("usable_indexes") or std["peaks"]))
                  if std else [])
        present = sorted(int(k) for k in self.ladder_entry["peaks"])
        selected = (self.selected_indexes if self.selected_indexes is not None
                    else [i for i in present if not std or i in usable])
        return {
            "ladder": self.ladder_entry,
            "selected": selected,
            "standard": ({
                "name": std["name"],
                "saved_at": std.get("saved_at"),
                "peaks": {int(k): v for k, v in std["peaks"].items()},
                "usable_indexes": usable,
            } if std else None),
            "standard_error": self.transform_error if not std else None,
            "model": self.transform_model,
            "fit": self.fit_payload(),
        }

    def fit_ladder(self, model, indexes):
        """Fit this run's ladder onto the standard with the ticked indexes.

        Every model is fitted so they can be compared; `model` is the one the
        compounds will be put through. Raises ValueError with a message.
        """
        with self.lock:
            if self.standard is None:
                raise ValueError(self.transform_error or "No standard ladder to fit onto.")
            local = {int(k): float(v) for k, v in self.ladder_entry["peaks"].items()}
            selected = sorted({int(i) for i in indexes} & set(local))
            std_peaks = {int(k): float(v) for k, v in self.standard["peaks"].items()}
            usable = {int(i) for i in (self.standard.get("usable_indexes") or std_peaks)}
            chosen = sorted(set(selected) & usable & set(std_peaks))

            fit = calibrate(std_peaks, local, indexes=chosen)
            try:
                transform = build_transform(local, self.standard, model=model,
                                            indexes=selected)
            except StandardError_ as exc:
                raise ValueError(str(exc)) from None

            self.transform_model = model
            self.selected_indexes = selected
            self.ladder.excluded = set(local) - set(selected)
            self._plot_ladder()           # unticked indexes are greyed on the figure
            self.fit = fit
            self.transform = transform
            self.compounds = []
            for f in self.figures.glob("fit_*"):
                f.unlink()
            stem = self.figures / f"fit_{model}_{int(time.time() * 1000)}"
            plot_fit(fit, model, self.ladder_name, self.standard["name"], stem)
            self.fit_figure = stem.name + ".png"

    def fit_payload(self):
        if not self.fit or not self.transform:
            return None
        t = self.transform
        span = (float(self.ladder.traces[0]["rt"][0]), float(self.ladder.traces[0]["rt"][-1]))
        mono = t.check_monotonic(*span)
        return {
            "model": t.model,
            "figure": self.fit_figure,
            "transform": t.to_dict(),
            "monotonic": mono,
            "models": [
                {k: m[k] for k in ("key", "label", "equation", "rmse", "std_error",
                                   "max_abs_residual", "r2", "adjusted_r2", "exact")}
                for m in self.fit.get("models", [])
            ],
            "uncalibrated": self.fit.get("uncalibrated"),
            "suggested": self.fit.get("best"),
            "excluded_by_standard": sorted(
                set(self.selected_indexes or [])
                - {int(i) for i in (self.standard.get("usable_indexes")
                                    or self.standard["peaks"])}),
            "error": self.fit.get("error"),
        }

    # ------------------------------------------------------------- analysis
    def run_chromatograms(self, job: Job):
        # one job at a time per session: two overlapping runs would each reset and
        # refill self.compounds, leaving duplicates behind
        with self.lock:
            self._run_chromatograms(job)

    def _run_chromatograms(self, job: Job):
        """Read every compound and put it through the fit made on the ladder page."""
        if self.ladder is None:
            raise ValueError("Read the ladder first.")
        n_files = len(self.compound_specs)
        step = 0.76 / max(1, n_files)

        saved = (load_compound_edits(self.overrides_folder)
                 if self.overrides_folder else {})
        self.compounds = []
        for i, spec in enumerate(self.compound_specs):
            base = 0.02 + step * i
            job.update(f"Reading {spec['name']}", base, "chromatograms")
            run = load_run(
                spec["path"], self.cache,
                progress=lambda m, b=base: job.update(m, min(b + step * 0.8, 0.78)),
            )
            job.update(f"Building the MS2 chromatogram for {spec['name']}", base + step * 0.9)
            edits = saved.get(spec.get("file") or "", {})
            try:
                compound = Compound(run, spec["name"], spec["nominal_mass"], self.params,
                                    self.ladder, manual=edits.get("peaks"),
                                    windows=edits.get("windows"))
            except ValueError:
                if not edits:
                    raise
                # saved windows that no longer hold their peak are let go
                compound = Compound(run, spec["name"], spec["nominal_mass"], self.params,
                                    self.ladder, manual=edits.get("peaks"))
            compound.file = spec.get("file") or ""
            compound.set_transform(self.transform,
                                   index_scale(self.standard) if self.transform else None)
            self.compounds.append(compound)

        job.update("Drawing chromatograms", 0.80, "plotting")
        self.chromatogram_figures = []
        for f in self.figures.glob("chrom_*"):
            f.unlink()
        for n, c in enumerate(self.compounds):
            self.chromatogram_figures.append(self._plot_compound(n))
        self.confirmed = False
        job.update("Chromatograms ready for review", 1.0, "review")
        job.done = True

    def _plot_compound(self, n):
        c = self.compounds[n]
        cstem = self.figures / f"chrom_compound_{safe_name(c.name)}"
        plot_compound(c, cstem, formats=("png",))
        return {
            "key": cstem.name,
            "title": f"Chromatogram of {c.name}",
            "kind": "compound",
            "compound": n,
            "file": cstem.name + ".png",
        }

    def compound(self, n):
        if not 0 <= n < len(self.compounds):
            raise LookupError(f"There is no compound {n + 1}.")
        return self.compounds[n]

    def set_compound_edits(self, n, raw_peaks, raw_windows):
        """Hand-placed peaks and windows for compound n. Raises ValueError with
        a message, leaving the compound as it was."""
        with self.lock:
            c = self.compound(n)
            peaks, windows = parse_edits(raw_peaks, raw_windows)
            c.set_edits(peaks, windows)
            if c.dropped:
                dropped = c.dropped
                c.set_edits(*parse_edits(*self._saved_edits(c)))
                raise ValueError("Peak " + ", ".join(map(str, dropped))
                                 + " was placed past the retention time limit.")
            self.chromatogram_figures = [
                self._plot_compound(n) if f.get("compound") == n else f
                for f in self.chromatogram_figures
            ]
            if self.overrides_folder and c.file:
                store_edits(self.overrides_folder, c.file, c.manual, c.windows)
            # the scans behind this compound's spectra just changed
            self.confirmed = False
            self.spectrum_figures = []

    def _saved_edits(self, c):
        saved = (load_compound_edits(self.overrides_folder).get(c.file, {})
                 if self.overrides_folder and c.file else {})
        return saved.get("peaks"), saved.get("windows")

    def run_spectra(self, job: Job):
        with self.lock:
            self._run_spectra(job)

    def _run_spectra(self, job: Job):
        job.update("Averaging scans across each chromatogram peak", 0.05, "spectra")
        for f in self.figures.glob("spec_*"):
            f.unlink()
        self.spectrum_figures = []

        collected = []
        for c in self.compounds:
            collected.extend(c.spectra())
        if not collected:
            raise ValueError(
                "No MSn scans fall inside the chromatogram peak windows. "
                "Widen the peak window or re-check the peaks."
            )

        for i, spec in enumerate(collected, start=1):
            stem = self.figures / safe_name(f"spec_{spec['compound']}_{spectrum_slug(spec)}")
            plot_spectrum(spec, self.params, stem, formats=("png",))
            self.spectrum_figures.append(
                {
                    "key": stem.name,
                    "title": f"MS{spec['level']} of {spec['compound']}",
                    "subtitle": spec["chain"] or "full scan",
                    "kind": f"ms{spec['level']}",
                    "level": spec["level"],
                    "compound": spec["compound"],
                    "n_scans": spec["n_scans"],
                    "file": stem.name + ".png",
                }
            )
            job.update(
                f"Plotted {i} of {len(collected)} mass spectra",
                0.05 + 0.9 * i / len(collected),
            )
        job.update(f"{len(collected)} mass spectra ready", 1.0, "spectra_done")
        job.done = True

    # --------------------------------------------------------------- report
    def summary(self):
        warnings = list(self.ladder.warnings()) if self.ladder else []
        for c in self.compounds:
            warnings.extend(c.warnings())
        return {
            "session": self.id,
            "params": self.params.to_dict(),
            "ladder": {
                "run": self.ladder_name,
                "masses": self.ladder_masses,
                "peaks": self.ladder.table() if self.ladder else [],
                "monotonic": self.ladder.is_monotonic if self.ladder else False,
                "limit": self.ladder.limit if self.ladder else None,
                "n_points": self.ladder.n_points if self.ladder else 0,
            },
            "compounds": [
                {
                    "name": c.name,
                    "run": c.run.source_name,
                    "nominal_mass": c.nominal_mass,
                    "ms2_precursor": c.precursor_mz,
                    "activation": c.activation,
                    "ms_levels": c.run.available_levels,
                    "on_standard_axis": c.on_standard_axis,
                    "index_from": ("standard" if c.on_standard_axis and c.index_scale
                                   else "ladder"),
                    "absent": c.absent,
                    "window_fwhm": c.params.window_fwhm,
                    "peaks": [
                        {
                            "peak_no": pk.slot,
                            "manual": pk.manual,
                            "exact": pk.exact,
                            "window_manual": pk.window_manual,
                            "fwhm": round(pk.fwhm, 5) if pk.fwhm else None,
                            "rt": round(pk.rt_standard, 4) if c.on_standard_axis
                                  else round(pk.rt, 4),
                            "rt_raw": round(pk.rt, 4),
                            "rt_standard": (round(pk.rt_standard, 4)
                                            if pk.rt_standard is not None else None),
                            "index": round(pk.index, 4) if pk.index is not None else None,
                            "extrapolated": (pk.rt_extrapolated if c.on_standard_axis
                                             else pk.index_extrapolated),
                            "height": pk.height,
                            "area": pk.area,
                            "rt_start": round(
                                pk.rt_standard_start if c.on_standard_axis else pk.rt_start, 4),
                            "rt_end": round(
                                pk.rt_standard_end if c.on_standard_axis else pk.rt_end, 4),
                            "rt_start_raw": round(pk.rt_start, 4),
                            "rt_end_raw": round(pk.rt_end, 4),
                        }
                        for pk in c.peaks
                    ],
                }
                for c in self.compounds
            ],
            "standard": ({
                "name": self.standard["name"],
                "saved_at": self.standard.get("saved_at"),
                "model": self.transform.model,
                "transform": self.transform.to_dict(),
            } if self.transform else None),
            "standard_error": self.transform_error,
            "axis": ("standard" if self.transform else "instrument"),
            "warnings": warnings,
        }

    def build_bundle(self, job: Job) -> Path:
        with self.lock:
            return self._build_bundle(job)

    def _build_bundle(self, job: Job) -> Path:
        job.update("Rendering print-quality figures", 0.04, "export")
        out = self.root / "export"
        if out.exists():
            shutil.rmtree(out)
        (out / "chromatograms").mkdir(parents=True)
        (out / "spectra").mkdir(parents=True)
        (out / "data").mkdir(parents=True)

        plot_ladder(self.ladder, self.ladder_name,
                    out / "chromatograms" / f"ladder_{safe_name(self.ladder_name)}",
                    formats=EXPORT_FORMATS)
        if self.fit and self.transform:
            plot_fit(self.fit, self.transform.model, self.ladder_name,
                     self.standard["name"], out / "chromatograms" / "ladder_fit",
                     formats=EXPORT_FORMATS)
        for c in self.compounds:
            plot_compound(c, out / "chromatograms" / f"chromatogram_{safe_name(c.name)}",
                          formats=EXPORT_FORMATS)

        # ------------------------------------------------------------ spectra
        job.update("Rendering mass spectra", 0.20)
        spectra_rows = []
        plotted = []
        for c in self.compounds:
            plotted.extend(c.spectra())
        for i, spec in enumerate(plotted, start=1):
            name = safe_name(f"{spec['compound']}_{spectrum_slug(spec)}")
            plot_spectrum(spec, self.params, out / "spectra" / name, formats=EXPORT_FORMATS)
            spectra_rows.append(write_peaklist(out, name, spec))
            job.update(f"Exported {i} of {len(plotted)} spectra", 0.20 + 0.44 * i / len(plotted))

        # the same channels split by peak: not plotted, but needed to compare the
        # two anomers against each other and against another run
        job.update("Writing per-peak spectra", 0.66)
        for c in self.compounds:
            for spec in c.peak_spectra():
                name = safe_name(f"{spec['compound']}_{spectrum_slug(spec)}_{spec['scope']}")
                spectra_rows.append(write_peaklist(out, name, spec))

        # ------------------------------------------------- chromatogram traces
        job.update("Writing chromatogram traces", 0.76)
        ladder_rt = self.ladder.traces[0]["rt"]
        with open(out / "data" / "ladder_chromatograms.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["rt_min"] + [f"mz_{t['mass']:g}" for t in self.ladder.traces])
            for i, rt in enumerate(ladder_rt):
                w.writerow([f"{rt:.5f}"] + [f"{t['intensity'][i]:.1f}" for t in self.ladder.traces])

        for c in self.compounds:
            base = safe_name(c.name)
            with open(out / "data" / f"chromatogram_{base}_ms2_tic.csv", "w", newline="") as fh:
                w = csv.writer(fh)
                if c.on_standard_axis:
                    w.writerow(["rt_standard_min", "ms2_tic", "in_peak_window"])
                    for rt, rt_std, tic in zip(c.rt, c.rt_standard, c.tic):
                        w.writerow([f"{rt_std:.5f}", f"{tic:.1f}", c.window_of(rt)])
                else:
                    w.writerow(["rt_min", "ms2_tic", "in_peak_window"])
                    for rt, tic in zip(c.rt, c.tic):
                        w.writerow([f"{rt:.5f}", f"{tic:.1f}", c.window_of(rt)])
            xic = c.ms1_xic()
            if xic is not None:
                with open(out / "data" / f"chromatogram_{base}_ms1_xic.csv", "w", newline="") as fh:
                    w = csv.writer(fh)
                    label = "rt_standard_min" if c.on_standard_axis else "rt_min"
                    w.writerow([label, f"xic_{c.nominal_mass:g}", "in_peak_window"])
                    for rt, y in zip(*xic):
                        shown = float(c.transform(rt)) if c.on_standard_axis else rt
                        w.writerow([f"{shown:.5f}", f"{y:.1f}", c.window_of(rt)])

        # ------------------------------------------------------------- tables
        job.update("Writing tables", 0.86)
        summary = self.summary()
        summary["spectra"] = spectra_rows
        summary["peak_ratios"] = {
            c.name: c.peak_ratios() for c in self.compounds if c.peak_ratios()
        }
        (out / "results.json").write_text(json.dumps(summary, indent=2))

        with open(out / "data" / "ladder_index.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["index", "ladder_mass", "rt_min", "height", "area",
                        "window_start_min", "window_end_min", "used_for_calibration"])
            for r in summary["ladder"]["peaks"]:
                w.writerow([r["index"], f"{r['mass']:g}", r["rt"], f"{r['height']:.1f}",
                            f"{r['area']:.1f}", r["rt_start"], r["rt_end"], r["calibrating"]])

        with open(out / "data" / "compound_index.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            on_std = any(c.on_standard_axis for c in self.compounds)
            rt_col = "rt_standard_min" if on_std else "rt_min"
            w.writerow(["compound", "run", "standard", "transform_model",
                        "nominal_mass", "ms2_precursor", "peak_no",
                        rt_col, "extrapolated", "index", "index_from",
                        "height", "area", "window_start_min", "window_end_min",
                        "fwhm_min", "peak_by_hand", "window_by_hand",
                        "area_fraction", "height_fraction"])
            for c in self.compounds:
                ratios = c.peak_ratios() or {}
                std = c.transform.standard_name if c.on_standard_axis else ""
                model = c.transform.model if c.on_standard_axis else ""
                for pk in c.peaks:
                    n = pk.slot
                    shown = pk.rt_standard if c.on_standard_axis else pk.rt
                    start = pk.rt_standard_start if c.on_standard_axis else pk.rt_start
                    end = pk.rt_standard_end if c.on_standard_axis else pk.rt_end
                    flag = (pk.rt_extrapolated if c.on_standard_axis
                            else pk.index_extrapolated)
                    w.writerow([
                        c.name, c.run.source_name, std, model,
                        c.nominal_mass, c.precursor_mz, n,
                        round(shown, 4), flag, fmt_number(pk.index, 4),
                        "standard" if c.index_scale and c.on_standard_axis else "ladder",
                        f"{pk.height:.1f}", f"{pk.area:.1f}",
                        round(start, 4), round(end, 4), fmt_number(pk.fwhm, 5),
                        pk.manual, pk.window_manual,
                        fmt_number(ratios.get(f"peak{n}_area_fraction")),
                        fmt_number(ratios.get(f"peak{n}_height_fraction")),
                    ])

        with open(out / "data" / "compound_peak_ratios.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["compound", "run", "ms2_precursor"] + RATIO_FIELDS)
            for c in self.compounds:
                ratios = c.peak_ratios()
                if not ratios:
                    continue
                w.writerow([c.name, c.run.source_name, c.precursor_mz]
                           + [fmt_number(ratios[f]) for f in RATIO_FIELDS])

        with open(out / "data" / "spectra_summary.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(SPECTRUM_FIELDS)
            for row in spectra_rows:
                w.writerow([row[f] for f in SPECTRUM_FIELDS])

        if self.transform:
            (out / "data" / "transform.json").write_text(
                json.dumps({
                    "standard": self.standard["name"],
                    "standard_saved_at": self.standard.get("saved_at"),
                    "local_ladder": self.ladder_name,
                    **self.transform.to_dict(),
                }, indent=2)
            )

        (out / "README.txt").write_text(_readme(summary))
        self._publish_compound_results(out, summary, job)

        job.update("Packing the download", 0.95)
        bundle = self.root / f"ltq_index_{safe_name(self.ladder_name)}_{self.id}.zip"
        if bundle.exists():
            bundle.unlink()
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(out.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(out))
        job.update("Results ready to download", 1.0, "export_done")
        job.done = True
        return bundle


RATIO_FIELDS = [
    "peak1_rt", "peak2_rt", "delta_rt", "peak1_index", "peak2_index", "delta_index",
    "peak1_height", "peak2_height", "peak1_area", "peak2_area",
    "height_ratio_1_over_2", "area_ratio_1_over_2",
    "peak1_area_fraction", "peak2_area_fraction",
    "peak1_height_fraction", "peak2_height_fraction",
]

SPECTRUM_FIELDS = [
    "compound", "ms_level", "scope", "cid_sequence", "n_scans",
    "window_start_min", "window_end_min", "n_peaks", "base_peak_mz",
    "base_peak_intensity", "total_intensity", "peak_list",
]


def _publish_compound_results(self, out: Path, summary, job: Job):
    """Copy each compound's normalized results into the shared compound/ folder.

    One folder per compound, so runs of the same compound from different days
    accumulate side by side on the same time axis and can be compared later.
    """
    if not self.ladder_dir or not self.transform:
        return
    job.update("Saving normalized results to the compound folder", 0.93)
    published = []
    seen = set()
    for c in self.compounds:
        try:
            target = compound_output_dir(self.ladder_dir, c.name)
        except OSError as exc:
            job.update(f"Could not write to the compound folder: {exc}")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = target / f"{safe_name(c.run.source_name)}_{stamp}"
        run_dir.mkdir(parents=True, exist_ok=True)

        entry = next(e for e in summary["compounds"] if e["name"] == c.name)
        record = {
            "compound": c.name,
            "run": c.run.source_name,
            "source_file": str(self._source_for(c)),
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "axis": "standard",
            "standard": self.standard["name"],
            "standard_saved_at": self.standard.get("saved_at"),
            "local_ladder": self.ladder_name,
            "transform": self.transform.to_dict(),
            "params": self.params.to_dict(),
            "nominal_mass": c.nominal_mass,
            "ms2_precursor": c.precursor_mz,
            "peaks": entry["peaks"],
            "peak_ratios": c.peak_ratios(),
        }
        (run_dir / "result.json").write_text(json.dumps(record, indent=2))

        base = safe_name(c.name)
        for name in (f"chromatogram_{base}.png", f"chromatogram_{base}.pdf"):
            src = out / "chromatograms" / name
            if src.exists():
                shutil.copy2(src, run_dir / name)
        for src in sorted((out / "data").glob(f"chromatogram_{base}_*.csv")):
            shutil.copy2(src, run_dir / src.name)
        for src in sorted((out / "data").glob(f"peaklist_{base}_*.csv")):
            shutil.copy2(src, run_dir / src.name)

        with open(run_dir / "peaks.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["compound", "run", "standard", "transform_model", "peak_no",
                        "rt_standard_min", "rt_raw_min", "extrapolated", "index",
                        "height", "area", "window_start_min", "window_end_min",
                        "peak_by_hand", "window_by_hand"])
            for pk in entry["peaks"]:
                w.writerow([c.name, c.run.source_name, self.standard["name"],
                            self.transform.model, pk["peak_no"],
                            pk["rt_standard"], pk["rt_raw"], pk["extrapolated"],
                            pk["index"], f"{pk['height']:.1f}", f"{pk['area']:.1f}",
                            pk["rt_start"], pk["rt_end"],
                            pk["manual"], pk["window_manual"]])
        if str(run_dir) not in seen:
            seen.add(str(run_dir))
            published.append(str(run_dir))
    self.published_results = published


def _source_for(self, compound):
    for spec in self.compound_specs:
        if spec["name"] == compound.name:
            return spec["path"]
    return compound.run.source_name


def fmt_number(value, places=6):
    return "" if value is None else round(float(value), places)


def write_peaklist(out: Path, name: str, spec):
    """Write one spectrum's peak list and return its row for spectra_summary.csv."""
    mz, inten = spec["mz"], spec["intensity"]
    top = float(inten.max()) if inten.size else 0.0
    with open(out / "data" / f"peaklist_{name}.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["mz", "intensity", "relative_intensity_pct"])
        for m, v in zip(mz, inten):
            w.writerow([f"{m:.4f}", f"{v:.2f}", f"{(100 * v / top) if top else 0:.4f}"])
    starts = [a for a, _ in spec["windows"]]
    ends = [b for _, b in spec["windows"]]
    return {
        "compound": spec["compound"],
        "ms_level": spec["level"],
        "scope": spec["scope"],
        "cid_sequence": spec["chain"] or "full scan",
        "n_scans": spec["n_scans"],
        "window_start_min": round(min(starts), 4),
        "window_end_min": round(max(ends), 4),
        "n_peaks": int(mz.size),
        "base_peak_mz": round(float(mz[int(inten.argmax())]), 4) if mz.size else "",
        "base_peak_intensity": round(top, 2),
        "total_intensity": round(float(inten.sum()), 2),
        "peak_list": f"data/peaklist_{name}.csv",
    }


def _readme(summary):
    lines = [
        "LTQ ladder-index results",
        "=" * 40,
        "",
        f"Ladder run: {summary['ladder']['run']}",
        f"Ladder masses: {', '.join(f'{m:g}' for m in summary['ladder']['masses'])}",
        f"Calibration uses index 1-{summary['ladder']['limit']} "
        f"of {summary['ladder']['n_points']} found",
        "",
        "chromatograms/  ladder and compound chromatograms (PNG + PDF)",
        "spectra/        MS1/MS2/MS3/MS4 figures, one per channel, summed over both peaks",
        "data/           every processed number as CSV (see below)",
        "results.json    the same content in one machine-readable file",
        "",
        "data/ contents",
        "-" * 40,
        "ladder_index.csv            index, mass, retention time, height, area",
        "ladder_chromatograms.csv    full extracted ion traces, one column per mass",
        "compound_index.csv          both peaks per compound, with index and area share",
        "compound_peak_ratios.csv    peak 1 vs peak 2: ratios, fractions, delta RT and index",
        "chromatogram_*_ms2_tic.csv  full MS2 TIC trace, flagged by peak window",
        "chromatogram_*_ms1_xic.csv  MS1 trace at the nominal mass, same flagging",
        "spectra_summary.csv         one row per spectrum: base peak, totals, scan count",
        "peaklist_*.csv              m/z, intensity, relative intensity (% base peak)",
        "",
        "Peak lists come in three scopes. '<compound>_MS2_1419.5' pools both",
        "chromatogram peaks and matches the figure; the '_peak1' and '_peak2'",
        "variants split the same channel by peak, so the two anomers can be",
        "compared against each other and against another run.",
        "",
        "Index assignment",
        "-" * 40,
        "Ladder peaks are numbered 1-N by increasing retention time, two per ladder",
        "mass. A compound peak's index is the linear interpolation of its retention",
        "time between the two bracketing ladder peaks. Indices marked extrapolated",
        "fall outside the ladder range.",
        "",
        "Compound peaks",
        "-" * 40,
    ]
    for c in summary["compounds"]:
        lines.append(f"{c['name']}  (MS2 precursor m/z {c['ms2_precursor']})")
        for pk in c["peaks"]:
            flag = " [extrapolated]" if pk["extrapolated"] else ""
            idx = f"{pk['index']:.2f}" if pk["index"] is not None else "n/a"
            lines.append(f"    peak {pk['peak_no']}: {pk['rt']:.2f} min -> index {idx}{flag}")
    if summary["warnings"]:
        lines += ["", "Warnings", "-" * 40] + [f"- {w}" for w in summary["warnings"]]
    return "\n".join(lines) + "\n"


# defined after the helpers they use, then attached to Session
Session._publish_compound_results = _publish_compound_results
Session._source_for = _source_for
