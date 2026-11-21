"""Per-ladder settings made by hand, shared by the calibration app and the main app.

Both apps let the operator set a ladder's own retention-time limit and place its
peaks by hand. Those choices are kept per ladder file in ladder_overrides.json,
beside config.json, so a ladder fixed in one app comes up fixed in the other.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from .analysis import candidate_peaks

OVERRIDES_FILENAME = "ladder_overrides.json"
LEGACY_RT_MAX_FILENAME = "ladder_rt_max.json"   # the earlier rt_max-only file


def parse_pick(value):
    """A hand-made choice for one index, from JSON.

    None means the index has no peak; a number is a time to snap to the top of
    the peak it lies on; {"rt": minutes, "exact": true} is a peak put on that
    scan without snapping. Raises ValueError for anything else.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        rt = float(value.get("rt"))
        return {"rt": rt, "exact": True} if value.get("exact") else rt
    if isinstance(value, bool):
        raise ValueError(value)
    return float(value)


def parse_picks(raw, expected):
    """Validate a {index: pick} mapping from a request. Raises ValueError with a
    message fit to show the operator."""
    picks = {}
    for key, value in (raw or {}).items():
        try:
            i = int(key)
        except (TypeError, ValueError):
            raise ValueError(f"'{key}' is not a ladder index.") from None
        if not 1 <= i <= expected:
            raise ValueError(f"Index {i} is outside 1-{expected}.")
        try:
            picks[i] = parse_pick(value)
        except (TypeError, ValueError):
            raise ValueError(f"'{value}' is not a retention time.") from None
    return picks


def parse_rt_max(value, default):
    """A limit from a request: blank means the default. Raises ValueError."""
    if value in (None, ""):
        return float(default)
    try:
        rt_max = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{value}' is not a number of minutes.") from None
    if not rt_max > 0:
        raise ValueError("The retention time limit must be above 0 min.")
    return rt_max


# -------------------------------------------------------------- stored file
def overrides_path(folder) -> Path:
    return Path(folder) / OVERRIDES_FILENAME


def load_overrides(folder) -> dict:
    """{ladder file name: {"rt_max": minutes, "peaks": {index: pick}}}.

    Both keys are optional. The older ladder_rt_max.json, which held only rt_max,
    is read too so nothing set before the peak editor existed is lost; the newer
    file wins where both name a ladder. A broken file is ignored.
    """
    folder = Path(folder)
    out: dict[str, dict] = {}
    for path in (folder / LEGACY_RT_MAX_FILENAME, overrides_path(folder)):
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ladders = raw.get("ladders", raw) if isinstance(raw, dict) else {}
        for name, value in ladders.items():
            if str(name).startswith("_"):
                continue
            entry = out.setdefault(str(name), {})
            if not isinstance(value, dict):        # legacy: {"file.raw": 55}
                value = {"rt_max": value}
            if "rt_max" in value:
                try:
                    entry["rt_max"] = float(value["rt_max"])
                except (TypeError, ValueError):
                    pass
            if isinstance(value.get("peaks"), dict):
                peaks = {}
                for k, v in value["peaks"].items():
                    try:
                        peaks[int(k)] = parse_pick(v)
                    except (TypeError, ValueError):
                        continue
                entry["peaks"] = peaks
    return {k: v for k, v in out.items() if v}


def save_overrides(folder, overrides: dict):
    body = {
        "_note": "Per-ladder settings made by hand in the calibration and main apps. "
                 "rt_max is in minutes; ladders without one use the default limit. "
                 "peaks maps a ladder index to the retention time of the peak placed "
                 "there by hand, or null when that index has no peak.",
        "ladders": {},
    }
    for name in sorted(overrides, key=str.lower):
        entry = overrides[name]
        clean = {}
        if entry.get("rt_max") is not None:
            clean["rt_max"] = entry["rt_max"]
        if entry.get("peaks"):
            clean["peaks"] = {str(k): v for k, v in sorted(entry["peaks"].items())}
        if clean:
            body["ladders"][name] = clean
    path = overrides_path(folder)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    legacy = Path(folder) / LEGACY_RT_MAX_FILENAME
    if legacy.is_file():
        legacy.unlink()      # its values now live in the new file


def store_override(folder, file_name, *, rt_max, default_rt_max, peaks):
    """Remember one ladder's limit and hand placements; defaults are not stored."""
    overrides = load_overrides(folder)
    entry = {}
    if rt_max is not None and float(rt_max) != float(default_rt_max):
        entry["rt_max"] = float(rt_max)
    if peaks:
        entry["peaks"] = dict(peaks)
    if entry:
        overrides[file_name] = entry
    else:
        overrides.pop(file_name, None)
    save_overrides(folder, overrides)


# --------------------------------------------------------------- page data
def with_rt_max(params, rt_max):
    return dataclasses.replace(params, rt_max=float(rt_max))


def ladder_entry(ladder, *, name, file, figure, rt_max, default_rt_max, masses) -> dict:
    """What a page needs to draw one ladder card and its peak editor."""
    warnings = ladder.warnings()
    if ladder.dropped:
        warnings.insert(0, "Hand-placed peak for index "
                        + ", ".join(str(i) for i in ladder.dropped)
                        + " is past the retention time limit, so the automatic pick is used.")
    return {
        "name": name,
        "file": file,
        "figure": figure,
        "peaks": {int(pk.index): round(float(pk.rt), 5)
                  for tr in ladder.traces for pk in tr["peaks"]},
        "manual": {int(pk.index): round(float(pk.rt), 5)
                   for tr in ladder.traces for pk in tr["peaks"] if pk.manual},
        # hand-placed on the clicked scan, without snapping to a peak top
        "exact": sorted(int(pk.index) for tr in ladder.traces for pk in tr["peaks"]
                        if pk.exact),
        "auto_peaks": {int(k): round(float(v), 5) for k, v in ladder.auto_rt.items()},
        "absent": ladder.absent,
        "dropped": ladder.dropped,
        "expected": 2 * len(masses),
        "masses": [float(m) for m in masses],
        "n_peaks": ladder.n_points,
        "monotonic": ladder.is_monotonic,
        "warnings": warnings,
        "rt_max": float(rt_max),
        "rt_max_default": float(default_rt_max),
        "rt_max_custom": float(rt_max) != float(default_rt_max),
    }


def saved_picks(entry):
    """The hand-made choices in a ladder entry worth keeping: placed peaks
    (exact ones marked as such) and indexes marked as having no peak."""
    exact = {int(i) for i in entry.get("exact", [])}
    picks = {int(k): ({"rt": v, "exact": True} if int(k) in exact else v)
             for k, v in entry["manual"].items()}
    picks.update({int(i): None for i in entry["absent"]})
    return picks


def traces_payload(ladder, name) -> dict:
    """The extracted ion traces behind one ladder, for the in-browser editor."""
    rt = ladder.traces[0]["rt"] if ladder.traces else []
    return {
        "name": name,
        "rt": [round(float(v), 4) for v in rt],
        "smooth_window": int(ladder.params.smooth_window),
        "traces": [
            {
                "mass": tr["mass"],
                "intensity": [float(f"{v:.5g}") for v in tr["intensity"]],
                "candidates": candidate_peaks(tr["rt"], tr["intensity"], ladder.params),
            }
            for tr in ladder.traces
        ],
    }
