"""
s04_opt_lab.py — S0/S4 一次優化機會實驗室（預註冊制）

預註冊文件：S04_OPT_PREREG_2026-10-24.md（變體、標準、口徑皆以該文件為準）。
本檔不改 strategy_core.py、CONFIG、S3/BRK 邏輯；只在實驗室內以參數化門檻
重現 evaluate_candidate 的 S0/S3/S4/BRK 訊號邏輯（逐字對照 e941c2e 版），
並以與 backtest.py 完全相同的組合規則（優先序、槽位、隔日進場確認、
simulate_exit、20% 槽位權重）跑 2020–2026 全系統。

指標加速說明：prepare_indicators 全為 trailing 指標（rolling / ewm adjust=False），
預先在全序列上算好與逐日切片算出的第 i 列值完全相同，僅為加速，不改變訊號。

輸出：s04_opt_results.json（同步回 repo）＋ log 印出判決表。
"""
import datetime
import json

import numpy as np
import pandas as pd
import yfinance as yf

from screener import build_dynamic_index_universe
from strategy_core import (
    CONFIG, prepare_indicators, market_regime_ok, simulate_exit,
    calc_position_size,
)

OUT_FILE = "s04_opt_results.json"
MAX_CONCURRENT = CONFIG["MAX_CONCURRENT_POSITIONS"]
SLOT_WEIGHT = 1.0 / MAX_CONCURRENT

# ---------------- 變體定義（與預註冊文件一致，實驗前凍結） ----------------
# 每變體只改一個門檻維度；None = 現行值。
SYSTEMS = [
    ("BASE", {}),
    ("S0-V1", {"s0_amp": 0.060}),
    ("S0-V2", {"s0_vdu": 0.85}),
    ("S0-V3", {"s0_ign_vol": 1.00}),
    ("S4-V1", {"s4_gap_open": 1.005, "s4_gap_close": 1.025}),
    ("S4-V2", {"s4_vol": 1.2}),
    ("S4-V3", {"s4_box": 0.120}),
]

CUR = {"s0_amp": 0.042, "s0_vdu": 0.65, "s0_ign_vol": 1.20,
       "s4_gap_open": 1.010, "s4_gap_close": 1.032, "s4_vol": 1.5, "s4_box": 0.090}


def ud_ratio(close, op, vol, i):
    """calc_up_down_vol_ratio(df.iloc[i-9:i+1]) 的陣列版。"""
    c = close[i - 9:i + 1]
    o = op[i - 9:i + 1]
    v = vol[i - 9:i + 1]
    up = v[c >= o]
    dn = v[c < o]
    up_avg = float(up.mean()) if len(up) else 1.0
    dn_avg = float(dn.mean()) if len(dn) else max(up_avg * 0.5, 1.0)
    return round(up_avg / max(dn_avg, 1.0), 2)


def signals_for_day(a, i, bench_ret20, m_ok, p):
    """重現 evaluate_candidate 的 S0/S3/S4/BRK 匹配（已過共用前置篩選）。
    a: 各欄位 numpy 陣列 dict；回傳 [(strategy, entry, stop)]（優先序順）。"""
    close, high, low, op = a["Close"], a["High"], a["Low"], a["Open"]
    vol, ma5, ma10, ma20, ma60 = a["vol"], a["MA5"], a["MA10"], a["MA20"], a["MA60"]
    mv5, mv20, rsi = a["MV5"], a["MV20"], a["RSI14"]
    if np.isnan(ma60[i]) or mv20[i] < CONFIG["MIN_MV20_LOTS"] or close[i] < CONFIG["MIN_PRICE"]:
        return []
    if not m_ok:
        return []
    ret20 = (close[i] - close[i - 20]) / close[i - 20]
    ret40 = (close[i] - close[i - 40]) / close[i - 40]
    rs_score = round((0.6 * ret20 + 0.4 * ret40) * 100, 2)
    rs_strong = (ret20 >= bench_ret20 + 0.015) and (rs_score >= 3.5)
    ud = ud_ratio(close, op, vol, i)
    s_gates_ok = rs_strong and ud >= 1.30
    if not s_gates_ok:
        # BRK 不經 S 家族閘門，仍需往下判斷
        pass
    trend_up = (close[i] > ma20[i] > ma60[i]) and (ma20[i] > ma20[i - 4]) and (ma60[i] > ma60[i - 4])
    candle_pos = (close[i] - low[i]) / max(high[i] - low[i], 0.01)
    prev_4_high = high[i - 4:i].max()
    prev_10_high = high[i - 10:i].max()
    prev_20_high = high[i - 20:i].max()
    matched = []

    # S0
    amp_4d = (high[i - 4:i].max() - low[i - 4:i].min()) / max(low[i - 4:i].min(), 1)
    vdu_ok = bool((vol[i - 4:i] < p["s0_vdu"] * mv20[i]).any())
    if s_gates_ok and trend_up and (amp_4d <= p["s0_amp"]) and vdu_ok and \
       (close[i] > prev_4_high) and (close[i] > op[i] * 1.012) and \
       (vol[i] >= p["s0_ign_vol"] * mv5[i]) and (candle_pos >= 0.70) and (55 <= rsi[i] <= 72):
        entry = round(float(close[i]), 2)
        stop = round(float(max(low[i - 4:i + 1].min() * 0.996, entry * 0.975)), 2)
        matched.append(("S0_VCP收斂", entry, stop))

    # S3（現行，逐字重現）
    dipped = bool((low[i - 3:i] < ma10[i - 3:i] * 0.996).any())
    power = (close[i] > prev_4_high) and (close[i] >= ma5[i]) and (close[i] >= ma10[i]) and \
            (close[i] >= op[i] * 1.025) and (candle_pos >= 0.75) and (vol[i] >= 1.35 * mv5[i])
    if s_gates_ok and trend_up and dipped and power and (ud >= 1.40) and (55 <= rsi[i] <= 68):
        entry = round(float(close[i]), 2)
        stop = round(float(max(low[i] * 0.996, entry * 0.975)), 2)
        matched.append(("S3_破底翻2B", entry, stop))

    # S4
    box_amp_10 = (high[i - 10:i].max() - low[i - 10:i].min()) / max(low[i - 10:i].min(), 1)
    breakout_power = (close[i] > prev_10_high) and \
                     ((op[i] > close[i - 1] * p["s4_gap_open"]) or (close[i] >= close[i - 1] * p["s4_gap_close"]))
    if s_gates_ok and trend_up and (box_amp_10 <= p["s4_box"]) and breakout_power and \
       (candle_pos >= 0.72) and (0 <= (close[i] - ma20[i]) / ma20[i] <= 0.085) and \
       (vol[i] >= p["s4_vol"] * mv20[i]):
        entry = round(float(close[i]), 2)
        stop = round(float(max(low[i] * 0.995, entry * 0.975)), 2)
        matched.append(("S4_威力跳空", entry, stop))

    # BRK20（現行，優先序最低）
    if (close[i] > prev_20_high) and (vol[i] >= 1.5 * mv20[i]) and (close[i] > ma60[i]):
        entry = round(float(close[i]), 2)
        stop = round(float(max(low[i] * 0.995, entry * 0.975)), 2)
        matched.append(("BRK20_突破量增", entry, stop))

    out = []
    for strat, entry, stop in matched:
        pos = calc_position_size(entry, stop)
        if pos["lots"] <= 0:
            continue
        out.append((strat, entry, pos["adjusted_stop"]))
    return out


def summarize(trades):
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "avg_return_pct": 0.0,
                "rr_ratio": 0.0, "years": 0, "first_half_avg": 0.0, "second_half_avg": 0.0}
    rows = sorted(trades, key=lambda r: r["entry_date"])
    wins = [r["net_return_pct"] for r in rows if r["net_return_pct"] > 0]
    losses = [r["net_return_pct"] for r in rows if r["net_return_pct"] <= 0]
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = abs(float(np.mean(losses))) if losses else 1.0
    mid = len(rows) // 2
    fh = rows[:mid] or rows
    sh = rows[mid:] or rows
    return {
        "trades": len(rows),
        "win_rate": round(sum(1 for r in rows if r["win"]) / len(rows) * 100, 1),
        "avg_return_pct": round(float(np.mean([r["net_return_pct"] for r in rows])), 2),
        "rr_ratio": round(avg_win / max(avg_loss, 0.1), 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(-avg_loss, 2),
        "years": len({r["entry_date"][:4] for r in rows}),
        "first_half_avg": round(float(np.mean([r["net_return_pct"] for r in fh])), 2),
        "second_half_avg": round(float(np.mean([r["net_return_pct"] for r in sh])), 2),
    }


def run_system(stocks, bench_map, params):
    """stocks: [(sid, df_prepared)]；回傳 (trades, censored, skipped_full, skipped_busy)。"""
    trades = []
    censored = skipped_full = skipped_busy = 0
    open_positions = []
    for sid, df in stocks:
        a = {c: df[c].to_numpy(dtype=float) for c in
             ["Close", "High", "Low", "Open", "MA5", "MA10", "MA20", "MA60", "MV5", "MV20", "RSI14"]}
        a["vol"] = df["vol_lots"].to_numpy(dtype=float)
        idx = df.index
        for i in range(60, len(df) - 1):
            b = bench_map.get(idx[i])
            if b is None:
                continue
            b_close, b_ma20, b_ret20 = b
            if np.isnan(b_ma20) or np.isnan(b_ret20):
                continue
            ret20 = (a["Close"][i] - a["Close"][i - 20]) / a["Close"][i - 20]
            m_ok = market_regime_ok(float(b_close), float(b_ma20), float(ret20))
            sigs = signals_for_day(a, i, float(b_ret20), m_ok, params)
            if not sigs:
                continue
            strat, signal_close, stop_p = sigs[0]
            nxt = df.iloc[i + 1]
            n_open, n_high, n_close = float(nxt["Open"]), float(nxt["High"]), float(nxt["Close"])
            if n_open > signal_close * 1.028 or n_open <= stop_p:
                continue
            trigger_buy = round(max(signal_close, n_open * 1.003), 2)
            if n_high < trigger_buy or n_close < n_open * 0.998:
                continue
            entry_date = idx[i + 1]
            overlapping = [p for p in open_positions if p["entry_date"] <= entry_date < p["exit_date"]]
            if any(p["stock_id"] == sid for p in overlapping):
                skipped_busy += 1
                continue
            if len(overlapping) >= MAX_CONCURRENT:
                skipped_full += 1
                continue
            result = simulate_exit(df, i + 1, trigger_buy, stop_p)
            if result["censored"]:
                censored += 1
                continue
            exit_date = idx[result["exit_idx"]]
            trades.append({"stock_id": sid, "strategy": strat,
                           "entry_date": entry_date.strftime("%Y-%m-%d"),
                           "exit_date": exit_date.strftime("%Y-%m-%d"),
                           "net_return_pct": result["net_ret_pct"], "win": result["win"]})
            open_positions.append({"stock_id": sid, "entry_date": entry_date, "exit_date": exit_date})
    return trades, censored, skipped_full, skipped_busy


def main():
    universe = build_dynamic_index_universe()
    sample_items = list(universe.items())[:220]
    tickers = ["0050.TW"] + [f"{sid}{sfx}" for sid, (_, sfx) in sample_items]
    data = yf.download(tickers, start="2020-01-01", group_by="ticker", threads=True, progress=False)

    bench_df = data["0050.TW"].dropna(subset=["Close"]).copy()
    bench_df["B_MA20"] = bench_df["Close"].rolling(20).mean()
    bench_df["B_RET20"] = bench_df["Close"].pct_change(20)
    bench_map = {dt: (float(r["Close"]), float(r["B_MA20"]), float(r["B_RET20"]))
                 for dt, r in bench_df.iterrows()}

    stocks = []
    for sid, (sname, sfx) in sample_items:
        t_sym = f"{sid}{sfx}"
        if t_sym not in data.columns.get_level_values(0):
            continue
        df_raw = data[t_sym].dropna(subset=["Close", "High", "Low", "Open", "Volume"]).copy()
        if len(df_raw) < 110:
            continue
        stocks.append((sid, prepare_indicators(df_raw)))
    print(f"stocks loaded: {len(stocks)}")

    systems = {}
    for name, delta in SYSTEMS:
        params = dict(CUR)
        params.update(delta)
        trades, cens, s_full, s_busy = run_system(stocks, bench_map, params)
        by_strat = {}
        for s in ["S0_VCP收斂", "S3_破底翻2B", "S4_威力跳空", "BRK20_突破量增"]:
            by_strat[s] = summarize([t for t in trades if t["strategy"] == s])
        # 系統整體 RR / MDD（與 backtest.py 同式）
        df_t = pd.DataFrame(sorted(trades, key=lambda r: r["entry_date"]))
        if not df_t.empty:
            df_t["port_impact"] = df_t["net_return_pct"] * SLOT_WEIGHT
            eq = 100.0 * (1.0 + df_t["port_impact"] / 100.0).cumprod()
            dd = ((eq - eq.cummax()) / eq.cummax()) * 100.0
            wins = df_t[df_t["net_return_pct"] > 0]["net_return_pct"]
            losses = df_t[df_t["net_return_pct"] <= 0]["net_return_pct"]
            aw = float(wins.mean()) if not wins.empty else 0.0
            al = abs(float(losses.mean())) if not losses.empty else 1.0
            sys_rr, sys_mdd = round(aw / max(al, 0.1), 2), round(float(dd.min()), 2)
            sys_wr = round(float(df_t["win"].mean() * 100), 1)
        else:
            sys_rr, sys_mdd, sys_wr = 0.0, 0.0, 0.0
        systems[name] = {
            "total_trades": len(trades), "win_rate": sys_wr, "rr_ratio": sys_rr, "mdd_pct": sys_mdd,
            "by_strategy": by_strat, "censored": cens,
            "skipped_full": s_full, "skipped_busy": s_busy,
        }
        print(f"[{name}] total={len(trades)} RR={sys_rr} MDD={sys_mdd} | " +
              " | ".join(f"{s}:{by_strat[s]['trades']}筆/{by_strat[s]['win_rate']}%/{by_strat[s]['avg_return_pct']:+.2f}%/RR{by_strat[s]['rr_ratio']}"
                         for s in by_strat))

    # 判決（依預註冊標準自動判定，僅供判讀參考）
    base = systems["BASE"]["by_strategy"]
    base_sys_rr = systems["BASE"]["rr_ratio"]
    verdicts = {}
    for name, target, floor_n in [("S0-V1", "S0_VCP收斂", 50), ("S0-V2", "S0_VCP收斂", 50),
                                  ("S0-V3", "S0_VCP收斂", 50), ("S4-V1", "S4_威力跳空", 28),
                                  ("S4-V2", "S4_威力跳空", 28), ("S4-V3", "S4_威力跳空", 28)]:
        v = systems[name]["by_strategy"][target]
        b = base[target]
        # 校準：BASE 若偏離 backtest 基準 ±30%，錨點改用 BASE×2（預註冊校準規則）
        anchor_n = {"S0_VCP收斂": 26, "S4_威力跳空": 14}[target]
        lo, hi = anchor_n * 0.7, anchor_n * 1.3
        calibrated = not (lo <= b["trades"] <= hi)
        freq_need = max(2 * b["trades"], floor_n) if calibrated else max(2 * anchor_n, floor_n)
        s3 = systems[name]["by_strategy"]["S3_破底翻2B"]
        brk = systems[name]["by_strategy"]["BRK20_突破量增"]
        checks = {
            "freq_ge_2x_and_floor": v["trades"] >= freq_need,
            "rr_ge_1.8": v["rr_ratio"] >= 1.8,
            "avg_positive": v["avg_return_pct"] > 0,
            "halves_positive": v["first_half_avg"] > 0 and v["second_half_avg"] > 0,
            "years_ge_4": v["years"] >= 4,
            "no_harm_s3_brk": (s3["avg_return_pct"] > 0 and brk["avg_return_pct"] > 0 and
                               s3["avg_return_pct"] >= base["S3_破底翻2B"]["avg_return_pct"] - 1.0 and
                               brk["avg_return_pct"] >= base["BRK20_突破量增"]["avg_return_pct"] - 1.0),
            "sys_rr_not_degraded": systems[name]["rr_ratio"] >= base_sys_rr - 0.10,
        }
        verdicts[name] = {"target": target, "variant": v, "checks": checks,
                          "passed": all(checks.values()), "calibrated_anchor": calibrated,
                          "freq_need": freq_need,
                          "overfit_flag": (target == "S4_威力跳空" and v["trades"] < 50)}

    out = {"generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "window": "2020-01-01~", "prereg": "S04_OPT_PREREG_2026-10-24.md",
           "systems": systems, "verdicts": verdicts,
           "notes": "法人濾網未套用（無歷史資料）；名單為目前成分股，非 point-in-time，僅供相對比較；"
                    "BRK20 在 backtest.py TG 細項缺失問題與本實驗室無關（本實驗室直接統計）。"}
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n===== S0/S4 優化判決（預註冊標準） =====")
    for name, vd in verdicts.items():
        v = vd["variant"]
        print(f"{name} [{vd['target']}] {v['trades']}筆 勝率{v['win_rate']}% 平均{v['avg_return_pct']:+.2f}% "
              f"RR{v['rr_ratio']} 前半{v['first_half_avg']:+.2f}% 後半{v['second_half_avg']:+.2f}% "
              f"年份{v['years']} → {'過關' if vd['passed'] else '未過關'} "
              f"{checks_str(vd['checks'])}" + (" [S4 高過擬合風險：筆數<50]" if vd["overfit_flag"] else ""))


def checks_str(checks):
    return " ".join(f"{k}={'✓' if val else '✗'}" for k, val in checks.items())


if __name__ == "__main__":
    main()
