"""Read an mzML file into a compact, cacheable in-memory run.

A Run holds one Level per MS level. Peak lists are stored as one flat float32
array per level plus an offset table, which keeps a 95 min LTQ run to a few tens
of megabytes and makes the on-disk cache a single .npz + .json pair.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

CACHE_VERSION = 3

# "ITMS + p ESI Full ms3 1419.50@cid40.00 1013.50@cid40.00 [275.00-1200.00]"
_ACTIVATION_RE = re.compile(r"(\d+\.?\d*)@([a-zA-Z]+)(\d+\.?\d*)")
_RANGE_RE = re.compile(r"\[[\d.\s\-]+\]\s*$")


def parse_filter(filter_string: str):
    """Return the activation chain as [(precursor_mz, 'cid', energy), ...]."""
    if not filter_string:
        return []
    return [
        (float(mz), kind.lower(), float(energy))
        for mz, kind, energy in _ACTIVATION_RE.findall(filter_string)
    ]


def channel_key(filter_string: str) -> str:
    """A filter string with the scan range removed, used to group MSn channels."""
    return _RANGE_RE.sub("", filter_string or "").strip()


def describe_chain(filter_string: str) -> str:
    """'1419.5 -> 1013.5' style CID sequence for figure annotation."""
    chain = parse_filter(filter_string)
    if not chain:
        return ""
    parts = [f"{chain[0][0]:g}"]
    for mz, kind, energy in chain[1:]:
        parts.append(f"{mz:g}")
    joined = " \u2192 ".join(parts)
    energies = ", ".join(f"{k.upper()} {e:g}" for _, k, e in chain)
    return f"{joined}   ({energies})"


@dataclass
class Level:
    """All scans of one MS level, in acquisition order."""

    rt: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float64))
    tic: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float64))
    scan: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int32))
    offsets: np.ndarray = field(default_factory=lambda: np.zeros(1, np.int64))
    mz: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    intensity: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    filters: list = field(default_factory=list)

    def __len__(self):
        return len(self.rt)

    def peaks(self, i):
        a, b = self.offsets[i], self.offsets[i + 1]
        return self.mz[a:b], self.intensity[a:b]

    def xic(self, target_mz, tolerance):
        """Summed intensity in [target-tol, target+tol] for every scan."""
        out = np.zeros(len(self), np.float64)
        lo, hi = target_mz - tolerance, target_mz + tolerance
        for i in range(len(self)):
            mz, inten = self.peaks(i)
            if mz.size == 0:
                continue
            a, b = np.searchsorted(mz, [lo, hi])
            if b > a:
                out[i] = inten[a:b].sum()
        return out

    def scans_until(self, rt_max):
        """How many leading scans fall at or before rt_max.

        Scans are stored in acquisition order, which is retention-time order, so
        the limit is a prefix slice. A limit of None or anything <= 0 means no cut.
        """
        if rt_max is None or rt_max <= 0 or not len(self):
            return len(self)
        return int(np.searchsorted(self.rt, float(rt_max), side="right"))

    def channels(self):
        """Map of channel key -> scan indices, ordered by first appearance."""
        groups = {}
        for i, f in enumerate(self.filters):
            groups.setdefault(channel_key(f), []).append(i)
        return {k: np.asarray(v, np.int64) for k, v in groups.items()}


class Run:
    def __init__(self, levels, source_name, meta=None):
        self.levels = levels
        self.source_name = source_name
        self.meta = meta or {}

    def level(self, n) -> Level:
        return self.levels.get(n, Level())

    @property
    def available_levels(self):
        return sorted(n for n, lv in self.levels.items() if len(lv))

    # ---------------------------------------------------------------- caching
    @staticmethod
    def _paths(stem: Path):
        # plain concatenation, so a file named "6E1.5.raw" keeps its full stem
        stem = Path(stem)
        return stem.parent / f"{stem.name}.npz", stem.parent / f"{stem.name}.json"

    def save(self, stem: Path):
        arrays = {}
        info = {
            "version": CACHE_VERSION,
            "source_name": self.source_name,
            "meta": self.meta,
            "levels": {},
        }
        for n, lv in self.levels.items():
            p = f"L{n}_"
            arrays[p + "rt"] = lv.rt
            arrays[p + "tic"] = lv.tic
            arrays[p + "scan"] = lv.scan
            arrays[p + "offsets"] = lv.offsets
            arrays[p + "mz"] = lv.mz
            arrays[p + "int"] = lv.intensity
            info["levels"][str(n)] = lv.filters
        npz, js = self._paths(stem)
        np.savez_compressed(npz, **arrays)
        js.write_text(json.dumps(info))

    @classmethod
    def load(cls, stem: Path):
        npz, js = cls._paths(stem)
        if not (js.exists() and npz.exists()):
            return None
        info = json.loads(js.read_text())
        if info.get("version") != CACHE_VERSION:
            return None
        data = np.load(npz)
        levels = {}
        for key, filters in info["levels"].items():
            n = int(key)
            p = f"L{n}_"
            levels[n] = Level(
                rt=data[p + "rt"],
                tic=data[p + "tic"],
                scan=data[p + "scan"],
                offsets=data[p + "offsets"],
                mz=data[p + "mz"],
                intensity=data[p + "int"],
                filters=list(filters),
            )
        return cls(levels, info["source_name"], info.get("meta"))


def read_mzml(path, source_name=None, progress=None, max_level=6) -> Run:
    from pyteomics import mzml

    path = Path(path)
    acc = {}
    seen = 0
    with mzml.MzML(str(path)) as reader:
        for spec in reader:
            level = int(spec.get("ms level", 1))
            if level > max_level:
                continue
            scan_meta = spec["scanList"]["scan"][0]
            store = acc.setdefault(
                level,
                {"rt": [], "tic": [], "scan": [], "mz": [], "int": [], "filters": [], "n": []},
            )
            store["rt"].append(float(scan_meta["scan start time"]))
            store["tic"].append(float(spec.get("total ion current", 0.0) or 0.0))
            store["scan"].append(_scan_number(spec.get("id", "")))
            store["filters"].append(str(scan_meta.get("filter string", "")))

            mz = np.asarray(spec.get("m/z array", []), np.float64)
            inten = np.asarray(spec.get("intensity array", []), np.float64)
            if mz.size and not np.all(np.diff(mz) >= 0):
                order = np.argsort(mz)
                mz, inten = mz[order], inten[order]
            store["mz"].append(mz.astype(np.float32))
            store["int"].append(inten.astype(np.float32))
            store["n"].append(mz.size)

            seen += 1
            if progress and seen % 500 == 0:
                progress(f"Read {seen} scans from {path.name}")

    levels = {}
    for level, store in acc.items():
        counts = np.asarray(store["n"], np.int64)
        offsets = np.zeros(len(counts) + 1, np.int64)
        np.cumsum(counts, out=offsets[1:])
        levels[level] = Level(
            rt=np.asarray(store["rt"], np.float64),
            tic=np.asarray(store["tic"], np.float64),
            scan=np.asarray(store["scan"], np.int32),
            offsets=offsets,
            mz=(np.concatenate(store["mz"]) if store["mz"] else np.zeros(0, np.float32)),
            intensity=(np.concatenate(store["int"]) if store["int"] else np.zeros(0, np.float32)),
            filters=store["filters"],
        )

    meta = {"n_scans": seen, "mzml": path.name}
    ms2 = levels.get(2)
    if ms2 is not None and len(ms2):
        chain = parse_filter(ms2.filters[0])
        if chain:
            meta["ms2_precursor"] = chain[0][0]
            meta["ms2_activation"] = f"{chain[0][1].upper()} {chain[0][2]:g}"
    return Run(levels, source_name or path.stem, meta)


def _scan_number(spec_id: str) -> int:
    m = re.search(r"scan=(\d+)", spec_id or "")
    return int(m.group(1)) if m else -1


def cache_key(raw_path: Path) -> str:
    """Identify a .raw by name, size and modification time.

    Two different files that happen to share a name get different keys, and the
    same file reused in a later session is recognised and not converted again.
    """
    stat = raw_path.stat()
    digest = hashlib.sha1(
        f"{raw_path.name}|{stat.st_size}|{int(stat.st_mtime)}".encode()
    ).hexdigest()[:10]
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in raw_path.stem)
    return f"{safe[:60]}-{digest}"


def load_run(raw_path, cache_dir, progress=None) -> Run:
    """Convert (if needed), parse (if needed) and return a Run for one .raw file."""
    from .rawconv import raw_to_mzml

    raw_path = Path(raw_path)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key(raw_path)
    stem = cache_dir / key

    cached = Run.load(stem)
    if cached is not None:
        if progress:
            progress(f"Reusing the scans already read from {raw_path.name}")
        return cached

    mzml_path = raw_to_mzml(raw_path, cache_dir, out_name=key, progress=progress)
    if progress:
        progress(f"Reading scans from {raw_path.name}")
    run = read_mzml(mzml_path, source_name=raw_path.stem, progress=progress)
    run.save(stem)
    return run
