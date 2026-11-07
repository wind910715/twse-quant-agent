"""Revenue-strategy backtests (frozen spec labs/revenue_spec_20261107.md).

Strategies: A = monthly-revenue momentum + new-high priority (MA filter),
             B = monthly-revenue SURGE top-30, D = investment-trust + revenue
             (D not implemented yet: needs institutional data fetch).
Shared engine: monthly rebalance on the 11th (rolled to next trading day),
signals from data available before the rebalance close, execution at close,
costs buy 0.1425% / sell 0.4425%, benchmark 0050 total return.

Usage:
    python3 labs/revenue_backtest.py --strategy A --data labs/data_cache \
        --out labs/results/revenue_A_20261107.json [--grid]
"""
import argparse
import json
import os

import numpy as np
import pandas as pd

BUY_COST = 0.001425
SELL_COST = 0.004425
START = "2015-02-11"
END = "2026-10-30"


def load_data(data_dir):
    rev = pd.read_csv(os.path.join(data_dir, "revenue.csv"))
    px = pd.read_csv(os.path.join(data_dir, "prices.csv"))
    bench = pd.read_csv(os.path.join(data_dir, "benchmark_0050.csv"))
    rev["date"] = pd.to_datetime(rev["date"])
    px["date"] = pd.to_datetime(px["date"])
    bench["date"] = pd.to_datetime(bench["date"])
    # Index by availability date (FinMind 'date' = 1st of the month AFTER the
    # revenue month). Using the revenue month itself would leak next month's
    # revenue into the rebalance: on the 11th of M only revenue through M-1
    # is announced, and its availability date (1st of M) is <= the 11th.
    rev_m = rev.pivot_table(index="date", columns="stock_id",
                            values="revenue", aggfunc="first").sort_index()
    px = px[["date", "stock_id", "close", "volume", "amount"]]
    close_m = px.pivot_table(index="date", columns="stock_id",
                             values="close", aggfunc="first").sort_index()
    close_m = close_m.where(close_m > 0)
    amt_m = px.pivot_table(index="date", columns="stock_id",
                           values="amount", aggfunc="first").sort_index()
    bench = bench.set_index("date")["close"].sort_index()
    return rev_m, close_m, amt_m, bench


def build_indicators(rev_m, close_m, amt_m):
    """Precompute all indicator matrices once (reused by every grid run)."""
    ind = {}
    ind["ma20"] = close_m.rolling(20).mean()
    ind["ma60"] = close_m.rolling(60).mean()
    ind["ma120"] = close_m.rolling(120).mean()
    ind["ret5"] = close_m / close_m.shift(5) - 1
    ind["amt20"] = amt_m.rolling(20).mean()
    for n in (3, 6, 12, 24):
        ind[f"rev_mean_{n}"] = rev_m.rolling(n).mean()
        ind[f"rev_max_{n}"] = rev_m.rolling(n).max()
    yoy = rev_m - rev_m.shift(12)
    for lb in (12, 24, 36):
        mu = yoy.shift(1).rolling(lb).mean()
        sd = yoy.shift(1).rolling(lb).std(ddof=1)
        ind[f"surge_{lb}"] = (yoy - mu) / sd
    return ind


def rebalance_dates(cal, start=START, end=END):
    """11th of each month in [start, end], rolled forward to next trading day."""
    out = []
    months = pd.period_range(pd.Period(start[:7]), pd.Period(end[:7]), freq="M")
    cal = cal[(cal >= pd.Timestamp(start)) & (cal <= pd.Timestamp(end))]
    for per in months:
        target = pd.Timestamp(per.year, per.month, 11)
        nxt = cal[cal >= target]
        if len(nxt):
            out.append(nxt[0])
    return pd.DatetimeIndex(out)


def _signal_day(close_m, rdate):
    cal = close_m.index
    pos = cal.get_indexer([rdate], method="pad")[0]
    return cal[pos - 1] if pos >= 1 else None


def _rev_row(rev_m, rdate):
    avail = rev_m.index[rev_m.index <= rdate]
    return avail[-1] if len(avail) else None


def signals_A(ind, close_m, rev_m, rdate, top_n=10, rev_win=12):
    sday = _signal_day(close_m, rdate)
    rrow = _rev_row(rev_m, rdate)
    if sday is None or rrow is None:
        return []
    last = close_m.loc[sday]
    # Candidate: near-3-month mean revenue > near-N-month mean revenue
    # (N = rev_win, base 12). New-high window is fixed at 12 months.
    cond = (last >= ind["ma20"].loc[sday]) & (last >= ind["ma60"].loc[sday]) \
        & (last >= ind["ma120"].loc[sday]) \
        & (ind["rev_mean_3"].loc[rrow] > ind[f"rev_mean_{rev_win}"].loc[rrow])
    cand = cond[cond].index.tolist()
    if not cand:
        return []
    high = (rev_m.loc[rrow] == ind["rev_max_12"].loc[rrow]).reindex(cand)
    score = pd.DataFrame({"high": high.fillna(False).astype(int),
                          "ret5": ind["ret5"].loc[sday].reindex(cand)})
    score = score.dropna().sort_values(["high", "ret5"], ascending=[False, False])
    return score.index.tolist()[:top_n]


def signals_B(ind, close_m, rev_m, rdate, top_n=30, lookback=24):
    sday = _signal_day(close_m, rdate)
    rrow = _rev_row(rev_m, rdate)
    if sday is None or rrow is None:
        return []
    surge = ind[f"surge_{lookback}"].loc[rrow].replace([np.inf, -np.inf], np.nan)
    amt20 = ind["amt20"].loc[sday]
    ok = amt20.reindex(surge.index) > 20_000_000
    surge = surge[ok.fillna(False)].dropna()
    return surge.sort_values(ascending=False).index.tolist()[:top_n]


def run_portfolio(close_m, bench, reb_dates, pick_fn, use_stop, slot_n):
    """Daily simulation. Full reconstitution each rebalance; stop only mid-month.

    pick_fn(rdate) -> ranked stock list. Each pick gets 1/slot_n of NAV;
    unfilled slots stay in cash (frozen spec, matches main-system slot rule).
    Returns (nav Series, trades list).
    """
    cal = close_m.index[(close_m.index >= reb_dates[0]) & (close_m.index <= pd.Timestamp(END))]
    close_f = close_m.ffill()
    reb_set = set(reb_dates)
    cash, holdings, trades, nav = 1.0, {}, [], {}
    for d in cal:
        px = close_m.loc[d]
        if d in reb_set:
            picks = pick_fn(d)
            # Sell everything at today's close.
            for sid, h in list(holdings.items()):
                p = px.get(sid, np.nan)
                if pd.isna(p):
                    p = h["last_close"]
                proceeds = h["shares"] * p * (1 - SELL_COST)
                cash += proceeds
                trades.append({"stock": sid, "entry_date": str(h["entry_date"].date()),
                               "entry_price": h["entry"], "exit_date": str(d.date()),
                               "exit_price": float(p),
                               "return_pct": float((p * (1 - SELL_COST)) / (h["entry"] * (1 + BUY_COST)) - 1)})
                del holdings[sid]
            # Buy picks at 1/slot_n of NAV each; unfilled slots stay in cash.
            if picks and cash > 0:
                alloc = cash / slot_n
                for sid in picks:
                    p = px.get(sid, np.nan)
                    if pd.isna(p) or p <= 0:
                        continue
                    shares = alloc / (p * (1 + BUY_COST))
                    holdings[sid] = {"shares": float(shares), "entry": float(p),
                                     "entry_date": d, "last_close": float(p)}
                cash = cash - sum(h["shares"] * h["entry"] * (1 + BUY_COST)
                                  for h in holdings.values())
        else:
            # Mid-month stop check (strategy A only).
            if use_stop:
                for sid, h in list(holdings.items()):
                    p = px.get(sid, np.nan)
                    if pd.isna(p):
                        continue
                    h["last_close"] = float(p)
                    if p < 0.9 * h["entry"]:
                        cash += h["shares"] * p * (1 - SELL_COST)
                        trades.append({"stock": sid,
                                       "entry_date": str(h["entry_date"].date()),
                                       "entry_price": h["entry"],
                                       "exit_date": str(d.date()),
                                       "exit_price": float(p),
                                       "return_pct": float((p * (1 - SELL_COST)) / (h["entry"] * (1 + BUY_COST)) - 1)})
                        del holdings[sid]
        for sid, h in holdings.items():
            p = close_f.loc[d].get(sid, np.nan)
            if not pd.isna(p):
                h["last_close"] = float(p)
        nav[d] = cash + sum(h["shares"] * h["last_close"] for h in holdings.values())
    nav = pd.Series(nav).sort_index()
    # Open holdings at END count as censored, not trades.
    return nav, trades


def summarize(nav, trades, bench, reb_dates):
    nav = nav.sort_index()
    bench = bench.reindex(nav.index).ffill()
    bench = bench / bench.iloc[0]
    days = len(nav)
    total = float(nav.iloc[-1] / nav.iloc[0] - 1)
    ann = float((nav.iloc[-1] / nav.iloc[0]) ** (252 / days) - 1)
    b_ann = float((bench.iloc[-1] / bench.iloc[0]) ** (252 / days) - 1)
    rets = nav.pct_change().dropna()
    b_rets = bench.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0
    b_sharpe = float(b_rets.mean() / b_rets.std() * np.sqrt(252)) if b_rets.std() > 0 else 0.0
    mdd = float((nav / nav.cummax() - 1).min())
    vol = float(rets.std() * np.sqrt(252))
    t = pd.DataFrame(trades)
    if len(t):
        wins = t[t["return_pct"] > 0]
        losses = t[t["return_pct"] <= 0]
        tstats = {"n": int(len(t)),
                  "win_rate": round(float(len(wins) / len(t) * 100), 1),
                  "avg_return_pct": round(float(t["return_pct"].mean() * 100), 2),
                  "avg_win_pct": round(float(wins["return_pct"].mean() * 100), 2) if len(wins) else 0.0,
                  "avg_loss_pct": round(float(losses["return_pct"].mean() * 100), 2) if len(losses) else 0.0,
                  "rr": round(float(abs(wins["return_pct"].mean() / losses["return_pct"].mean())), 2) if len(losses) and losses["return_pct"].mean() != 0 else None}
    else:
        tstats = {"n": 0, "win_rate": 0.0, "avg_return_pct": 0.0,
                  "avg_win_pct": 0.0, "avg_loss_pct": 0.0, "rr": None}
    # Halves by rebalance month.
    half = len(reb_dates) // 2
    cut = reb_dates[half]
    def _half_ret(s, a, b):
        s2 = s[(s.index >= a) & (s.index <= b)]
        return float(s2.iloc[-1] / s2.iloc[0] - 1) if len(s2) > 1 else 0.0
    h1 = {"strategy": round(_half_ret(nav, nav.index[0], cut) * 100, 2),
          "bench": round(_half_ret(bench, nav.index[0], cut) * 100, 2)}
    h2 = {"strategy": round(_half_ret(nav, cut, nav.index[-1]) * 100, 2),
          "bench": round(_half_ret(bench, cut, nav.index[-1]) * 100, 2)}
    return {"total_return_pct": round(total * 100, 2),
            "annualized_pct": round(ann * 100, 2),
            "bench_annualized_pct": round(b_ann * 100, 2),
            "sharpe": round(sharpe, 2), "bench_sharpe": round(b_sharpe, 2),
            "mdd_pct": round(mdd * 100, 2), "volatility_pct": round(vol * 100, 2),
            "trades": tstats, "half1": h1, "half2": h2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=["A", "B"], required=True)
    ap.add_argument("--data", default="labs/data_cache")
    ap.add_argument("--out", required=True)
    ap.add_argument("--grid", action="store_true")
    args = ap.parse_args()
    rev_m, close_m, amt_m, bench = load_data(args.data)
    ind = build_indicators(rev_m, close_m, amt_m)
    cal = close_m.index
    reb_dates = rebalance_dates(cal)
    if args.strategy == "A":
        base = {"top_n": 10, "rev_win": 12}
        grid = [(w, n) for w in (6, 12, 24) for n in (8, 10, 12)] if args.grid else [(12, 10)]
        pick = lambda d, w=12, n=10: signals_A(ind, close_m, rev_m, d, top_n=n, rev_win=w)
        use_stop = True
    else:
        base = {"top_n": 30, "lookback": 24}
        grid = [(lb, n) for lb in (12, 24, 36) for n in (20, 30, 50)] if args.grid else [(24, 30)]
        pick = lambda d, lb=24, n=30: signals_B(ind, close_m, rev_m, d, top_n=n, lookback=lb)
        use_stop = False
    results, grid_rows = {}, []
    for params in grid:
        if args.strategy == "A":
            w, n = params
            fn = lambda d: signals_A(ind, close_m, rev_m, d, top_n=n, rev_win=w)
        else:
            lb, n = params
            fn = lambda d: signals_B(ind, close_m, rev_m, d, top_n=n, lookback=lb)
        nav, trades = run_portfolio(close_m, bench, reb_dates, fn, use_stop,
                                    slot_n=params[1])
        s = summarize(nav, trades, bench, reb_dates)
        grid_rows.append({"params": params, "annualized_pct": s["annualized_pct"],
                          "sharpe": s["sharpe"], "beats_bench": s["annualized_pct"] > s["bench_annualized_pct"]})
        if params == grid[0] and not args.grid or params == (base.get("rev_win", base.get("lookback")), base["top_n"]):
            results = s
            results["trades_detail"] = trades
    beats = sum(1 for g in grid_rows if g["beats_bench"])
    out = {"strategy": args.strategy, "spec": "labs/revenue_spec_20261107.md",
           "period": [START, END], "base_params": base,
           "costs": {"buy": BUY_COST, "sell": SELL_COST},
           "benchmark": "0050 total return (yfinance adj close)",
           "result": results, "grid": grid_rows,
           "grid_beats_bench": f"{beats}/{len(grid_rows)}",
           "pass": bool(results.get("annualized_pct", 0) > results.get("bench_annualized_pct", 0)
                        and results.get("sharpe", 0) > results.get("bench_sharpe", 0)
                        and results["half1"]["strategy"] > results["half1"]["bench"]
                        and results["half2"]["strategy"] > results["half2"]["bench"]
                        and beats >= 7)}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: out[k] for k in ("strategy", "pass", "grid_beats_bench")},
                     ensure_ascii=False))
    print(json.dumps(out["result"], ensure_ascii=False, indent=1)[:2000])


if __name__ == "__main__":
    main()
