"""Simulated antenna tests with a known difference, run through the program's own analysis, to measure how the verdict behaves:
how often it names the stronger antenna, how often it names a winner when the antennas are the same, and how often its 95% range
contains the true difference. Run it with the number of simulated tests per row (300 takes about 7 minutes):

    python simulate_validation.py 300             (the main table)
    python simulate_validation.py 250 ablation    (starts with complete data and adds one problem at a time, for coverage)

The results depend on the noise this makes up (see `experiment`), so they test the program's logic, not real propagation. The
real-data checks are in docs/METHODOLOGY.md."""
import sys
import time

import numpy as np
import pandas as pd

from compare_stats import analyze, default_labels, split_by_labels

HOME = (39.0, -77.0)
N_RX = 80


def _receivers(rng):
    lat, lon = rng.uniform(25, 55, N_RX), rng.uniform(-125, 15, N_RX)
    names = [f"R{i}" for i in range(N_RX)]
    return names, {n: (float(a), float(b)) for n, a, b in zip(names, lat, lon)}


def experiment(rng, names, locs, true_gain_b, n_tx, pattern, shared_fade=0.6, report_sd=3.0, pattern_sd=1.5, drift_sd=2.0,
               listen=0.85, floor=-28.0):
    """One simulated test. Antenna B is `true_gain_b` dB stronger than A on average. Every receiver also has its own
    +/- `pattern_sd` difference between the antennas, every transmission fades for all receivers together (`shared_fade`),
    every report has its own noise (`report_sd`), the band drifts steadily by `drift_sd` dB over an hour, a receiver listens in
    a given slot with probability `listen`, and a report below `floor` dB is not decoded (`floor=None` keeps every report). Returns (call, mean, low, high, n) of the
    program's strength result, where mean is A minus B, or None when nothing could be compared."""
    base_a = rng.normal(-17, 6, N_RX)
    base_b = base_a + true_gain_b + rng.normal(0, pattern_sd, N_RX)
    times = pd.date_range("2026-01-01 12:00", periods=n_tx, freq="2min")
    fade = rng.normal(0, shared_fade, n_tx)
    slope = rng.normal(0, drift_sd) / 60.0  # dB per minute
    labels = {t: lab for t, lab in zip(times, default_labels(list(times), pattern))}
    rows = []
    for i, t in enumerate(times):
        base = base_a if labels[t] == "A" else base_b
        heard = rng.random(N_RX) < listen
        snr = base + fade[i] + slope * 2 * i + rng.normal(0, report_sd, N_RX)
        for r in np.flatnonzero(heard & ((snr > floor) if floor is not None else True)):
            rows.append((names[r], float(np.round(snr[r])), t))
    spots = pd.DataFrame(rows, columns=["spotter", "snr", "time"])
    spots["band"] = "20m"
    a, b = split_by_labels(spots, labels)
    a, b = a[a["round"].notna()], b[b["round"].notna()]
    if a.empty or b.empty:
        return None
    s = analyze(a, b, locs, HOME, None, None, True, "mi", exposure=None)["strength"]
    return s["call"], s["mean"], s["lo"], s["hi"], s["n"]


def main(runs=150, seed=2026):
    rng = np.random.default_rng(seed)
    names, locs = _receivers(rng)
    print(f"{runs} simulated tests per row. Truth: B is d dB stronger than A.\n")
    print(" d(dB)  tx  order |  says B  says A  equal/unclear | 95% range holds the truth")
    started = time.time()
    for d in (0, 1, 3, 6):
        for n_tx in (6, 12, 30):
            for pattern in ("ABAB", "ABBA"):
                calls, held, valid = [], 0, 0
                for _ in range(runs):
                    r = experiment(rng, names, locs, d, n_tx, pattern)
                    if r is None or r[4] < 2 or not np.isfinite(r[2]):
                        continue
                    valid += 1
                    calls.append(r[0])
                    held += r[2] <= -d <= r[3]  # the program reports A minus B, so the truth is minus d
                if not valid:
                    continue
                counts = pd.Series(calls).value_counts()
                says_b, says_a = counts.get("B", 0) / valid, counts.get("A", 0) / valid
                print(f" {d:>4}  {n_tx:>3}  {pattern} |  {100 * says_b:>5.0f}%  {100 * says_a:>5.0f}%   {100 * (1 - says_a - says_b):>5.0f}%"
                      f"        | {100 * held / valid:>4.0f}%   (runs {valid})")
    print(f"\n{time.time() - started:.0f} seconds")


def ablation(runs=200, seed=7, true_gain=3.0):
    """Coverage of the 95% range, starting from complete data and adding one problem at a time, to see what narrows it."""
    rng = np.random.default_rng(seed)
    names, locs = _receivers(rng)
    clean = dict(floor=None, listen=1.0, drift_sd=0.0, shared_fade=0.6, pattern_sd=1.5, report_sd=3.0)
    scenarios = [
        ("complete data (nothing missing, no drift)", {}),
        ("+ reports below -28 dB lost", dict(floor=-28.0)),
        ("+ receivers listen 85% of slots", dict(listen=0.85)),
        ("+ steady band drift (2 dB an hour)", dict(drift_sd=2.0)),
        ("+ no shared fading (0 dB)", dict(shared_fade=0.0)),
        ("everything on (the main table)", dict(floor=-28.0, listen=0.85, drift_sd=2.0)),
    ]
    print(f"{runs} tests per cell, true gain {true_gain:g} dB. Share of 95% ranges that hold the truth:\n")
    print(f"{'':44s} 12 tx ABAB  12 tx ABBA  30 tx ABAB  30 tx ABBA")
    for label, change in scenarios:
        cells = []
        for n_tx, pattern in ((12, "ABAB"), (12, "ABBA"), (30, "ABAB"), (30, "ABBA")):
            held = valid = 0
            for _ in range(runs):
                r = experiment(rng, names, locs, true_gain, n_tx, pattern, **{**clean, **change})
                if r is None or r[4] < 2 or not np.isfinite(r[2]):
                    continue
                valid += 1
                held += r[2] <= -true_gain <= r[3]
            cells.append(f"{100 * held / max(valid, 1):>6.0f}%    ")
        print(f"{label:44s} " + " ".join(cells))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[2] == "ablation":
        ablation(int(sys.argv[1]))
    else:
        main(int(sys.argv[1]) if len(sys.argv) > 1 else 150)
