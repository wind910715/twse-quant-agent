"""Margin trading balances (latest trading day) - TWSE OpenAPI MI_MARGN.

https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN
Returns the latest trading day snapshot; no date parameter, and the
payload carries no date field (the file name / fetch date is the record).
TPEx margin endpoint is still to be confirmed (TPEx swagger download
fails from this VM; see README).
"""
import pandas as pd

from twdata.client import get_json, to_int

URL = "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"


def margin_balance():
    rows = get_json(URL, pace=0)
    out = []
    for r in rows:
        out.append({
            "stock_id": r["股票代號"].strip(), "name": r["股票名稱"].strip(),
            "margin_buy": to_int(r["融資買進"]), "margin_sell": to_int(r["融資賣出"]),
            "margin_balance": to_int(r["融資今日餘額"]),
            "margin_prev_balance": to_int(r["融資前日餘額"]),
            "short_buy": to_int(r["融券買進"]), "short_sell": to_int(r["融券賣出"]),
            "short_balance": to_int(r["融券今日餘額"]),
            "short_prev_balance": to_int(r["融券前日餘額"]),
            "market": "twse",
        })
    return pd.DataFrame(out)
