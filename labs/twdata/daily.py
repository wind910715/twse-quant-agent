"""Daily close quotes (latest trading day only) - TWSE + TPEx OpenAPI.

TWSE:  https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
TPEx:  https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes
Both return the latest trading day snapshot; neither accepts a date
parameter. For historical daily prices use yfinance (see revenue_spec).
"""
import pandas as pd

from twdata.client import get_json, roc_to_iso, to_float, to_int

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"


def daily_close_twse():
    rows = get_json(TWSE_URL, pace=0)
    out = []
    for r in rows:
        out.append({
            "stock_id": r["Code"].strip(), "name": r["Name"].strip(),
            "date": roc_to_iso(r["Date"]),
            "open": to_float(r["OpeningPrice"]), "high": to_float(r["HighestPrice"]),
            "low": to_float(r["LowestPrice"]), "close": to_float(r["ClosingPrice"]),
            "volume": to_int(r["TradeVolume"]), "amount": to_int(r["TradeValue"]),
            "market": "twse",
        })
    return pd.DataFrame(out)


def daily_close_tpex():
    rows = get_json(TPEX_URL, pace=0)
    out = []
    for r in rows:
        out.append({
            "stock_id": r["SecuritiesCompanyCode"].strip(),
            "name": r["CompanyName"].strip(),
            "date": roc_to_iso(r["Date"]),
            "open": to_float(r["Open"]), "high": to_float(r["High"]),
            "low": to_float(r["Low"]), "close": to_float(r["Close"]),
            "volume": to_int(r["TradingShares"]),
            "amount": to_int(r["TransactionAmount"]),
            "market": "tpex",
        })
    return pd.DataFrame(out)


def daily_close():
    """All TWSE + TPEx securities, latest trading day."""
    return pd.concat([daily_close_twse(), daily_close_tpex()],
                     ignore_index=True)
