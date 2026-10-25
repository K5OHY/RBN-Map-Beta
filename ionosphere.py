"""The maximum usable frequency (MUF) and the critical frequency (foF2) near a location, from the world's ionosondes.

An ionosonde is a small radar that measures the ionosphere overhead. The readings are collected by GIRO (the Global Ionospheric
Radio Observatory, University of Massachusetts Lowell) and republished in near real time by Andrew KC2G at prop.kc2g.com. Only a
few dozen sounders report at any moment, so the estimate for a place is made from the nearest ones that are working, and says
how far away they are."""
import math
from datetime import datetime, timezone

import requests

from wspr_data import HEADERS

URL = "https://prop.kc2g.com/api/stations.json"
EARTH_KM = 6371.0
MAX_KM = 3500.0       # a sounder farther away than this says too little about a place
MAX_AGE_MIN = 90      # and a reading older than this has gone stale (many sounders have been silent for months)
MIN_CONFIDENCE = 50   # KC2G's 0 to 100 score for how much to trust a reading
DISTANCES = (500, 1000, 2000, 3000)
BANDS = [("160m", 1.9), ("80m", 3.6), ("60m", 5.35), ("40m", 7.1), ("30m", 10.12), ("20m", 14.2), ("17m", 18.1),
         ("15m", 21.2), ("12m", 24.9), ("10m", 28.4), ("6m", 50.1)]


def _km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


def fetch():
    """The ionosondes with a fresh, trustworthy reading, as a list of dicts. Returns [] when the feed cannot be read."""
    try:
        data = requests.get(URL, headers=HEADERS, timeout=15).json()
    except Exception:
        return []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out = []
    for s in data:
        try:
            age = (now - datetime.fromisoformat(s["time"])).total_seconds() / 60
            if age > MAX_AGE_MIN or (s.get("cs") or 0) < MIN_CONFIDENCE or not s.get("mufd") or not s.get("fof2"):
                continue
            lon = float(s["station"]["longitude"])
            out.append({"code": s["station"]["code"], "name": s["station"]["name"].strip(), "lat": float(s["station"]["latitude"]),
                        "lon": lon - 360 if lon > 180 else lon, "muf": float(s["mufd"]), "fof2": float(s["fof2"]),
                        "hmf2": float(s["hmf2"]) if s.get("hmf2") else None, "age_min": age})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _secant(ground_km, layer_km):
    """1 / cos of the angle at which a single-hop signal meets a mirror layer at height `layer_km`, over `ground_km` of ground."""
    theta = ground_km / (2 * EARTH_KM)
    takeoff = math.atan((math.cos(theta) - EARTH_KM / (EARTH_KM + layer_km)) / math.sin(theta))
    return 1 / math.sqrt(1 - (EARTH_KM * math.cos(takeoff) / (EARTH_KM + layer_km)) ** 2)


def muf_for(muf3000, layer_km, ground_km, fof2=0.0):
    """The MUF for a path of `ground_km`, scaled from the measured 3,000 km MUF by the geometry of the hop. Never below foF2."""
    return max(muf3000 * _secant(ground_km, layer_km) / _secant(3000.0, layer_km), fof2)


def estimate(lat, lon, sounders):
    """The MUF (for 500, 1,000, 2,000 and 3,000 km paths) and foF2 near (lat, lon) from the nearest working sounders, or None
    when none is within MAX_KM."""
    near = sorted(((_km(lat, lon, s["lat"], s["lon"]), s) for s in sounders), key=lambda t: t[0])
    near = [(d, s) for d, s in near if d <= MAX_KM][:2]
    if not near:
        return None
    weights = [1 / max(d, 100.0) ** 2 for d, _ in near]  # the closer sounder counts for more
    blend = lambda key: sum(w * s[key] for w, (_, s) in zip(weights, near)) / sum(weights)  # noqa: E731
    heights = [s["hmf2"] for _, s in near if s["hmf2"]]
    layer = sum(heights) / len(heights) if heights else 300.0
    muf3000, fof2 = blend("muf"), blend("fof2")
    return {"muf": {d: muf_for(muf3000, layer, d, fof2) for d in DISTANCES}, "fof2": fof2, "layer_km": layer,
            "sounders": [(s["name"], d, s["age_min"]) for d, s in near], "nearest_km": near[0][0], "lat": lat}


def top_band(freq_mhz):
    """The highest amateur band (HF and 6m) whose lower edge is under `freq_mhz`, as its name, or None."""
    under = [name for name, f in BANDS if f <= freq_mhz]
    return under[-1] if under else None


def _fmt_km(km):
    return f"{km:,.0f}"


def interpret(est):
    """A few plain sentences about what the estimate means, as a list of strings."""
    if not est:
        return []
    muf = est["muf"]
    far = top_band(muf[3000])
    sweet = 0.85 * muf[3000]
    try_band = top_band(sweet)
    bands_to_try = (f"{try_band} to {far}" if try_band and try_band != far else far)
    lines = [f"For a 3,000 km path the MUF is about {muf[3000]:.1f} MHz, so {far} is the highest band under it. Working a little "
             f"below the MUF (a common rule of thumb is about 85%, near {sweet:.1f} MHz) is usually steadier than working right "
             f"at it, so {bands_to_try} {'are reasonable bands' if try_band and try_band != far else 'is a reasonable band'} to try."]
    shorter = ", ".join(f"about {muf[d]:.1f} MHz at {_fmt_km(d)} km (up to {top_band(muf[d])})" for d in (2000, 1000, 500)
                        if top_band(muf[d]))
    if shorter:
        lines.append("Shorter hops cannot use as high a frequency, and these figures are calculated from the 3,000 km one: "
                     + shorter + ".")
    nvis = top_band(est["fof2"])
    if nvis:
        lines.append(f"For local contacts by NVIS (steep skywave) the ceiling is foF2, about {est['fof2']:.1f} MHz. {nvis} and the "
                     "bands below it are under that ceiling, and anything higher will tend to skip over a nearby station. "
                     "Being under the ceiling is not a guarantee: absorption and noise can still limit the lowest bands.")
    names = " and ".join(f"{name} ({_fmt_km(d)} km away, {a:.0f} min ago)" for name, d, a in est["sounders"])
    km = est["nearest_km"]
    low = " Near the equator the ionosphere varies more from place to place, so treat it with extra caution."         if abs(est["lat"]) < 25 else ""
    if km <= 2500:
        lines.append(f"This is an estimate from {names}. It describes the ionosphere over those sounders, not your exact path. "
                     "In a check of sounders against each other, the middle error was about 1.3 MHz when they were within "
                     f"2,000 km of each other, and it can be larger.{low}")
    else:
        lines.append(f"This is an estimate from {names}. The nearest working sounder is far away, so treat it as a rough guide, "
                     f"not a measurement for your path.{low}")
    return lines
