"""Retention-time calibration between ladder runs.

One ladder is nominated as the standard. Every other ladder is fitted to it with
y = ax^2 + bx + c, where x is a retention time in the run being calibrated and y is
the retention time of the same ladder index in the standard.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from .analysis import DEFAULT_LADDER_MASSES, Params

CONFIG_DEFAULTS = {
    "ladder_dir": "",
    "ladder_masses": list(DEFAULT_LADDER_MASSES),
    "params": {},
}
RAW_SUFFIXES = (".raw", ".RAW")


class ConfigError(RuntimeError):
    pass


def load_config(path):
    """Read config.json. Only `ladder_dir` is required."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"No config file at {path}. Copy config.example.json to config.json and "
            "set ladder_dir to the folder holding your ladder .raw files."
        )
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

    config = dict(CONFIG_DEFAULTS)
    config.update(raw)
    if not config.get("ladder_dir"):
        raise ConfigError(f'{path} has no "ladder_dir" field.')

    directory = Path(str(config["ladder_dir"])).expanduser()
    if not directory.is_dir():
        raise ConfigError(f'ladder_dir "{directory}" is not a folder.')
    config["ladder_dir"] = directory

    masses = [float(m) for m in config.get("ladder_masses") or DEFAULT_LADDER_MASSES]
    if len(masses) < 2:
        raise ConfigError("ladder_masses needs at least two masses.")
    config["ladder_masses"] = masses
    config["params"] = Params.from_dict(config.get("params") or {})
    config["config_path"] = path
    return config


def find_ladders(directory):
    """Every .raw file in the folder, sorted by name."""
    directory = Path(directory)
    files = [p for p in directory.iterdir() if p.is_file() and p.suffix in RAW_SUFFIXES]
    return sorted(files, key=lambda p: p.name.lower())

MODELS = [
    ("linear", 1, "ax + b"),
    ("quadratic", 2, "ax\u00b2 + bx + c"),
    ("cubic", 3, "ax\u00b3 + bx\u00b2 + cx + d"),
]
COEFFICIENT_NAMES = "abcdefg"

# Choosing between nested models: a higher degree is only preferred if it cuts the
# standard error by more than SELECTION_MARGIN, and only while the simpler model is
# still worse than SELECTION_FLOOR. The floor is in minutes and sits well below any
# real chromatographic reproducibility, so once a fit is that good the extra term is
# fitting rounding noise rather than chemistry.
SELECTION_MARGIN = 0.05     # 5% improvement in standard error
SELECTION_FLOOR = 0.002     # minutes, about a tenth of a second


def choose_model(models, metric="std_error"):
    """The simplest model that nothing meaningfully better replaces.

    `models` must be ordered simplest first. Models without the metric (an exact
    fit has no degrees of freedom left) cannot be chosen.
    """
    usable = [m for m in models if m.get(metric) is not None]
    if not usable:
        return models[0]["key"] if models else None
    best = usable[0]
    for candidate in usable[1:]:
        if best[metric] <= SELECTION_FLOOR:
            break
        if candidate[metric] < best[metric] * (1 - SELECTION_MARGIN):
            best = candidate
    return best["key"]


def coefficient_names(degree):
    """Highest power first, so a is always the leading coefficient."""
    return list(COEFFICIENT_NAMES[: degree + 1])


def fit_polynomial(x, y, degree):
    """Least-squares polynomial fit, returned with coefficients highest power first.

    numpy.polynomial.Polynomial.fit works in a mapped domain, so the normal
    equations stay well conditioned even though retention times reach 50 min and a
    cubic term therefore reaches six figures. convert() brings the coefficients
    back to plain powers of x.
    """
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    if x.size < degree + 1:
        raise ValueError(f"A degree-{degree} fit needs at least {degree + 1} indexes.")
    if np.ptp(x) <= 0:
        raise ValueError("All selected indexes have the same retention time.")

    series = np.polynomial.Polynomial.fit(x, y, degree).convert()
    ascending = list(series.coef) + [0.0] * (degree + 1 - len(series.coef))
    descending = [float(v) for v in ascending[: degree + 1]][::-1]

    fitted = np.polyval(descending, x)
    residual = y - fitted
    ss_res = float((residual ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    n = int(x.size)
    n_params = degree + 1
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else 1.0

    # These three models are nested, so RMSE and R2 can only improve as the degree
    # rises whether or not the extra term is real. std_error divides by the degrees
    # of freedom left over instead of by n, so a term has to earn its place.
    std_error = math.sqrt(ss_res / (n - n_params)) if n > n_params else None
    adjusted_r2 = (1.0 - (1.0 - r2) * (n - 1) / (n - n_params)
                   if n > n_params and ss_tot > 0 else None)

    return {
        "coefficients": descending,
        "fitted": fitted,
        "residual": residual,
        "n_points": n,
        "n_params": n_params,
        "rmse": math.sqrt(ss_res / n),
        "std_error": std_error,
        "max_abs_residual": float(np.abs(residual).max()),
        "mean_abs_residual": float(np.abs(residual).mean()),
        "r2": r2,
        "adjusted_r2": adjusted_r2,
        # with exactly degree+1 points a polynomial passes through every one of
        # them, so a zero residual says nothing about how good the fit is
        "exact": bool(n == n_params),
    }


def _equation(coefficients):
    """'y = 1.02x + 0.41' style, for display."""
    degree = len(coefficients) - 1
    parts = []
    for i, value in enumerate(coefficients):
        power = degree - i
        size = abs(value)
        number = f"{size:.6g}" if size >= 1e-4 or size == 0 else f"{size:.4e}"
        term = number + ("x" if power == 1 else f"x^{power}" if power > 1 else "")
        if i == 0:
            parts.append(("-" if value < 0 else "") + term)
        else:
            parts.append((" - " if value < 0 else " + ") + term)
    return "y = " + "".join(parts)


def calibrate(standard_rt, other_rt, indexes=None, curve_points=140):
    """Fit one ladder to the standard with a linear, quadratic and cubic model.

    `standard_rt` and `other_rt` map ladder index -> retention time. Only indexes
    present in both, and in `indexes` when given, take part.
    """
    shared = sorted(set(standard_rt) & set(other_rt))
    selected = set(indexes) if indexes is not None else set(shared)
    chosen = [i for i in shared if i in selected]

    result = {
        "available_indexes": shared,
        "used_indexes": chosen,
        "n_used": len(chosen),
        "points": [
            {"index": i, "x": round(other_rt[i], 5), "y": round(standard_rt[i], 5),
             "used": i in selected}
            for i in shared
        ],
        "models": [],
        "best": None,
        "uncalibrated": None,
        "error": None,
    }
    if len(chosen) < 2:
        result["error"] = (
            f"{len(chosen)} index(es) selected. Even a straight line needs two."
        )
        return result

    x = np.array([other_rt[i] for i in chosen], float)
    y = np.array([standard_rt[i] for i in chosen], float)
    raw_residual = y - x
    result["uncalibrated"] = {
        "rmse": float(np.sqrt((raw_residual ** 2).mean())),
        "max_abs_residual": float(np.abs(raw_residual).max()),
    }

    lo, hi = float(x.min()), float(x.max())
    pad = 0.04 * (hi - lo or 1.0)
    grid = np.linspace(lo - pad, hi + pad, curve_points)

    skipped = []
    for key, degree, label in MODELS:
        if len(chosen) < degree + 1:
            skipped.append(f"{key} needs {degree + 1}")
            continue
        try:
            fit = fit_polynomial(x, y, degree)
        except ValueError as exc:
            skipped.append(f"{key}: {exc}")
            continue
        curve = np.polyval(fit["coefficients"], grid)
        result["models"].append({
            "key": key,
            "degree": degree,
            "label": label,
            "names": coefficient_names(degree),
            "coefficients": [round(v, 12) for v in fit["coefficients"]],
            "equation": _equation(fit["coefficients"]),
            "n_params": fit["n_params"],
            "rmse": fit["rmse"],
            "std_error": fit["std_error"],
            "max_abs_residual": fit["max_abs_residual"],
            "mean_abs_residual": fit["mean_abs_residual"],
            "r2": fit["r2"],
            "adjusted_r2": fit["adjusted_r2"],
            "exact": fit["exact"],
            "residuals": {str(i): round(float(r), 5)
                          for i, r in zip(chosen, fit["residual"])},
            "curve": [[round(float(gx), 5), round(float(gy), 5)]
                      for gx, gy in zip(grid, curve)],
        })

    if not result["models"]:
        result["error"] = "; ".join(skipped) or "nothing could be fitted."
        return result
    if skipped:
        result["error"] = "Not enough indexes for: " + ", ".join(skipped) + "."

    result["best"] = choose_model(result["models"])
    judged = [m for m in result["models"] if m["std_error"] is not None]
    result["best_by_rmse"] = (min(judged, key=lambda m: m["rmse"])["key"] if judged
                              else result["best"])
    return result


def summarise(fits):
    """Per-model averages and error spread across every fitted ladder."""
    good = [f for f in fits if f.get("models")]
    out = {
        "n_files": len(good),
        "n_skipped": len(fits) - len(good),
        "models": {},
        "best_counts": {key: 0 for key, _, _ in MODELS},
        "overall_best": None,
    }
    if not good:
        return out

    out["mean_uncalibrated_rmse"] = float(np.mean(
        [f["uncalibrated"]["rmse"] for f in good if f.get("uncalibrated")]
    ))
    for f in good:
        if f.get("best"):
            out["best_counts"][f["best"]] = out["best_counts"].get(f["best"], 0) + 1

    for key, degree, label in MODELS:
        rows = [m for f in good for m in f["models"] if m["key"] == key]
        if not rows:
            continue
        names = coefficient_names(degree)
        coefficients = np.array([m["coefficients"] for m in rows], float)
        rmse = [m["rmse"] for m in rows]
        std_errors = [m["std_error"] for m in rows if m["std_error"] is not None]
        out["models"][key] = {
            "degree": degree,
            "label": label,
            "names": names,
            "n_files": len(rows),
            "mean_coefficients": dict(zip(names, [float(v) for v in coefficients.mean(axis=0)])),
            "sd_coefficients": dict(zip(
                names,
                [float(v) for v in (coefficients.std(axis=0, ddof=1)
                                    if len(rows) > 1 else np.zeros(degree + 1))],
            )),
            "mean_rmse": float(np.mean(rmse)),
            "worst_rmse": float(np.max(rmse)),
            "mean_std_error": float(np.mean(std_errors)) if std_errors else None,
            "worst_residual": float(np.max([m["max_abs_residual"] for m in rows])),
            "mean_r2": float(np.mean([m["r2"] for m in rows])),
            "n_exact": sum(1 for m in rows if m["exact"]),
        }

    # same parsimony rule, applied to the averages
    ordered = [dict(out["models"][key], key=key) for key, _, _ in MODELS
               if key in out["models"]]
    out["overall_best"] = choose_model(ordered, metric="mean_std_error")
    out["selection_rule"] = {
        "metric": "std_error",
        "margin": SELECTION_MARGIN,
        "floor_min": SELECTION_FLOOR,
    }
    return out
