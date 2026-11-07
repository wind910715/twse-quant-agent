"""twdata: tiny client for the Taiwan public market data we actually use.

Sources (all free, no key, no registration):
- TWSE OpenAPI (openapi.twse.com.tw): daily close, margin, valuation
  (latest trading day only, no date parameter)
- TPEx OpenAPI (tpex.org.tw/openapi): daily close (latest trading day only)
- MOPS (mopsov.twse.com.tw): monthly revenue (historical, one page/month/market)
- TWSE website JSON (www.twse.com.tw/fund): institutional buy/sell
  (historical, by date)

See README.md for coverage, limits and examples.
"""
from twdata.daily import daily_close, daily_close_twse, daily_close_tpex
from twdata.revenue import monthly_revenue
from twdata.margin import margin_balance
from twdata.valuation import valuation
from twdata.institutional import foreign, trust, institutional

__all__ = [
    "daily_close", "daily_close_twse", "daily_close_tpex",
    "monthly_revenue", "margin_balance", "valuation",
    "foreign", "trust", "institutional",
]
