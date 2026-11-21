"""Figure generation. The same functions draw the on-screen review images and
the exported files, so what you approve is what you download."""

from __future__ import annotations

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .analysis import Params, label_spectrum_peaks

INK = "#12242E"
MUTED = "#5C7079"
HAIRLINE = "#C9D4D8"
COMPOUND = "#1B5E6B"
MARK = "#B4441F"

# a teal-to-rust ramp; the ladder reads as a sequence rather than eight unrelated colours
LADDER_COLORS = [
    "#0B5D6E", "#0E7C86", "#2E9B8F", "#6BB187",
    "#A8C06B", "#D9BE55", "#E08A3C", "#C6532C",
]

TITLE_PAD = 30   # room for the title plus one line of subtitle
SUB_Y = 1.012

plt.rcParams.update(
    {
        "font.family": "sans-serif",
        "font.sans-serif": ["IBM Plex Sans", "DejaVu Sans", "Arial"],
        "font.size": 9,
        "axes.edgecolor": HAIRLINE,
        "axes.labelcolor": INK,
        "axes.titlecolor": INK,
        "axes.linewidth": 0.8,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "savefig.facecolor": "white",
        "legend.frameon": False,
        "figure.dpi": 110,
    }
)

_SUPER = str.maketrans(
    "0123456789-", "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207b"
)


def intensity_scale(top):
    """Fold the exponent into the axis label instead of a floating offset box."""
    if not top or top <= 0 or not math.isfinite(top):
        return 1.0, ""
    exponent = 3 * (int(math.floor(math.log10(top))) // 3)
    if exponent == 0:
        return 1.0, ""
    return 10.0 ** exponent, f" (\u00d710{str(exponent).translate(_SUPER)})"


def _header(ax, title, subtitle):
    ax.set_title(title, loc="left", fontsize=11, fontweight="600", pad=TITLE_PAD)
    if subtitle:
        ax.text(0.0, SUB_Y, subtitle, transform=ax.transAxes,
                fontsize=8, color=MUTED, va="bottom", ha="left")


def _finish(fig, ax, path, formats=("png",)):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    written = []
    if path is not None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        for ext in formats:
            # append rather than with_suffix: figure names carry m/z values such as
            # 1419.5, whose ".5" would otherwise be treated as a file suffix
            out = path.parent / f"{path.name}.{ext}"
            fig.savefig(out, dpi=200 if ext == "png" else None, bbox_inches="tight")
            written.append(out)
    return written


def plot_ladder(ladder, run_name, path=None, formats=("png",)):
    fig, ax = plt.subplots(figsize=(11, 5.4))
    top = max((float(tr["intensity"].max()) for tr in ladder.traces), default=1.0) or 1.0
    scale, unit = intensity_scale(top)
    span = float(ladder.traces[0]["rt"][-1] - ladder.traces[0]["rt"][0]) or 1.0

    for i, tr in enumerate(ladder.traces):
        color = LADDER_COLORS[i % len(LADDER_COLORS)]
        ax.plot(tr["rt"], tr["intensity"] / scale, lw=1.1, color=color,
                label=f"m/z {tr['mass']:g}", zorder=3)
        # lift the second label when two peaks of one mass nearly coincide
        offsets = [8] * len(tr["peaks"])
        for j in range(1, len(tr["peaks"])):
            if tr["peaks"][j].rt - tr["peaks"][j - 1].rt < 0.02 * span:
                offsets[j] = 21
        for pk, dy in zip(tr["peaks"], offsets):
            y = pk.height / scale
            calibrating = ladder.uses(pk.index)
            ax.annotate(f"{int(pk.index)}", xy=(pk.rt, y), xytext=(0, dy),
                        textcoords="offset points", ha="center", fontsize=8.5,
                        fontweight="600" if calibrating else "400",
                        color=color if calibrating else HAIRLINE, zorder=5)
            # a hand-placed peak is a diamond so it stands out from automatic picks
            ax.plot([pk.rt], [y], marker="D" if pk.manual else "v",
                    ms=4.5 if pk.manual else 3.5,
                    color=color if calibrating else HAIRLINE, zorder=4)

    subtitle = (
        f"extracted ion traces, \u00b1{ladder.params.mz_tolerance:g} Da   |   "
        f"numbers are ladder index 1\u2013{ladder.max_index}"
    )
    if ladder.params.rt_max and ladder.params.rt_max > 0:
        subtitle += f"   |   scans after {ladder.params.rt_max:g} min ignored"
    if any(pk.manual for tr in ladder.traces for pk in tr["peaks"]):
        subtitle += "   |   \u25c6 placed by hand"
    absent = getattr(ladder, "absent", [])
    if absent:
        subtitle += "   |   no peak for index " + ", ".join(str(i) for i in absent)
    excluded = sorted(getattr(ladder, "excluded", ()))
    if excluded:
        subtitle += ("   |   index " + ", ".join(str(i) for i in excluded)
                     + " not used, greyed out")
    if ladder.limit < ladder.max_index:
        subtitle += (
            f"   |   only index 1\u2013{ladder.limit} used for calibration, "
            "the rest are greyed out"
        )

    ax.set_xlabel("Retention time (min)")
    ax.set_ylabel("Intensity" + unit)
    ax.set_ylim(0, top / scale * 1.18)
    ax.set_xlim(ladder.traces[0]["rt"][0], ladder.traces[0]["rt"][-1])
    _header(ax, f"Ladder chromatogram \u2014 {run_name}", subtitle)
    ax.legend(ncol=8, loc="upper center", bbox_to_anchor=(0.5, -0.14), fontsize=8)
    written = _finish(fig, ax, path, formats)
    plt.close(fig)
    return written


def plot_compound(compound, path=None, formats=("png",)):
    fig, ax = plt.subplots(figsize=(11, 4.6))
    rt, tic = compound.axis_rt, compound.tic
    top = float(tic.max()) or 1.0
    scale, unit = intensity_scale(top)
    standard = compound.on_standard_axis

    ax.fill_between(rt, tic / scale, color=COMPOUND, alpha=0.10, zorder=2)
    ax.plot(rt, tic / scale, lw=1.1, color=COMPOUND, zorder=3)

    for pk in compound.peaks:
        apex, start, end = compound.axis_peak(pk)
        y = pk.height / scale
        ax.axvspan(start, end, color=MARK, alpha=0.14, zorder=1,
                   hatch="////" if pk.window_manual else None, ec=MARK if pk.window_manual else None,
                   lw=0)
        ax.plot([apex], [y], marker="D" if pk.manual else "v", ms=4.5 if pk.manual else 4,
                color=MARK, zorder=4)
        ax.annotate(compound.peak_label(pk),
                    xy=(apex, y), xytext=(0, 9), textcoords="offset points",
                    ha="center", fontsize=8.5, color=MARK, fontweight="600",
                    linespacing=1.35, zorder=5)

    precursor = f"{compound.precursor_mz:g}" if compound.precursor_mz else "unknown"
    subtitle = f"MS2 of precursor m/z {precursor}"
    if compound.activation:
        subtitle += f" ({compound.activation})"
    if compound.nominal_mass:
        subtitle += f"   |   nominal mass {compound.nominal_mass:g}"
    if standard:
        t = compound.transform
        subtitle += (f"   |   {t.model} transform of {compound.run.source_name}'s ladder "
                     f"onto {t.standard_name}, {len(t.indexes)} indexes")
        if any(getattr(p, "rt_extrapolated", False) for p in compound.peaks):
            subtitle += "   |   * outside the fitted range"
    else:
        subtitle += "   |   index interpolated from the ladder"
        if any(p.index_extrapolated for p in compound.peaks):
            subtitle += "   |   * extrapolated beyond the ladder"
    if compound.params.rt_max and compound.params.rt_max > 0:
        subtitle += f"   |   scans after {compound.params.rt_max:g} min ignored"
    notes = [f"windows {compound.params.window_fwhm:g} x FWHM"]
    if standard and compound.index_scale:
        notes.append("index from the standard ladder")
    if any(p.manual for p in compound.peaks):
        notes.append("◆ placed by hand")
    if any(p.window_manual for p in compound.peaks):
        notes.append("hatched window set by hand")
    if compound.absent:
        notes.append("no peak " + ", ".join(map(str, compound.absent)))
    # inside the axes, top right: the subtitle above has room for one line only
    ax.text(0.995, 0.97, "   |   ".join(notes), transform=ax.transAxes,
            fontsize=7.5, color=MUTED, ha="right", va="top")

    ax.set_xlabel(f"Retention time on the {compound.transform.standard_name} axis (min)"
                  if standard else "Retention time (min)")
    ax.set_ylabel("MS2 total ion current" + unit)
    ax.set_ylim(0, top / scale * 1.45)
    ax.set_xlim(rt[0], rt[-1])
    _header(ax, f"Chromatogram of {compound.name}", subtitle)
    written = _finish(fig, ax, path, formats)
    plt.close(fig)
    return written


def plot_spectrum(spec, params: Params, path=None, formats=("png",)):
    mz, inten = spec["mz"], spec["intensity"]
    fig, ax = plt.subplots(figsize=(11, 4.4))
    top = (float(inten.max()) if inten.size else 1.0) or 1.0
    scale, unit = intensity_scale(top)

    ax.vlines(mz, 0, inten / scale, lw=0.9, color=COMPOUND, zorder=3)
    picked = label_spectrum_peaks(mz, inten, params)
    span = float(mz.max() - mz.min()) if mz.size else 1.0
    tier, previous = 0, None
    for i in picked:
        # step labels upward when neighbours are close, so rotated text cannot overlap
        if previous is not None and mz[i] - previous < 0.028 * span:
            tier = (tier + 1) % 3
        else:
            tier = 0
        previous = mz[i]
        ax.annotate(f"{mz[i]:.1f}", xy=(mz[i], inten[i] / scale), xytext=(0, 4 + 26 * tier),
                    textcoords="offset points", ha="center", fontsize=7.5,
                    color=INK, rotation=90, zorder=4)

    level = spec["level"]
    windows = spec["windows"]
    indices = [i for i in spec["indices"] if i is not None]
    if level == 1:
        lead = "full scan"
    elif level == 2:
        chain = spec["chain_mz"]
        lead = f"precursor m/z {chain[0]:g}" if chain else "precursor unknown"
    else:
        lead = f"CID sequence  {spec['chain']}"

    ranges = " + ".join(f"{a:.2f}\u2013{b:.2f}" for a, b in windows)
    idx_text = " and ".join(f"{i:.2f}" for i in indices) or "n/a"

    ax.set_xlabel("m/z")
    ax.set_ylabel("Summed intensity" + unit)
    ax.set_ylim(0, top / scale * 1.5)
    _header(
        ax,
        f"MS{level} of {spec['compound']}",
        f"{lead}   |   both chromatogram peaks summed, {ranges} min "
        f"({spec['n_scans']} scans)   |   index {idx_text}",
    )
    written = _finish(fig, ax, path, formats)
    plt.close(fig)
    return written


def plot_fit(fit, model_key, run_name, standard_name, path=None, formats=("png",)):
    """This run's ladder retention times (x) against the standard's (y), with the
    chosen model's curve through the indexes used and its residuals underneath.

    `fit` is the result of calibration.calibrate(): points carry index, x, y and
    whether they were used; models carry the curve and residuals.
    """
    import numpy as np

    model = next((m for m in fit.get("models", []) if m["key"] == model_key), None)
    fig, (ax, rax) = plt.subplots(
        2, 1, figsize=(8.6, 6.4), sharex=True,
        gridspec_kw={"height_ratios": [3, 1.15], "hspace": 0.08},
    )
    pts = fit.get("points", [])
    xs = [p["x"] for p in pts] or [0.0, 1.0]
    lo, hi = min(xs), max(xs)
    pad = 0.05 * (hi - lo or 1.0)

    ax.plot([lo - pad, hi + pad], [lo - pad, hi + pad], ls=(0, (4, 3)), lw=0.8,
            color=HAIRLINE, zorder=1, label="y = x (no shift)")
    if model:
        cx, cy = zip(*model["curve"])
        ax.plot(cx, cy, lw=1.6, color=COMPOUND, zorder=2, label=f"{model['label']} fit")
    for p in pts:
        used = p["used"]
        ax.plot([p["x"]], [p["y"]], marker="o", ms=5.5,
                color=MARK if used else "white", mec=MARK if used else MUTED,
                mew=1.0, zorder=3)
        ax.annotate(str(p["index"]), xy=(p["x"], p["y"]), xytext=(-6, 6),
                    textcoords="offset points", ha="right", fontsize=8,
                    color=INK if used else MUTED)
    ax.set_ylabel(f"Standard RT (min)")
    ax.legend(loc="upper left", fontsize=8)

    if model:
        res = {int(k): v for k, v in model["residuals"].items()}
        used = [p for p in pts if p["index"] in res]
        rax.axhline(0, color=HAIRLINE, lw=0.8)
        rax.vlines([p["x"] for p in used], 0, [res[p["index"]] for p in used],
                   color=MARK, lw=1.2)
        rax.plot([p["x"] for p in used], [res[p["index"]] for p in used], "o",
                 ms=4, color=MARK)
        span = max([abs(v) for v in res.values()] + [1e-3])
        rax.set_ylim(-span * 1.4, span * 1.4)
    rax.set_ylabel("Residual (min)")
    rax.set_xlabel(f"{run_name} RT (min)")
    rax.set_xlim(lo - pad, hi + pad)
    for a in (ax, rax):
        a.spines["top"].set_visible(False)
        a.spines["right"].set_visible(False)

    if model:
        subtitle = (f"{model['equation']}   |   RMSE {model['rmse']:.4f} min   |   "
                    f"{len(model['residuals'])} indexes used; hollow points left out")
    else:
        subtitle = fit.get("error") or "no fit"
    _header(ax, f"Ladder fit \u2014 {run_name} onto {standard_name}", subtitle)
    written = []
    if path is not None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        for ext in formats:
            out = path.parent / f"{path.name}.{ext}"
            fig.savefig(out, dpi=200 if ext == "png" else None, bbox_inches="tight")
            written.append(out)
    plt.close(fig)
    return written
