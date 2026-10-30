"""
p2_backtest.py — P2 實際張數權重回測 (2026-10-30 使用者拍板,實驗分支專用)。

目的: 驗證回測 20% 平權 vs 實盤 calc_position_size 實際張數權重 (約 35% 上限、
monitor 無組合上限) 的落差。同一 run、同一宇宙、同一資料、同一訊號,
僅權重與組合資金約束不同,故無宇宙漂移。

- flat (對照組): 與生產 backtest.py 完全相同 — 每筆權重 20%、僅 5 檔數量上限。
  應重現生產回測數字 (模組宇宙漂移),作為內建 sanity check。
- real (測試組): 每筆權重 = sig["capital_pct"]/100 (calc_position_size 實際資金佔比);
  除 5 檔數量上限外,另加組合資金上限: 新進場時已持有部位資金佔比合計 >= 100% 即跳過。
  另統計最高同時名目 (診斷實盤無上限時的名目,驗證約 280% 的說法)。

事前宣告 P2 判定: 若 real MDD 比 flat MDD 差超過 5 個百分點,則 35% 上限過於激進,
須先降上限或減檔數,不得直接以實盤權重上線;反之則實盤權重可接受 (另報累計/RR 變化)。
不動 main、不動 monitor/screener、不寫 latest_backtest.json、不發 TG、不下單。
僅產出 p2_results.json / p2_report.md。
"""
import json
import datetime

import pandas as pd
import yfinance as yf

from screener import build_dynamic_index_universe
from strategy_core import (
    CONFIG, STRATEGY_NAMES as ALL_STRATEGIES, prepare_indicators,
    evaluate_candidate, market_regime_ok, simulate_exit,
)

MAX_CONCURRENT = CONFIG["MAX_CONCURRENT_POSITIONS"]
SLOT_WEIGHT = 1.0 / MAX_CONCURRENT
CAPITAL_CAP_PCT = 100.0

P2_JSON = "p2_results.json"
P2_MD = "p2_report.md"


def summarize_rows(rows):
    if not rows:
        return {"trades": 0, "win_rate": 0.0, "avg_return_pct": 0.0}
    wins = sum(r["win"] for r in rows)
    return {
        "trades": len(rows),
        "win_rate": round(wins / len(rows) * 100, 1),
        "avg_return_pct": round(sum(r["net_return_pct"] for r in rows) / len(rows), 2),
    }


def run_mode(data, sample_items, bench_df, twii_series, mode):
    """mode: 'flat' (生產口徑) 或 'real' (實際張數權重 + 資金上限)。"""
    trades = []
    censored_count = 0
    skipped_portfolio_full = 0
    skipped_stock_busy = 0
    skipped_capital = 0
    open_positions = []  # [{stock_id, entry_date, exit_date, capital_pct}]

    for sid, (sname, sfx) in sample_items:
        t_sym = f"{sid}{sfx}"
        if t_sym not in data.columns.get_level_values(0):
            continue
        df_raw = data[t_sym].dropna(subset=["Close", "High", "Low", "Open", "Volume"]).copy()
        if len(df_raw) < 110:
            continue
        df = prepare_indicators(df_raw)
        start_idx, end_idx = 60, len(df) - 2
        for i in range(start_idx, end_idx):
            dt_idx = df.index[i]
            if bench_df is None or dt_idx not in bench_df.index:
                continue
            b_row = bench_df.loc[dt_idx]
            if pd.isna(b_row["B_MA20"]) or pd.isna(b_row["B_RET20"]):
                continue
            cur_close = float(df["Close"].iloc[i])
            stock_ret20 = (cur_close - float(df["Close"].iloc[i - 20])) / float(df["Close"].iloc[i - 20])
            m_ok = market_regime_ok(float(b_row["Close"]), float(b_row["B_MA20"]), stock_ret20)
            sigs = evaluate_candidate(df_raw.iloc[:i + 1], sid, sname, sfx,
                                      chip_info=None, bench_ret_20d=float(b_row["B_RET20"]),
                                      market_ok=m_ok, require_chip=False, bench_series=twii_series)
            if not sigs:
                continue
            sig = sigs[0]
            signal_close = sig["entry_price"]
            stop_p = sig["hard_stop"]
            next_bar = df.iloc[i + 1]
            n_open = float(next_bar["Open"])
            n_high = float(next_bar["High"])
            n_close = float(next_bar["Close"])
            if n_open > signal_close * 1.028 or n_open <= stop_p:
                continue
            trigger_buy = round(max(signal_close, n_open * 1.003), 2)
            if n_high < trigger_buy or n_close < n_open * 0.998:
                continue
            entry_date = df.index[i + 1]
            overlapping = [p for p in open_positions if p["entry_date"] <= entry_date < p["exit_date"]]
            if any(p["stock_id"] == sid for p in overlapping):
                skipped_stock_busy += 1
                continue
            if len(overlapping) >= MAX_CONCURRENT:
                skipped_portfolio_full += 1
                continue
            cap_pct = float(sig.get("capital_pct", 0.0))
            if mode == "real" and sum(p["capital_pct"] for p in overlapping) >= CAPITAL_CAP_PCT:
                skipped_capital += 1
                continue
            result = simulate_exit(df, i + 1, trigger_buy, stop_p)
            if result["censored"]:
                censored_count += 1
                continue
            exit_date = df.index[result["exit_idx"]]
            trades.append({
                "stock_id": sid, "strategy": sig["strategy"],
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "exit_date": exit_date.strftime("%Y-%m-%d"),
                "exit_reason": result["exit_reason"],
                "net_return_pct": result["net_ret_pct"], "win": result["win"],
                "capital_pct": cap_pct,
            })
            open_positions.append({"stock_id": sid, "entry_date": entry_date,
                                   "exit_date": exit_date, "capital_pct": cap_pct})

    # 最高同時名目: 每筆進場時點的持有資金佔比合計 (含自身); flat 為假設性診斷, real 為實際。
    max_concurrent_cap = 0.0
    for t in trades:
        ed = datetime.datetime.strptime(t["entry_date"], "%Y-%m-%d")
        s = sum(o["capital_pct"] for o in open_positions
                if o["entry_date"] <= ed < o["exit_date"])
        max_concurrent_cap = max(max_concurrent_cap, s)

    df_t = pd.DataFrame(trades)
    if df_t.empty:
        return {"trades": 0, "win_rate": 0.0, "rr_ratio": 0.0, "cum_return_pct": 0.0,
                "mdd_pct": 0.0, "by_strategy": {}, "max_concurrent_cap_pct": 0.0,
                "censored": censored_count, "skipped_full": skipped_portfolio_full,
                "skipped_busy": skipped_stock_busy, "skipped_capital": skipped_capital}
    df_t = df_t.sort_values("entry_date").reset_index(drop=True)
    weight = (df_t["capital_pct"] / 100.0) if mode == "real" else SLOT_WEIGHT
    df_t["port_impact"] = df_t["net_return_pct"] * weight
    equity_curve = 100.0 * (1.0 + df_t["port_impact"] / 100.0).cumprod()
    peak = equity_curve.cummax()
    dd = ((equity_curve - peak) / peak) * 100.0
    wins = df_t[df_t["net_return_pct"] > 0]["net_return_pct"]
    losses = df_t[df_t["net_return_pct"] <= 0]["net_return_pct"]
    avg_win = float(wins.mean()) if not wins.empty else 0.0
    avg_loss = abs(float(losses.mean())) if not losses.empty else 1.0
    mid = len(df_t) // 2
    return {
        "trades": int(len(df_t)),
        "win_rate": round(float(df_t["win"].mean() * 100), 1),
        "rr_ratio": round(avg_win / max(avg_loss, 0.1), 2),
        "avg_win_pct": round(avg_win, 2), "avg_loss_pct": round(-avg_loss, 2),
        "cum_return_pct": round(float(equity_curve.iloc[-1] - 100.0), 2),
        "mdd_pct": round(float(dd.min()), 2),
        "by_strategy": {s: summarize_rows(df_t[df_t["strategy"] == s].to_dict("records"))
                        for s in ALL_STRATEGIES},
        "first_half": summarize_rows(df_t.iloc[:mid].to_dict("records")),
        "second_half": summarize_rows(df_t.iloc[mid:].to_dict("records")),
        "max_concurrent_cap_pct": round(max_concurrent_cap, 1),
        "censored": censored_count, "skipped_full": skipped_portfolio_full,
        "skipped_busy": skipped_stock_busy, "skipped_capital": skipped_capital,
    }


def main():
    universe = build_dynamic_index_universe()
    sample_items = list(universe.items())[:220]
    tickers = ["0050.TW", "^TWII"] + [f"{sid}{sfx}" for sid, (_, sfx) in sample_items]
    data = yf.download(tickers, start="2020-01-01", group_by="ticker", threads=True, progress=False)
    bench_df = None
    if "0050.TW" in data.columns.get_level_values(0):
        bench_df = data["0050.TW"].dropna(subset=["Close"]).copy()
        bench_df["B_MA20"] = bench_df["Close"].rolling(20).mean()
        bench_df["B_RET20"] = bench_df["Close"].pct_change(20)
    twii_series = None
    if "^TWII" in data.columns.get_level_values(0):
        twii_series = data["^TWII"]["Close"].dropna()
        if twii_series.empty:
            twii_series = None

    results = {"design": {
        "universe": "build_dynamic_index_universe()[:220] (same as backtest.py)",
        "window": "2020-01-01 ~ latest (yfinance)",
        "flat": "production: 20% weight each, 5-position count cap only",
        "real": "calc_position_size capital_pct weight, 5-position cap + 100% capital cap",
        "verdict_rule": "real MDD worse than flat MDD by >5pp => 35% cap too aggressive",
    }, "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "modes": {}}
    for mode in ("flat", "real"):
        summ = run_mode(data, sample_items, bench_df, twii_series, mode)
        results["modes"][mode] = summ
        print(f"P2 {mode}: n={summ['trades']} win={summ['win_rate']}% RR={summ['rr_ratio']} "
              f"cum={summ['cum_return_pct']}% MDD={summ['mdd_pct']}% "
              f"maxcap={summ['max_concurrent_cap_pct']}% skipped_cap={summ['skipped_capital']}", flush=True)
    f, r = results["modes"]["flat"], results["modes"]["real"]
    results["verdict"] = {
        "real_mdd_worse_by_pp": round(f["mdd_pct"] - r["mdd_pct"], 2),
        "cap_too_aggressive": bool(r["mdd_pct"] < f["mdd_pct"] - 5.0),
        "note": "cap_too_aggressive True => 35% cap too aggressive, lower cap or reduce count first.",
    }
    with open(P2_JSON, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    lines = [f"# P2 實際張數權重回測報告 ({results['generated_at']})", "",
             "## 設計 (預註冊,跑前凍結)", ""]
    for k, v in results["design"].items():
        lines.append(f"- {k}: {v}")
    lines += ["", "## 兩組結果", "",
              "| 組別 | n | 勝率% | RR | 累計% | MDD% | 最高同時名目% | 資金上限跳過 |",
              "|---|---|---|---|---|---|---|---|"]
    for mode, s in results["modes"].items():
        lines.append(f"| {mode} | {s['trades']} | {s['win_rate']} | {s['rr_ratio']} | {s['cum_return_pct']} "
                     f"| {s['mdd_pct']} | {s['max_concurrent_cap_pct']} | {s['skipped_capital']} |")
    lines += ["", "## P2 判定 (事前標準)", "", f"- {results['verdict']}"]
    with open(P2_MD, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("WROTE", P2_JSON, P2_MD, flush=True)


if __name__ == "__main__":
    main()
