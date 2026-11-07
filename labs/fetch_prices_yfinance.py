#!/usr/bin/env python3
"""Fetch daily prices via yfinance (bulk, no FinMind quota).

Why: FinMind free tier is 600 requests/hour with a token; one request per
stock means 1939 requests for prices alone. yfinance downloads many tickers
per call, so the whole universe costs ~20 calls. Cross-validated against
FinMind closes (see labs/revenue_spec_20261107.md).

Usage:
    python3 labs/fetch_prices_yfinance.py --universe labs/data_cache/universe.csv \
        --out labs/data_cache [--limit N] [--chunk 100]

Outputs (in --out):
    prices.csv          stock_id,date,open,high,low,close,volume,amount
                        (amount = close * volume, an approximation: yfinance
                        has no traded-value field)
    benchmark_0050.csv  date, close (0050.TW adjusted close, total return)
    prices_yf_log.json  counts, failures, elapsed

Tickers: twse -> "{sid}.TW", tpex -> "{sid}.TWO".
"""
import argparse
import json
import os
import time

import pandas as pd


def load_universe(path, limit=0):
    df = pd.read_csv(path, dtype={"stock_id": str})
    if limit:
        df = df.head(limit)
    return [(r.stock_id, r.type) for r in df.itertuples()]


def _ticker(sid, typ):
    return f"{sid}.{'TW' if typ == 'twse' else 'TWO'}"


def _frame_to_rows(ticker, sid, frame):
    """One ticker's OHLCV frame -> list of dict rows."""
    if frame is None or frame.empty:
        return []
    frame = frame[["Open", "High", "Low", "Close", "Volume"]].copy()
    frame = frame.dropna(subset=["Close"])
    frame = frame[frame["Close"] > 0]
    rows = []
    for date, r in frame.iterrows():
        close = float(r["Close"])
        vol = float(r["Volume"]) if pd.notna(r["Volume"]) else 0.0
        rows.append({"stock_id": sid, "date": date.strftime("%Y-%m-%d"),
                     "open": float(r["Open"]), "high": float(r["High"]),
                     "low": float(r["Low"]), "close": close,
                     "volume": vol, "amount": close * vol})
    return rows


def fetch_chunk(tickers, start, end):
    """tickers: list of (sid, ticker). Returns (rows, failed_sids)."""
    import yfinance as yf
    rows, failed = [], []
    syms = [t for _, t in tickers]
    try:
        data = yf.download(syms, start=start, end=end, group_by="ticker",
                           auto_adjust=False, threads=True, progress=False)
    except Exception as e:
        print(f"[yf] chunk download failed: {e}; falling back per-ticker",
              flush=True)
        data = None
    if data is not None and not data.empty:
        if isinstance(data.columns, pd.MultiIndex):
            for sid, sym in tickers:
                try:
                    rows += _frame_to_rows(sym, sid, data[sym])
                except Exception:
                    failed.append(sid)
        else:  # single-ticker chunk: flat columns
            sid, sym = tickers[0]
            try:
                rows += _frame_to_rows(sym, sid, data)
            except Exception:
                failed.append(sid)
        got = {r["stock_id"] for r in rows}
        for sid, _ in tickers:
            if sid not in got and sid not in failed:
                failed.append(sid)
        return rows, failed
    # Fallback: one Ticker at a time.
    for sid, sym in tickers:
        try:
            h = yf.Ticker(sym).history(start=start, end=end, auto_adjust=False)
            r = _frame_to_rows(sym, sid, h)
            if r:
                rows += r
            else:
                failed.append(sid)
        except Exception:
            failed.append(sid)
    return rows, failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="labs/data_cache/universe.csv")
    ap.add_argument("--out", default="labs/data_cache")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--chunk", type=int, default=100)
    ap.add_argument("--start", default="2014-06-02")
    ap.add_argument("--end", default="2026-11-01")
    args = ap.parse_args()
    t0 = time.time()
    uni = load_universe(args.universe, args.limit)
    parts_dir = os.path.join(args.out, "parts_yf")
    os.makedirs(parts_dir, exist_ok=True)
    # Resume: skip sids already in a part file.
    done = set()
    for fn in sorted(os.listdir(parts_dir)):
        if fn.startswith("prices_yf_") and fn.endswith(".csv"):
            try:
                done.update(pd.read_csv(os.path.join(parts_dir, fn),
                                        usecols=["stock_id"])["stock_id"].astype(str))
            except Exception:
                pass
    todo = [(s, t) for s, t in uni if s not in done]
    print(f"[yf] {len(todo)} to fetch ({len(done)} already done)", flush=True)
    failed, part_no = [], 0
    for i in range(0, len(todo), args.chunk):
        chunk = todo[i:i + args.chunk]
        syms = [(_ticker(s, t), s) for s, t in chunk]
        rows, bad = fetch_chunk([(s, sym) for sym, s in syms],
                                args.start, args.end)
        failed += bad
        if rows:
            part_no += 1
            pd.DataFrame(rows).to_csv(
                os.path.join(parts_dir, f"prices_yf_{part_no:03d}.csv"),
                index=False)
        print(f"[yf] {min(i + args.chunk, len(todo))}/{len(todo)} "
              f"elapsed {time.time() - t0:.0f}s failed {len(failed)}", flush=True)
    frames = []
    for fn in sorted(os.listdir(parts_dir)):
        if fn.startswith("prices_yf_") and fn.endswith(".csv"):
            frames.append(pd.read_csv(os.path.join(parts_dir, fn)))
    if frames:
        df = pd.concat(frames, ignore_index=True).sort_values(["stock_id", "date"])
        df.to_csv(os.path.join(args.out, "prices.csv"), index=False)
        print(f"[yf] prices.csv: {len(df)} rows, "
              f"{df['stock_id'].nunique()} stocks", flush=True)
    import yfinance as yf
    h = yf.Ticker("0050.TW").history(start="2015-01-01", end="2026-11-01",
                                     auto_adjust=True)
    bench = pd.DataFrame({"date": h.index.strftime("%Y-%m-%d"),
                          "close": h["Close"].values})
    bench.to_csv(os.path.join(args.out, "benchmark_0050.csv"), index=False)
    print(f"[yf] benchmark 0050 rows {len(bench)}", flush=True)
    log = {"started": time.strftime("%Y-%m-%d %H:%M:%S"),
           "universe": len(uni), "failed": failed,
           "elapsed_sec": round(time.time() - t0, 1)}
    with open(os.path.join(args.out, "prices_yf_log.json"), "w") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    print("[done]", json.dumps(log, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
