#!/usr/bin/env python3
"""Intraday-entry + close-filter swing system, v1-D daily-bar proxy.

Frozen spec: ~/workspace/your_files/日內進場波段留倉系統_SPEC_v1.md (v1, 2026-10-28).
Implements the spec literally. Ambiguities resolved literally are listed in
SPEC_NOTES at the bottom of this file and in results.json meta.assumptions.

Universe: 2330, 2308, 6488, 2345, 6669, 8299, 2327, 3711 (.TW).
Window: 2020-01-01 .. 2026-10-28 (inclusive). Source: Yahoo Finance daily bars.
Cost: 0.6% per completed trade (entry + exit), including filter-day exits.

Signal day (ALL four must hold):
  1. Direction: close > EMA20 (close-based, standard EMA).
  2. VCP proxy: (HH10 - LL10) / close <= 10% AND 5-day avg volume < 20-day avg volume.
  3. Breakout: close > prior-20-day high (signal day EXCLUDED) AND
     volume > 1.5x 20-day avg volume.
  4. MACD confirm: DIF > DEA (standard 12/26/9 on close).

Entry: next trading day's open. Max 1 position per stock; signals ignored while holding.
Close filter (filtered group): entry-day close > entry price (strict, gross) -> hold
  as swing; else exit at entry-day close (reason "filter_out").
Swing exits (close-based): hard stop close < entry - 2*ATR14 (ATR locked at signal
  day); trend exit close < EMA20; first trigger wins; exit price = trigger close.
Control group: same signals/entry/exit rules, NO close filter.
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

STOCKS = ["2330", "2308", "6488", "2345", "6669", "8299", "2327", "3711"]

COST = 0.006
SPLIT_DATE = "2023-06-30"


def download_stock(stock, start, end_inclusive):
    """Download Yahoo daily bars for one .TW stock. Returns cleaned DataFrame."""
    end_exclusive = (datetime.strptime(end_inclusive, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        t = yf.Ticker(f"{stock}.TW")
        df = t.history(start=start, end=end_exclusive, auto_adjust=False)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.tz_localize(None) if getattr(df.index, "tz", None) is not None else df
    df.index = pd.to_datetime(df.index).normalize()
    # Yahoo carries market-holiday rows with 0 volume and flat prices; drop them
    # so volume averages and true range are not corrupted.
    df = df[df["Volume"].fillna(0) > 0]
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df if not df.empty else None


def add_indicators(df):
    """Compute all spec indicators on the daily bars."""
    out = df.copy()
    c, h, l, v = out["Close"], out["High"], out["Low"], out["Volume"]
    out["EMA20"] = c.ewm(span=20, adjust=False).mean()
    out["HH10"] = h.rolling(10).max()
    out["LL10"] = l.rolling(10).min()
    out["VOL5"] = v.rolling(5).mean()
    out["VOL20"] = v.rolling(20).mean()
    out["PRIOR_HIGH20"] = h.shift(1).rolling(20).max()
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    out["DIF"] = ema12 - ema26
    out["DEA"] = out["DIF"].ewm(span=9, adjust=False).mean()
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    out["ATR14"] = tr.rolling(14).mean()
    return out


def is_signal(row):
    """All four spec conditions on one signal-day row (NaN-safe)."""
    need = ["Close", "EMA20", "HH10", "LL10", "VOL5", "VOL20", "PRIOR_HIGH20",
            "Volume", "DIF", "DEA", "ATR14"]
    if any(pd.isna(row[k]) for k in need):
        return False
    if not (row["Close"] > row["EMA20"]):
        return False
    if not ((row["HH10"] - row["LL10"]) / row["Close"] <= 0.10):
        return False
    if not (row["VOL5"] < row["VOL20"]):
        return False
    if not (row["Close"] > row["PRIOR_HIGH20"]):
        return False
    if not (row["Volume"] > 1.5 * row["VOL20"]):
        return False
    if not (row["DIF"] > row["DEA"]):
        return False
    return True


def simulate_group(df, use_filter):
    """Walk trading days; return list of trade dicts for one group.

    use_filter=True: entry-day close decides hold vs filter_out; exit checks
    start the day AFTER entry. use_filter=False: exit checks start on entry day.
    """
    trades = []
    n = len(df)
    dates = df.index
    holding = None  # dict(entry_idx, entry_price, atr, signal_date)
    i = 0
    while i < n:
        row = df.iloc[i]
        if holding is not None:
            entry_idx = holding["entry_idx"]
            entry_price = holding["entry_price"]
            atr = holding["atr"]
            exited = False
            if use_filter and i == entry_idx:
                # Filter day: gross close vs entry price, strictly greater holds.
                if row["Close"] <= entry_price:
                    trades.append(make_trade(df, holding, i, "filter_out"))
                    exited = True
                # else: hold; no exit checks on the filter day itself.
            else:
                if row["Close"] < entry_price - 2.0 * atr:
                    trades.append(make_trade(df, holding, i, "hard_stop"))
                    exited = True
                elif row["Close"] < row["EMA20"]:
                    trades.append(make_trade(df, holding, i, "trend_exit"))
                    exited = True
            if exited:
                holding = None
                # A signal on the exit day itself may still enter next day.
                if is_signal(row) and i + 1 < n and not pd.isna(df.iloc[i + 1]["Open"]):
                    holding = open_position(df, i)
                i += 1
                continue
            if i == n - 1:
                # Window end: force exit at last close.
                trades.append(make_trade(df, holding, i, "window_end"))
                holding = None
            i += 1
            continue
        # Flat: a signal today enters at tomorrow's open.
        if is_signal(row) and i + 1 < n and not pd.isna(df.iloc[i + 1]["Open"]):
            holding = open_position(df, i)
        i += 1
    return trades


def open_position(df, signal_idx):
    entry_idx = signal_idx + 1
    return {
        "signal_date": df.index[signal_idx].strftime("%Y-%m-%d"),
        "entry_idx": entry_idx,
        "entry_date": df.index[entry_idx].strftime("%Y-%m-%d"),
        "entry_price": float(df.iloc[entry_idx]["Open"]),
        "atr": float(df.iloc[signal_idx]["ATR14"]),
    }


def make_trade(df, holding, exit_idx, reason):
    entry_price = holding["entry_price"]
    exit_price = float(df.iloc[exit_idx]["Close"])
    gross = exit_price / entry_price - 1.0
    return {
        "signal_date": holding["signal_date"],
        "entry_date": holding["entry_date"],
        "entry_price": round(entry_price, 4),
        "exit_date": df.index[exit_idx].strftime("%Y-%m-%d"),
        "exit_price": round(exit_price, 4),
        "exit_reason": reason,
        "gross_return": round(gross, 6),
        "net_return": round(gross - COST, 6),
    }


def hypothetical_swing(df, signal_idx):
    """Control-rule path for one entry, standalone (used for blocked analysis).

    Returns (exit_date, exit_price, reason, net_return) or None if no entry.
    """
    if signal_idx + 1 >= len(df) or pd.isna(df.iloc[signal_idx + 1]["Open"]):
        return None
    entry_price = float(df.iloc[signal_idx + 1]["Open"])
    atr = float(df.iloc[signal_idx]["ATR14"])
    for j in range(signal_idx + 1, len(df)):
        row = df.iloc[j]
        if row["Close"] < entry_price - 2.0 * atr:
            reason = "hard_stop"
            break
        if row["Close"] < row["EMA20"]:
            reason = "trend_exit"
            break
    else:
        j = len(df) - 1
        reason = "window_end"
    exit_price = float(df.iloc[j]["Close"])
    gross = exit_price / entry_price - 1.0
    return {
        "entry_date": df.index[signal_idx + 1].strftime("%Y-%m-%d"),
        "exit_date": df.index[j].strftime("%Y-%m-%d"),
        "exit_reason": reason,
        "net_return": round(gross - COST, 6),
    }


def blocked_analysis(df, filtered_trades):
    """For each filter_out trade: next-day gross return + hypothetical swing path."""
    sig_index = {d.strftime("%Y-%m-%d"): k for k, d in enumerate(df.index)}
    rows = []
    for t in filtered_trades:
        if t["exit_reason"] != "filter_out":
            continue
        s_idx = sig_index[t["signal_date"]]
        e_idx = s_idx + 1
        next_day = None
        if e_idx + 1 < len(df):
            next_day = round(float(df.iloc[e_idx + 1]["Close"]) / t["entry_price"] - 1.0, 6)
        hypo = hypothetical_swing(df, s_idx)
        rows.append({
            "stock": t.get("stock"),
            "signal_date": t["signal_date"],
            "entry_date": t["entry_date"],
            "entry_price": t["entry_price"],
            "filter_exit_price": t["exit_price"],
            "filter_net_return": t["net_return"],
            "next_day_gross_return": next_day,
            "hypothetical_swing": hypo,
        })
    return rows


def stats_for(trades):
    """Summary stats for one group of trades."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": None, "avg_net_return": None, "total_net_return": 0.0,
                "pf": None, "max_drawdown": None, "halves": {}}
    nets = [t["net_return"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gain = sum(wins)
    loss = abs(sum(losses))
    pf = (gain / loss) if loss > 0 else (float("inf") if gain > 0 else None)
    # Max drawdown on cumulative-sum equity curve ordered by exit date.
    curve, run = [], 0.0
    for t in sorted(trades, key=lambda x: x["exit_date"]):
        run += t["net_return"]
        curve.append(run)
    peak, mdd = curve[0], 0.0
    for x in curve:
        peak = max(peak, x)
        mdd = min(mdd, x - peak)
    halves = {}
    for name, pred in [("first_half", lambda d: d <= SPLIT_DATE),
                       ("second_half", lambda d: d > SPLIT_DATE)]:
        sub = [t for t in trades if pred(t["entry_date"])]
        halves[name] = {
            "n": len(sub),
            "avg_net_return": round(sum(t["net_return"] for t in sub) / len(sub), 6) if sub else None,
            "total_net_return": round(sum(t["net_return"] for t in sub), 6),
        }
    return {
        "n": n,
        "win_rate": round(len(wins) / n, 4),
        "avg_net_return": round(sum(nets) / n, 6),
        "total_net_return": round(sum(nets), 6),
        "pf": (round(pf, 4) if pf is not None and math.isfinite(pf) else pf),
        "max_drawdown": round(mdd, 6),
        "halves": halves,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2026-10-28")
    ap.add_argument("--outdir", default="results/intraday_swing_v1")
    args = ap.parse_args()

    per_stock = {}
    all_filtered, all_control, blocked = [], [], []
    errors = {}
    for stock in STOCKS:
        df = download_stock(stock, args.start, args.end)
        if df is None:
            errors[stock] = "no data from Yahoo"
            continue
        df = add_indicators(df)
        per_stock[stock] = {
            "first_date": df.index[0].strftime("%Y-%m-%d"),
            "last_date": df.index[-1].strftime("%Y-%m-%d"),
            "bars": len(df),
        }
        f_trades = simulate_group(df, use_filter=True)
        c_trades = simulate_group(df, use_filter=False)
        for t in f_trades:
            t["stock"] = stock
        for t in c_trades:
            t["stock"] = stock
        all_filtered.extend(f_trades)
        all_control.extend(c_trades)
        b = blocked_analysis(df, f_trades)
        for r in b:
            r["stock"] = stock
        blocked.extend(b)

    f_stats = stats_for(all_filtered)
    c_stats = stats_for(all_control)
    blocked_next = [r["next_day_gross_return"] for r in blocked if r["next_day_gross_return"] is not None]
    blocked_hypo = [r["hypothetical_swing"]["net_return"] for r in blocked if r["hypothetical_swing"]]
    blocked_summary = {
        "blocked_count": len(blocked),
        "mean_next_day_gross_return": round(sum(blocked_next) / len(blocked_next), 6) if blocked_next else None,
        "mean_hypothetical_swing_net_return": round(sum(blocked_hypo) / len(blocked_hypo), 6) if blocked_hypo else None,
        "hypothetical_winners": sum(1 for x in blocked_hypo if x > 0),
        "details": blocked,
    }

    fh_stats, sh_stats = f_stats["halves"].get("first_half", {}), f_stats["halves"].get("second_half", {})
    gates = {
        "n_ge_30": f_stats["n"] >= 30,
        "avg_net_positive": (f_stats["avg_net_return"] or 0) > 0,
        "pf_ge_1_3": (f_stats["pf"] is not None and math.isfinite(f_stats["pf"]) and f_stats["pf"] >= 1.3)
                      or (f_stats["pf"] == float("inf")),
        "first_half_positive": (fh_stats.get("avg_net_return") or 0) > 0,
        "second_half_positive": (sh_stats.get("avg_net_return") or 0) > 0,
    }
    verdict = "PASS" if all(gates.values()) else "FAIL"

    results = {
        "meta": {
            "spec": "日內進場波段留倉系統_SPEC_v1.md v1-D daily-bar proxy",
            "universe": STOCKS,
            "window": [args.start, args.end],
            "cost_per_trade": COST,
            "split_date": SPLIT_DATE,
            "per_stock": per_stock,
            "errors": errors,
            "assumptions": [
                "A1: '過去 N 日' windows include the signal day, EXCEPT the 20-day high which the spec explicitly excludes the signal day from. So HH10/LL10, VOL5/VOL20 (both conditions) are trailing windows ending on the signal day.",
                "A2: Close filter compares GROSS prices (entry-day close > entry price, strictly); the 0.6% cost is deducted from every completed trade's return only.",
                "A3: ATR14 for the hard stop is locked at the signal-day value (last complete bar before entry).",
                "A4: Filtered group exit checks start the day AFTER entry (entry day is the filter day only); control group exit checks start on entry day.",
                "A5: Same-day double trigger: hard stop takes precedence for the reason label; exit price is the same trigger-day close either way.",
                "A6: Positions still open at window end are force-exited at the last available close (reason 'window_end', cost applied), so every trade has an exit.",
                "A7: Halves split by ENTRY date against 2023-06-30; the gate uses per-half AVG net return > 0.",
                "A8: PF = sum(net gains)/sum(|net losses|); infinite if no losing trades and gains exist.",
                "A9: Max drawdown is measured on the cumulative-sum equity curve of trades ordered by exit date (pooled across stocks).",
                "A10: Yahoo market-holiday rows with 0 volume are dropped before indicator computation.",
                "A11: Net return = exit/entry - 1 - 0.006 (subtractive cost).",
                "A12: 'Blocked next-day performance' is reported two ways: next-trading-day gross return after entry, and the hypothetical full swing-path net return under control rules.",
            ],
        },
        "verdict": {"filtered_group": verdict, "gates": gates},
        "filtered": {"stats": f_stats, "trades": all_filtered},
        "control": {"stats": c_stats, "trades": all_control},
        "blocked_analysis": blocked_summary,
    }

    os.makedirs(args.outdir, exist_ok=True)
    with open(os.path.join(args.outdir, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)

    def fmt(x, pct=False):
        if x is None:
            return "n/a"
        if isinstance(x, float) and math.isinf(x):
            return "inf"
        return f"{x * 100:.2f}%" if pct else f"{x:.4f}"

    f, c = f_stats, c_stats
    summary = f"""# 日內進場＋收盤濾網系統 v1-D 回測結果

> 代理版聲明：本結果為日線代理版（進場價＝訊號隔日開盤價），只能證明「收盤濾網概念」與「組合邏輯」，不能直接等同盤中進場績效。完整盤中版為 v2。
> 窗口：{args.start} ～ {args.end}；宇宙：{", ".join(STOCKS)}；成本：每筆 0.6%。

## Verdict（有濾網組）：{verdict}

| 指標 | 有濾網組 | 無濾網對照組 |
|---|---|---|
| 筆數 n | {f["n"]} | {c["n"]} |
| 勝率 | {fmt(f["win_rate"], pct=True)} | {fmt(c["win_rate"], pct=True)} |
| 平均淨報酬 | {fmt(f["avg_net_return"], pct=True)} | {fmt(c["avg_net_return"], pct=True)} |
| 累計淨報酬 | {fmt(f["total_net_return"], pct=True)} | {fmt(c["total_net_return"], pct=True)} |
| PF | {fmt(f["pf"])} | {fmt(c["pf"])} |
| 最大回撤（累計報酬曲線） | {fmt(f["max_drawdown"], pct=True)} | {fmt(c["max_drawdown"], pct=True)} |

## 過關條件（事前宣告，有濾網組，須同時成立）

- n ≥ 30：{"✅" if gates["n_ge_30"] else "❌"}（{f["n"]}）
- 平均淨報酬 > 0：{"✅" if gates["avg_net_positive"] else "❌"}（{fmt(f["avg_net_return"], pct=True)}）
- PF ≥ 1.3：{"✅" if gates["pf_ge_1_3"] else "❌"}（{fmt(f["pf"])}）
- 前半（進場日 ≤ 2023-06-30）平均淨報酬 > 0：{"✅" if gates["first_half_positive"] else "❌"}（{fmt(fh_stats.get("avg_net_return"), pct=True)}，n={fh_stats.get("n")}）
- 後半（進場日 > 2023-06-30）平均淨報酬 > 0：{"✅" if gates["second_half_positive"] else "❌"}（{fmt(sh_stats.get("avg_net_return"), pct=True)}，n={sh_stats.get("n")}）

## 濾網價值

- 被濾網擋下筆數：{blocked_summary["blocked_count"]}
- 被擋下者隔日平均報酬（次日收盤 vs 進場價，毛報酬）：{fmt(blocked_summary["mean_next_day_gross_return"], pct=True)}
- 被擋下者若改走波段出場的假設平均淨報酬：{fmt(blocked_summary["mean_hypothetical_swing_net_return"], pct=True)}（其中假設獲利筆數 {blocked_summary["hypothetical_winners"]} / {len(blocked_hypo)}）

## 限制聲明

- 日線代理版：進場價為訊號隔日開盤價，非盤中實際成交價；盤中滑價、假突破回落皆未反映。
- 價格未調整股利；以收盤價序列為準。
- 各檔實際起算日與假設清單見 results.json meta。
"""
    with open(os.path.join(args.outdir, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(summary)
    print(f"Verdict (filtered): {verdict}")
    print(f"Filtered: n={f['n']} avg={f['avg_net_return']} pf={f['pf']} win={f['win_rate']}")
    print(f"Control:  n={c['n']} avg={c['avg_net_return']} pf={c['pf']} win={c['win_rate']}")
    print(f"Blocked: {blocked_summary['blocked_count']} trades")
    return 0


if __name__ == "__main__":
    sys.exit(main())
