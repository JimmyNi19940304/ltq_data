"""Compound peaks and spectrum windows set by hand in the main app.

Kept per compound file in compound_overrides.json, beside config.json, so a
compound opened again comes up with the same peaks and windows. Every time is
the compound run's own retention time, not the standard axis, so a saved edit
stays right when the run's ladder is refitted.
"""

from __future__ import annotations

import json
from pathlib import Path

from .ladderedit import parse_pick

OVERRIDES_FILENAME = "compound_overrides.json"
SLOTS = (1, 2)


def _slot(key):
    try:
        slot = int(key)
    except (TypeError, ValueError):
        raise ValueError(f"'{key}' is not a peak number.") from None
    if slot not in SLOTS:
        raise ValueError(f"Peak {slot} is not 1 or 2.")
    return slot


def parse_edits(raw_peaks, raw_windows):
    """Validate the editor's {slot: pick} and {slot: [start, end]}. Raises
    ValueError with a message for the operator."""
    peaks = {}
    for key, value in (raw_peaks or {}).items():
        slot = _slot(key)
        try:
            peaks[slot] = parse_pick(value)
        except (TypeError, ValueError):
            raise ValueError(f"'{value}' is not a retention time.") from None
    windows = {}
    for key, value in (raw_windows or {}).items():
        slot = _slot(key)
        if value is None:
            continue
        try:
            start, end = (float(v) for v in value)
        except (TypeError, ValueError):
            raise ValueError(f"The window for peak {slot} needs a start and an end time.") from None
        if not end > start:
            raise ValueError(f"The window for peak {slot} has to end after it starts.")
        windows[slot] = (start, end)
    return peaks, windows


def overrides_path(folder) -> Path:
    return Path(folder) / OVERRIDES_FILENAME


def load_overrides(folder) -> dict:
    """{compound file name: {"peaks": {slot: pick}, "windows": {slot: (s, e)}}}.
    A broken file, or a broken entry in it, is ignored."""
    path = overrides_path(folder)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for name, value in (raw.get("compounds") or {}).items():
        if not isinstance(value, dict):
            continue
        try:
            peaks, windows = parse_edits(value.get("peaks"), value.get("windows"))
        except ValueError:
            continue
        if peaks or windows:
            out[str(name)] = {"peaks": peaks, "windows": windows}
    return out


def store_edits(folder, file_name, peaks, windows):
    """Remember one compound file's hand edits; none at all removes its entry."""
    overrides = load_overrides(folder)
    if peaks or windows:
        overrides[file_name] = {"peaks": peaks, "windows": windows}
    else:
        overrides.pop(file_name, None)
    body = {
        "_note": "Compound peaks and spectrum windows set by hand in the main app, "
                 "per compound file. Times are the run's own retention times in "
                 "minutes. peaks maps peak 1 or 2 to the time it was placed at "
                 "(exact: placed on that scan without snapping), or null for no "
                 "peak; windows maps a peak to its [start, end]. A peak without a "
                 "window uses the automatic N x FWHM window.",
        "compounds": {},
    }
    for name in sorted(overrides, key=str.lower):
        entry = overrides[name]
        body["compounds"][name] = {
            "peaks": {str(k): v for k, v in sorted(entry["peaks"].items())},
            "windows": {str(k): [round(a, 5), round(b, 5)]
                        for k, (a, b) in sorted(entry["windows"].items())},
        }
    path = overrides_path(folder)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _sig(values, digits=5):
    return [float(f"{v:.{digits}g}") for v in values]


def trace_payload(compound):
    """What the browser editor draws: the MS2 TIC on both time axes, and the
    peaks with their windows. The smoothed trace is sent so the browser measures
    a moved peak's FWHM exactly as the server will."""
    rt = compound.rt
    axis = compound.axis_rt
    return {
        "name": compound.name,
        "rt": [round(float(v), 5) for v in rt],
        "axis": [round(float(v), 5) for v in axis],
        "on_standard_axis": compound.on_standard_axis,
        "axis_label": (f"Retention time on the {compound.transform.standard_name} axis (min)"
                       if compound.on_standard_axis else "Retention time (min)"),
        "tic": _sig(compound.tic),
        "smooth": _sig(compound.smoothed),
        "window_fwhm": compound.params.window_fwhm,
        "precursor": compound.precursor_mz,
        # the standard ladder's (rts, indexes), so the editor shows a moved peak's
        # index as the server will compute it; None means it comes from the ladder
        "index_scale": ([list(map(float, v)) for v in compound.index_scale]
                        if compound.on_standard_axis and compound.index_scale else None),
        "auto": {pk.slot: pk.rt for pk in compound.auto_peaks},
        "manual": {k: (v["rt"] if isinstance(v, dict) else v)
                   for k, v in compound.manual.items() if v is not None},
        "exact": [k for k, v in compound.manual.items() if isinstance(v, dict)],
        "absent": compound.absent,
        "windows": {k: list(v) for k, v in compound.windows.items()},
        "peaks": {
            pk.slot: {"rt": pk.rt, "window": [pk.rt_start, pk.rt_end],
                      "fwhm": pk.fwhm, "index": pk.index}
            for pk in compound.peaks
        },
    }
