"""Monthly revenue via MOPS (historical, one page per month per market).

Source: https://mopsov.twse.com.tw/nas/t21/{sii|otc}/t21sc03_{ROCYYY}_{M}_0.html
This is the primary source FinMind itself uses. Values on the page are in
NT$ thousands; output revenue is in NT$ (value * 1000) to match the
FinMind format. Availability date follows the FinMind convention: 1st of
the month AFTER the revenue month.

Note: fetch_revenue_mops.py has its own copy of this parse logic because
it was written before twdata existed; reconcile the two later.
"""
import re

import pandas as pd
from bs4 import BeautifulSoup

from twdata.client import get_html

BASE = "https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{roc}_{m}_0.html"
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
    """Revenue for one month. market: 'sii' (listed), 'otc', or 'both'."""
    markets = ("sii", "otc") if market == "both" else (market,)
    frames = []
    for mkt in markets:
        url = BASE.format(market=mkt, roc=year - 1911, m=month)
        rows = parse_page(get_html(url), year, month)
        frames.append(pd.DataFrame(rows))
    if not frames:
        return pd.DataFrame(columns=["stock_id", "date", "revenue_year",
                                     "revenue_month", "revenue"])
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates(["stock_id", "revenue_year", "revenue_month"])
