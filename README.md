# RBN Signal Mapper

See who heard you, how strong, and from where. Map the [Reverse Beacon Network](https://www.reversebeacon.net/) stations that spotted your CQ, or the [WSPR](https://wsprnet.org/) stations that heard your beacon. Compare mode tells you which of two antennas, frequencies or power levels is getting out better.

**Try the beta online, nothing to install: [rbn-map-beta.streamlit.app](https://rbn-map-beta.streamlit.app/)** (the stable version is at [rbnmap.streamlit.app](https://rbnmap.streamlit.app/))

![RBN Signal Mapper Screenshot](images/Screenshot.png)

## Quick start

1. Open the site (or [run it locally](#run-it-locally)).
2. Type your **callsign** in the sidebar.
3. Pick where the spots come from, then click **Load spots**.
4. Use the filters in the sidebar, and download the map or a PDF report if you want to share it.

The hosted site does not remember your settings; a local copy does (in `settings.json`). Free hosting can fall asleep: click the button to wake it.

## Getting your spots

**RBN: Download by date.** Pick a day or a range (up to 7 days). Works for anything up to yesterday (UTC), because RBN publishes a day's file after that day ends. Only spots of your callsign are kept.

**RBN: Paste from RBN site.** For spots from the last few minutes or hours, and for quick tests. Look up your callsign on [reversebeacon.net](https://www.reversebeacon.net/), copy the spot rows, paste them into the box, and click Load spots. The app reads each field by what it looks like, so odd spacing is fine.

**WSPR (wspr.live).** Pick a date (today works) and click Load spots. The app looks up WSPR spots sent by the callsign you entered, so use the callsign your WSPR transmitter sends. Reports take about 5 minutes to arrive in full; a newer cycle is still used and marked *may be incomplete*. Click Load spots again to refresh.

WSPR differs from RBN in a few ways: SNR is mostly negative (about -30 to +5 dB), the stations are called receivers, and *Show all skimmers* is off because there is no list of every WSPR receiver.

## The map

- Paths from you to every station that heard you, drawn as great circles and coloured by band.
- Dots sized and coloured by SNR. Click one for every spot from that station. The farthest station is circled.
- Filters: **Band**, **UTC time window** (one-minute steps), and **Minimum SNR** (RBN). They update the map instantly.
- Stats above the map, a compass chart of your spots by direction below it, and a sortable spot table.
- Map style (light, dark, satellite, street), miles or kilometres, and **Download map** (a standalone HTML file).
- **Download report (PDF)** next to it: key numbers, direction chart, a by-band table and the stations that heard you.

Your location comes from the grid you typed, else the RBN skimmer list, else the FCC ([callook.info](https://callook.info)), else [HamDB](https://hamdb.org), else the centre of your country (the app warns you). Enter a grid for an exact pin.

## Compare two antennas

Tick **Compare two antennas or tests**. Name your antennas under *Name your antennas* if you like.

### With WSPR (best for antennas)

A WSPR transmission lasts about 110 seconds of each 2-minute slot, so there is a short break between transmissions. Flip the antenna switch in that break.

1. Connect the transmitter to an A/B coax switch with one antenna on each output. Use one band, one power and one callsign for the whole test.
2. Let the transmitter send in every slot. If it can send an extra packet between cycles (for example a 6-character locator option), turn that off.
3. Wait for the transmission to end, flip the switch, and let the next one start. Go **A, B, A, B** or **A, B, B, A**, always starting on A. Note the time of the first transmission.
4. Run at least 8 transmissions (4 pairs, about 16 minutes). Fewer is a quick check; 12 to 16 is better for small differences.
5. Wait about 5 minutes, pick the date, and click Load spots.
6. Under **Your test**, set **Transmissions in your test** (every 2-minute cycle on either antenna counts: A then B is 2), and **Starting at** if it was not the latest test. It shows how small a difference that many pairs can reliably show. Check the **Order** and the Antenna column match your notes, and read the result. Fix any row, or clear it to leave it out.

Use an even number of transmissions: an odd one has no partner and is left out.

### With RBN

Call CQ with the first antenna on one frequency, then with the other antenna on a nearby frequency, back to back (a minute or two apart). Paste the spots and click Load spots. The two biggest frequency groups become A and B; change them with the dropdowns. Skimmers report slightly different frequencies for the same signal, so spots closer than the **Frequency gap** (0.5 kHz) count as one. RBN is compared by frequency only, because skimmers report a spot a minute or more after you send, which is too blurry to separate A and B by the clock.

Send plenty of CQs on each side, at the same power and speed (or enter the power under **Power**).

### Reading the result

- **Result.** The verdict first, in plain English, with the evidence and any caveats. Then a scoreboard, who heard you on each side, and a timeline of every spot in the order it happened. **Download report (PDF)** sits under the verdict.
- **Direction & distance.** Which side is better toward each compass direction and at each range.
- **Receiver by receiver.** Every station that heard both sides, as a chart and a table.
- **Maps.** A's map and B's map side by side.

Differences are always shown as who is ahead and by how much (for example *B +2.0 dB*), never as a negative number.

### How the verdict is decided

- **Strength.** Only stations that heard both sides count, so each station is its own control. Each station's difference is averaged, with a 95% range. A side wins if the whole range favours it and the gap is at least 1 dB; a smaller gap is *equal*.
- **Reach.** How many different stations heard each side. For WSPR it is judged one pair of transmissions at a time, because fading on your end moves every receiver together. Fewer than 6 pairs can't be called.
- **Too close to call** means the data can't separate them; it is not the same as equal. If one side leads, the verdict gives the chance it really is ahead (for example about 88%), and for RBN how much more data would settle it.
- **Direction and distance** are only called when at least 4 stations heard both sides there, the gap is at least 2 dB, and a stricter 99% range agrees.
- **Fairness.** For WSPR only receivers that were listening during both sides' transmissions count, and cycles heard by 2 or fewer receivers are ignored when normal cycles are heard by many more (usually one receiver with a wrong clock). A cycle with no partner on the other antenna is left out. Power differences are corrected (WSPR carries the power). Skimmers at the same place are counted once. If A and B were sent more than about 10 minutes apart, the verdict warns that the time of day could explain the difference.

## Run it locally

You need [Python 3.9+](https://www.python.org/downloads/).

**Windows (PowerShell).** Set it up once:

```bash
git clone https://github.com/K5OHY/RBN_Map.git
cd RBN_Map
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Then start it from the project folder:

```bash
.venv\Scripts\streamlit run web.py
```

Use exactly that command. Plain `streamlit run web.py` fails with "not recognized" because the packages live inside `.venv`. If `python` is not found, try `py -m venv .venv`.

**macOS / Linux.**

```bash
git clone https://github.com/K5OHY/RBN_Map.git
cd RBN_Map
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/streamlit run web.py
```

Next time, just run the last command again. Open http://localhost:8501 if the browser does not open, and stop the app with Ctrl+C (close the browser tab first if it hangs).

The skimmer list refreshes itself every 24 hours. To force it: `.venv\Scripts\python rbn_to_csv.py` (macOS/Linux: `.venv/bin/python rbn_to_csv.py`).

## Troubleshooting

| What you see | What to do |
| --- | --- |
| "RBN has no data file for ... yet" | RBN publishes a day's file after the UTC day ends. Pick an earlier date, or paste today's spots. |
| "RBN has no spots of ... for that date" | Check the callsign or try another day. |
| "Couldn't read any spot rows" | Copy the rows straight from the RBN spot table. The message quotes the first line it couldn't read. |
| "Couldn't find a location for ..." | Enter your grid square in the sidebar. |
| "No location for ..." under the stats | Those skimmers are not in RBN's node list, so they are counted but not drawn. |
| Compare found only one frequency group | Your spots are on one frequency, or the **Frequency gap** merged your tests. Lower the gap. |
| A few WSPR cycles show only one or two receivers while the rest show dozens | Usually one receiver with a wrong clock. The app ignores them and says how many. |
| "Not enough data yet" / too close to call | Run more transmissions per antenna (see above). |
| Downloading is slow | A day of RBN data is a large file. Each day in a range is a separate download. Results are cached for an hour. |

## Files

| File | Purpose |
| --- | --- |
| `web.py` | The Streamlit app |
| `compare_stats.py` | The A/B maths and the plain-English verdict |
| `report_pdf.py` | Lays out the PDF reports |
| `rbn_data.py` | Skimmer list, callsign location lookup, grid conversion |
| `wspr_data.py` | Fetches WSPR spots from wspr.live |
| `spotter_coords.csv` | Cached skimmer locations (updated automatically) |
| `cty.dat` | Country prefixes from [country-files.com](https://www.country-files.com), for the approximate-location fallback |
| `rbn_to_csv.py` | Refreshes the skimmer list now |
| `Update_spotters.py` | Older helper, not needed |
| `requirements.txt` | Python packages |
| `settings.json` | Your saved settings (local, never committed) |

## Data sources and thanks

[Reverse Beacon Network](https://www.reversebeacon.net/), [wspr.live](https://wspr.live/) and [WSPRnet](https://wsprnet.org/), [callook.info](https://callook.info), [HamDB](https://hamdb.org), [country-files.com](https://www.country-files.com), Esri and OpenStreetMap map tiles. Built with [Streamlit](https://streamlit.io/), [Folium](https://python-visualization.github.io/folium/), [pandas](https://pandas.pydata.org/), [Matplotlib](https://matplotlib.org/) and [GeographicLib](https://geographiclib.sourceforge.io/).

## License

MIT License.
