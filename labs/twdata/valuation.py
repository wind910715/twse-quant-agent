"""Valuation (P/E, P/B, dividend yield, latest trading day) - TWSE BWIBBU_d.

https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_d
(The no-suffix BWIBBU path returns an error page; BWIBBU_ALL is the
per-code query, BWIBBU_d is the per-date query - this one.)
Date in the payload is Western 'YYYYMMDD'. P/E is null for loss makers.
"""
import pandas as pd

from twdata.client import get_json, roc_to_iso, to_float

URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_d"


def valuation():
    rows = get_json(URL, pace=0)
    out = []
    for r in rows:
        out.append({
            "stock_id": r["Code"].strip(), "name": r["Name"].strip(),
            "date": roc_to_iso(r["Date"]),
            "close": to_float(r["ClosePrice"]),
            "dividend_yield": to_float(r["DividendYield"]),
            "pe": to_float(r["PEratio"]), "pb": to_float(r["PBratio"]),
            "market": "twse",
        })
    return pd.DataFrame(out)
