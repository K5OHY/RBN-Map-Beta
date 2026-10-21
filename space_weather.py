"""Current space weather from NOAA's Space Weather Prediction Center (free public feeds), and a short plain-English reading of it
for HF operators. The numbers are global, not specific to any path: the WSPR picture on the Band conditions page is what is
actually working near you, and this explains the why and gives an early warning of change."""
from datetime import datetime

import requests

from wspr_data import HEADERS

SWPC = "https://services.swpc.noaa.gov"
FEEDS = {
    "flux": "/products/summary/10cm-flux.json",
    "kp": "/products/noaa-planetary-k-index.json",
    "scales": "/products/noaa-scales.json",
    "wind": "/products/summary/solar-wind-speed.json",
    "field": "/products/summary/solar-wind-mag-field.json",
    "xray": "/json/goes/primary/xrays-6-hour.json",
    "flux30": "/products/10cm-flux-30-day.json",
    "solar_text": "/text/daily-solar-indices.txt",
    "geo_text": "/text/daily-geomagnetic-indices.txt",
}


def _get(name, timeout=12):
    resp = requests.get(SWPC + FEEDS[name], headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text if FEEDS[name].endswith(".txt") else resp.json()


def _rows(text):
    """The data rows of a NOAA daily text product, each as a list of tokens (comment and header lines are skipped)."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) > 4 and parts[0].isdigit() and len(parts[0]) == 4:
            out.append(parts)
    return out


def xray_class(flux):
    """The flare class of a GOES long-wavelength X-ray flux in W/m2, such as 'B6.6' or 'M1.2'."""
    if flux is None or not flux > 0:
        return None
    for letter, low in (("X", 1e-4), ("M", 1e-5), ("C", 1e-6), ("B", 1e-7), ("A", 1e-8)):
        if flux >= low:
            return f"{letter}{flux / low:.1f}"
    return "A0.0"


def fetch():
    """The current readings, each None when its feed could not be read. Returns a dict, never raises."""
    out = dict.fromkeys(("flux", "kp", "a_index", "xray", "xray_max", "wind", "bz", "bt", "R", "S", "G", "ssn", "flux_week",
                         "flux_month"))
    out["times"] = {}
    try:
        row = _get("flux")[0]
        out["flux"], out["times"]["flux"] = float(row["flux"]), row.get("time_tag")
    except Exception:
        pass
    try:  # how the daily flux has been moving: the last 7 days against the whole 30
        values = [float(r["flux"]) for r in _get("flux30") if r.get("flux")]
        if len(values) >= 14:
            out["flux_week"], out["flux_month"] = sum(values[-7:]) / 7, sum(values) / len(values)
    except Exception:
        pass
    try:  # the sunspot number of the latest complete day
        rows = [r for r in _rows(_get("solar_text")) if float(r[3]) > 0]
        out["ssn"], out["times"]["ssn"] = float(rows[-1][4]), f"{rows[-1][0]}-{rows[-1][1]}-{rows[-1][2]}"
    except Exception:
        pass
    try:  # the mid-latitude A index of the latest complete day
        rows = [r for r in _rows(_get("geo_text")) if float(r[3]) >= 0]
        out["a_index"], out["times"]["a_index"] = float(rows[-1][3]), f"{rows[-1][0]}-{rows[-1][1]}-{rows[-1][2]}"
    except Exception:
        pass
    try:
        row = _get("kp")[-1]
        out["kp"], out["times"]["kp"] = float(row["Kp"]), row.get("time_tag")
    except Exception:
        pass
    try:
        now = _get("scales")["0"]
        for key in ("R", "S", "G"):
            out[key] = int(now[key]["Scale"])
    except Exception:
        pass
    try:
        row = _get("wind")[0]
        out["wind"], out["times"]["wind"] = float(row["proton_speed"]), row.get("time_tag")
    except Exception:
        pass
    try:
        row = _get("field")[0]
        out["bz"], out["bt"] = float(row["bz_gsm"]), float(row["bt"])
    except Exception:
        pass
    try:
        longwave = [r for r in _get("xray") if r.get("energy") == "0.1-0.8nm" and r.get("flux")]
        out["xray"] = longwave[-1]["flux"]
        out["xray_max"] = max(r["flux"] for r in longwave)
        out["times"]["xray"] = longwave[-1].get("time_tag")
    except Exception:
        pass
    return out


def _trend_text(week, month):
    """' Over the past week it has been rising (117 on average against 110 over 30 days).' or ''."""
    if week is None or month is None or abs(week - month) < 5:
        return ""
    way = "rising" if week > month else "falling"
    return f" Over the past week it has been {way} ({week:.0f} on average against {month:.0f} over 30 days)."


def _flux_sentence(flux):
    if flux < 70:
        return (f"Solar flux is {flux:.0f}, very low: expect 40m and below to carry the load, with 20m mostly by day and the "
                "upper bands unlikely.")
    if flux < 90:
        return (f"Solar flux is {flux:.0f}, low: 20m is the workhorse, and 17m and 15m open only on good days.")
    if flux < 120:
        return (f"Solar flux is {flux:.0f}, fair to good: 20m and 17m should be solid, 15m often open, and 12m and 10m only "
                "now and then.")
    if flux < 150:
        return (f"Solar flux is {flux:.0f}, good: 20m through 15m should be strong, with 12m and 10m opening when the sun is up "
                "on your path.")
    if flux < 200:
        return f"Solar flux is {flux:.0f}, very good: 12m and 10m should be open widely, and 6m may open now and then."
    return f"Solar flux is {flux:.0f}, excellent: expect 10m wide open and a chance of 6m."


def _kp_sentence(kp):
    if kp < 2.5:
        return f"The geomagnetic field is quiet (Kp {kp:.1f}), so paths over the northern latitudes should hold up."
    if kp < 4:
        return (f"The geomagnetic field is slightly unsettled (Kp {kp:.1f}): northern and polar paths may be a little weaker "
                "or flutter.")
    if kp < 5:
        return (f"The geomagnetic field is active (Kp {kp:.1f}): expect weaker, noisier paths toward the north and over "
                "the poles, and look south and east-west instead.")
    return (f"A geomagnetic storm is in progress (Kp {kp:.1f}): HF is likely degraded, especially at high latitudes and on "
            "the upper bands, and the low bands can be noisy.")


def _xray_sentence(xray, xray_max):
    cls, peak = xray_class(xray), xray_class(xray_max)
    big_now = xray is not None and xray >= 1e-5
    big_peak = xray_max is not None and xray_max >= 1e-5
    if big_now:
        strength = "strong" if xray >= 1e-4 else "moderate"
        return (f"A {strength} flare is in progress (X-ray {cls}): HF can fade out on the sunlit side of the Earth for "
                "minutes to an hour. Try the shaded side, or wait it out.")
    if big_peak:
        return (f"There was {'an' if peak[0] in 'XM' else 'a'} {peak} flare in the last 6 hours, but the X-ray level is back to {cls}, so any radio fade-out "
                "should have recovered.")
    return f"The X-ray level is {cls}, so there are no flares to worry about."


def _wind_sentence(wind, bz, bt):
    parts = []
    if wind is not None and wind >= 600:
        parts.append(f"the solar wind is fast ({wind:.0f} km/s), which tends to disturb the field")
    elif wind is not None and wind >= 500:
        parts.append(f"the solar wind is elevated ({wind:.0f} km/s)")
    if bz is not None and bz <= -5:
        parts.append(f"the magnetic field points south (Bz {bz:.0f} nT), which lets the wind stir Earth's field")
    if not parts:
        return None
    return ("Watch for the field becoming unsettled in the next several hours to a day: " + ", and ".join(parts) + ".")


def interpret(data):
    """A few plain sentences about what the readings mean for HF, as a list of strings."""
    lines = []
    if data.get("flux") is not None:
        lines.append(_flux_sentence(data["flux"]) + _trend_text(data.get("flux_week"), data.get("flux_month")))
    if data.get("kp") is not None:
        lines.append(_kp_sentence(data["kp"]))
    if data.get("xray") is not None:
        lines.append(_xray_sentence(data["xray"], data.get("xray_max")))
    wind = _wind_sentence(data.get("wind"), data.get("bz"), data.get("bt"))
    if wind:
        lines.append(wind)
    scales = [f"{name}{data[key]}" for key, name in (("R", "radio blackout R"), ("S", "solar radiation storm S"),
                                                        ("G", "geomagnetic storm G")) if data.get(key)]
    if scales:
        lines.append("NOAA currently reports " + ", ".join(scales) + ".")
    if lines:
        lines.append("These are worldwide numbers, not your path: the WSPR picture below is what is actually working near you.")
    return lines


def when_text(iso, fmt):
    """A time from the feeds formatted with strftime `fmt` (like '%H:%M'), or '' when it cannot be read."""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "")).strftime(fmt)
    except Exception:
        return ""


def readings(data):
    """The readings for the dashboard strip: a list of dicts with label, value, unit, a one-word reading, its tone ('good', 'ok',
    'warn', 'bad' or 'quiet') and a tooltip. Readings whose feed could not be read are left out."""
    out = []

    def add(label, value, unit, word, tone, help_):
        out.append({"label": label, "value": value, "unit": unit, "word": word, "tone": tone, "help": help_})

    flux = data.get("flux")
    if flux is not None:
        word, tone = (("Very low", "bad") if flux < 70 else ("Low", "warn") if flux < 90 else ("Fair", "warn") if flux < 120
                      else ("Good", "good") if flux < 150 else ("Very good", "good") if flux < 200 else ("Excellent", "good"))
        add("Solar flux", f"{flux:.0f}", "", word, tone, "The 10.7 cm radio flux, measured daily at noon in Penticton. Higher "
            "means better HF: about 70 is poor, 120 good, 150 or more very good.")
    ssn = data.get("ssn")
    if ssn is not None:
        word, tone = ("Low", "quiet") if ssn < 25 else ("Moderate", "ok") if ssn < 80 else ("High", "good") if ssn < 150 else ("Very high", "good")
        add("Sunspots", f"{ssn:.0f}", "", word, tone, "The sunspot number of the latest complete day. More spots usually means a "
            "more active sun and better HF.")
    kp = data.get("kp")
    if kp is not None:
        word, tone = (("Quiet", "good") if kp < 2.5 else ("Unsettled", "warn") if kp < 4 else ("Active", "warn") if kp < 5
                      else ("Storm", "bad"))
        add("Kp index", f"{kp:.1f}", "", word, tone, "How disturbed Earth's magnetic field is, 0 to 9, updated every 3 hours. "
            "Under 3 is quiet, 5 or more is a storm.")
    xray, peak = data.get("xray"), data.get("xray_max")
    if xray is not None:
        word, tone = (("Strong flare", "bad") if xray >= 1e-4 else ("Flare", "bad") if xray >= 1e-5
                      else ("Recovering", "warn") if peak is not None and peak >= 1e-5 else ("Low activity", "good"))
        add("X-ray", xray_class(xray), "", word, tone, "The flare level from the GOES satellite. A and B are quiet, C is minor, "
            "M can cause a brief radio fade-out, X a strong one.")
    wind, bz = data.get("wind"), data.get("bz")
    if wind is not None:
        word, tone = ("Calm", "good") if wind < 500 else ("Elevated", "warn") if wind < 600 else ("Fast", "bad")
        if tone == "good" and bz is not None and bz <= -5:
            word, tone = "Bz south", "warn"
        add("Solar wind", f"{wind:.0f}", "km/s", word, tone, "The speed of the wind reaching Earth. Around 400 is calm, 500 or "
            "more is elevated, 600 or more is fast. A strongly south (negative) Bz lets the wind stir Earth's field.")
    return out
