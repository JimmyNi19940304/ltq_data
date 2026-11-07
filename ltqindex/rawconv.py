"""Convert Thermo .raw files to mzML using ThermoRawFileParser.

The parser is an external executable. It is located, in order of preference:
  1. the LTQ_RAW_PARSER environment variable
  2. tools/ThermoRawFileParser/ThermoRawFileParser next to this project
  3. `ThermoRawFileParser` on PATH
  4. a ThermoRawFileParser.exe run through mono (Linux/macOS) or directly (Windows)

Run `python tools/get_parser.py` once to download a self-contained build.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"


class ParserNotFound(RuntimeError):
    pass


class ConversionError(RuntimeError):
    pass


def _candidate_paths():
    env = os.environ.get("LTQ_RAW_PARSER")
    if env:
        yield Path(env)

    names = ["ThermoRawFileParser", "ThermoRawFileParser.exe"]
    for base in (TOOLS_DIR, TOOLS_DIR / "ThermoRawFileParser"):
        for name in names:
            yield base / name

    for name in names:
        found = shutil.which(name)
        if found:
            yield Path(found)


def find_parser():
    """Return the command prefix that runs ThermoRawFileParser, e.g. ['/path/to/exe']."""
    for path in _candidate_paths():
        if not path.is_file():
            continue
        if path.suffix.lower() == ".exe" and platform.system() != "Windows":
            mono = shutil.which("mono")
            if not mono:
                continue
            return [mono, str(path)]
        if not os.access(path, os.X_OK) and platform.system() != "Windows":
            try:
                path.chmod(path.stat().st_mode | 0o111)
            except OSError:
                continue
        return [str(path)]
    raise ParserNotFound(
        "ThermoRawFileParser was not found. Run `python tools/get_parser.py` to "
        "download it, or set LTQ_RAW_PARSER to the executable."
    )


def parser_status():
    """Human-readable availability check, used by the /api/health endpoint."""
    try:
        cmd = find_parser()
    except ParserNotFound as exc:
        return {"available": False, "detail": str(exc)}
    try:
        out = subprocess.run(
            cmd + ["--version"], capture_output=True, text=True, timeout=120
        )
        version = (out.stdout or out.stderr).strip().splitlines()
        version = version[-1] if version else "unknown"
    except Exception as exc:  # pragma: no cover - defensive
        return {"available": False, "detail": f"{' '.join(cmd)} failed: {exc}"}
    return {"available": True, "detail": f"{version} ({cmd[-1]})"}


def raw_to_mzml(raw_path, out_dir, out_name=None, progress=None) -> Path:
    """Convert one .raw file to indexed mzML. Returns the mzML path.

    Conversion is skipped when an mzML newer than the .raw already exists.
    """
    raw_path = Path(raw_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mzml_path = out_dir / f"{out_name or raw_path.stem}.mzML"

    if mzml_path.exists() and mzml_path.stat().st_mtime >= raw_path.stat().st_mtime:
        if progress:
            progress(f"Reusing converted data for {raw_path.name}")
        return mzml_path

    cmd = find_parser() + [
        "-i", str(raw_path),
        "-b", str(mzml_path),
        "-f", "2",          # indexed mzML
        "-l", "4",          # errors only
        "-e",               # tolerate instrument properties this model does not report
    ]
    if progress:
        progress(f"Converting {raw_path.name} to mzML")

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not mzml_path.exists():
        detail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise ConversionError(f"Could not convert {raw_path.name}.\n{detail}")
    return mzml_path
