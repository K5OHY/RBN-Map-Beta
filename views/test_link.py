"""A whole antenna test in a web address: the antenna names, how they were switched, the start and the order, and the averaging
choices, so a link opens the same comparison for someone else (the callsign, date and band travel in the basic link)."""
import re

from ui_common import WSPR_SOURCE

BLOCKS = "In long blocks"
ORDERS = {"abab": "A, B, A, B …", "abba": "A, B, B, A …"}
_TIME = re.compile(r"^(\d{1,2} [A-Za-z]{3} )?\d{2}:\d{2}$")
_EDIT = re.compile(r"^(\d{1,3})([ABX])$")


def _int(text, low, high):
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if low <= value <= high else None


def read(ss, q):
    """Once per visit, set the comparison's choices from the web address `q`. Anything missing or unreadable is ignored. The
    values that depend on the loaded spots (the blocks' count and start) wait in `_blk_pending` until they are on screen."""
    if ss.get("_test_link_read"):
        return
    ss["_test_link_read"] = True
    if any(q.get(p) for p in ("cb", "na", "nb", "sw", "n", "ts", "ord", "bl", "bn", "bs", "bp", "ed")):
        ss["_setup_given"] = True  # the link carries the test's setup, so there is nothing to confirm
    for param, key in (("na", "cmp_name_a"), ("nb", "cmp_name_b")):
        if q.get(param):
            ss[key] = q[param][:60]
    blocks = q.get("sw") == "blocks"
    if blocks:
        ss["test_mode"] = BLOCKS
    n = _int(q.get("n"), 2, 80)
    if n and not blocks:
        ss["test_cycles"] = n - n % 2
    if q.get("ts") and _TIME.match(q["ts"]):
        ss["test_start"] = q["ts"]
    if q.get("ord") in ORDERS:
        ss["blk_order" if blocks else "test_order"] = ORDERS[q["ord"]]
    if blocks:
        length = _int(q.get("bl"), 4, 480)
        if length:
            ss["blk_len"] = length
        pasted = "\n".join(line.strip() for line in q.get("bp", "")[:1500].split(";") if line.strip())
        ss["_blk_pending"] = {"n": _int(q.get("bn"), 2, 40), "start": q.get("bs", "")[:16], "paste": pasted}
    edits = {}
    for item in q.get("ed", "")[:600].split(","):
        m = _EDIT.match(item.strip())
        if m:
            edits[int(m[1])] = None if m[2] == "X" else m[2]
    if edits and not blocks:
        ss["_log_pending"] = edits
    reports = _int(q.get("mr"), 1, 60)
    if reports:
        ss["rx_min_reports_blk" if blocks else "rx_min_reports_pair"] = reports
    if q.get("fl") == "1":
        ss["avg_floor"] = True
    near = _int(q.get("nr"), 1, 5000)
    if near:
        ss[f"avg_near_{ss.get('units_in', 'mi')}"] = near
    weak = _int(q.get("wk"), 1, 100)
    if weak:
        ss["avg_weak"] = weak - weak % 5


def apply_blocks(ss, sig):
    """The blocks' count and first start from a link, put into the widgets that carry this data's `sig`. Used once."""
    pending = ss.pop("_blk_pending", None)
    if not pending:
        return
    if pending["n"]:
        ss[f"blk_n_{sig}"] = pending["n"] - pending["n"] % 2
    if pending["start"]:
        ss[f"blk_start_{sig}"] = pending["start"]
    if pending.get("paste"):
        ss[f"blk_paste_{sig}"] = pending["paste"]


def log_base(ss, key):
    """The antenna cells a link corrected by hand, for the quick test's table whose widget key is `key`: {row: 'A', 'B' or None}.
    Streamlit does not let a page set an editable table's edits, so these go into the table's starting values instead, and stay
    the same for as long as the table's key does."""
    pending = ss.pop("_log_pending", None)
    if pending:
        ss["_log_base"] = {"key": key, "edits": pending}
    base = ss.get("_log_base")
    return base["edits"] if base and base["key"] == key else {}


def params(ss, units):
    """The choices on screen as web address parameters, for the link box."""
    out = {}
    for param, key in (("na", "cmp_name_a"), ("nb", "cmp_name_b")):
        if (ss.get(key) or "").strip():
            out[param] = ss[key].strip()[:60]
    if (ss.get("callsign_b_in") or "").strip() and ss.get("src_in") == WSPR_SOURCE:
        out["cb"] = ss["callsign_b_in"].strip()[:12]
    blocks = ss.get("test_mode") == BLOCKS
    reverse = {text: code for code, text in ORDERS.items()}
    if blocks:
        out["sw"] = "blocks"
        if ss.get("blk_len"):
            out["bl"] = str(ss["blk_len"])
        for prefix, param in (("blk_n_", "bn"), ("blk_start_", "bs")):
            found = [k for k in ss if isinstance(k, str) and k.startswith(prefix)]
            if len(found) == 1 and ss[found[0]]:
                out[param] = str(ss[found[0]])
        found = [k for k in ss if isinstance(k, str) and k.startswith("blk_paste_")]
        if len(found) == 1 and (ss[found[0]] or "").strip():
            lines = [line.strip() for line in ss[found[0]].splitlines() if line.strip()]
            out["bp"] = ";".join(lines)[:1500]
        order = reverse.get(ss.get("blk_order"))
    else:
        if ss.get("test_cycles"):
            out["n"] = str(ss["test_cycles"])
        if ss.get("test_start") not in (None, "Latest"):
            out["ts"] = str(ss["test_start"])
        order = reverse.get(ss.get("test_order"))
        logs = [k for k in ss if isinstance(k, str) and k.startswith("antenna_log_")]
        if len(logs) == 1:
            base = ss.get("_log_base")
            cells = dict(base["edits"]) if base and base["key"] == logs[0] else {}
            if isinstance(ss[logs[0]], dict):
                for row, change in (ss[logs[0]].get("edited_rows") or {}).items():
                    if "Antenna" in change:
                        cells[int(row)] = change["Antenna"]
            items = [f"{row}{'X' if lab is None else lab}" for row, lab in sorted(cells.items())]
            if items:
                out["ed"] = ",".join(items)
    if order and order != "abab":
        out["ord"] = order
    reports = ss.get("rx_min_reports_blk" if blocks else "rx_min_reports_pair")
    if reports:
        out["mr"] = str(reports)
    if ss.get("avg_floor"):
        out["fl"] = "1"
    if ss.get(f"avg_near_{units}"):
        out["nr"] = str(int(ss[f"avg_near_{units}"]))
    if ss.get("avg_weak"):
        out["wk"] = str(int(ss["avg_weak"]))
    return out
