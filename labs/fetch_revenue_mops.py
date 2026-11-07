#!/usr/bin/env python3
"""Fetch monthly revenue from MOPS (public disclosure, no key, no quota).

Source: https://mopsov.twse.com.tw/nas/t21/{sii|otc}/t21sc03_{ROCYYY}_{M}_0.html
One page per month per market, ~1000 companies each. This replaces the
FinMind revenue fetch (free tier 600 requests/hour made the 1939-stock
fetch span 3+ hours). MOPS is the primary source FinMind itself uses.

Values are in NT$ thousands on the page; output revenue is in NT$
(value * 1000) to match the FinMind format. Availability date follows the
FinMind convention: 1st of the month AFTER the revenue month.

Usage:
    python3 labs/fetch_revenue_mops.py --out labs/data_cache
        [--start 2013-01] [--end 2026-10] [--pace 1.0] [--limit-months N]

Outputs (in --out):
    revenue.csv       stock_id,date,revenue_year,revenue_month,revenue
    mops_log.json     counts, failed months, cross-check vs FinMind (if any)
    parts_mops/       per-month checkpoint files (resume skips done months)
"""
import argparse
import json
import os
import re
import time
import urllib.request
import urllib.error
from datetime import date

import pandas as pd
from bs4 import BeautifulSoup

BASE = "https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{roc}_{m}_0.html"
CODE_RE = re.compile(r"^\d{4}$")
NUM_RE = re.compile(r"^[\d,.\-]+$")


def fetch_page(url, retries=3):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (research; contact: local)"})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.build_opener().open(req, timeout=60) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def parse_page(html, year, month):
    """Return list of (stock_id, revenue_ntd) for one month."""
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
        rows.append({"stock_id": cells[0], "date": _avail_date(year, month),
                     "revenue_year": year, "revenue_month": month,
                     "revenue": int(round(rev * 1000))})
    # Dedupe by stock (a code should appear once, but be safe).
    seen, out = set(), []
    for r in rows:
        if r["stock_id"] not in seen:
            seen.add(r["stock_id"])
            out.append(r)
    return out


def _avail_date(year, month):
    y, m = (year, month + 1) if month < 12 else (year + 1, 1)
    return date(y, m, 1).isoformat()


def _months(start, end):
    y, m = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    while (y, m) <= (ey, em):
        yield y, m
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="labs/data_cache")
    ap.add_argument("--start", default="2013-01")
    ap.add_argument("--end", default="2026-10")
    ap.add_argument("--pace", type=float, default=1.0)
    ap.add_argument("--limit-months", type=int, default=0)
    args = ap.parse_args()
    parts_dir = os.path.join(args.out, "parts_mops")
    os.makedirs(parts_dir, exist_ok=True)
    t0 = time.time()
    months = list(_months(args.start, args.end))
    if args.limit_months:
        months = months[-args.limit_months:]
    done = set()
    for fn in sorted(os.listdir(parts_dir)):
        if fn.startswith("mops_") and fn.endswith(".csv"):
            done.add(fn[5:-4])  # mops_{market}_{YYYYMM}
    failed, total_rows = [], 0
    for (y, m) in months:
        for market in ("sii", "otc"):
            key = f"{market}_{y}{m:02d}"
            if key in done:
                continue
            url = BASE.format(market=market, roc=y - 1911, m=m)
            try:
                rows = parse_page(fetch_page(url), y, m)
            except Exception as e:
                print(f"[mops] FAIL {key}: {e}", flush=True)
                failed.append(key)
                time.sleep(args.pace)
                continue
            if not rows:
                print(f"[mops] EMPTY {key} (no company rows)", flush=True)
                failed.append(key)
                time.sleep(args.pace)
                continue
            pd.DataFrame(rows).to_csv(
                os.path.join(parts_dir, f"mops_{key}.csv"), index=False)
            total_rows += len(rows)
            print(f"[mops] {key}: {len(rows)} rows", flush=True)
            time.sleep(args.pace)
    frames = []
    for fn in sorted(os.listdir(parts_dir)):
        if fn.startswith("mops_") and fn.endswith(".csv"):
            frames.append(pd.read_csv(os.path.join(parts_dir, fn),
                                      dtype={"stock_id": str}))
    if frames:
        df = pd.concat(frames, ignore_index=True).drop_duplicates(
            ["stock_id", "revenue_year", "revenue_month"])
        df = df.sort_values(["stock_id", "revenue_year", "revenue_month"])
        df.to_csv(os.path.join(args.out, "revenue.csv"), index=False)
        print(f"[mops] revenue.csv: {len(df)} rows, "
              f"{df['stock_id'].nunique()} stocks", flush=True)
    log = {"started": time.strftime("%Y-%m-%d %H:%M:%S"),
           "months": len(months), "rows": total_rows,
           "failed_months": failed, "elapsed_sec": round(time.time() - t0, 1)}
    # Cross-check vs FinMind parts if present.
    fm_parts = os.path.join(args.out, "parts")
    if os.path.isdir(fm_parts) and frames:
        try:
            fm = pd.concat(
                [pd.read_csv(os.path.join(fm_parts, fn), dtype={"stock_id": str})
                 for fn in sorted(os.listdir(fm_parts))
                 if fn.startswith("revenue_") and fn.endswith(".csv")],
                ignore_index=True)
            m = df.merge(fm, on=["stock_id", "revenue_year", "revenue_month"],
                         suffixes=("_mops", "_fm"))
            m = m[m["revenue_fm"] > 0]
            if len(m):
                diff = (m["revenue_mops"] / m["revenue_fm"] - 1).abs() * 100
                log["crosscheck_vs_finmind"] = {
                    "overlap_rows": int(len(m)),
                    "median_abs_diff_pct": round(float(diff.median()), 4),
                    "pct_within_0.1pct": round(float((diff <= 0.1).mean() * 100), 2)}
                print("[mops] cross-check vs FinMind:",
                      log["crosscheck_vs_finmind"], flush=True)
        except Exception as e:
            print(f"[mops] cross-check skipped: {e}", flush=True)
    with open(os.path.join(args.out, "mops_log.json"), "w") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    print("[done]", json.dumps(log, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
