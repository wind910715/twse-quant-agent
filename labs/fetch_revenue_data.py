"""Fetch FinMind monthly revenue + daily prices for the revenue-strategy backtests.

Usage (local, surrogate auth via custom.finmind):
    python3 labs/fetch_revenue_data.py --out labs/data_cache [--limit N] [--resume]
Usage (GitHub Actions, token auth):
    FINMIND_TOKEN=xxx python3 labs/fetch_revenue_data.py --out labs/data_cache

Outputs (in --out):
    universe.csv      stock_id, industry_category, type
    revenue.csv       stock_id, date, revenue_year, revenue_month, revenue
    prices.csv        stock_id, date, open, high, low, close, volume, amount
    benchmark_0050.csv  date, close (0050.TW adjusted close, total return)
    fetch_log.json    counts, failures, elapsed

Design notes (frozen 2026-11-07):
- Universe = TaiwanStockInfo snapshot, type twse/tpex, 4-digit numeric IDs only,
  excluding financials (金融保險/金融業), ETF/ETN/beneficiary securities.
  Delisted stocks are NOT in the snapshot: disclosed survivorship limitation.
- Revenue range 2013-01-01..2026-10-31 (24m SURGE lookback + 12m windows).
- Prices range 2014-06-02..2026-10-31 (MA120 warmup before 2015-02-11 start).
- Polite pacing + retries; checkpoint parts every 100 stocks; --resume skips
  stock_ids already present in parts.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

import pandas as pd

BASE = "https://api.finmindtrade.com/api/v4/data"
TOKEN = os.environ.get("FINMIND_TOKEN", "").strip()

try:
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    from dynamic_credentials import add_surrogate_to_request, read_json_response
    HAVE_SURROGATE = True
except Exception:
    HAVE_SURROGATE = False


def _get_json(url):
    req = urllib.request.Request(url)
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    elif HAVE_SURROGATE:
        add_surrogate_to_request(req, "custom.finmind",
                                 allowed_hosts=["api.finmindtrade.com"])
    else:
        raise RuntimeError("no FINMIND_TOKEN and no surrogate available")
    with urllib.request.build_opener().open(req, timeout=60) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def fetch(dataset, data_id, start, end, retries=3):
    q = {"dataset": dataset, "start_date": start, "end_date": end}
    if data_id:
        q["data_id"] = data_id
    url = BASE + "?" + urllib.parse.urlencode(q)
    last_err = None
    for attempt in range(retries):
        try:
            d = _get_json(url)
            if d.get("status") != 200:
                raise RuntimeError(f"status {d.get('status')}: {d.get('msg')}")
            return d.get("data", [])
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            time.sleep(10 * (attempt + 1))
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"fetch failed {dataset}/{data_id}: {last_err}")


def fetch_universe(out_dir):
    rows = fetch("TaiwanStockInfo", None, "2026-11-06", "2026-11-07")
    seen = {}
    for r in rows:
        sid = str(r.get("stock_id", "")).strip()
        if len(sid) != 4 or not sid.isdigit():
            continue
        if r.get("type") not in ("twse", "tpex"):
            continue
        ind = r.get("industry_category", "")
        if ind in ("金融保險", "金融業", "ETF", "ETN", "上櫃ETF",
                   "受益證券", "指數投資證券(ETN)", "創新板股票"):
            continue
        seen.setdefault(sid, {"stock_id": sid,
                              "industry_category": ind,
                              "type": r.get("type")})
    df = pd.DataFrame(sorted(seen.values(), key=lambda x: x["stock_id"]))
    df.to_csv(os.path.join(out_dir, "universe.csv"), index=False)
    print(f"[universe] {len(df)} stocks", flush=True)
    return df["stock_id"].tolist()


def _done_ids(parts_dir, prefix):
    done = set()
    if not os.path.isdir(parts_dir):
        return done
    for fn in sorted(os.listdir(parts_dir)):
        if fn.startswith(prefix) and fn.endswith(".csv"):
            try:
                part = pd.read_csv(os.path.join(parts_dir, fn), usecols=["stock_id"])
                done.update(part["stock_id"].astype(str).tolist())
            except Exception:
                pass
    return done


def fetch_series(stock_ids, dataset, start, end, parts_dir, prefix, colmap, pace=0.15):
    done = _done_ids(parts_dir, prefix)
    todo = [s for s in stock_ids if s not in done]
    print(f"[{prefix}] {len(todo)} to fetch ({len(done)} already done)", flush=True)
    buf, part_no, failed = [], 0, []
    t0 = time.time()
    for i, sid in enumerate(todo):
        try:
            rows = fetch(dataset, sid, start, end)
            for r in rows:
                row = {"stock_id": sid}
                for src, dst in colmap.items():
                    row[dst] = r.get(src)
                buf.append(row)
        except Exception as e:
            failed.append(sid)
            print(f"[{prefix}] FAIL {sid}: {e}", flush=True)
        if (i + 1) % 100 == 0 or i == len(todo) - 1:
            if buf:
                part_no += 1
                pd.DataFrame(buf).to_csv(
                    os.path.join(parts_dir, f"{prefix}_{part_no:03d}.csv"), index=False)
                buf = []
            el = time.time() - t0
            print(f"[{prefix}] {i + 1}/{len(todo)} elapsed {el:.0f}s failed {len(failed)}",
                  flush=True)
        time.sleep(pace)
    if buf:
        part_no += 1
        pd.DataFrame(buf).to_csv(
            os.path.join(parts_dir, f"{prefix}_{part_no:03d}.csv"), index=False)
    return failed


def combine(parts_dir, prefix, out_path):
    frames = []
    for fn in sorted(os.listdir(parts_dir)):
        if fn.startswith(prefix) and fn.endswith(".csv"):
            frames.append(pd.read_csv(os.path.join(parts_dir, fn)))
    if not frames:
        raise RuntimeError(f"no parts for {prefix}")
    df = pd.concat(frames, ignore_index=True)
    df.to_csv(out_path, index=False)
    print(f"[combine] {prefix}: {len(df)} rows -> {out_path}", flush=True)
    return df


def fetch_benchmark(out_dir):
    import yfinance as yf
    t = yf.Ticker("0050.TW")
    h = t.history(start="2015-01-01", end="2026-11-01", auto_adjust=True)
    df = pd.DataFrame({"date": h.index.strftime("%Y-%m-%d"), "close": h["Close"].values})
    df.to_csv(os.path.join(out_dir, "benchmark_0050.csv"), index=False)
    print(f"[benchmark] 0050 rows {len(df)}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="labs/data_cache")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pace", type=float, default=0.15)
    args = ap.parse_args()
    out_dir = args.out
    parts_dir = os.path.join(out_dir, "parts")
    os.makedirs(parts_dir, exist_ok=True)
    t0 = time.time()
    log = {"started": time.strftime("%Y-%m-%d %H:%M:%S")}
    ids = fetch_universe(out_dir)
    if args.limit:
        ids = ids[: args.limit]
        print(f"[limit] testing with first {args.limit} stocks", flush=True)
    failed_rev = fetch_series(ids, "TaiwanStockMonthRevenue", "2013-01-01",
                              "2026-10-31", parts_dir, "revenue",
                              {"date": "date", "revenue_year": "revenue_year",
                               "revenue_month": "revenue_month",
                               "revenue": "revenue"}, pace=args.pace)
    failed_px = fetch_series(ids, "TaiwanStockPrice", "2014-06-02",
                             "2026-10-31", parts_dir, "prices",
                             {"date": "date", "open": "open", "max": "high",
                              "min": "low", "close": "close",
                              "Trading_Volume": "volume",
                              "Trading_money": "amount"}, pace=args.pace)
    combine(parts_dir, "revenue", os.path.join(out_dir, "revenue.csv"))
    combine(parts_dir, "prices", os.path.join(out_dir, "prices.csv"))
    fetch_benchmark(out_dir)
    log.update({"universe": len(ids), "failed_revenue": failed_rev,
                "failed_prices": failed_px,
                "elapsed_sec": round(time.time() - t0, 1)})
    with open(os.path.join(out_dir, "fetch_log.json"), "w") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    print("[done]", json.dumps(log, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
