# Plan: WSPR-based antenna comparison

Status: **not started** — brainstormed and scoped 2026-09-29, not on any timeline.

## The idea

Use WSPR beacon reports instead of (or alongside) RBN CW spots to compare two
antennas, transmitting with something like a Zachtek WSPR desktop transmitter.

## Why this is a different mode, not a small tweak

Compare mode today splits spots by **frequency** ([frequency_groups()](../web.py:357)):
you CQ on 14069.5, then 14070.5, and the app tells the two tests apart by
where the frequency jumped. That works for CW because you can freely pick a
transmit frequency.

WSPR doesn't allow that. Every station on a band transmits in the same narrow
calling channel; there's no second frequency to switch to. The only way to
tell "antenna A" and "antenna B" apart is **time**: run antenna A for a
session, switch antennas, run antenna B for a second session. So the app
needs a second grouping mode — split by a *time* gap instead of a *frequency*
gap — and everything downstream (scoreboard, map, direction chart) needs to
work from either kind of group.

## Data source: wspr.live

Tested 2026-09-29 and it looks solid — much better-founded than the Vail
ReRBN option we passed on:

- `https://db1.wspr.live/` — public ClickHouse HTTP endpoint, no auth, JSON
  responses, mirrors the full WSPR archive back to 2008.
- Query shape: `GET /?query=<SQL> FORMAT JSON`. Example:
  ```sql
  SELECT time, band, tx_sign, tx_loc, rx_sign, rx_loc, rx_lat, rx_lon,
         frequency, power, snr, distance, azimuth
  FROM wspr.rx
  WHERE tx_sign = 'K5OHY' AND time BETWEEN '...' AND '...'
  ORDER BY time
  FORMAT JSON
  ```
- A lookup scanning ~2.5 billion rows over a year came back in ~3 seconds.
  No rate-limit documentation found; be a good citizen anyway (short
  timeout, cache with `st.cache_data`, clear User-Agent).
- Each row already carries the receiver's grid (`rx_loc`) and computed
  `distance`/`azimuth` — **no skimmer-location lookup needed for WSPR**,
  unlike RBN where we scrape and cache `spotter_coords.csv`.

### Fields that don't map 1:1 onto the current model

| Field | Note |
|---|---|
| `snr` | WSPR SNR is measured in a 2500 Hz reference bandwidth by convention and commonly runs roughly −30 to +3 dB. **Do not reuse** `SNR_MIN=5, SNR_MAX=35` ([web.py:46](../web.py:46)) — needs its own scale/legend or the map will render everything as "weak". |
| `band` | Stored as a numeric code in wspr.live, not a string like `"40m"`. Need a small lookup table — **verify the exact code list from the schema when building**, don't guess. |
| `frequency` | In Hz (e.g. `10140193`); the app's `freq` columns elsewhere are kHz — convert. |
| `power` | dBm, encoded in the WSPR transmission itself. This is new information RBN spots don't have — see guardrail below. |

## Design: reuse as much of compare mode as possible

Most of compare mode already operates on a generic DataFrame
(`spotter`/`freq`/`snr`/`time`/`band`) plus a `skimmers` dict of
`{callsign: (lat, lon)}`. Plan is to keep that shape so the rest of the
pipeline barely changes:

1. **`wspr_data.py`** (new file, parallel to `rbn_data.py`):
   - `fetch_wspr_spots(callsign, start, end)` → DataFrame with columns
     renamed/normalized to match what `skimmer_table()` / `build_map()`
     already expect: `spotter` (from `rx_sign`), `freq` (kHz), `snr`,
     `time`, `band` (mapped to a name), plus `lat`/`lon` inline from
     `rx_lat`/`rx_lon`.
   - Build an **ephemeral skimmers dict** from that same response —
     `{row.spotter: (row.lat, row.lon)}` — and pass it into the existing
     `skimmer_location()`/`build_map()` unchanged. Avoids touching the
     `spotter_coords.csv` cache path at all for this mode.
   - Same defensive pattern as `refresh_skimmer_cache`: timeout, try/except,
     clear error message pointing back to RBN mode if the request fails.

2. **`time_groups(spots, gap_minutes)`** (new function, sibling to
   `frequency_groups()` at [web.py:357](../web.py:357)): same "cluster
   wherever the gap exceeds a threshold" logic, just clustering on elapsed
   time between spots instead of frequency delta. Keeps the same
   dropdown-based UX (`compare_view` picks the two biggest groups,
   `option_label` shows spot/skimmer counts and time range) working
   identically for both modes.

3. **Generalize the SNR scale**: `snr_strength()`, `snr_color()`,
   `snr_radius()` ([web.py:155-165](../web.py:155)) currently close over the
   module-level `SNR_MIN`/`SNR_MAX`. Change them to take `snr_min`/`snr_max`
   as parameters (defaulting to today's CW values), so a WSPR call site can
   pass its own range without a second copy of the functions.

4. **New guardrails specific to WSPR** (can't do these for RBN, since RBN
   spots don't carry power):
   - Warn if the two sessions' `power` values differ by more than ~1 dB —
     a fair antenna test needs equal TX power, and WSPR is the one mode
     where the app can actually check that instead of trusting the user.
   - Warn if the two sessions are on different bands.
   - Show session duration for each side, since very unequal durations
     skew spot counts.

5. **UI**: a source/mode choice ("RBN spots (CQ)" vs "WSPR (antenna test)"),
   feeding an adapted `compare_view` that swaps `frequency_groups` for
   `time_groups` and the CW SNR scale for the WSPR one, but reuses
   `scoreboard_html`, `reach_bar_html`, `direction_chart`, `sector_means`,
   `head_to_head_table`, and `build_map` as-is.

## Rough effort (once picked up)

| Piece | Estimate |
|---|---|
| `wspr_data.py` fetch + normalization | 1–2 hrs |
| Generalize SNR scale functions | ~30 min |
| `time_groups()` + wiring into `compare_view` | 1–2 hrs |
| UI: mode toggle, callsign/date-range inputs | 1–2 hrs |
| Power/band mismatch guardrails + copy | ~1 hr |
| Testing against a real Zachtek run | depends on getting real data |

Roughly a weekend-sized project in focused time, buildable incrementally in
`beta/` and testable at each step without touching `main`.

## Open questions to resolve while building, not before

- Exact WSPR band-code → name mapping (read off wspr.live's schema/docs at
  build time rather than guessing).
- Auto-split by time gap (matches today's UX) vs. explicit start/end time
  pickers — lean toward auto-split first, manual override as a fallback for
  messy sessions.
- Whether "same skimmer heard both" (used for the head-to-head table and the
  SNR-delta significance test) is still a meaningful comparison for WSPR,
  where the receiver set for a low-power beacon may barely overlap between
  two very different antennas — may need a lower minimum-shared-skimmers
  bar, or a different confidence check.
