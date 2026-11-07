# Ladder Index

Two apps that share one Thermo `.raw` reader:

- **`app.py`** — process compounds against the ladder run alongside them, put their
  retention times on the standard's time axis, and plot their MS1/MS2/MS3/MS4 spectra.
- **`calibrate_app.py`** — compare ladder runs, and nominate one as the **standard**.

They meet through two folders created beside `ladder_dir`: `standard_ladder/` holds
the standard the calibration app saved, and `compound/` collects the normalized
results the main app produces.

The first is described below; the calibration app has its own section at the end.
Both cache converted scans in the same folder, so a ladder read by one is free for
the other.

Reads Thermo LTQ `.raw` files, plots ladder and compound chromatograms, converts
compound retention times onto the ladder's index scale, and — once you approve the
chromatograms — plots the averaged MS2/MS3/MS4 spectra and writes everything out.

Runs locally in your browser. Nothing leaves your machine.

## Install

On Ubuntu, one script does the whole thing — see **[DEPLOY.md](DEPLOY.md)** for a
server install with systemd services, firewall notes and troubleshooting:

```bash
sudo apt install -y python3-venv unzip curl libicu-dev
bash setup.sh
./run.sh
```

By hand, on any platform:

```bash
pip install -r requirements.txt
python tools/get_parser.py      # one-off, ~50 MB
python app.py                   # then open http://127.0.0.1:5000
```

`get_parser.py` downloads [ThermoRawFileParser](https://github.com/compomics/ThermoRawFileParser),
which does the actual `.raw` reading. On Windows, macOS and Linux it fetches a
self-contained build, so there is no .NET or mono install to worry about. If your
platform only has the portable build, you will need mono (`apt install mono-runtime`
or `brew install mono`). If the parser already lives somewhere else, point at it
with the `LTQ_RAW_PARSER` environment variable instead.

The status line at the top right of the page tells you whether the parser was found.

## Using it

The page walks through four steps and only moves forward when you say so.

**Step 1 — Input data.** One ladder run and at least one compound; both are
required. The ladder here is the one that was run alongside those compounds, not
the standard: the app fits this ladder onto the saved standard and applies that same
transform to the compounds. Pick the model — linear, quadratic or cubic — in the
"Standard time axis" panel. If no standard has been saved, the panel says so and the
app falls back to reporting the instrument's own times with ladder indexes. The ladder needs a `.raw` file and its mass list, which defaults to 365,
527, 689, 851, 1013, 1175, 1337, 1499. Each compound needs a name, a nominal mass
and a file. Up to five compounds.

Files can be uploaded through the browser, or given as a path already on this
machine. Paths are much faster for 250 MB `.raw` files, since nothing is copied.

**Step 2 — Chromatograms.** The ladder chromatogram shows one extracted ion trace
per mass, with the two tallest peaks of each numbered 1 upward by retention time.
Each compound gets an MS2 total-ion-current chromatogram showing its name, its
precursor, and both peaks labelled with the index interpolated from the ladder. An
index scale strip puts the ladder ticks and the compound peaks on one axis, and the
tables below give every number.

This step also controls **how far up the ladder the calibration reaches**. By
default all indexes are used. If the top of the ladder is weak or poorly resolved,
set "use ladder index up to" lower and replot: the excluded peaks are still drawn
and numbered, just greyed out and taken out of the interpolation. A compound that
now falls beyond the shortened ladder is extrapolated, marked with an asterisk in
the figure and flagged in the table and CSV.

Then answer the question. **Yes** builds the mass spectra. **No** returns you to
step 1 with a checklist of what to look at; replotting reuses the scans already
read, so it takes a couple of seconds rather than a couple of minutes.

**Step 3 — Mass spectra.** MS1, MS2 and every MS3/MS4 channel the instrument
recorded, one figure per channel per compound, grouped by compound.

**Step 4 — Download.** A manifest of what you are about to get, then a button to
build it.

Any step you have already completed can be revisited, either from its Back button
or by clicking the step in the rail at the top. Revisiting shows what is already
there and recomputes nothing. Replotting the chromatograms is the one thing that
invalidates the steps after it: the spectra and the results file are rebuilt the
next time you go forward, and until then those rail steps are locked.

## The standard time axis

The calibration app writes the chosen standard to `standard_ladder/standard.json`:
its index-to-retention-time table, the masses and settings it was read with, which
indexes were ticked as usable, and where it came from. Saving again archives the
previous one under `standard_ladder/archive/`.

The main app then reads that file, fits the ladder you supplied onto it using the
indexes both runs share, and applies the resulting polynomial to the compounds that
ladder accompanied. Compound chromatograms, peak tables and exported CSVs are all on
the standard's axis, labelled with the standard's name. The ladder index still
appears on the ladder chromatogram, because that is what matches peaks between the
two runs, but compound peaks are reported as transformed times rather than indexes.

Three things worth knowing about the transform:

- **Only reported times move.** Which scans go into a mass spectrum is still decided
  on the instrument's own retention times, so the spectra are untouched by the
  choice of model. Raw times are kept in `results.json` and in each compound's
  `result.json` so any figure can be traced back to the instrument file.
- **A cubic can turn over.** Fitted across 10–50 min it may stop increasing outside
  that range, which would put the chromatogram out of order. The app checks the
  curve across the whole span it is about to map and warns, naming the minute where
  it turns, rather than silently producing a folded axis.
- **The ladder range applies here too.** Trimming the ladder on the review page also
  removes those indexes from the transform fit, and an index unticked when the
  standard was saved is excluded from every fit against it.

## The compound folder

On export, each compound's normalized results are copied into
`compound/<name>/<run>_<timestamp>/`, so repeat runs of the same compound accumulate
side by side on one time axis. Each folder holds `result.json` (peaks on both axes,
the transform that produced them, and the settings used), `peaks.csv`, the
chromatogram figure, the chromatogram traces and the spectrum peak lists.

Nothing is published when no standard is available, since there would be no common
axis to publish onto.

## What comes out

```
chromatograms/  ladder and compound chromatograms, PNG and PDF
spectra/        MS1/MS2/MS3/MS4, one figure per channel per compound, PNG and PDF
data/           every processed number, as CSV
results.json    the same content in one machine-readable file
README.txt      what the index means, and any warnings raised
```

The `data/` folder is built for comparing runs later, so it holds the traces and
ratios rather than just the summary numbers:

| File | What's in it |
| --- | --- |
| `ladder_index.csv` | Index, mass, retention time, height, area, and whether the peak calibrated |
| `ladder_chromatograms.csv` | The full extracted ion traces: retention time, then one column per ladder mass |
| `compound_index.csv` | Both peaks per compound with index, height, area, and each peak's share of the total |
| `compound_peak_ratios.csv` | Peak 1 against peak 2 — height and area ratios, area and height fractions, and the gap between them in both minutes and index units |
| `chromatogram_<name>_ms2_tic.csv` | The whole MS2 TIC trace, with each scan flagged 0, 1 or 2 for which peak window it fell in |
| `chromatogram_<name>_ms1_xic.csv` | The MS1 trace at the nominal mass, flagged the same way |
| `spectra_summary.csv` | One row per spectrum: level, CID sequence, scan count, window, base peak, totals |
| `peaklist_*.csv` | m/z, intensity, and relative intensity as a percentage of the base peak |

Peak lists come in three scopes. `6E1_MS2_1419.5` pools both chromatogram peaks and
is what the figure shows; `..._peak1` and `..._peak2` split the same channel by
peak. The two halves sum exactly to the pooled spectrum, so you can compare the two
anomers against each other, or the same anomer against another run, without
reprocessing the `.raw`.

## How the numbers are worked out

**The retention-time cut.** Every scan later than `rt_max`, 70 min by default, is
dropped before anything else runs. Nothing past it reaches a trace, a peak, a window
or a spectrum, and the chromatogram axes end there, so the tail of a long run cannot
contribute a spurious peak. It is applied when the data is analysed rather than when
it is read, so changing it costs a replot and not a reconversion. Setting it to 0
keeps the whole run. Cutting it low enough to bite into real ladder peaks does not
fail silently: the ladder's retention times stop increasing with index and the page
says so.

**Ladder index.** For each ladder mass, intensity is summed over ±0.5 Da in every
MS1 scan up to the cut to give an extracted ion trace. The two tallest peaks are found on a
smoothed copy of that trace, then the apex is refined against the raw trace so
smoothing cannot shift a narrow peak. Peaks are sorted by retention time across all
masses and numbered from 1, giving 16 points for eight masses.

**Compound index.** A compound peak's index is `numpy.interp` of its retention time
against those ladder points, or against the first N of them if you shortened the
ladder in step 2. A peak outside that range is extrapolated from the two nearest
points and flagged with an asterisk in the figure and an `extrapolated` column in
the CSV.

**Spectra.** Each of the two chromatogram peaks defines a time window: by default
the width at 90% of the peak height, so nearly the whole peak is covered. Both
peaks are the same compound eluting as two anomers, so their scans are pooled — one
spectrum per channel, not one per peak. Every scan falling inside either window is
collected, grouped by scan filter so MS1, MS2 and each MS3/MS4 channel stay
separate, and summed: centroids within 0.3 Da merge into an intensity-weighted
centre and their intensities add. The CID sequence in each MS3/MS4 title comes from
the scan filter, so `1419.5 → 1013.5` is what the instrument actually did.

## Settings

| Setting | Default | What it changes |
| --- | --- | --- |
| Ignore after | 70 min | Scans later than this are dropped before anything else. 0 keeps the whole run |
| m/z tolerance | 0.5 Da | Half-width of the ladder extracted ion window |
| Smoothing | 3 scans | Moving average used for peak finding only, never for reported heights |
| Minimum height | 0.05 | Peak height floor, as a fraction of the trace maximum |
| Minimum prominence | 0.05 | How far a peak must rise above its surroundings |
| Peak separation | 3 scans | Closest two peaks may be |
| Peak window height | 0.9 | Where the accumulation window is cut. 0.9 covers nearly the whole peak; 0.5 would be full width at half maximum |
| Spectrum merge | 0.3 Da | How close centroids must be to merge when summing scans |
| Labels per spectrum | 12 | How many m/z values get labelled |

## Notes

Converted scans are cached under `workspace/scan-cache`, keyed by file name, size
and modification time. The first read of a `.raw` takes roughly 25 seconds; after
that the same file is recognised and reused, including in later sessions. Delete
that folder to reclaim the space.

MS4 is handled wherever the instrument recorded it; it simply does not appear in
runs that stop at MS3. MS1 is always plotted, which is a quick way to confirm the
precursor really is what you told the software it was.

The peak finder takes the two tallest peaks per trace, which suits the anomeric
pairs these runs are built around. A trace with a different number of real peaks
needs the thresholds adjusted, and the page warns you when a ladder mass yields
fewer than two peaks or when the ladder's retention times do not increase with index.


---

# Ladder Calibration (`calibrate_app.py`)

Fits the retention times of every ladder run in a folder onto one chosen standard,
so runs from different days can be compared on the same time axis.

```bash
cp config.example.json config.json     # then edit ladder_dir
python calibrate_app.py                # http://127.0.0.1:5001
```

## config.json

```json
{
  "ladder_dir": "/data/ladders",
  "ladder_masses": [365, 527, 689, 851, 1013, 1175, 1337, 1499],
  "params": { "rt_max": 70, "mz_tolerance": 0.5, "smooth_window": 3 }
}
```

Only `ladder_dir` is required; it is the folder holding the ladder `.raw` files, and
every `.raw` in it is loaded. The other two fields fall back to the same defaults
the main app uses, including the 70 min retention-time cut — add `"rt_max"` to
`params` to change it, or set it to 0 to keep the whole run. Point `--config` elsewhere if you keep several.

## Page 1 — ladders, indexes and standard

Every ladder in the folder is read and plotted the same way the main app plots one:
extracted ion traces for each mass, with the two tallest peaks of each numbered 1
upward by retention time. Pick one run as the standard with the radio button at the
top of its card.

Beside each chromatogram is a checkbox per index, listed with its retention time and
all ticked by default, plus All and None buttons. This is where you decide which
indexes are worth fitting with — with the trace right next to the list, so you can
see the peak you are judging.

The choice belongs to the ladder, not to a pairing, so an index you untick is left
out of every fit that ladder takes part in. That includes when it is the standard:
untick index 12 there and it disappears from all of them. Fewer than three ticked
and the card header turns amber, because a quadratic cannot be fitted through two
points.

## Page 2 — calibration fits

Each remaining ladder gets a card. The scatter plots one point per ladder index: x
is that index's retention time in the run being calibrated, y is the same index's
retention time in the standard. Three models are fitted through the indexes ticked
on page 1 and drawn together over the points, each in its own colour and dash:

| Model | Form | Colour |
| --- | --- | --- |
| Linear | `ax + b` | rust, dashed |
| Quadratic | `ax² + bx + c` | teal, solid |
| Cubic | `ax³ + bx² + cx + d` | indigo, dotted |

Coefficients run highest power first, so `a` is always the leading term.

Below the scatter is a residual strip: standard minus fitted, per index, one series
per model. It exists because on a 40-minute axis the three curves lie almost on top
of each other even when they differ meaningfully — a model that is systematically
off shows up as an arch in the residuals long before it is visible in the overlay.
The strip switches to seconds when the residuals get small.

Beside the plot each model gets its equation, its coefficients, and RMSE, standard
error, largest residual, R² and adjusted R². Points left out are still drawn, as
hollow dashed circles. The indexes appear as chips — solid for used, dashed for
dropped — with a line saying which side dropped each one: unticked on this ladder,
unticked on the standard, or not found in both runs. A button jumps straight back to
that ladder's card on page 1.

### Which model is "best"

The three models are nested, so RMSE and R² can only improve as the degree rises,
whether or not the extra term describes anything real. Ranking on RMSE would hand
the contest to the cubic nearly every time.

So the tag goes to the simplest model that nothing beats by more than 5% on
**standard error**, which divides the residual sum of squares by the degrees of
freedom left rather than by the number of points. A term has to pay for itself. The
comparison also stops once a fit is already better than 0.002 min, about a tenth of
a second, since below that it is fitting rounding noise rather than chemistry. Both
constants are `SELECTION_MARGIN` and `SELECTION_FLOOR` in `ltqindex/calibration.py`.

When RMSE alone would have chosen differently, the card says so, so you can disagree
with the rule and read the numbers yourself. A model with exactly as many points as
coefficients passes through all of them; it is marked and cannot be chosen, because
its zero residual is arithmetic rather than agreement.

## Overall stats

Below those tables is **Save as standard**, which writes the ladder currently
selected as the standard to `standard_ladder/standard.json` for the main app to
use. Only the indexes still ticked on page 1 are recorded as usable, so an index
judged bad here cannot come back when a compound is normalized later.

At the bottom of page 2, two tables. The first gives, per model, the mean
coefficients across every fitted ladder with ± one standard deviation, plus mean and
worst RMSE, mean standard error, worst residual, mean R², and how many ladders that
model was best for. The second gives RMSE per ladder per model side by side, so one
awkward run is visible instead of averaged away, along with each ladder's error
before any fit. Both download as one CSV.

Both pages can be revisited freely, from the Back button or the step rail, which is
the point: changing the standard and seeing how the errors move is how you find out
which run makes the best reference. Index selections live on page 1 and survive a
change of standard, so you only judge each ladder's peaks once.

## How the fit is done

Each model is fitted with `numpy.polynomial.Polynomial.fit`, which works in a mapped
domain and is then converted back to plain powers of x. Retention times reach 50 min,
so a cubic term reaches six figures and an unscaled solve would be needlessly
ill-conditioned.

One consequence worth knowing: on a relationship that is genuinely linear, the
higher-order coefficients of the quadratic and cubic are ill-determined — the basis
is nearly degenerate — and can come back as small non-zero numbers. They cancel, so
the fitted curve is still accurate to the noise floor. Judge a term by what it
contributes across the retention-time range, not by the bare coefficient.

Errors are in minutes. The fit is a mapping, not a resampling: nothing is done to
the scans themselves.
