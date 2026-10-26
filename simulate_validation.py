"""Simulated antenna tests with a known difference, run through the program's own analysis, to measure how the verdict behaves:
how often it names the stronger antenna, how often it names a winner when the antennas are the same, and how often its 95% range
contains the true difference. Run it with the number of simulated tests per row (300 takes about 7 minutes):

    python simulate_validation.py 300

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
               listen=0.85):
    """One simulated test. Antenna B is `true_gain_b` dB stronger than A on average. Every receiver also has its own
    +/- `pattern_sd` difference between the antennas, every transmission fades for all receivers together (`shared_fade`),
    every report has its own noise (`report_sd`), the band drifts steadily by `drift_sd` dB over an hour, a receiver listens in
    a given slot with probability `listen`, and a report below -28 dB is not decoded. Returns (call, mean, low, high, n) of the
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
        for r in np.flatnonzero(heard & (snr > -28)):
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


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 150)
