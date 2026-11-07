"""Download ThermoRawFileParser, which is what actually reads Thermo .raw files.

    python tools/get_parser.py

Self-contained builds are used where available, so no .NET or mono install is
needed. Falls back to the portable build, which needs mono on Linux/macOS.
"""

from __future__ import annotations

import json
import platform
import shutil
import ssl
import sys
import urllib.request
import zipfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
RELEASES = "https://api.github.com/repos/compomics/ThermoRawFileParser/releases"


def pick_asset(assets, keys):
    for key in keys:
        for a in assets:
            if key in a["name"].lower():
                return a
    return None


def platform_keys():
    system, machine = platform.system(), platform.machine().lower()
    if system == "Windows":
        return ["-win.zip"]
    if system == "Darwin":
        return ["-osx-arm64.zip", "-osx.zip"] if machine in ("arm64", "aarch64") else ["-osx.zip"]
    return ["-linux.zip"]


def main():
    ctx = ssl.create_default_context()
    print("Looking up releases…")
    with urllib.request.urlopen(RELEASES, context=ctx, timeout=60) as r:
        releases = json.load(r)

    asset = None
    for rel in releases:
        asset = pick_asset(rel.get("assets", []), platform_keys())
        if asset:
            print(f"Found {asset['name']} in {rel['tag_name']}")
            break
    if asset is None:
        for rel in releases:
            asset = pick_asset(rel.get("assets", []), [".zip"])
            if asset:
                print(f"No self-contained build for this platform; using {asset['name']}.")
                print("It needs mono on Linux/macOS: apt install mono-runtime, or brew install mono.")
                break
    if asset is None:
        sys.exit("No downloadable build found.")

    dest_dir = TOOLS / "ThermoRawFileParser"
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    archive = TOOLS / "_parser.zip"

    print(f"Downloading {round(asset['size'] / 1e6, 1)} MB…")
    with urllib.request.urlopen(asset["browser_download_url"], context=ctx, timeout=600) as r, \
            open(archive, "wb") as out:
        shutil.copyfileobj(r, out)

    print("Unpacking…")
    with zipfile.ZipFile(archive) as z:
        z.extractall(dest_dir)
    archive.unlink()

    for name in ("ThermoRawFileParser", "ThermoRawFileParser.exe"):
        exe = dest_dir / name
        if exe.is_file():
            exe.chmod(exe.stat().st_mode | 0o111)
            print(f"Ready: {exe}")
            return
    sys.exit(f"Unpacked to {dest_dir} but found no executable inside.")


if __name__ == "__main__":
    main()
