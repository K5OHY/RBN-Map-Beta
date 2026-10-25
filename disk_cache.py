"""A small folder of things already downloaded, so a day that is over is fetched once and not again. A finished day never changes,
so the next person who asks for it (or the same person after a restart, when running locally) is served from here instead of
asking the Reverse Beacon Network or wspr.live again. wspr.live allows only 20 requests a minute for the whole site.

Nothing here is needed for the app to work: any problem reading or writing the folder is treated as "not cached".
The folder is RBN_CACHE_DIR if set, otherwise a folder in the system's temporary directory, and it is trimmed to MAX_BYTES by
deleting the least recently used files first."""
import hashlib
import json
import os
import tempfile
from pathlib import Path

import pandas as pd

VERSION = 1  # change it to throw away everything cached by an older version of the code
MAX_BYTES = 400 * 1024 * 1024


def folder():
    path = Path(os.environ.get("RBN_CACHE_DIR") or Path(tempfile.gettempdir()) / "rbn_signal_mapper_cache")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _name(kind, key, suffix):
    digest = hashlib.sha1(f"{VERSION}|{kind}|{key}".encode("utf-8")).hexdigest()[:24]
    return folder() / f"{kind}_{digest}{suffix}"


def _prune():
    files = sorted((f for f in folder().iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    for f in files:
        if total <= MAX_BYTES:
            break
        total -= f.stat().st_size
        f.unlink(missing_ok=True)


def get_frame(kind, key):
    """(DataFrame, meta dict) saved under this kind and key, or None."""
    try:
        data, meta = _name(kind, key, ".csv.gz"), _name(kind, key, ".json")
        if not (data.exists() and meta.exists()):
            return None
        frame = pd.read_csv(data)
        if "time" in frame:
            frame["time"] = pd.to_datetime(frame["time"])
        data.touch()
        return frame, json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return None


def put_frame(kind, key, frame, meta=None):
    """Saves a frame. Each file is written under a temporary name and then renamed, so another visitor reading at the same moment
    never sees half a file."""
    try:
        data, info = _name(kind, key, ".csv.gz"), _name(kind, key, ".json")
        tmp_data, tmp_info = data.with_name(data.name + ".part"), info.with_name(info.name + ".part")
        frame.to_csv(tmp_data, index=False, compression="gzip")
        tmp_info.write_text(json.dumps(meta or {}), encoding="utf-8")
        os.replace(tmp_data, data)
        os.replace(tmp_info, info)  # the meta file goes last: get_frame needs both
        _prune()
    except Exception:
        pass


def get_bytes(kind, key):
    try:
        path = _name(kind, key, ".bin")
        if not path.exists():
            return None
        path.touch()
        return path.read_bytes()
    except Exception:
        return None


def put_bytes(kind, key, content):
    try:
        path = _name(kind, key, ".bin")
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(content)
        os.replace(tmp, path)
        _prune()
    except Exception:
        pass

