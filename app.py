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

from ltqindex.analysis import DEFAULT_LADDER_MASSES, Params
from ltqindex.calibration import MODELS, ConfigError, load_config
from ltqindex.standards import StandardError_, folders, load_standard
from ltqindex.pipeline import Job, Session, safe_name
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
        "usable_indexes": record["usable_indexes"],
        "standard_dir": str(paths["standard_dir"]),
        "compound_dir": str(paths["compound_dir"]),
    }

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
    (field `ladder_path` / `compound_path_N`) which avoids a large upload.
    """
    session = Session(WORK_ROOT)

    ladder_src = _stash(session, request.files.get("ladder_file"),
                        request.form.get("ladder_path", "").strip(), "ladder")
    if ladder_src is None:
        return jsonify({"error": "Choose a ladder .raw file."}), 400
    session.ladder_path = ladder_src

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
        session.compound_specs.append({"path": path, "name": name, "nominal_mass": nominal})

    names = [c["name"] for c in session.compound_specs]
    if len(set(names)) != len(names):
        return jsonify({"error": "Give each compound a different name."}), 400

    session.params = Params.from_dict(request.form.to_dict())
    session.ladder_dir = ladder_dir_from_config()
    model = (request.form.get("transform_model") or "quadratic").strip()
    if model not in {k for k, _, _ in MODELS}:
        return jsonify({"error": f"Unknown transform model '{model}'."}), 400
    session.transform_model = model
    SESSIONS[session.id] = session

    job = start_job(session.run_chromatograms)
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


def _parse_limit(value):
    """'all', '' or None mean calibrate with every ladder index."""
    if value in (None, "", "all"):
        return None
    try:
        return max(2, int(value))
    except (TypeError, ValueError):
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


@app.post("/api/session/<sid>/reanalyse")
def reanalyse(sid):
    """Re-run chromatograms with edited settings. Converted scans stay cached."""
    session = get_session(sid)
    payload = request.get_json(silent=True) or {}
    session.params = Params.from_dict(payload.get("params"))
    model = payload.get("transform_model")
    if model:
        if model not in {k for k, _, _ in MODELS}:
            return jsonify({"error": f"Unknown transform model '{model}'."}), 400
        session.transform_model = model
    if payload.get("ladder_masses"):
        session.ladder_masses = _parse_masses(str(payload["ladder_masses"]))
    if "ladder_limit" in payload:
        session.ladder_limit = _parse_limit(payload["ladder_limit"])
    job = start_job(session.run_chromatograms)
    return jsonify({"job": job.id})


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
                "Adjust the peak window if the shaded region does not cover the peak you want to average.",
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
