"""Shared HTTP helpers for twdata (TWSE / TPEx / MOPS public data)."""
import json
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (research; contact: local)"}


def get_json(url, retries=3, pace=1.0):
    """GET a JSON endpoint. Returns the parsed object."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.build_opener().open(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
            if pace:
                time.sleep(pace)
            return data
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"GET JSON failed {url}: {last}")


def get_html(url, retries=3, pace=1.0):
    """GET an HTML page. Returns decoded text."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.build_opener().open(req, timeout=60) as r:
                text = r.read().decode("utf-8", errors="replace")
            if pace:
                time.sleep(pace)
            return text
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"GET HTML failed {url}: {last}")


def to_int(s):
    """'1,234,567 ' -> 1234567; '--'/'-' -> None."""
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace(" ", "")
    if s in ("", "-", "--", "－"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def to_float(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace(" ", "")
    if s in ("", "-", "--", "－"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_to_iso(roc):
    """ROC date '1151106' -> '2026-11-06'. Also accepts Western '20261106'."""
    s = str(roc).strip()
    if len(s) == 8 and s.startswith("20"):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) == 7:
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}"
    return s
