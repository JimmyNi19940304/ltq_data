"""The saved standard ladder, and the transform from a local ladder onto it.

The calibration app nominates one ladder run as the standard and saves its index
to retention-time table here. The main app then reads it, fits a transform from
whatever ladder was run alongside a compound onto that standard, and reports the
compound's retention times on the standard's time axis.

Folder layout, created beside the `ladder_dir` from config.json:

    <parent>/standard_ladder/standard.json     the active standard
    <parent>/standard_ladder/archive/...       every standard saved before it
    <parent>/compound/<compound>/...           normalized compound results
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np

from .calibration import MODELS, coefficient_names, fit_polynomial

STANDARD_DIRNAME = "standard_ladder"
COMPOUND_DIRNAME = "compound"
STANDARD_FILE = "standard.json"
FORMAT_VERSION = 2


class StandardError_(RuntimeError):
    """Raised when a standard cannot be read or written."""


def folders(ladder_dir, create=False):
    """Where the standard and compound folders live, given the ladder folder."""
    parent = Path(ladder_dir).expanduser().resolve().parent
    paths = {
        "parent": parent,
        "standard_dir": parent / STANDARD_DIRNAME,
        "compound_dir": parent / COMPOUND_DIRNAME,
    }
    paths["standard_file"] = paths["standard_dir"] / STANDARD_FILE
    paths["archive_dir"] = paths["standard_dir"] / "archive"
    if create:
        for key in ("standard_dir", "compound_dir", "archive_dir"):
            paths[key].mkdir(parents=True, exist_ok=True)
    return paths


def save_standard(ladder_dir, *, name, peaks, masses, params, selected_indexes=None,
                  source_file=None, n_ladder_peaks=None, note=None):
    """Write the active standard, archiving whatever was there before.

    `peaks` maps ladder index -> retention time. Only the indexes in
    `selected_indexes` are recorded as usable for fitting, so an index the
    operator judged bad on page 1 cannot quietly come back later.
    """
    paths = folders(ladder_dir, create=True)
    peaks = {int(k): float(v) for k, v in peaks.items()}
    if len(peaks) < 2:
        raise StandardError_("A standard needs at least two ladder peaks.")
    usable = (sorted(int(i) for i in selected_indexes if int(i) in peaks)
              if selected_indexes is not None else sorted(peaks))
    if len(usable) < 2:
        raise StandardError_(
            "At least two ladder indexes must be ticked for the standard to be usable."
        )

    record = {
        "format_version": FORMAT_VERSION,
        "name": name,
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_file": str(source_file) if source_file else None,
        "ladder_masses": [float(m) for m in masses],
        "params": params.to_dict() if hasattr(params, "to_dict") else dict(params or {}),
        "peaks": {str(k): round(v, 6) for k, v in sorted(peaks.items())},
        "usable_indexes": usable,
        "n_ladder_peaks": int(n_ladder_peaks if n_ladder_peaks is not None else len(peaks)),
        "note": note,
    }

    if paths["standard_file"].exists():
        try:
            previous = json.loads(paths["standard_file"].read_text())
            stamp = str(previous.get("saved_at", "")).replace(":", "").replace("-", "")[:15]
            archived = paths["archive_dir"] / f"{previous.get('name', 'standard')}_{stamp}.json"
            shutil.copy2(paths["standard_file"], archived)
        except (OSError, json.JSONDecodeError):
            pass  # an unreadable previous standard is not a reason to refuse a new one

    paths["standard_file"].write_text(json.dumps(record, indent=2))
    return {"record": record, "path": str(paths["standard_file"]),
            "standard_dir": str(paths["standard_dir"]),
            "compound_dir": str(paths["compound_dir"])}


def load_standard(ladder_dir):
    """The active standard, or None when none has been saved yet."""
    if not ladder_dir:
        return None
    path = folders(ladder_dir)["standard_file"]
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise StandardError_(f"{path} could not be read: {exc}") from exc
    record["peaks"] = {int(k): float(v) for k, v in record.get("peaks", {}).items()}
    record.setdefault("usable_indexes", sorted(record["peaks"]))
    record["path"] = str(path)
    return record


def list_archive(ladder_dir):
    paths = folders(ladder_dir)
    if not paths["archive_dir"].is_dir():
        return []
    out = []
    for f in sorted(paths["archive_dir"].glob("*.json"), reverse=True):
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"name": rec.get("name"), "saved_at": rec.get("saved_at"),
                    "file": f.name})
    return out


# --------------------------------------------------------------- the transform
class Transform:
    """Maps retention times in a local run onto the standard's time axis.

    Fitted on the ladder indexes the two runs share, then applied to the
    compound acquired alongside that local ladder. It is a relabelling of the
    time axis: no scan is moved, resampled or dropped.
    """

    def __init__(self, model, coefficients, indexes, points, metrics, standard_name,
                 valid_range, monotonic_range=None):
        self.model = model
        self.coefficients = list(coefficients)
        self.indexes = list(indexes)
        self.points = points
        self.metrics = metrics
        self.standard_name = standard_name
        self.valid_range = valid_range            # (min, max) local rt actually fitted
        self.monotonic_range = monotonic_range    # where the curve still increases

    def __call__(self, rt):
        return np.polyval(self.coefficients, np.asarray(rt, float))

    def apply(self, rt):
        return self(rt)

    def is_inside(self, rt):
        lo, hi = self.valid_range
        return bool(lo <= rt <= hi)

    def check_monotonic(self, lo, hi, samples=2000):
        """Is the mapping still one-to-one across [lo, hi]?

        Called with the span actually being mapped, which reaches beyond the
        ladder peaks. A curve that has turned over would reorder the
        chromatogram, so the caller needs to know before trusting it.
        """
        grid = np.linspace(float(lo), float(hi), samples)
        values = np.polyval(self.coefficients, grid)
        falling = np.diff(values) <= 0
        if not falling.any():
            return {"monotonic": True, "turns_at": None}
        first = int(np.argmax(falling))
        return {"monotonic": False, "turns_at": round(float(grid[first]), 4)}

    def to_dict(self):
        names = coefficient_names(len(self.coefficients) - 1)
        return {
            "model": self.model,
            "standard": self.standard_name,
            "coefficients": dict(zip(names, [float(c) for c in self.coefficients])),
            "coefficients_desc": [float(c) for c in self.coefficients],
            "indexes": self.indexes,
            "n_indexes": len(self.indexes),
            "fitted_rt_range": [round(v, 5) for v in self.valid_range],
            "monotonic_rt_range": ([round(v, 5) for v in self.monotonic_range]
                                   if self.monotonic_range else None),
            "metrics": self.metrics,
            "points": self.points,
        }


def _monotonic_span(coefficients, lo, hi, samples=800):
    """The largest stretch around the fitted range where the curve still rises.

    A cubic fitted over 10-50 min can turn over outside it. Mapping times through
    a curve that has turned would reorder the chromatogram, so the caller is told
    where the mapping is still one-to-one.
    """
    # probe well beyond the ladder peaks: a compound chromatogram runs from zero
    # to the retention-time limit, which is wider than the ladder's own span
    pad = max(hi - lo, 30.0)
    grid = np.linspace(min(lo - pad, 0.0), hi + pad, samples)
    values = np.polyval(coefficients, grid)
    rising = np.diff(values) > 0
    centre = int(np.searchsorted(grid, (lo + hi) / 2))
    centre = min(max(centre, 0), rising.size - 1)
    if not rising[centre]:
        return None
    start = centre
    while start > 0 and rising[start - 1]:
        start -= 1
    end = centre
    while end < rising.size - 1 and rising[end + 1]:
        end += 1
    return float(grid[start]), float(grid[end + 1])


def build_transform(local_peaks, standard, model="quadratic", indexes=None):
    """Fit `model` mapping local ladder retention times onto the standard's."""
    degrees = {key: degree for key, degree, _ in MODELS}
    if model not in degrees:
        raise StandardError_(f"Unknown model '{model}'. Choose one of {sorted(degrees)}.")
    degree = degrees[model]

    standard_peaks = {int(k): float(v) for k, v in standard["peaks"].items()}
    allowed = set(standard.get("usable_indexes") or standard_peaks)
    if indexes is not None:
        allowed &= {int(i) for i in indexes}
    local_peaks = {int(k): float(v) for k, v in local_peaks.items()}
    shared = sorted(set(local_peaks) & set(standard_peaks) & allowed)

    if len(shared) < degree + 1:
        raise StandardError_(
            f"The compound's ladder shares {len(shared)} usable index(es) with the "
            f"standard, but a {model} fit needs {degree + 1}."
        )

    x = np.array([local_peaks[i] for i in shared], float)
    y = np.array([standard_peaks[i] for i in shared], float)
    fit = fit_polynomial(x, y, degree)

    points = [
        {"index": i, "local_rt": round(float(xi), 5), "standard_rt": round(float(yi), 5),
         "fitted_rt": round(float(fi), 5), "residual": round(float(ri), 5)}
        for i, xi, yi, fi, ri in zip(shared, x, y, fit["fitted"], fit["residual"])
    ]
    raw_residual = y - x
    metrics = {
        "rmse": fit["rmse"],
        "std_error": fit["std_error"],
        "max_abs_residual": fit["max_abs_residual"],
        "r2": fit["r2"],
        "adjusted_r2": fit["adjusted_r2"],
        "exact": fit["exact"],
        "uncalibrated_rmse": float(np.sqrt((raw_residual ** 2).mean())),
    }
    lo, hi = float(x.min()), float(x.max())
    return Transform(
        model=model,
        coefficients=fit["coefficients"],
        indexes=shared,
        points=points,
        metrics=metrics,
        standard_name=standard.get("name", "standard"),
        valid_range=(lo, hi),
        monotonic_range=_monotonic_span(fit["coefficients"], lo, hi),
    )


def compound_output_dir(ladder_dir, compound_name, create=True):
    """Where one compound's normalized results are written."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(compound_name))
    path = folders(ladder_dir, create=create)["compound_dir"] / (safe or "compound")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path
