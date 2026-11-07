"""Shared HTTP helpers for twdata (TWSE / TPEx / MOPS public data).

TPEx's large JSON responses often arrive truncated over plain urllib
(IncompleteRead / RemoteDisconnected - the server's Content-Length is
unreliable). curl fetches the same URLs cleanly, so get_json falls back
to curl after urllib fails. Retries are built in either way.
"""
import json
import subprocess
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (research; contact: local)"}


def _curl_get(url, timeout=60):
    """Fetch a URL with curl. Returns decoded text. Raises on failure."""
    out = subprocess.run(
        ["curl", "-sS", "-m", str(timeout), "-A", UA["User-Agent"], url],
        capture_output=True, text=True, timeout=timeout + 10)
    if out.returncode != 0:
        raise RuntimeError(f"curl failed {url}: {out.stderr.strip()[:200]}")
    return out.stdout


def get_json(url, retries=3, pace=1.0):
    """GET a JSON endpoint. urllib first, curl fallback on transport errors."""
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
            try:
                data = json.loads(_curl_get(url))
                if pace:
                    time.sleep(pace)
                return data
            except Exception as e2:
                last = e2
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
