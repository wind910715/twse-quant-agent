"""twdata: tiny client for the Taiwan public market data we actually use.

Sources (all free, no key, no registration):
- TWSE OpenAPI (openapi.twse.com.tw): daily close, margin, valuation,
  latest-month revenue (latest trading day / latest month only)
- TPEx OpenAPI (tpex.org.tw/openapi): daily close, margin, institutional,
  latest-month revenue (latest trading day / latest month only)
- MOPS (mopsov.twse.com.tw): monthly revenue (historical, one page/month)
- TWSE website JSON (www.twse.com.tw/fund): institutional buy/sell
  (historical, by date)

See README.md for coverage, limits and examples.
"""
from twdata.daily import daily_close, daily_close_twse, daily_close_tpex
from twdata.revenue import (monthly_revenue, revenue_latest,
                            revenue_latest_tpex, revenue_latest_twse)
from twdata.margin import (margin_balance, margin_balance_tpex,
                           margin_balance_twse)
from twdata.valuation import valuation
from twdata.institutional import (foreign, institutional, institutional_tpex,
                                  trust)

__all__ = [
    "daily_close", "daily_close_twse", "daily_close_tpex",
    "monthly_revenue", "revenue_latest", "revenue_latest_twse",
    "revenue_latest_tpex",
    "margin_balance", "margin_balance_twse", "margin_balance_tpex",
    "valuation", "foreign", "trust", "institutional", "institutional_tpex",
]
