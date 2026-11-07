"""Chromatogram peak picking, ladder index calibration and spectrum averaging."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields as dataclass_fields
from typing import List, Optional

import numpy as np
from scipy.signal import find_peaks, peak_widths

from .msdata import Run, channel_key, describe_chain, parse_filter

DEFAULT_LADDER_MASSES = [365, 527, 689, 851, 1013, 1175, 1337, 1499]


@dataclass
class Params:
    rt_max: float = 70.0            # ignore every scan after this retention time; 0 = no cut
    mz_tolerance: float = 0.5       # +/- Da window for a ladder extracted-ion trace
    smooth_window: int = 3          # moving-average width in scans (1 = off)
    min_rel_height: float = 0.05    # peak height, as a fraction of the trace maximum
    min_rel_prominence: float = 0.05
    min_separation: int = 3         # minimum scans between two peaks
    window_rel_height: float = 0.9  # 0.9 = accumulate across almost the whole peak
    spectrum_mz_tolerance: float = 0.3   # merge peaks this close when averaging scans
    spectrum_label_count: int = 12
    spectrum_label_min_rel: float = 0.02

    @classmethod
    def from_dict(cls, d):
        """Build settings from form or JSON values, keeping defaults for anything
        missing or unparseable. Types come from the defaults, not the annotations,
        which `from __future__ import annotations` turns into strings."""
        d = d or {}
        defaults = cls()
        kwargs = {}
        for field in dataclass_fields(cls):
            value = d.get(field.name)
            if value in (None, ""):
                continue
            caster = type(getattr(defaults, field.name))
            try:
                kwargs[field.name] = int(float(value)) if caster is int else caster(value)
            except (TypeError, ValueError):
                continue
        p = cls(**kwargs)
        p.smooth_window = max(1, p.smooth_window)
        p.min_separation = max(1, p.min_separation)
        p.window_rel_height = min(0.95, max(0.05, p.window_rel_height))
        return p

    to_dict = asdict


@dataclass
class Peak:
    rt: float
    height: float
    area: float
    rt_start: float
    rt_end: float
    scan_index: int
    index: Optional[float] = None
    index_extrapolated: bool = False
    rt_standard: Optional[float] = None
    rt_standard_start: Optional[float] = None
    rt_standard_end: Optional[float] = None
    rt_extrapolated: bool = False

    to_dict = asdict


def smooth(y, window):
    if window <= 1 or y.size < window:
        return y.astype(float)
    kernel = np.ones(window) / window
    return np.convolve(y.astype(float), kernel, mode="same")


def pick_peaks(rt, y, params: Params, n_peaks=2) -> List[Peak]:
    """Return the n tallest peaks of a trace, ordered by retention time."""
    y = np.asarray(y, float)
    if y.size == 0 or y.max() <= 0:
        return []
    ys = smooth(y, params.smooth_window)
    ceiling = ys.max()
    idx, props = find_peaks(
        ys,
        height=params.min_rel_height * ceiling,
        prominence=params.min_rel_prominence * ceiling,
        distance=params.min_separation,
    )
    if idx.size == 0:
        idx = np.array([int(np.argmax(ys))])
        props = {"prominences": np.array([ceiling])}

    order = np.argsort(props["peak_heights"] if "peak_heights" in props else ys[idx])[::-1]
    chosen = np.sort(idx[order[:n_peaks]])
    if chosen.size == 0:
        return []

    widths = peak_widths(ys, chosen, rel_height=params.window_rel_height)
    peaks = []
    for k, apex in enumerate(chosen):
        left = float(np.interp(widths[2][k], np.arange(rt.size), rt))
        right = float(np.interp(widths[3][k], np.arange(rt.size), rt))
        if right <= left:
            span = float(np.median(np.diff(rt))) if rt.size > 1 else 0.01
            left, right = rt[apex] - span, rt[apex] + span

        # smoothing can move the apex of a narrow peak by a scan or two, so the
        # reported time and height come from the raw trace near that position
        half = max(1, params.smooth_window)
        lo_i, hi_i = max(0, apex - half), min(y.size, apex + half + 1)
        i = int(lo_i + np.argmax(y[lo_i:hi_i]))
        left = min(left, float(rt[i]))
        right = max(right, float(rt[i]))
        mask = (rt >= left) & (rt <= right)
        area = float(np.trapezoid(y[mask], rt[mask])) if mask.sum() > 1 else 0.0
        peaks.append(
            Peak(
                rt=float(rt[i]),
                height=float(y[i]),
                area=area,
                rt_start=left,
                rt_end=right,
                scan_index=int(i),
            )
        )
    return peaks


# ------------------------------------------------------------------- ladder
class Ladder:
    """The retention-time to index calibration built from the ladder run.

    `limit` caps how far up the ladder the calibration reaches. Peaks above it are
    still found and drawn, they just take no part in interpolating compound
    indices, which matters when the top of the ladder is weak or poorly resolved.
    """

    def __init__(self, traces, params: Params, limit=None):
        self.traces = traces          # [{mass, rt, intensity, peaks:[Peak, Peak]}]
        self.params = params
        rts, indices = [], []
        n = 1
        for tr in traces:
            for pk in tr["peaks"]:
                pk.index = float(n)
                rts.append(pk.rt)
                indices.append(float(n))
                n += 1
        self.rt_points = np.asarray(rts, float)
        self.index_points = np.asarray(indices, float)
        self.limit = self.set_limit(limit)

    def set_limit(self, limit):
        if limit in (None, "", "all"):
            self.limit = self.n_points
        else:
            self.limit = max(2, min(int(limit), self.n_points))
        return self.limit

    @property
    def calibration(self):
        """The (retention times, indices) actually used for interpolation."""
        return self.rt_points[: self.limit], self.index_points[: self.limit]

    @property
    def n_points(self):
        return self.rt_points.size

    @property
    def is_monotonic(self):
        rts = self.rt_points[: self.limit]
        return bool(rts.size > 1 and np.all(np.diff(rts) > 0))

    def warnings(self):
        out = []
        expected = 2 * len(self.traces)
        if self.n_points < expected:
            out.append(
                f"Only {self.n_points} of {expected} ladder peaks were found. "
                "Lower the minimum peak height, or check the ladder file."
            )
        if not self.is_monotonic:
            rts = self.rt_points[: self.limit]
            bad = [
                f"index {int(self.index_points[i])} at {rts[i]:.2f} min"
                for i in range(1, rts.size)
                if rts[i] <= rts[i - 1]
            ]
            out.append(
                "Ladder retention times do not increase with index: "
                + ", ".join(bad)
                + ". Indices assigned from this ladder will be unreliable."
            )
        return out

    def to_index(self, rt):
        """Linear interpolation between bracketing ladder peaks.

        Outside the calibrated range the two nearest points are extrapolated, and
        the result is flagged so the figures can mark it.
        """
        rts, idx = self.calibration
        if rts.size < 2:
            return None, False
        if rts[0] <= rt <= rts[-1]:
            return float(np.interp(rt, rts, idx)), False
        if rt < rts[0]:
            slope = (idx[1] - idx[0]) / (rts[1] - rts[0])
            return float(idx[0] + slope * (rt - rts[0])), True
        slope = (idx[-1] - idx[-2]) / (rts[-1] - rts[-2])
        return float(idx[-1] + slope * (rt - rts[-1])), True

    def table(self):
        rows = []
        for tr in self.traces:
            for pk in tr["peaks"]:
                rows.append(
                    {
                        "mass": tr["mass"],
                        "index": int(pk.index),
                        "rt": round(pk.rt, 4),
                        "height": pk.height,
                        "area": pk.area,
                        "rt_start": round(pk.rt_start, 4),
                        "rt_end": round(pk.rt_end, 4),
                        "calibrating": int(pk.index) <= self.limit,
                    }
                )
        return sorted(rows, key=lambda r: r["index"])


def analyse_ladder(run: Run, masses, params: Params, limit=None) -> Ladder:
    ms1 = run.level(1)
    if not len(ms1):
        raise ValueError(
            f"{run.source_name} contains no MS1 scans, so ladder traces cannot be extracted."
        )
    n = ms1.scans_until(params.rt_max)
    if n < 3:
        raise ValueError(
            f"{run.source_name} has {n} MS1 scan(s) at or before "
            f"{params.rt_max:g} min. Raise the retention-time limit."
        )
    rt = ms1.rt[:n]
    traces = []
    for mass in masses:
        y = ms1.xic(float(mass), params.mz_tolerance)[:n]
        traces.append(
            {
                "mass": float(mass),
                "rt": rt,
                "intensity": y,
                "smoothed": smooth(y, params.smooth_window),
                "peaks": pick_peaks(rt, y, params, n_peaks=2),
            }
        )
    return Ladder(traces, params, limit=limit)


# ----------------------------------------------------------------- compound
class Compound:
    def __init__(self, run: Run, name, nominal_mass, params: Params, ladder: Ladder):
        self.run = run
        self.name = name
        self.nominal_mass = nominal_mass
        self.params = params

        ms2 = run.level(2)
        if not len(ms2):
            raise ValueError(
                f"{run.source_name} contains no MS2 scans, so no MS2 chromatogram can be built."
            )
        n = ms2.scans_until(params.rt_max)
        if n < 3:
            raise ValueError(
                f"{run.source_name} has {n} MS2 scan(s) at or before "
                f"{params.rt_max:g} min. Raise the retention-time limit."
            )
        self.rt = ms2.rt[:n]
        self.tic = ms2.tic[:n]
        self.smoothed = smooth(self.tic, params.smooth_window)
        self.peaks = pick_peaks(self.rt, self.tic, params, n_peaks=2)
        for pk in self.peaks:
            pk.index, pk.index_extrapolated = ladder.to_index(pk.rt)

        chain = parse_filter(ms2.filters[0]) if ms2.filters else []
        self.precursor_mz = chain[0][0] if chain else None
        self.activation = f"{chain[0][1].upper()} {chain[0][2]:g}" if chain else ""
        self.ms2_filter = ms2.filters[0] if ms2.filters else ""

        # set by set_transform when a standard ladder is available
        self.transform = None
        self.rt_standard = None
        self.transform_warning = None

    # ----------------------------------------------------- standard time axis
    def set_transform(self, transform):
        """Put this compound on the standard ladder's time axis.

        Only the reported times move. Scan selection for the mass spectra still
        uses the instrument's own retention times, so which scans go into a
        spectrum is unchanged.
        """
        self.transform = transform
        self.transform_warning = None
        if transform is None:
            self.rt_standard = None
            for pk in self.peaks:
                pk.rt_standard = None
                pk.rt_standard_start = pk.rt_standard_end = None
            return

        check = transform.check_monotonic(float(self.rt[0]), float(self.rt[-1]))
        if not check["monotonic"]:
            self.transform_warning = (
                f"{self.name}: the {transform.model} transform stops increasing at "
                f"{check['turns_at']:g} min, inside this run's retention-time range. "
                "Times past that point would be out of order. Use a lower-degree model."
            )
        self.rt_standard = transform(self.rt)
        for pk in self.peaks:
            pk.rt_standard = float(transform(pk.rt))
            pk.rt_standard_start = float(transform(pk.rt_start))
            pk.rt_standard_end = float(transform(pk.rt_end))
            pk.rt_extrapolated = not transform.is_inside(pk.rt)

    @property
    def on_standard_axis(self):
        return self.transform is not None

    @property
    def axis_rt(self):
        """Retention times to plot against: standard axis when one is available."""
        return self.rt_standard if self.on_standard_axis else self.rt

    def axis_peak(self, pk):
        """(apex, window start, window end) on whichever axis is in use."""
        if self.on_standard_axis:
            return pk.rt_standard, pk.rt_standard_start, pk.rt_standard_end
        return pk.rt, pk.rt_start, pk.rt_end

    def warnings(self):
        out = []
        if self.transform_warning:
            out.append(self.transform_warning)
        for pk in self.peaks:
            if self.on_standard_axis and getattr(pk, "rt_extrapolated", False):
                out.append(
                    f"{self.name}: the peak at {pk.rt:.2f} min sits outside the "
                    f"{self.transform.valid_range[0]:.2f}-{self.transform.valid_range[1]:.2f} min "
                    "range the transform was fitted over, so its standard-axis time is "
                    "extrapolated."
                )
        if len(self.peaks) < 2:
            out.append(
                f"{self.name}: only {len(self.peaks)} peak(s) found in the MS2 chromatogram. "
                "Lower the minimum peak height or check the file."
            )
        if self.precursor_mz is not None and self.nominal_mass:
            if abs(self.precursor_mz - float(self.nominal_mass)) > 1.5:
                out.append(
                    f"{self.name}: the nominal mass you entered ({self.nominal_mass:g}) does not "
                    f"match the MS2 precursor in the file ({self.precursor_mz:g})."
                )
        for pk in self.peaks:
            if pk.index_extrapolated and not self.on_standard_axis:
                out.append(
                    f"{self.name}: the peak at {pk.rt:.2f} min falls outside the ladder "
                    "retention-time range, so its index is extrapolated."
                )
        return out

    def peak_label(self, pk: Peak):
        """What goes above a peak: standard-axis time, or the ladder index when
        no standard has been saved yet."""
        if self.on_standard_axis:
            flag = "*" if getattr(pk, "rt_extrapolated", False) else ""
            return f"{pk.rt_standard:.2f} min{flag}"
        if pk.index is None:
            return "index n/a"
        return f"index {pk.index:.2f}" + ("*" if pk.index_extrapolated else "")

    def ms1_xic(self):
        """MS1 trace at the nominal mass, so the precursor can be checked later."""
        ms1 = self.run.level(1)
        if not len(ms1) or not self.nominal_mass:
            return None
        n = ms1.scans_until(self.params.rt_max)
        return ms1.rt[:n], ms1.xic(float(self.nominal_mass), self.params.mz_tolerance)[:n]

    def window_of(self, rt):
        """Which chromatogram peak window a retention time falls in, 0 if none."""
        for n, pk in enumerate(self.peaks, start=1):
            if pk.rt_start <= rt <= pk.rt_end:
                return n
        return 0

    def _channels(self):
        for level in self.run.available_levels:
            lv = self.run.level(level)
            for key, scan_idx in lv.channels().items():
                yield level, lv, key, scan_idx

    def _spectrum(self, level, lv, key, scan_idx, windows, indices, scope):
        rt = lv.rt[scan_idx]
        inside = np.zeros(rt.size, bool)
        for start, end in windows:
            inside |= (rt >= start) & (rt <= end)
        if self.params.rt_max and self.params.rt_max > 0:
            inside &= rt <= self.params.rt_max
        sel = scan_idx[inside]
        if sel.size == 0:
            return None
        mz, inten = sum_scans(lv, sel, self.params.spectrum_mz_tolerance)
        if mz.size == 0:
            return None
        return {
            "compound": self.name,
            "level": level,
            "filter": key,
            "chain": describe_chain(key),
            "chain_mz": [c[0] for c in parse_filter(key)],
            "n_scans": int(sel.size),
            "windows": windows,
            "indices": indices,
            "scope": scope,
            "mz": mz,
            "intensity": inten,
        }

    def spectra(self):
        """One spectrum per MSn channel, summed over both chromatogram peaks.

        Both peaks are the same compound eluting as two anomers, so their scans are
        pooled rather than plotted separately: every scan falling inside either peak
        window contributes, and the intensities are summed.
        """
        if not self.peaks:
            return []
        windows = [(pk.rt_start, pk.rt_end) for pk in self.peaks]
        indices = [pk.index for pk in self.peaks]
        out = []
        for level, lv, key, scan_idx in self._channels():
            spec = self._spectrum(level, lv, key, scan_idx, windows, indices, "both")
            if spec:
                out.append(spec)
        return out

    def peak_spectra(self):
        """The same channels, but one spectrum per chromatogram peak.

        These are not plotted; they exist so the two anomers can be compared
        against each other, and against the same compound in another run.
        """
        out = []
        for n, pk in enumerate(self.peaks, start=1):
            for level, lv, key, scan_idx in self._channels():
                spec = self._spectrum(
                    level, lv, key, scan_idx,
                    [(pk.rt_start, pk.rt_end)], [pk.index], f"peak{n}",
                )
                if spec:
                    spec["peak_no"] = n
                    out.append(spec)
        return out

    def peak_ratios(self):
        """Relative size of the two chromatogram peaks, for run-to-run matching."""
        if len(self.peaks) != 2:
            return None
        first, second = self.peaks
        total_area = first.area + second.area
        total_height = first.height + second.height
        return {
            "peak1_rt": round(first.rt, 4),
            "peak2_rt": round(second.rt, 4),
            "delta_rt": round(second.rt - first.rt, 4),
            "peak1_index": round(first.index, 4) if first.index is not None else None,
            "peak2_index": round(second.index, 4) if second.index is not None else None,
            "delta_index": (
                round(second.index - first.index, 4)
                if None not in (first.index, second.index) else None
            ),
            "peak1_height": first.height,
            "peak2_height": second.height,
            "peak1_area": first.area,
            "peak2_area": second.area,
            "height_ratio_1_over_2": (first.height / second.height) if second.height else None,
            "area_ratio_1_over_2": (first.area / second.area) if second.area else None,
            "peak1_area_fraction": (first.area / total_area) if total_area else None,
            "peak2_area_fraction": (second.area / total_area) if total_area else None,
            "peak1_height_fraction": (first.height / total_height) if total_height else None,
            "peak2_height_fraction": (second.height / total_height) if total_height else None,
        }


def sum_scans(level, scan_indices, tolerance):
    """Pool centroid peaks from several scans into one summed spectrum.

    Peaks within `tolerance` of each other collapse to their intensity-weighted
    centre, and their intensities add up.
    """
    mzs, ints = [], []
    for i in scan_indices:
        mz, inten = level.peaks(int(i))
        if mz.size:
            mzs.append(mz.astype(np.float64))
            ints.append(inten.astype(np.float64))
    if not mzs:
        return np.zeros(0), np.zeros(0)

    mz = np.concatenate(mzs)
    inten = np.concatenate(ints)
    order = np.argsort(mz)
    mz, inten = mz[order], inten[order]

    # start a new cluster on a large gap, or once a cluster gets too wide
    starts = np.zeros(mz.size, bool)
    starts[0] = True
    anchor = mz[0]
    for i in range(1, mz.size):
        if mz[i] - mz[i - 1] > tolerance or mz[i] - anchor > 2 * tolerance:
            starts[i] = True
            anchor = mz[i]
    groups = np.cumsum(starts) - 1
    n_groups = groups[-1] + 1

    weight = np.bincount(groups, weights=inten, minlength=n_groups)
    centre = np.bincount(groups, weights=inten * mz, minlength=n_groups)
    out_mz = centre / np.where(weight > 0, weight, 1.0)
    empty = weight <= 0
    if empty.any():  # all-zero-intensity clusters keep their plain mean
        counts = np.maximum(np.bincount(groups, minlength=n_groups)[empty], 1)
        out_mz[empty] = np.bincount(groups, weights=mz, minlength=n_groups)[empty] / counts
    return out_mz, weight


def label_spectrum_peaks(mz, intensity, params: Params, min_gap=None):
    """Choose which spectrum peaks get an m/z label.

    The minimum gap defaults to a fraction of the plotted mass range rather than a
    fixed number of daltons, because an MS1 survey scan spans 250-2000 and would
    otherwise stack isotope labels on top of each other.
    """
    if mz.size == 0 or intensity.max() <= 0:
        return []
    if min_gap is None:
        span = float(mz.max() - mz.min()) or 1.0
        min_gap = max(1.0, 0.006 * span)
    threshold = params.spectrum_label_min_rel * intensity.max()
    order = np.argsort(intensity)[::-1]
    picked = []
    for i in order:
        if intensity[i] < threshold or len(picked) >= params.spectrum_label_count:
            break
        if all(abs(mz[i] - mz[j]) >= min_gap for j in picked):
            picked.append(i)
    return sorted(picked, key=lambda i: mz[i])
