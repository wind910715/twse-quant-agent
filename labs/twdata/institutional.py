"""Institutional buy/sell (historical, by date) - TWSE website JSON.

The TWSE OpenAPI does NOT expose daily institutional buy/sell, so this
module uses the TWSE website's public JSON endpoints (same data, date
parameter works for history):
- foreign: 外資及陸資買賣超彙總表  TWT38U (all TWSE stocks, one row each)
  Columns 3-5 are 外資 buy/sell/net (shares). Columns 6-8 are always 0
  and columns 9-11 repeat group 1 (合計); both are ignored.
- trust:   投信買賣超彙總表        TWT44U (only stocks 投信 traded that day;
  missing stocks mean 0). This is the one strategy D needs.
- dealer (自營商, TWT43U) is NOT wrapped: it returns one row per dealer
  per stock (18k rows), not a per-stock summary.

All nets are in shares. 投信 15 張 = 15,000 shares.
"""
import pandas as pd

from twdata.client import get_json, to_int

FOREIGN_URL = "https://www.twse.com.tw/fund/TWT38U?response=json&date={date}"
TRUST_URL = "https://www.twse.com.tw/fund/TWT44U?response=json&date={date}"


def _iso(yyyymmdd):
    s = str(yyyymmdd)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def foreign(date):
    """外資及陸資買賣超. date: 'YYYYMMDD'."""
    d = get_json(FOREIGN_URL.format(date=date), pace=0)
    out = []
    for row in d.get("data", []):
        out.append({"stock_id": row[1].strip(), "date": _iso(date),
                    "foreign_buy": to_int(row[3]),
                    "foreign_sell": to_int(row[4]),
                    "foreign_net": to_int(row[5]),
                    "market": "twse"})
    return pd.DataFrame(out)


def trust(date):
    """投信買賣超. date: 'YYYYMMDD'. Only stocks 投信 traded; missing = 0."""
    d = get_json(TRUST_URL.format(date=date), pace=0)
    out = []
    for row in d.get("data", []):
        out.append({"stock_id": row[1].strip(), "date": _iso(date),
                    "trust_buy": to_int(row[3]),
                    "trust_sell": to_int(row[4]),
                    "trust_net": to_int(row[5]),
                    "market": "twse"})
    return pd.DataFrame(out)


def institutional(date):
    """外資 + 投信 merged per stock (missing side = 0). date: 'YYYYMMDD'."""
    f = foreign(date)
    t = trust(date)
    m = f.merge(t, on=["stock_id", "date", "market"], how="outer")
    for c in ("foreign_buy", "foreign_sell", "foreign_net",
              "trust_buy", "trust_sell", "trust_net"):
        m[c] = m[c].fillna(0).astype(int)
    return m
