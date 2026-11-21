"""Calibrate HPLC retention times from one ladder run to a chosen standard.

    python calibrate_app.py          then open http://127.0.0.1:5001

Reads config.json for `ladder_dir`, the folder holding the ladder .raw files.
This is a single-user local tool, so the loaded ladders live in one module-level
state rather than per-session.
"""

from __future__ import annotations

import argparse
import threading
import traceback
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from ltqindex.analysis import analyse_ladder
from ltqindex.calibration import ConfigError, calibrate, find_ladders, load_config, summarise
from ltqindex.standards import (StandardError_, folders, list_archive, load_standard,
                                save_standard)
from ltqindex.ladderedit import (ladder_entry, load_overrides, overrides_path,
                                 parse_picks, parse_rt_max, saved_picks, store_override,
                                 traces_payload, with_rt_max)
from ltqindex.msdata import load_run
from ltqindex.pipeline import Job, safe_name
from ltqindex.plotting import plot_ladder
from ltqindex.rawconv import parser_status

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "workspace"
FIGURES = WORK / "calibrate-figures"
SCAN_CACHE = WORK / "scan-cache"        # shared with the main app
CONFIG_PATH = ROOT / "config.json"

app = Flask(__name__)
STATE = {"ladders": [], "config": None, "config_error": None, "standard_error": None}
JOBS: dict[str, Job] = {}
LADDER_OBJECTS: dict = {}     # ladder name -> analysed Ladder, for the peak editor


def read_config():
    try:
        STATE["config"] = load_config(CONFIG_PATH)
        STATE["config_error"] = None
    except ConfigError as exc:
        STATE["config"] = None
        STATE["config_error"] = str(exc)
    return STATE["config"]


def start_job(fn, *args) -> Job:
    job = Job(id=f"job{len(JOBS) + 1}")
    JOBS[job.id] = job

    def target():
        try:
            fn(job, *args)
        except Exception as exc:
            job.error = str(exc) or exc.__class__.__name__
            job.stage = "error"
            job.message = job.error
            job.done = True
            traceback.print_exc()

    threading.Thread(target=target, daemon=True).start()
    return job


# ---------------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(ROOT / "templates", "calibrate.html")


@app.get("/api/config")
def config():
    cfg = read_config()
    if cfg is None:
        return jsonify({"error": STATE["config_error"], "parser": parser_status()}), 200
    files = find_ladders(cfg["ladder_dir"])
    paths = folders(cfg["ladder_dir"])
    try:
        saved = load_standard(cfg["ladder_dir"])
    except StandardError_ as exc:
        saved = None
        STATE["standard_error"] = str(exc)
    return jsonify({
        "ladder_dir": str(cfg["ladder_dir"]),
        "standard_dir": str(paths["standard_dir"]),
        "compound_dir": str(paths["compound_dir"]),
        "saved_standard": ({"name": saved["name"], "saved_at": saved["saved_at"],
                            "n_peaks": len(saved["peaks"]),
                            "usable_indexes": saved["usable_indexes"]}
                           if saved else None),
        "standard_error": STATE.get("standard_error"),
        "archive": list_archive(cfg["ladder_dir"]),
        "config_path": str(cfg["config_path"]),
        "files": [f.name for f in files],
        "masses": cfg["ladder_masses"],
        "params": cfg["params"].to_dict(),
        "overrides_file": str(overrides_path(overrides_folder())),
        "parser": parser_status(),
        "loaded": [l["name"] for l in STATE["ladders"]],
    })


# ----------------------------------------------------------------- page 1
def overrides_folder() -> Path:
    """ladder_overrides.json lives beside config.json, shared with the main app."""
    return CONFIG_PATH.parent


def analyse_one(path: Path, position: int, cfg, rt_max, manual=None, progress=None) -> dict:
    """Read, peak-pick and plot one ladder with its own rt_max and hand-placed peaks.

    Indexes are numbered by their place in the full ladder (fixed slots), so a
    peak marked as missing leaves a gap rather than renumbering the rest.
    """
    run = load_run(path, SCAN_CACHE, progress=progress)
    params = with_rt_max(cfg["params"], rt_max)
    ladder = analyse_ladder(run, cfg["ladder_masses"], params, manual=manual,
                            fixed_slots=True)
    LADDER_OBJECTS[path.stem] = ladder

    stem = FIGURES / f"ladder_{position:02d}_{safe_name(path.stem)}"
    plot_ladder(ladder, path.stem, stem, formats=("png",))
    entry = ladder_entry(ladder, name=path.stem, file=path.name, figure=stem.name + ".png",
                         rt_max=rt_max, default_rt_max=cfg["params"].rt_max,
                         masses=cfg["ladder_masses"])
    entry["path"] = str(path)
    entry["position"] = position
    return entry


def scan_ladders(job: Job):
    cfg = read_config()
    if cfg is None:
        raise RuntimeError(STATE["config_error"])
    files = find_ladders(cfg["ladder_dir"])
    if not files:
        raise RuntimeError(f"No .raw files in {cfg['ladder_dir']}.")

    FIGURES.mkdir(parents=True, exist_ok=True)
    for old in FIGURES.glob("*.png"):
        old.unlink()
    LADDER_OBJECTS.clear()

    overrides = load_overrides(overrides_folder())
    default = cfg["params"].rt_max
    ladders = []
    for i, path in enumerate(files):
        base = i / len(files)
        job.update(f"Reading {path.name}", base + 0.02, "scanning")
        saved = overrides.get(path.name, {})
        rt_max = saved.get("rt_max", default)
        progress = lambda m, b=base: job.update(m, min(b + 0.8 / len(files), 0.97))
        try:
            entry = analyse_one(path, i, cfg, rt_max, manual=saved.get("peaks"),
                                progress=progress)
        except ValueError:
            if "rt_max" not in saved:
                raise
            # a saved limit that no longer works should not block the whole scan
            entry = analyse_one(path, i, cfg, default, manual=saved.get("peaks"))
            entry["warnings"].insert(0, f"Saved retention time limit {rt_max:g} min "
                                        "could not be used, so the config default was.")
        ladders.append(entry)
        job.update(f"Plotted {i + 1} of {len(files)} ladders", (i + 1) / len(files))

    STATE["ladders"] = ladders
    job.update(f"{len(ladders)} ladders ready", 1.0, "ready")
    job.done = True


@app.post("/api/scan")
def scan():
    job = start_job(scan_ladders)
    return jsonify({"job": job.id})


@app.get("/api/job/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if job is None:
        abort(404, description="Unknown job")
    return jsonify(job.to_dict())


@app.get("/api/ladders")
def ladders():
    counts = sorted({l["n_peaks"] for l in STATE["ladders"]})
    return jsonify({
        "ladders": STATE["ladders"],
        "mixed_peak_counts": len(counts) > 1,
    })


def _find_loaded(name):
    index = next((i for i, l in enumerate(STATE["ladders"]) if l["name"] == name), None)
    cfg = STATE["config"] or read_config()
    if index is None:
        return None, None, (jsonify({"error": "That ladder is not loaded. "
                                              "Reload from ladder_dir."}), 400)
    if cfg is None:
        return None, None, (jsonify({"error": STATE["config_error"]}), 400)
    return index, cfg, None


def _replace_entry(index, entry):
    STATE["ladders"][index] = entry
    counts = sorted({l["n_peaks"] for l in STATE["ladders"]})
    return jsonify({"ladder": entry, "mixed_peak_counts": len(counts) > 1,
                    "saved_to": str(overrides_path(overrides_folder()))})


@app.post("/api/ladder/rt_max")
def set_ladder_rt_max():
    """Re-pick one ladder's peaks with its own rt_max and remember the value.

    Body: {"name": ladder name, "rt_max": minutes, or null to go back to the
    config default}. Only that ladder is re-read, from the scan cache. Peaks
    placed by hand are kept where they still fall inside the new limit.
    """
    payload = request.get_json(silent=True) or {}
    index, cfg, error = _find_loaded(payload.get("name"))
    if error:
        return error
    current = STATE["ladders"][index]
    try:
        rt_max = parse_rt_max(payload.get("rt_max"), cfg["params"].rt_max)
        entry = analyse_one(Path(current["path"]), current.get("position", index), cfg,
                            rt_max, manual=saved_picks(current))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # a hand-placed peak now past the limit is let go rather than kept in reserve
    store_override(overrides_folder(), entry["file"], rt_max=rt_max,
                   default_rt_max=cfg["params"].rt_max, peaks=saved_picks(entry))
    return _replace_entry(index, entry)


@app.post("/api/ladder/peaks")
def set_ladder_peaks():
    """Place peaks by hand on one ladder.

    Body: {"name": ladder name, "picks": {index: pick}}, a pick being a time to
    snap to the peak top, {"rt": t, "exact": true}, or null for "this index has no
    peak". The picks replace every earlier hand-made choice for that ladder, so
    {} goes back to automatic picking.
    """
    payload = request.get_json(silent=True) or {}
    index, cfg, error = _find_loaded(payload.get("name"))
    if error:
        return error
    current = STATE["ladders"][index]
    try:
        picks = parse_picks(payload.get("picks"), 2 * len(cfg["ladder_masses"]))
        entry = analyse_one(Path(current["path"]), current.get("position", index), cfg,
                            current["rt_max"], manual=picks)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if entry["dropped"]:
        return jsonify({"error": "Index " + ", ".join(map(str, entry["dropped"]))
                        + " was placed past the retention time limit."}), 400

    store_override(overrides_folder(), entry["file"], rt_max=current["rt_max"],
                   default_rt_max=cfg["params"].rt_max, peaks=saved_picks(entry))
    return _replace_entry(index, entry)


@app.get("/api/ladder/traces")
def ladder_traces():
    """The extracted ion traces behind one ladder, for the in-browser peak editor."""
    name = request.args.get("name")
    ladder = LADDER_OBJECTS.get(name)
    if ladder is None:
        return jsonify({"error": "That ladder is not loaded. Reload from ladder_dir."}), 400
    return jsonify(traces_payload(ladder, name))


@app.get("/api/figure/<path:name>")
def figure(name):
    target = (FIGURES / name).resolve()
    if not target.is_file() or FIGURES.resolve() not in target.parents:
        abort(404)
    return send_file(target, mimetype="image/png", max_age=0)


# ----------------------------------------------------------------- page 2
@app.post("/api/calibration")
def calibration():
    """Fit every non-standard ladder to the chosen standard.

    Which indexes take part is decided on page 1, per ladder. An index only counts
    if it was found in both runs and left ticked on both, so deselecting one on the
    standard drops it from every fit.
    """
    payload = request.get_json(silent=True) or {}
    standard_name = payload.get("standard")
    selections = payload.get("selections") or {}

    by_name = {l["name"]: l for l in STATE["ladders"]}
    if standard_name not in by_name:
        return jsonify({"error": "Choose a standard ladder that has been loaded."}), 400
    standard = by_name[standard_name]
    standard_rt = {int(k): v for k, v in standard["peaks"].items()}
    standard_allowed = _index_set(selections.get(standard_name))

    fits = []
    for ladder in STATE["ladders"]:
        if ladder["name"] == standard_name:
            continue
        other_rt = {int(k): v for k, v in ladder["peaks"].items()}
        file_allowed = _index_set(selections.get(ladder["name"]))

        shared = set(standard_rt) & set(other_rt)
        chosen = set(shared)
        if standard_allowed is not None:
            chosen &= standard_allowed
        if file_allowed is not None:
            chosen &= file_allowed

        result = calibrate(standard_rt, other_rt, indexes=sorted(chosen))
        result["file"] = ladder["file"]
        result["name"] = ladder["name"]
        # why each dropped index went, so page 2 can say so without guessing
        result["excluded_by_standard"] = sorted(
            i for i in shared - chosen
            if standard_allowed is not None and i not in standard_allowed
        )
        result["excluded_by_file"] = sorted(
            i for i in shared - chosen
            if file_allowed is not None and i not in file_allowed
            and (standard_allowed is None or i in standard_allowed)
        )
        result["missing_from_one_run"] = sorted(
            (set(standard_rt) | set(other_rt)) - shared
        )
        fits.append(result)

    return jsonify({
        "standard": standard_name,
        "standard_peaks": standard_rt,
        "standard_indexes": sorted(standard_allowed) if standard_allowed is not None
                            else sorted(standard_rt),
        "fits": fits,
        "summary": summarise(fits),
    })


def _index_set(value):
    return None if value is None else {int(i) for i in value}


@app.post("/api/standard/save")
def save_active_standard():
    """Write the chosen standard to standard_ladder/ for the main app to use.

    Only the indexes ticked on page 1 are recorded as usable, so an index judged
    bad here cannot come back when a compound is normalized later.
    """
    payload = request.get_json(silent=True) or {}
    name = payload.get("standard")
    by_name = {l["name"]: l for l in STATE["ladders"]}
    if name not in by_name:
        return jsonify({"error": "Choose a standard ladder that has been loaded."}), 400
    cfg = STATE["config"] or read_config()
    if cfg is None:
        return jsonify({"error": STATE["config_error"]}), 400

    ladder = by_name[name]
    peaks = {int(k): v for k, v in ladder["peaks"].items()}
    selected = payload.get("selections", {}).get(name)
    selected = [int(i) for i in selected] if selected is not None else sorted(peaks)

    try:
        info = save_standard(
            cfg["ladder_dir"], name=name, peaks=peaks,
            masses=cfg["ladder_masses"],
            params=with_rt_max(cfg["params"], ladder.get("rt_max", cfg["params"].rt_max)),
            manual_indexes=sorted(int(i) for i in ladder.get("manual", {})),
            absent_indexes=ladder.get("absent", []),
            selected_indexes=selected, source_file=ladder["path"],
            n_ladder_peaks=ladder["n_peaks"], note=payload.get("note"),
        )
    except StandardError_ as exc:
        return jsonify({"error": str(exc)}), 400

    STATE["standard_error"] = None
    record = info["record"]
    return jsonify({
        "saved": True,
        "path": info["path"],
        "standard_dir": info["standard_dir"],
        "compound_dir": info["compound_dir"],
        "name": record["name"],
        "saved_at": record["saved_at"],
        "usable_indexes": record["usable_indexes"],
        "n_peaks": record["n_ladder_peaks"],
    })


@app.errorhandler(400)
@app.errorhandler(404)
def json_error(err):
    return jsonify({"error": getattr(err, "description", str(err))}), err.code


@app.errorhandler(Exception)
def unexpected(err):
    code = getattr(err, "code", 500)
    if code != 500:
        return jsonify({"error": getattr(err, "description", str(err))}), code
    traceback.print_exc()
    return jsonify({"error": f"{err.__class__.__name__}: {err}"}), 500


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--config", default=None, help="path to config.json")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    global CONFIG_PATH
    if args.config:
        CONFIG_PATH = Path(args.config).expanduser().resolve()

    status = parser_status()
    print(f"ThermoRawFileParser: {status['detail']}"
          if status["available"] else
          "!! ThermoRawFileParser not set up. Run: python tools/get_parser.py")
    cfg = read_config()
    if cfg is None:
        print("!! " + STATE["config_error"])
    else:
        print(f"ladder_dir: {cfg['ladder_dir']} "
              f"({len(find_ladders(cfg['ladder_dir']))} .raw files)")
    print(f"Open http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
