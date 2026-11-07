"""Real-time quotes (intraday snapshots).

Two exchange website backends, both free and keyless. Neither is an
official open API: both are the exchanges' own quote pages' backends, so
they can change or rate-limit without notice. The broker API (Yuanta
SPARK / President Futures) is the more reliable route; use these as the
fast, free path and the broker as backup.

- Stocks + indices: TWSE MIS, GET
  https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=<codes>&json=1&delay=0
  codes look like tse_2330.tw (listed), otc_5483.tw (OTC),
  tse_t00.tw (TAIEX), otc_o00.tw (OTC index). One call takes many codes
  separated by |. Returns the latest price, open/high/low, volume and
  the 5-level bid/ask (underscore-separated, best first).
- Futures: TAIFEX MIS, POST
  https://mis.taifex.com.tw/futures/api/getQuoteList
  body {"MarketType":"0","SymbolType":"F","KindID":"1","CID":"","ExpireMonth":""}
  KindID 1 = TAIEX futures (TXF-S spot + TXFK6 near month + ...).
  Returns last, bid/ask, volume, open interest, settlement and the
  update time (CTime). Verified 2026-11-07: TXFK6 Last 39786 matches
  the TAIFEX daily report exactly.

Both return the last quote when the market is closed. Live updating
during trading hours still needs a Monday (trading day) confirmation.
"""
import json
import re
import time
import urllib.error
import urllib.request

import pandas as pd

from twdata.client import to_float, to_int

class _PostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """The TAIFEX MIS sometimes 302s a POST to another host; urllib's
    default turns the follow-up into a GET, which the API rejects with
    405. Re-issue the redirect as a POST with the same body."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return urllib.request.Request(
            newurl, data=req.data, method="POST",
            headers={k: v for k, v in req.headers.items()
                     if k.lower() not in ("content-length", "host")})


MIS_URL = ("https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
           "?ex_ch={codes}&json=1&delay=0")
TAIFEX_MIS_URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"
UA = {"User-Agent": "Mozilla/5.0",
      "Referer": "https://mis.taifex.com.tw/futures/"}


def _mis_get(codes, tries=3):
    req = urllib.request.Request(MIS_URL.format(codes="|".join(codes)),
                                 headers={"User-Agent": UA["User-Agent"]})
    last = None
    for i in range(tries):
        try:
            with urllib.request.build_opener().open(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    # curl 備援（urllib 讀 chunked 偶爾 IncompleteRead）
    try:
        import subprocess
        out = subprocess.run(
            ["curl", "-sS", "-m", "20", "-A", UA["User-Agent"],
             MIS_URL.format(codes="|".join(codes))],
            capture_output=True, text=True, timeout=30).stdout
        return json.loads(out)
    except Exception:
        raise last


def _parse_mis_item(it):
    def best(side):
        s = (it.get(side) or "").strip().rstrip("_")
        if not s:
            return None
        try:
            return float(s.split("_")[0])
        except ValueError:
            return None
    tlong = it.get("tlong")
    try:
        tlong = int(tlong) if tlong else None
    except (TypeError, ValueError):
        tlong = None
    return {
        "stock_id": it.get("c"), "name": it.get("n"),
        "price": to_float(it.get("z")), "prev_close": to_float(it.get("y")),
        "open": to_float(it.get("o")), "high": to_float(it.get("h")),
        "low": to_float(it.get("l")), "volume": to_int(it.get("v")),
        "bid": best("b"), "ask": best("a"),
        "time": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tlong / 1000))
                 if tlong else None),
    }


def realtime_stocks(stock_ids):
    """Latest quotes for listed/OTC stocks. Market auto-detected
    (tries tse_ first, then otc_ for anything missing)."""
    ids = [str(s).strip() for s in stock_ids]
    out, seen = [], set()
    for prefix in ("tse_", "otc_"):
        todo = [s for s in ids if s not in seen]
        if not todo:
            break
        codes = [f"{prefix}{s}.tw" for s in todo]
        try:
            d = _mis_get(codes)
        except Exception:
            continue
        for it in d.get("msgArray", []):
            if it.get("c") and it.get("z"):
                out.append(_parse_mis_item(it))
                seen.add(it["c"])
        time.sleep(0.5)
    return pd.DataFrame(out)


def realtime_index():
    """TAIEX (t00) + OTC index (o00), latest quotes."""
    d = _mis_get(["tse_t00.tw", "otc_o00.tw"])
    rows = []
    for it in d.get("msgArray", []):
        r = _parse_mis_item(it)
        r["stock_id"] = {"t00": "TAIEX", "o00": "OTC"}.get(it.get("c"), it.get("c"))
        rows.append(r)
    return pd.DataFrame(rows)


def realtime_futures(kind_id="1"):
    """TAIFEX futures quotes for one KindID (1 = TAIEX futures).
    Returns one row per contract in that category."""
    body = json.dumps({"MarketType": "0", "SymbolType": "F",
                       "KindID": str(kind_id), "CID": "",
                       "ExpireMonth": ""}).encode("utf-8")
    req = urllib.request.Request(TAIFEX_MIS_URL, data=body, headers={
        "Content-Type": "application/json;charset=UTF-8",
        "Referer": UA["Referer"], "User-Agent": UA["User-Agent"],
        "Accept": "application/json, text/plain, */*"})
    opener = urllib.request.build_opener(_PostRedirectHandler())
    d, last = None, None
    for i in range(3):
        try:
            with opener.open(req, timeout=30) as r:
                d = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    if d is None:
        # curl 備援（urllib 讀 chunked 偶爾 IncompleteRead／斷線）
        try:
            import subprocess
            out = subprocess.run(
                ["curl", "-sS", "-m", "30", "-A", UA["User-Agent"], "-X", "POST",
                 "-H", "Content-Type: application/json;charset=UTF-8",
                 "-H", "Referer: " + UA["Referer"],
                 "-H", "Accept: application/json, text/plain, */*",
                 "-d", body.decode("utf-8"), TAIFEX_MIS_URL],
                capture_output=True, text=True, timeout=40).stdout
            d = json.loads(out)
        except Exception:
            raise last
    if d.get("RtCode") != "0":
        raise RuntimeError(f"TAIFEX MIS RtCode={d.get('RtCode')}: {d.get('RtMsg')}")
    rows = []
    for it in d.get("RtData", {}).get("QuoteList", []):
        rows.append({
            "symbol_id": it.get("SymbolID"), "name": it.get("DispCName"),
            "code": it.get("DispEName"), "status": it.get("Status"),
            "last": to_float(it.get("CLastPrice")),
            "bid": to_float(it.get("CBidPrice1")),
            "ask": to_float(it.get("CAskPrice1")),
            "volume": to_int(it.get("CTotalVolume")),
            "open": to_float(it.get("COpenPrice")),
            "high": to_float(it.get("CHighPrice")),
            "low": to_float(it.get("CLowPrice")),
            "ref_price": to_float(it.get("CRefPrice")),
            "settlement": to_float(it.get("SettlementPrice")),
            "open_interest": to_int(it.get("OpenInterest")),
            "change": to_float(it.get("CDiff")),
            "change_pct": to_float(it.get("CDiffRate")),
            "date": it.get("CDate"), "time": it.get("CTime"),
        })
    return pd.DataFrame(rows)


def realtime_dashboard():
    """The three dashboard instruments in one dict: TAIEX, OTC index,
    and the near-month TAIEX future (nearest TXF expiry, picked live -
    never a hardcoded month code, so the December roll needs no edit)."""
    idx = realtime_index()
    fut = realtime_futures("1")
    tx = _front_month_tx(fut)
    out = {}
    for _, r in idx.iterrows():
        out[r["stock_id"]] = {"price": r["price"],
                              "prev_close": r["prev_close"],
                              "change_pct": (None if not r["price"] or not r["prev_close"]
                                             else round((r["price"] / r["prev_close"] - 1) * 100, 2)),
                              "time": r["time"]}
    if tx is not None:
        r = tx
        out["TX"] = {"price": r["last"], "bid": r["bid"], "ask": r["ask"],
                     "prev_close": r["ref_price"],
                     "change_pct": r["change_pct"],
                     "open_interest": r["open_interest"],
                     "settlement": r["settlement"],
                     "time": r["time"]}
    return out


# Futures month codes: A=Jan ... L=Dec. SymbolID looks like TXFK6-F
# (K = November, 6 = 2026). The near month changes at each settlement,
# so pick it from the live list instead of hardcoding it.
_MONTH_CODE = {c: i + 1 for i, c in enumerate("ABCDEFGHIJKL")}
_TXF_RE = re.compile(r"^TXF([A-L])([0-9])-F$")


def _front_month_tx(fut):
    """Nearest-expiry TXF row from a realtime_futures frame (or None)."""
    best, best_key = None, None
    for _, r in fut.iterrows():
        m = _TXF_RE.match(str(r.get("symbol_id", "")))
        if not m:
            continue
        key = (int(m.group(2)), _MONTH_CODE[m.group(1)])
        if best_key is None or key < best_key:
            best, best_key = r, key
    return best


def realtime_quotes(stock_ids=()):
    """One uniform live frame: TAIEX + OTC index + front-month TXF,
    plus any stocks named (e.g. ['2330', '5483']). The 'everything live'
    call - one row per instrument, same columns, ready for the CLI or
    the dashboard to read."""
    rows = []
    for _, r in realtime_index().iterrows():
        rows.append({
            "symbol": r["stock_id"], "name": r["name"],
            "price": r["price"], "prev_close": r["prev_close"],
            "change_pct": (None if not r["price"] or not r["prev_close"]
                           else round((r["price"] / r["prev_close"] - 1) * 100, 2)),
            "open": r["open"], "high": r["high"], "low": r["low"],
            "volume": r["volume"], "bid": r["bid"], "ask": r["ask"],
            "time": r["time"], "source": "twse_mis",
        })
    tx = _front_month_tx(realtime_futures("1"))
    if tx is not None:
        rows.append({
            "symbol": tx["symbol_id"], "name": tx["name"],
            "price": tx["last"], "prev_close": tx["ref_price"],
            "change_pct": tx["change_pct"],
            "open": tx["open"], "high": tx["high"], "low": tx["low"],
            "volume": tx["volume"], "bid": tx["bid"], "ask": tx["ask"],
            "time": tx["time"], "source": "taifex_mis",
        })
    if stock_ids:
        for _, r in realtime_stocks(stock_ids).iterrows():
            rows.append({
                "symbol": r["stock_id"], "name": r["name"],
                "price": r["price"], "prev_close": r["prev_close"],
                "change_pct": (None if not r["price"] or not r["prev_close"]
                               else round((r["price"] / r["prev_close"] - 1) * 100, 2)),
                "open": r["open"], "high": r["high"], "low": r["low"],
                "volume": r["volume"], "bid": r["bid"], "ask": r["ask"],
                "time": r["time"], "source": "twse_mis",
            })
    return pd.DataFrame(rows)
