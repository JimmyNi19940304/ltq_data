#!/usr/bin/env bash
# Set up Ladder Index on a fresh Ubuntu machine.
#
#   ./setup.sh                      # asks where the data lives
#   ./setup.sh /srv/ltq-data        # takes the data folder as an argument
#
# Creates a virtual environment, installs the Python libraries, downloads
# ThermoRawFileParser, and writes config.json. Everything stays inside this
# folder and the data folder you name; nothing is installed system-wide.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# a .zip does not always carry the executable bit, so restore it here rather
# than making the reader remember chmod
chmod +x setup.sh run.sh install-services.sh 2>/dev/null || true

GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; RED=$'\033[0;31m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$BOLD" "$1" "$OFF"; }
ok()   { printf '    %s✓%s %s\n' "$GREEN" "$OFF" "$1"; }
warn() { printf '    %s!%s %s\n' "$YELLOW" "$OFF" "$1"; }
die()  { printf '\n%sError:%s %s\n\n' "$RED" "$OFF" "$1" >&2; exit 1; }

# ---------------------------------------------------------------- prerequisites
step "Checking prerequisites"

command -v python3 >/dev/null || die "python3 is not installed."
PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 - <<'EOF' || die "Python 3.9 or newer is required."
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
EOF
ok "python3 $PY_VERSION"

MISSING=()
python3 -c 'import venv, ensurepip' 2>/dev/null || MISSING+=("python3-venv")
command -v unzip >/dev/null || MISSING+=("unzip")
command -v curl  >/dev/null || MISSING+=("curl")
# ThermoRawFileParser is a self-contained .NET build and needs libicu at runtime
ldconfig -p 2>/dev/null | grep -q libicu || MISSING+=("libicu-dev")

if [ ${#MISSING[@]} -gt 0 ]; then
    printf '\n%sSome system packages are missing.%s Run this, then start setup again:\n\n' "$BOLD" "$OFF"
    printf '    sudo apt update && sudo apt install -y %s\n\n' "${MISSING[*]}"
    exit 1
fi
ok "python3-venv, unzip, curl, libicu"

# ------------------------------------------------------------------ data folder
step "Data folder"

DATA_DIR="${1:-}"
if [ -z "$DATA_DIR" ]; then
    printf '    Where should the ladder files and results live?\n'
    printf '    This folder will hold ladders/, standard_ladder/ and compound/.\n'
    read -r -p "    Data folder [$HOME/ltq-data]: " DATA_DIR
    DATA_DIR="${DATA_DIR:-$HOME/ltq-data}"
fi
DATA_DIR="${DATA_DIR/#\~/$HOME}"
mkdir -p "$DATA_DIR/ladders"
DATA_DIR="$(cd "$DATA_DIR" && pwd)"
ok "$DATA_DIR/ladders"

# --------------------------------------------------------------------- the venv
step "Creating the virtual environment"

if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
    ok "created .venv"
else
    ok "reusing the existing .venv"
fi
./.venv/bin/python -m pip install --quiet --upgrade pip

step "Installing the Python libraries (a minute or two)"
./.venv/bin/pip install --quiet -r requirements.txt
./.venv/bin/pip install --quiet waitress
./.venv/bin/python - <<'EOF'
import flask, numpy, scipy, matplotlib, pyteomics, psims, waitress  # noqa: F401
EOF
ok "flask, numpy, scipy, matplotlib, pyteomics, psims, waitress"

# ------------------------------------------------------------------- the parser
step "Downloading ThermoRawFileParser (about 50 MB)"

PARSER="tools/ThermoRawFileParser/ThermoRawFileParser"
if [ -x "$PARSER" ] && "./$PARSER" --version >/dev/null 2>&1; then
    ok "already installed ($("./$PARSER" --version 2>&1 | tail -1))"
else
    # The helper script asks the GitHub API which release is current. Some
    # networks block api.github.com while allowing ordinary downloads, so fall
    # back to a pinned release URL rather than stopping here.
    if ./.venv/bin/python tools/get_parser.py >/dev/null 2>&1 && [ -x "$PARSER" ]; then
        ok "installed the current release"
    else
        warn "the GitHub API was unreachable; using the pinned release instead"
        URL="https://github.com/compomics/ThermoRawFileParser/releases/download/v.2.0.0-dev/ThermoRawFileParser-v.2.0.0-dev-linux.zip"
        TMP="$(mktemp -d)"
        trap 'rm -rf "$TMP"' EXIT
        curl -fsSL -o "$TMP/parser.zip" "$URL" \
            || die "Could not download the parser. Check this machine's internet access, or copy tools/ThermoRawFileParser/ from a machine that has it."
        mkdir -p tools/ThermoRawFileParser
        unzip -q -o "$TMP/parser.zip" -d tools/ThermoRawFileParser
        chmod +x "$PARSER"
        ok "installed the pinned release"
    fi
    "./$PARSER" --version >/dev/null 2>&1 \
        || die "The parser was downloaded but will not run. Check that libicu is installed."
fi

# ------------------------------------------------------------------ config.json
step "Writing config.json"

if [ -f config.json ]; then
    CURRENT="$(./.venv/bin/python -c 'import json;print(json.load(open("config.json")).get("ladder_dir",""))' 2>/dev/null || true)"
    if [ "$CURRENT" = "$DATA_DIR/ladders" ]; then
        ok "already points at $DATA_DIR/ladders"
    else
        cp config.json "config.json.backup-$(date +%Y%m%d-%H%M%S)"
        warn "kept your old config.json as a .backup- file"
        CURRENT=""
    fi
else
    CURRENT=""
fi

if [ -z "${CURRENT:-}" ]; then
    LADDER_DIR="$DATA_DIR/ladders" ./.venv/bin/python - <<'EOF'
import json
import os
from pathlib import Path

config = json.loads(Path("config.example.json").read_text())
config["ladder_dir"] = os.environ["LADDER_DIR"]
Path("config.json").write_text(json.dumps(config, indent=2) + "\n")
EOF
    ok "config.json -> $DATA_DIR/ladders"
fi

# ----------------------------------------------------------------- verification
step "Checking it all works"

./.venv/bin/python - <<'EOF'
import sys

sys.path.insert(0, ".")
from ltqindex.rawconv import parser_status
from ltqindex.calibration import load_config
from ltqindex.standards import folders

status = parser_status()
if not status["available"]:
    sys.exit("the .raw reader is not working: " + status["detail"])
print(f"    \033[0;32m✓\033[0m .raw reader {status['detail'].split(' (')[0]}")

config = load_config("config.json")
paths = folders(config["ladder_dir"], create=True)
print(f"    \033[0;32m✓\033[0m ladders      {config['ladder_dir']}")
print(f"    \033[0;32m✓\033[0m standards    {paths['standard_dir']}")
print(f"    \033[0;32m✓\033[0m results      {paths['compound_dir']}")
EOF

RAW_COUNT="$(find "$DATA_DIR/ladders" -maxdepth 1 -iname '*.raw' | wc -l)"
printf '\n%sSetup finished.%s\n\n' "$BOLD" "$OFF"
if [ "$RAW_COUNT" -eq 0 ]; then
    printf '  Next: copy your ladder .raw files into\n      %s\n\n' "$DATA_DIR/ladders"
else
    printf '  Found %s ladder .raw file(s) already in place.\n\n' "$RAW_COUNT"
fi
printf '  Start both apps:\n      ./run.sh\n\n'
printf '  Or install them as services that start on boot:\n      ./install-services.sh\n\n'
