"""Local web front end for ladder-indexed LTQ analysis.

    python app.py            then open http://127.0.0.1:5000
"""

from __future__ import annotations

import argparse
import shutil
import threading
import traceback
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

from ltqindex.analysis import DEFAULT_LADDER_MASSES, Params, analyse_ladder
from ltqindex.calibration import MODELS, ConfigError, load_config
from ltqindex.standards import StandardError_, folders, load_standard
from ltqindex.compoundedit import trace_payload
from ltqindex.ladderedit import traces_payload
from ltqindex.msdata import load_run
from ltqindex.pipeline import Job, Session, safe_name
from ltqindex.plotting import plot_ladder
from ltqindex.rawconv import parser_status

ROOT = Path(__file__).resolve().parent
WORK_ROOT = ROOT / "workspace"
WORK_ROOT.mkdir(exist_ok=True)
CONFIG_PATH = ROOT / "config.json"


def ladder_dir_from_config():
    """The ladder folder, which is what locates standard_ladder/ and compound/."""
    try:
        return load_config(CONFIG_PATH)["ladder_dir"]
    except ConfigError:
        return None


def standard_status():
    """What the page shows about the saved standard ladder."""
    ladder_dir = ladder_dir_from_config()
    if ladder_dir is None:
        return {"available": False,
                "detail": "No config.json, so there is no standard ladder folder to read."}
    paths = folders(ladder_dir)
    try:
        record = load_standard(ladder_dir)
    except StandardError_ as exc:
        return {"available": False, "detail": str(exc),
                "standard_dir": str(paths["standard_dir"])}
    if record is None:
        return {"available": False,
                "detail": "No standard ladder saved yet. Save one in the calibration app.",
                "standard_dir": str(paths["standard_dir"])}
    return {
        "available": True,
        "name": record["name"],
        "saved_at": record.get("saved_at"),
        "n_peaks": len(record["peaks"]),
        "peaks": {int(k): v for k, v in record["peaks"].items()},
        "usable_indexes": record["usable_indexes"],
        "manual_indexes": record.get("manual_indexes", []),
        "absent_indexes": record.get("absent_indexes", []),
        "ladder_masses": record.get("ladder_masses"),
        "source_file": record.get("source_file"),
        "standard_file": str(paths["standard_file"]),
        "standard_dir": str(paths["standard_dir"]),
        "compound_dir": str(paths["compound_dir"]),
    }


STANDARD_FIGURES = WORK_ROOT / "standard-figure"


def standard_figure_path():
    """Draw the saved standard ladder from its own .raw file, peaks as saved.

    The peaks are put back exactly where the calibration app saved them, so the
    picture shows the standard the compounds are placed on, not a fresh guess.
    Drawn once per saved standard and then reused. Raises LookupError with a
    message when it cannot be drawn.
    """
    ladder_dir = ladder_dir_from_config()
    if ladder_dir is None:
        raise LookupError("No config.json, so there is no standard ladder folder to read.")
    try:
        record = load_standard(ladder_dir)
    except StandardError_ as exc:
        raise LookupError(str(exc)) from None
    if record is None:
        raise LookupError("No standard ladder saved yet.")
    source = Path(str(record.get("source_file") or ""))
    if not source.is_file():
        raise LookupError(f"The standard's .raw file is not at {source}, so it cannot be "
                          "drawn. Its index table is still used.")

    stem = STANDARD_FIGURES / safe_name(f"{record['name']}_{record.get('saved_at', '')}")
    png = stem.parent / (stem.name + ".png")
    if png.is_file():
        return png
    run = load_run(source, WORK_ROOT / "scan-cache")
    params = Params.from_dict(record.get("params") or {})
    masses = record.get("ladder_masses") or DEFAULT_LADDER_MASSES
    peaks = {int(k): float(v) for k, v in record["peaks"].items()}
    manual = {i: None for i in range(1, 2 * len(masses) + 1)}
    manual.update({i: {"rt": rt, "exact": True} for i, rt in peaks.items()})
    ladder = analyse_ladder(run, masses, params, manual=manual, fixed_slots=True)
    by_hand = {int(i) for i in record.get("manual_indexes", [])}
    for tr in ladder.traces:
        for pk in tr["peaks"]:
            pk.manual = int(pk.index) in by_hand
            pk.exact = False
    ladder.excluded = set(peaks) - {int(i) for i in record.get("usable_indexes", peaks)}
    for old in STANDARD_FIGURES.glob("*.png"):
        old.unlink()
    plot_ladder(ladder, f"{record['name']} (standard)", stem, formats=("png",))
    return png

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024 * 1024   # raw files run to hundreds of MB

SESSIONS: dict[str, Session] = {}
JOBS: dict[str, Job] = {}


def get_session(sid) -> Session:
    session = SESSIONS.get(sid)
    if session is None:
        abort(404, description="That analysis session has expired. Start a new run.")
    return session


def start_job(fn, *args) -> Job:
    job = Job(id=f"job{len(JOBS) + 1}_{id(fn) % 9973}")
    JOBS[job.id] = job

    def target():
        try:
            fn(job, *args)
        except Exception as exc:  # surfaced to the browser, full trace to the console
            job.error = str(exc) or exc.__class__.__name__
            job.stage = "error"
            job.message = job.error
            job.done = True
            traceback.print_exc()

    threading.Thread(target=target, daemon=True).start()
    return job


# --------------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(app.template_folder or "templates", "index.html")


@app.get("/api/standard/figure")
def standard_figure():
    try:
        png = standard_figure_path()
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    return send_file(png, mimetype="image/png", max_age=0)


@app.get("/api/health")
def health():
    return jsonify({"parser": parser_status(), "default_masses": DEFAULT_LADDER_MASSES,
                    "default_params": Params().to_dict(),
                    "standard": standard_status(),
                    "models": [{"key": k, "label": label} for k, _, label in MODELS]})


# ------------------------------------------------------------------- upload
@app.post("/api/session")
def create_session():
    """Accepts multipart form data: the ladder file plus 1-5 compound files.

    Each file may be uploaded, or given as a path already on this machine
    (field `ladder_path` / `compound_path_N`) which avoids a large upload. Only
    the ladder is read now; the compounds wait until its fit is approved.
    """
    session = Session(WORK_ROOT, overrides_folder=CONFIG_PATH.parent)

    ladder_src = _stash(session, request.files.get("ladder_file"),
                        request.form.get("ladder_path", "").strip(), "ladder")
    if ladder_src is None:
        return jsonify({"error": "Choose a ladder .raw file."}), 400
    session.ladder_path = ladder_src
    # the ladder's own name keys its hand edits, shared with the calibration app
    upload = request.files.get("ladder_file")
    session.ladder_file = (upload.filename if upload is not None and upload.filename
                           else Path(request.form.get("ladder_path", "").strip()).name)

    masses = _parse_masses(request.form.get("ladder_masses", ""))
    if len(masses) < 2:
        return jsonify({"error": "Enter at least two ladder masses."}), 400
    session.ladder_masses = masses

    count = int(request.form.get("compound_count", 0) or 0)
    if not 1 <= count <= 5:
        return jsonify({"error": "Add between one and five compound files."}), 400

    for i in range(count):
        path = _stash(session, request.files.get(f"compound_file_{i}"),
                      request.form.get(f"compound_path_{i}", "").strip(), f"compound{i}")
        if path is None:
            return jsonify({"error": f"Compound {i + 1} has no .raw file."}), 400
        name = (request.form.get(f"compound_name_{i}", "") or path.stem).strip()
        raw_mass = request.form.get(f"compound_mass_{i}", "").strip()
        try:
            nominal = float(raw_mass) if raw_mass else None
        except ValueError:
            return jsonify({"error": f"Nominal mass for {name} is not a number."}), 400
        if nominal is None:
            return jsonify({"error": f"Enter the nominal mass for {name}."}), 400
        upload = request.files.get(f"compound_file_{i}")
        # the file's own name keys its hand-placed peaks and windows
        file_name = (upload.filename if upload is not None and upload.filename
                     else Path(request.form.get(f"compound_path_{i}", "").strip()).name)
        session.compound_specs.append({"path": path, "name": name, "nominal_mass": nominal,
                                       "file": file_name})

    names = [c["name"] for c in session.compound_specs]
    if len(set(names)) != len(names):
        return jsonify({"error": "Give each compound a different name."}), 400

    session.params = Params.from_dict(request.form.to_dict())
    session.ladder_dir = ladder_dir_from_config()
    SESSIONS[session.id] = session

    job = start_job(session.run_ladder)
    return jsonify({"session": session.id, "job": job.id})


def _stash(session: Session, upload, server_path: str, tag: str):
    """Save an uploaded file, or link/copy one that is already on this machine."""
    if upload is not None and upload.filename:
        dest = session.uploads / secure_filename(upload.filename)
        upload.save(dest)
        return dest
    if server_path:
        src = Path(server_path).expanduser()
        if not src.is_file():
            abort(400, description=f"No file at {server_path}")
        dest = session.uploads / safe_name(src.name, tag)
        try:
            dest.symlink_to(src.resolve())
        except OSError:
            shutil.copy2(src, dest)
        return dest
    return None


def _parse_masses(text):
    out = []
    for chunk in text.replace(",", " ").replace(";", " ").split():
        try:
            out.append(float(chunk))
        except ValueError:
            continue
    return out or list(DEFAULT_LADDER_MASSES)


# -------------------------------------------------------------------- status
@app.get("/api/job/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if job is None:
        abort(404, description="Unknown job")
    return jsonify(job.to_dict())


@app.get("/api/session/<sid>/chromatograms")
def chromatograms(sid):
    session = get_session(sid)
    return jsonify({
        "figures": session.chromatogram_figures,
        "summary": session.summary() if session.ladder else None,
    })


# ------------------------------------------------------- step 2: the ladder
def _ladder_ready(session):
    if session.ladder is None:
        abort(400, description="The ladder has not been read yet.")


@app.get("/api/session/<sid>/ladder")
def ladder_page(sid):
    """The ladder, the standard it is fitted onto, and the fit if one was made."""
    session = get_session(sid)
    _ladder_ready(session)
    return jsonify(session.ladder_page())


@app.get("/api/session/<sid>/ladder/traces")
def ladder_traces(sid):
    session = get_session(sid)
    _ladder_ready(session)
    return jsonify(traces_payload(session.ladder, session.ladder_name))


@app.post("/api/session/<sid>/ladder/rt_max")
def ladder_rt_max(sid):
    """Re-pick the ladder with its own retention-time limit; null = the default."""
    session = get_session(sid)
    _ladder_ready(session)
    try:
        session.set_ladder_rt_max((request.get_json(silent=True) or {}).get("rt_max"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(session.ladder_page())


@app.post("/api/session/<sid>/ladder/peaks")
def ladder_peaks(sid):
    """Place ladder peaks by hand: {"picks": {index: time, {"rt", "exact"}, or null}}."""
    session = get_session(sid)
    _ladder_ready(session)
    try:
        session.set_ladder_picks((request.get_json(silent=True) or {}).get("picks"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(session.ladder_page())


@app.post("/api/session/<sid>/fit")
def fit(sid):
    """Fit the ladder onto the standard: {"model": "cubic", "indexes": [1, 2, ...]}."""
    session = get_session(sid)
    _ladder_ready(session)
    payload = request.get_json(silent=True) or {}
    model = payload.get("model") or session.transform_model
    if model not in {k for k, _, _ in MODELS}:
        return jsonify({"error": f"Unknown model '{model}'."}), 400
    try:
        session.fit_ladder(model, payload.get("indexes") or [])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(session.ladder_page())


@app.post("/api/session/<sid>/compounds")
def compounds(sid):
    """Read the compounds and put them through the approved fit."""
    session = get_session(sid)
    _ladder_ready(session)
    if session.standard is not None and session.transform is None:
        return jsonify({"error": "Fit the ladder onto the standard first."}), 400
    job = start_job(session.run_chromatograms)
    return jsonify({"job": job.id})


def _compound(session, n):
    try:
        return session.compound(n)
    except LookupError as exc:
        abort(404, description=str(exc))


@app.get("/api/session/<sid>/compound/<int:n>/trace")
def compound_trace(sid, n):
    """The MS2 TIC and peaks the compound peak editor draws."""
    session = get_session(sid)
    return jsonify(trace_payload(_compound(session, n)))


@app.post("/api/session/<sid>/compound/<int:n>/peaks")
def compound_peaks(sid, n):
    """Save hand-placed peaks and windows for one compound and redraw it."""
    session = get_session(sid)
    _compound(session, n)
    payload = request.get_json(silent=True) or {}
    try:
        session.set_compound_edits(n, payload.get("peaks"), payload.get("windows"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"figures": session.chromatogram_figures, "summary": session.summary()})


@app.post("/api/session/<sid>/confirm")
def confirm(sid):
    """The gate from the requirement: only past a yes do the spectra get made."""
    session = get_session(sid)
    payload = request.get_json(silent=True) or {}
    if not payload.get("correct"):
        session.confirmed = False
        return jsonify({
            "confirmed": False,
            "checklist": [
                "Confirm the ladder file really is the ladder run, and that its masses match the list above.",
                "Check every ladder mass shows two peaks and that index 1-N increases from left to right.",
                "Widen or narrow the m/z tolerance if a trace looks noisy or empty.",
                "Raise the minimum peak height to skip shoulders, or lower it if a real peak was missed.",
                "Check each compound's nominal mass against the MS2 precursor reported in the file.",
                "Use Edit peaks and windows on a chromatogram to move a peak or resize its shaded window.",
            ],
        })
    session.confirmed = True
    job = start_job(session.run_spectra)
    return jsonify({"confirmed": True, "job": job.id})


@app.get("/api/session/<sid>/spectra")
def spectra(sid):
    session = get_session(sid)
    return jsonify({"figures": session.spectrum_figures})


@app.post("/api/session/<sid>/export")
def export(sid):
    session = get_session(sid)
    if not session.confirmed:
        return jsonify({"error": "Approve the chromatograms before exporting."}), 400

    def build(job, s=session):
        s.bundle_path = s.build_bundle(job)

    job = start_job(build)
    return jsonify({"job": job.id})


@app.get("/api/session/<sid>/published")
def published(sid):
    """Where the normalized per-compound results were written, if anywhere."""
    session = get_session(sid)
    return jsonify({"published": getattr(session, "published_results", []),
                    "compound_dir": (str(folders(session.ladder_dir)["compound_dir"])
                                     if session.ladder_dir else None)})


@app.get("/api/session/<sid>/download")
def download(sid):
    session = get_session(sid)
    bundle = getattr(session, "bundle_path", None)
    if not bundle or not Path(bundle).exists():
        abort(404, description="No results bundle has been built yet.")
    return send_file(bundle, as_attachment=True, download_name=Path(bundle).name)


@app.get("/api/session/<sid>/figure/<path:name>")
def figure(sid, name):
    session = get_session(sid)
    target = (session.figures / name).resolve()
    if not target.is_file() or session.figures.resolve() not in target.parents:
        abort(404)
    return send_file(target, mimetype="image/png", max_age=0)


@app.errorhandler(400)
@app.errorhandler(404)
def json_error(err):
    return jsonify({"error": getattr(err, "description", str(err))}), err.code


@app.errorhandler(413)
def too_large(err):
    return jsonify({"error": "That upload is larger than the 8 GB limit."}), 413


@app.errorhandler(Exception)
def unexpected(err):
    """Keep every API reply JSON so the page can show what went wrong."""
    code = getattr(err, "code", 500)
    if code != 500:
        return jsonify({"error": getattr(err, "description", str(err))}), code
    traceback.print_exc()
    return jsonify({"error": f"{err.__class__.__name__}: {err}"}), 500


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    status = parser_status()
    if not status["available"]:
        print("!! ThermoRawFileParser is not set up yet:")
        print("   " + status["detail"])
        print("   Run: python tools/get_parser.py\n")
    else:
        print(f"ThermoRawFileParser: {status['detail']}")
    std = standard_status()
    print(f"Standard ladder: {std['name']}" if std["available"]
          else f"Standard ladder: {std['detail']}")
    print(f"Open http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
