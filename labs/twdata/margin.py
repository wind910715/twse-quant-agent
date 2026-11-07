"""Margin trading balances (latest trading day).

TWSE: https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN
TPEx: https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance
Both return the latest trading day snapshot; no date parameter. The TWSE
payload carries no date field, so the combined frame tags TWSE rows with
the TPEx date (both are the same latest trading day).
"""
import pandas as pd

from twdata.client import get_json, roc_to_iso, to_int

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance"


def margin_balance_twse():
    rows = get_json(TWSE_URL, pace=0)
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


def margin_balance_tpex():
    rows = get_json(TPEX_URL, pace=0)
    out = []
    for r in rows:
        out.append({
            "stock_id": r["SecuritiesCompanyCode"].strip(),
            "name": r["CompanyName"].strip(),
            "date": roc_to_iso(r["Date"]),
            "margin_buy": to_int(r["MarginPurchase"]),
            "margin_sell": to_int(r["MarginSales"]),
            "margin_balance": to_int(r["MarginPurchaseBalance"]),
            "margin_prev_balance": to_int(r["MarginPurchaseBalancePreviousDay"]),
            "short_buy": to_int(r["ShortSale"]),
            "short_sell": to_int(r["ShortConvering"]),
            "short_balance": to_int(r["ShortSaleBalance"]),
            "short_prev_balance": to_int(r["ShortSaleBalancePreviousDay"]),
            "market": "tpex",
        })
    return pd.DataFrame(out)


def margin_balance():
    """TWSE + TPEx margin balances, latest trading day."""
    tw = margin_balance_twse()
    tp = margin_balance_tpex()
    date = tp["date"].iloc[0] if len(tp) else None
    tw["date"] = date
    return pd.concat([tw, tp], ignore_index=True)
