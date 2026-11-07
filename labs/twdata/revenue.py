"""Monthly revenue.

History (2013-01 onward, both markets): MOPS t21sc03 pages, one per
month per market (see monthly_revenue). This is the source for backtests.

Latest month only (clean JSON, no history):
- TWSE listed:  /opendata/t187ap05_L  (revenue_latest_twse)
- TPEx OTC:     /mopsfin_t187ap05_O   (revenue_latest_tpex)
Both return the latest complete month. Verified 2026-11-07: TWSE 1088
rows / TPEx 893 rows for 2026-09, 2330 = 511,857,399,000, identical to
MOPS and FinMind (3 stocks differ by 1.8-9.2%, likely restatements:
6443, 4739, 4426 in 2026-09).

Values on all sources are in NT$ thousands; output revenue is in NT$
(value * 1000). Availability date = 1st of the month after the revenue
month (FinMind convention).
"""
import re

import pandas as pd
from bs4 import BeautifulSoup

from twdata.client import get_html, get_json

MOPS_BASE = "https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{roc}_{m}_0.html"
T187AP05_L_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
T187AP05_O_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"
CODE_RE = re.compile(r"^\d{4}$")
NUM_RE = re.compile(r"^[\d,.\-]+$")


def parse_page(html, year, month):
    """Parse one MOPS t21sc03 page -> list of revenue dicts."""
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all("td")]
        if len(cells) < 3 or not CODE_RE.match(cells[0]):
            continue
        val = cells[2].replace(",", "").strip()
        if not NUM_RE.match(val) or val in ("", "-", "--"):
            continue
        try:
            rev = float(val)
        except ValueError:
            continue
        if rev < 0:
            continue
        y, m = (year, month + 1) if month < 12 else (year + 1, 1)
        rows.append({"stock_id": cells[0],
                     "date": f"{y:04d}-{m:02d}-01",
                     "revenue_year": year, "revenue_month": month,
                     "revenue": int(round(rev * 1000))})
    seen, out = set(), []
    for r in rows:
        if r["stock_id"] not in seen:
            seen.add(r["stock_id"])
            out.append(r)
    return out


def monthly_revenue(year, month, market="both"):
    """Revenue for one month via MOPS. market: 'sii' (listed), 'otc', 'both'."""
    markets = ("sii", "otc") if market == "both" else (market,)
    frames = []
    for mkt in markets:
        url = MOPS_BASE.format(market=mkt, roc=year - 1911, m=month)
        rows = parse_page(get_html(url), year, month)
        frames.append(pd.DataFrame(rows))
    if not frames:
        return pd.DataFrame(columns=["stock_id", "date", "revenue_year",
                                     "revenue_month", "revenue"])
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates(["stock_id", "revenue_year", "revenue_month"])


def _parse_t187ap05(rows, market):
    """Parse a t187ap05_L / mopsfin_t187ap05_O JSON payload."""
    out = []
    for r in rows:
        code = (r.get("公司代號") or "").strip()
        if not code:
            continue
        ym = (r.get("資料年月") or "").strip()  # ROC '11509'
        if len(ym) != 5:
            continue
        year, month = int(ym[:3]) + 1911, int(ym[3:5])
        val = (r.get("營業收入-當月營收") or "").replace(",", "").strip()
        try:
            rev = float(val)
        except ValueError:
            continue
        if rev < 0:
            continue
        y, m = (year, month + 1) if month < 12 else (year + 1, 1)
        out.append({"stock_id": code, "date": f"{y:04d}-{m:02d}-01",
                    "revenue_year": year, "revenue_month": month,
                    "revenue": int(round(rev * 1000)), "market": market})
    return pd.DataFrame(out)


def revenue_latest_twse():
    """Latest month's revenue, listed companies (TWSE OpenAPI JSON)."""
    return _parse_t187ap05(get_json(T187AP05_L_URL, pace=0), "twse")


def revenue_latest_tpex():
    """Latest month's revenue, OTC companies (TPEx OpenAPI JSON)."""
    return _parse_t187ap05(get_json(T187AP05_O_URL, pace=0), "tpex")


def revenue_latest():
    """Latest month's revenue, TWSE + TPEx (OpenAPI JSON)."""
    return pd.concat([revenue_latest_twse(), revenue_latest_tpex()],
                     ignore_index=True)
