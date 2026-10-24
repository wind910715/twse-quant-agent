"""breakout_lab.py — 20 日突破+量增 (BRK20) 組合級驗證, 在 GitHub Actions 執行 (2026-10-24 起)
本機事件研究先發現: P3 的 RS 條件無增量價值, 但其對照組「20 日突破+量增」本體強
(2 年 546 筆, 平均 +3.67%, RR 3.53, 簡易出場)。本實驗以主系統口徑重驗:
隔日確認進場、系統出場引擎、硬停損比照家規 (訊號低點 x0.995 且不深於 -2.5%)、
組合 5 槽位、同股不重疊、大盤 regime 門檻。三本帳: BRK_ONLY / S_ONLY (S0/S3, v3.5 起 S4 已剃除) / MERGED (S 優先)。
過關: BRK_ONLY 的 RR >= 1.8 且累計為正、後半段平均為正; MERGED 優於 S_ONLY 才算有增量價值。
資料: yfinance 日 K (2020-01-01 起, 使用者指定長視窗), universe 與 screener 相同。結果寫 breakout_lab_results.json 並發 TG。
"""
import os
import json
import urllib.request
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

from strategy_core import (CONFIG, prepare_indicators, evaluate_candidate,
                           simulate_exit, calc_position_size, market_regime_ok)

try:
    from screener import build_dynamic_index_universe
except Exception:
    from strategy_core import build_dynamic_index_universe  # type: ignore

RESULTS_FILE = "breakout_lab_results.json"


def send_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("(no TG env, skip)")
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print("TG failed:", e)


def load_data():
    universe = build_dynamic_index_universe()
    items = list(universe.items())
    tickers = ["0050.TW"] + [f"{sid}{sfx}" for sid, (_, sfx) in items]
    data = yf.download(tickers, start="2020-01-01", group_by="ticker", threads=True, progress=False)
    bench, stocks = None, {}
    for t in tickers:
        try:
            df = data[t].dropna(subset=["Close"]).copy()
        except Exception:
            continue
        if len(df) < 60:
            continue
        df.index = pd.to_datetime(df.index).tz_localize(None)
        if t == "0050.TW":
            df["B_MA20"] = df["Close"].rolling(20).mean()
            df["B_RET20"] = df["Close"] / df["Close"].shift(20) - 1
            bench = df
        else:
            sid = t.split(".")[0]
            name, sfx = universe.get(sid, (sid, ".TW"))
            stocks[sid] = (name, sfx, df)
    return {"stocks": stocks, "bench": bench}


def collect_s(data):
    # 與 backtest.py 完全同口徑: evaluate_candidate(df_raw.iloc[:i+1], ...) + 隔日確認進場
    bench_df = data["bench"]
    cands = []
    for sid, (sname, sfx, df_raw) in data["stocks"].items():
        df = prepare_indicators(df_raw)
        for i in range(61, len(df) - 2):
            dt = df.index[i]
            if dt not in bench_df.index:
                continue
            b = bench_df.loc[dt]
            if pd.isna(b["B_MA20"]) or pd.isna(b["B_RET20"]):
                continue
            cur_close = float(df["Close"].iloc[i])
            s_ret20 = (cur_close - float(df["Close"].iloc[i - 20])) / float(df["Close"].iloc[i - 20])
            m_ok = market_regime_ok(float(b["Close"]), float(b["B_MA20"]), s_ret20)
            sigs = evaluate_candidate(df_raw.iloc[:i + 1], sid, sname, sfx,
                                      chip_info=None, bench_ret_20d=float(b["B_RET20"]),
                                      market_ok=m_ok, require_chip=False)
            if not sigs:
                continue
            sig = sigs[0]  # 同日同股多策略取優先序第一個, 不重複曝險
            nb = df.iloc[i + 1]
            n_open, n_high, n_close = float(nb["Open"]), float(nb["High"]), float(nb["Close"])
            if n_open > sig["entry_price"] * 1.028 or n_open <= sig["hard_stop"]:
                continue
            trigger = round(max(sig["entry_price"], n_open * 1.003), 2)
            if n_high < trigger or n_close < n_open * 0.998:
                continue
            cands.append({"sid": sid, "strategy": sig["strategy"], "entry_idx_date": df.index[i + 1],
                          "entry_price": trigger, "hard_stop": sig["hard_stop"]})
    return cands


def collect_brk(data):
    bench_df = data["bench"]
    cands = []
    for sid, (sname, sfx, df_raw) in data["stocks"].items():
        df = prepare_indicators(df_raw)
        prev20_high = df["High"].shift(1).rolling(20).max()
        for i in range(61, len(df) - 2):
            dt = df.index[i]
            if dt not in bench_df.index:
                continue
            b = bench_df.loc[dt]
            if pd.isna(b["B_MA20"]) or pd.isna(b["B_RET20"]):
                continue
            cur_close = float(df["Close"].iloc[i])
            s_ret20 = (cur_close - float(df["Close"].iloc[i - 20])) / float(df["Close"].iloc[i - 20])
            if not market_regime_ok(float(b["Close"]), float(b["B_MA20"]), s_ret20):
                continue
            if not (cur_close > float(prev20_high.iloc[i])
                    and float(df["vol_lots"].iloc[i]) >= 1.5 * float(df["MV20"].iloc[i])
                    and cur_close > float(df["MA60"].iloc[i])):
                continue
            stop0 = float(df["Low"].iloc[i]) * 0.995
            nb = df.iloc[i + 1]
            n_open, n_high, n_close = float(nb["Open"]), float(nb["High"]), float(nb["Close"])
            if n_open > cur_close * 1.028 or n_open <= stop0:
                continue
            trigger = round(max(cur_close, n_open * 1.003), 2)
            if n_high < trigger or n_close < n_open * 0.998:
                continue
            cands.append({"sid": sid, "strategy": "BRK20_突破量增", "entry_idx_date": df.index[i + 1],
                          "entry_price": trigger, "hard_stop": max(stop0, round(trigger * 0.975, 2))})
    return cands


def run_book(prepped, cands):
    trades, active = [], {}
    for c in sorted(cands, key=lambda x: x["entry_idx_date"]):
        sid = c["sid"]
        if sid in active and active[sid] >= c["entry_idx_date"]:
            continue
        open_now = [s for s, until in active.items() if until >= c["entry_idx_date"]]
        if len(open_now) >= CONFIG["MAX_CONCURRENT_POSITIONS"]:
            continue
        df = prepped[sid]
        entry_idx = df.index.get_loc(c["entry_idx_date"])
        if not isinstance(entry_idx, (int, np.integer)):
            continue
        sz = calc_position_size(c["entry_price"], c["hard_stop"])
        if sz["lots"] <= 0:
            continue
        sim = simulate_exit(df, int(entry_idx), c["entry_price"], sz["adjusted_stop"])
        if sim["censored"]:
            continue
        exit_date = df.index[sim["exit_idx"]]
        trades.append({"date": str(c["entry_idx_date"].date()), "stock_id": sid, "strategy": c["strategy"],
                       "net_return_pct": sim["net_ret_pct"], "win": sim["win"]})
        active[sid] = exit_date
    if not trades:
        return {"n": 0}
    rets = [t["net_return_pct"] for t in trades]
    wins = [t["net_return_pct"] for t in trades if t["win"]]
    losses = [t["net_return_pct"] for t in trades if not t["win"]]
    equity, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        equity *= (1 + 0.20 * r / 100)
        peak = max(peak, equity)
        mdd = min(mdd, (equity / peak - 1) * 100)
    half = len(rets) // 2
    by_strat = {}
    for t in trades:
        by_strat.setdefault(t["strategy"], []).append(t["net_return_pct"])
    return {"n": len(trades), "win": round(len(wins) / len(rets) * 100, 1),
            "avg": round(float(np.mean(rets)), 2),
            "rr": round(float(np.mean(wins) / abs(np.mean(losses))), 2) if wins and losses else None,
            "cum_slot20": round((equity - 1) * 100, 2), "mdd": round(mdd, 2),
            "h1_avg": round(float(np.mean(rets[:half])), 2), "h2_avg": round(float(np.mean(rets[half:])), 2),
            "by_strategy": {k: {"n": len(v), "avg": round(float(np.mean(v)), 2)} for k, v in by_strat.items()}}


def main():
    print("loading data (3y)...", flush=True)
    data = load_data()
    print(f"stocks={len(data['stocks'])}", flush=True)
    prepped = {sid: prepare_indicators(df) for sid, (_, _, df) in data["stocks"].items()}
    s_cands = collect_s(data)
    print(f"S candidates: {len(s_cands)}", flush=True)
    b_cands = collect_brk(data)
    print(f"BRK candidates: {len(b_cands)}", flush=True)
    results = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "BRK_ONLY": run_book(prepped, b_cands),
               "S_ONLY": run_book(prepped, s_cands),
               "MERGED": run_book(prepped, s_cands + b_cands)}
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    lines = ["🧪 BRK20 突破量增組合級驗證 (2020-2026, 系統出場引擎)"]
    for k in ("BRK_ONLY", "S_ONLY", "MERGED"):
        r = results[k]
        if r.get("n"):
            lines.append(f"{k}: {r['n']} 筆 勝率 {r['win']}% 平均 {r['avg']:+.2f}% RR {r['rr']} 累計(槽位20%) {r['cum_slot20']:+.2f}% MDD {r['mdd']:.2f}% 前/後半 {r['h1_avg']:+.2f}/{r['h2_avg']:+.2f}")
        else:
            lines.append(f"{k}: 無交易")
    send_telegram("\n".join(lines))
    print("\n".join(lines))
    print("DONE")


if __name__ == "__main__":
    main()
