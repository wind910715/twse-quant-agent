#!/usr/bin/env python3
"""dline_s3_lab.py — D 線情境三 S3 帳本驗證 (預註冊 DLINE_S3_PREREG_2026-10-24.md)

新檔實驗室，不動 strategy_core.py / CONFIG / S0-V1 / S3 / BRK20。
訊號定義抄 review-30k/event_study_30k.py 的 detect_s3()，一字不改
（見預註冊 §2；日線濾網沿用事件研究的「含當日收盤」口徑，
此為繼承自事件研究的同日資訊限制，見結果檔 caveats，不得事後改口徑）。

兩臂（事前分工，見預註冊 §1/§4）：
- PRIMARY：30K 原生出場引擎（review-30k/backtest_30k.py simulate_position
  的逐項移植）＋ 主系統組合紀律（5 槽位、每檔 20%、同股不重疊）。
  成本 0.585%/往返，成本會計原樣保留（全單一次往返扣除）。
- SECONDARY_LiteralCONFIG：字面套用 strategy_core.CONFIG 出場結構
  （TP1 6%/25%、TP2 14%/35%、TP3 25%/20%、鎖利、trailing、逐段成本），
  時間單位換算：MAX_HOLD 55 天 -> 495 根、3 日汰弱 -> 27 根。
  僅診斷，不參與判決。

資料：repo 內凍結的 Fugle 30K 還原快取（dline_data/fugle_30k_<code>.csv.gz，
2023-05-23 -> 2026-10-22，38 檔）。Actions 不連 Fugle/FinMind、不含憑證。
用法：
    python3 dline_s3_lab.py [--out dline_s3_results.json] [--codes 0050,1101]
    python3 dline_s3_lab.py --selftest   # 合成資料：訊號型態 + 主判臂出場分支
不發 Telegram；結果檔為唯一事實來源。
"""
import argparse
import gzip
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "dline_data")
RESULTS_FILE = "dline_s3_results.json"

# 事件研究同一批 38 檔 Fugle 成功者（預註冊 §3），不擴、不換。
CODES = ["0050", "0056", "00631L", "00637L", "00685L", "00753L", "00919",
         "00927", "00929", "1101", "1301", "1303", "1528", "1802", "1815",
         "2002", "2303", "2313", "2327", "2344", "2409", "2489", "2492",
         "2883", "2887", "3231", "3481", "3673", "4526", "4958", "4960",
         "6209", "6213", "6269", "6456", "6505", "6770", "8039"]
START = "2023-05-23"
END = "2026-10-22"
SPLIT_DATE = "2025-05-01"  # H1 < 此日，H2 >= 此日（進場日切分）
WARMUP_DAILY = 65
BUY_COST = 0.001425
SELL_COST = 0.004425
MAX_SLOTS = 5
SLOT_WEIGHT = 0.20
SEED = 42
# 主系統 CONFIG 字面值（診斷臂，見預註冊 §4.4；本檔不 import strategy_core，
# 避免實驗室反向影響生產模組，數值與 strategy_core.CONFIG 逐字對照）。
CFG = {"TP1": 0.060, "TP2": 0.140, "TP3": 0.250,
       "R1": 0.25, "R2": 0.35, "R3": 0.20,
       "LOCK1": 0.015, "LOCK2": 0.080, "LOCK3": 0.160,
       "TRAIL1": 0.055, "TRAIL2": 0.055, "TRAIL3": 0.060,
       "MAX_HOLD_BARS": 55 * 9, "WEAK_BARS": 3 * 9}


# ---------------------------------------------------------------- indicators
def calc_indicators(df):
    """抄 scanner.calc_30k_indicators（不含 wall-clock 丟尾巴：凍結歷史全已收盤）。"""
    df = df.dropna(subset=["Close", "Open", "High", "Low"]).copy()
    if len(df) < 65:
        return None
    df["MA5"] = df["Close"].rolling(5).mean()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA60"] = df["Close"].rolling(60).mean()
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26
    df["MACD"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["OSC"] = df["DIF"] - df["MACD"]
    return df


def daily_from_30k(df30):
    """抄 backtest_30k.daily_from_30k：由 30K 聚合日收盤。"""
    idx = df30.index.tz_convert("Asia/Taipei") if df30.index.tz is not None \
        else df30.index.tz_localize("Asia/Taipei")
    d = pd.DataFrame({"Close": df30["Close"].values}, index=idx)
    return d.resample("1D").last().dropna()


# ---------------------------------------------------------------- S3 pattern
def is_s3_pattern(osc_win, dif_win):
    """osc_win / dif_win 為長度 31 的陣列（最近 30 根 + 訊號根）。"""
    if len(osc_win) != 31:
        return False
    crosses = [k for k in range(1, 31) if osc_win[k - 1] <= 0 < osc_win[k]]
    if len(crosses) < 2:
        return False
    if crosses[-1] != 30:
        return False
    if dif_win[crosses[0]] >= 0:
        return False
    if dif_win[-1] <= 0 or osc_win[-1] <= 0:
        return False
    return True


def collect_s3_signals(df_ind, daily_close):
    """回傳訊號根位置 list。日線濾網以預算的日 MA 對照事件研究逐根 rolling，
    數學等價（見預註冊 §2）；含當日收盤口徑與事件研究一致。"""
    osc = df_ind["OSC"].to_numpy(dtype=float)
    dif = df_ind["DIF"].to_numpy(dtype=float)
    d_close = daily_close["Close"].to_numpy(dtype=float)
    d_dates = np.array([d.date() for d in daily_close.index])
    d_ma20 = pd.Series(d_close).rolling(20).mean().to_numpy()
    d_ma60 = pd.Series(d_close).rolling(60).mean().to_numpy()
    bar_dates = np.array([d.date() for d in df_ind.index])
    # 每根 K 對應的日線截止位置（<= 當日）
    didx = np.searchsorted(d_dates, bar_dates, side="right") - 1
    sigs = []
    for i in range(31, len(df_ind)):
        if not is_s3_pattern(osc[i - 30:i + 1], dif[i - 30:i + 1]):
            continue
        j = int(didx[i])
        if j + 1 < WARMUP_DAILY:  # 日線暖身不足（事件研究同條件）
            continue
        if np.isnan(d_ma60[j]):
            continue
        if d_close[j] < d_ma20[j] or d_close[j] < d_ma60[j]:
            continue
        if d_ma20[j] < d_ma20[j - 1]:
            continue
        sigs.append(i)
    return sigs


def levels_from_entry(entry, ma60_sig):
    """停損 / TP 水位（預註冊 §4.1/§4.3：以實際進場價重算，公式抄 30K 帳本）。"""
    stop = round(min(float(ma60_sig) * 0.995, entry * 0.985), 2)
    risk_amt = entry - stop
    tp1 = round(max(entry * 1.035, entry + risk_amt * 1.8), 2)
    tp2 = round(max(entry * 1.08, entry + risk_amt * 3.5), 2)
    return stop, tp1, tp2


# ---------------------------------------------------------------- primary exit (port of backtest_30k.simulate_position)
def sim_primary(df_ind, last_bars, sig_idx, entry_idx, entry, stop, tp1, tp2):
    """出場檢查自進場根開始（進場根 = 訊號次根）。成本會計原樣保留：
    全單一次往返扣除（TP1 半倉不另扣賣出成本），見預註冊 §4.3。"""
    entry_date = df_ind.index[entry_idx].date()
    tp1_hit = False
    remain = 1.0
    realized = 0.0
    n = len(df_ind)
    close_a = df_ind["Close"].to_numpy(dtype=float)
    low_a = df_ind["Low"].to_numpy(dtype=float)
    high_a = df_ind["High"].to_numpy(dtype=float)
    osc_a = df_ind["OSC"].to_numpy(dtype=float)
    ma20_a = df_ind["MA20"].to_numpy(dtype=float)
    for j in range(entry_idx, n):
        close, low, high = float(close_a[j]), float(low_a[j]), float(high_a[j])
        if close <= stop or low <= stop:
            exit_px = stop if low <= stop else close
            net = realized + remain * (exit_px / entry - 1) - BUY_COST - SELL_COST
            return {"net_pct": round(net * 100, 4), "exit_idx": j,
                    "exit_reason": "TP1後保本止盈清倉" if tp1_hit else "觸及硬停損清倉",
                    "censored": False}
        if close >= tp2 or high >= tp2:
            exit_px = tp2 if high >= tp2 else close
            net = realized + remain * (exit_px / entry - 1) - BUY_COST - SELL_COST
            return {"net_pct": round(net * 100, 4), "exit_idx": j,
                    "exit_reason": "達陣TP2波段目標清倉", "censored": False}
        if not tp1_hit and (close >= tp1 or high >= tp1):
            exec_tp1 = tp1 if high >= tp1 else close
            realized += 0.5 * (exec_tp1 / entry - 1)
            tp1_hit = True
            remain = 0.5
            stop = round(entry * 1.005, 2)
            continue
        osc_now = float(osc_a[j])
        osc_prev = float(osc_a[j - 1]) if j > 0 else 0.0
        if osc_now < 0 and osc_prev >= 0 and close < float(ma20_a[j]):
            net = realized + remain * (close / entry - 1) - BUY_COST - SELL_COST
            return {"net_pct": round(net * 100, 4), "exit_idx": j,
                    "exit_reason": "30K死叉破20MA清倉", "censored": False}
        if j in last_bars and not tp1_hit:
            days_held = (df_ind.index[j].date() - entry_date).days
            if days_held >= 5 and (close / entry - 1) < 0.01:
                net = realized + remain * (close / entry - 1) - BUY_COST - SELL_COST
                return {"net_pct": round(net * 100, 4), "exit_idx": j,
                        "exit_reason": "滿5日動能停滯清倉", "censored": False}
    return {"net_pct": None, "exit_idx": None, "exit_reason": "未出場(censored)",
            "censored": True}


# ---------------------------------------------------------------- secondary exit (literal CONFIG, scaled to bars)
def sim_literal(df_ind, entry_idx, entry, stop):
    """strategy_core.simulate_exit 的 30K 尺度移植（見預註冊 §4.4）。
    逐段賣出成本；MAX_HOLD 495 根；汰弱 27 根後判定；首根只看收盤破停損。"""
    tp1_p = entry * (1 + CFG["TP1"])
    tp2_p = entry * (1 + CFG["TP2"])
    tp3_p = entry * (1 + CFG["TP3"])
    n = len(df_ind)
    last_idx = min(entry_idx + CFG["MAX_HOLD_BARS"], n - 1)
    window_complete = (entry_idx + CFG["MAX_HOLD_BARS"]) <= (n - 1)
    close_a = df_ind["Close"].to_numpy(dtype=float)
    open_a = df_ind["Open"].to_numpy(dtype=float)
    high_a = df_ind["High"].to_numpy(dtype=float)
    low_a = df_ind["Low"].to_numpy(dtype=float)
    ma5_a = df_ind["MA5"].to_numpy(dtype=float)

    def settle(w, px):
        return w * (((px - entry) / entry) - SELL_COST)

    floor = stop
    close_trail = stop
    highest = entry
    hit1 = hit2 = hit3 = False
    realized = 0.0
    rem = 1.0
    exit_idx = last_idx
    exit_reason = "time_exit" if window_complete else "censored"
    closed = False
    for offset, j in enumerate(range(entry_idx, last_idx + 1)):
        bar_num = offset + 1
        o, h, lo, c = float(open_a[j]), float(high_a[j]), float(low_a[j]), float(close_a[j])
        ma5 = float(ma5_a[j]) if not np.isnan(ma5_a[j]) else c
        if bar_num >= 2:
            if o <= floor:
                realized += settle(rem, o); rem = 0.0
                exit_idx, exit_reason, closed = j, ("gap_stop" if not hit1 else "trail_exit"), True
                break
            if lo <= floor:
                realized += settle(rem, floor); rem = 0.0
                exit_idx, exit_reason, closed = j, ("hard_stop" if not hit1 else "trail_exit"), True
                break
            if hit1 and c <= close_trail:
                realized += settle(rem, c); rem = 0.0
                exit_idx, exit_reason, closed = j, "trail_close", True
                break
        else:
            if c <= stop:
                realized += settle(rem, stop); rem = 0.0
                exit_idx, exit_reason, closed = j, "hard_stop_day1", True
                break
        if not hit1 and h >= tp1_p:
            hit1 = True; realized += settle(CFG["R1"], tp1_p); rem = round(rem - CFG["R1"], 4)
        if hit1 and not hit2 and h >= tp2_p:
            hit2 = True; realized += settle(CFG["R2"], tp2_p); rem = round(rem - CFG["R2"], 4)
        if hit2 and not hit3 and h >= tp3_p:
            hit3 = True; realized += settle(CFG["R3"], tp3_p); rem = round(rem - CFG["R3"], 4)
        if (bar_num >= CFG["WEAK_BARS"]) and (not hit1) and (highest < entry * 1.025) \
                and (c < entry * 0.995) and (c < ma5):
            realized += settle(rem, c); rem = 0.0
            exit_idx, exit_reason, closed = j, "weak_30k", True
            break
        if c > highest:
            highest = c
        if hit3:
            floor = max(floor, entry * (1 + CFG["LOCK3"]))
            close_trail = max(close_trail, entry * 1.170, highest * (1 - CFG["TRAIL3"]))
        elif hit2:
            floor = max(floor, entry * (1 + CFG["LOCK2"]))
            close_trail = max(close_trail, entry * 1.085, highest * (1 - CFG["TRAIL2"]))
        elif hit1:
            floor = max(floor, entry * (1 + CFG["LOCK1"]))
            close_trail = max(close_trail, entry * 1.025, highest * (1 - CFG["TRAIL1"]))
        elif highest >= entry * 1.045:
            floor = max(floor, entry * 1.010)
        exit_idx = j
    if rem > 0 and not closed:
        if not window_complete:
            return {"net_pct": None, "exit_idx": last_idx, "exit_reason": "censored",
                    "censored": True}
        realized += settle(rem, float(close_a[last_idx])); rem = 0.0
        exit_reason = "time_exit"
    net = round((realized - BUY_COST) * 100, 2)
    return {"net_pct": net, "exit_idx": exit_idx, "exit_reason": exit_reason,
            "censored": False}


# ---------------------------------------------------------------- candidate -> trade -> portfolio
def build_trades(df_ind, sig_idx, arm):
    """對一檔的所有訊號根，套用進場護欄與指定臂出場，回傳 (trades, counts)。
    trades 已排除 censored；counts 含各類跳過數。"""
    n = len(df_ind)
    dates = pd.Series(df_ind.index.date, index=range(n))
    last_bars = set()
    for i in range(n):
        if i == n - 1 or dates.iloc[i + 1] != dates.iloc[i]:
            last_bars.add(i)
    close_a = df_ind["Close"].to_numpy(dtype=float)
    open_a = df_ind["Open"].to_numpy(dtype=float)
    ma60_a = df_ind["MA60"].to_numpy(dtype=float)
    counts = {"signals": len(sig_idx), "skipped_no_next_bar": 0, "skipped_gap_up": 0,
              "skipped_below_stop": 0, "skipped_risk_gate": 0, "censored": 0}
    trades = []
    for i in sig_idx:
        if i + 1 >= n:
            counts["skipped_no_next_bar"] += 1
            continue
        sig_close = float(close_a[i])
        entry = float(open_a[i + 1])
        if entry > sig_close * 1.028:
            counts["skipped_gap_up"] += 1
            continue
        ma60 = float(ma60_a[i])
        if np.isnan(ma60):
            counts["skipped_no_next_bar"] += 1
            continue
        stop, tp1, tp2 = levels_from_entry(entry, ma60)
        if entry <= stop:
            counts["skipped_below_stop"] += 1
            continue
        if (entry - stop) / entry > 0.038:
            counts["skipped_risk_gate"] += 1
            continue
        if arm == "primary":
            sim = sim_primary(df_ind, last_bars, i, i + 1, entry, stop, tp1, tp2)
        else:
            sim = sim_literal(df_ind, i + 1, entry, stop)
        if sim["censored"]:
            counts["censored"] += 1
            continue
        trades.append({
            "entry_time": df_ind.index[i + 1].strftime("%Y-%m-%d %H:%M"),
            "signal_time": df_ind.index[i].strftime("%Y-%m-%d %H:%M"),
            "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
            "exit_time": df_ind.index[sim["exit_idx"]].strftime("%Y-%m-%d %H:%M"),
            "net_pct": sim["net_pct"], "exit_reason": sim["exit_reason"],
        })
    return trades, counts


def build_random_trades(df_ind, count, arm):
    """同口徑隨機基準：每檔種子 42、區間 [65, n)（事件研究原式），
    不套日線濾網與 S3 型態，其餘管線與主判臂完全相同。"""
    n = len(df_ind)
    rng = np.random.default_rng(SEED)
    idxs = rng.integers(65, n, size=count).tolist() if n > 66 and count > 0 else []
    dates = pd.Series(df_ind.index.date, index=range(n))
    last_bars = set()
    for i in range(n):
        if i == n - 1 or dates.iloc[i + 1] != dates.iloc[i]:
            last_bars.add(i)
    close_a = df_ind["Close"].to_numpy(dtype=float)
    open_a = df_ind["Open"].to_numpy(dtype=float)
    ma60_a = df_ind["MA60"].to_numpy(dtype=float)
    counts = {"signals": count, "skipped_no_next_bar": 0, "skipped_gap_up": 0,
              "skipped_below_stop": 0, "skipped_risk_gate": 0, "censored": 0}
    trades = []
    for i in idxs:
        if i + 1 >= n:
            counts["skipped_no_next_bar"] += 1
            continue
        sig_close = float(close_a[i])
        entry = float(open_a[i + 1])
        if entry > sig_close * 1.028:
            counts["skipped_gap_up"] += 1
            continue
        ma60 = float(ma60_a[i])
        if np.isnan(ma60):
            counts["skipped_no_next_bar"] += 1
            continue
        stop, tp1, tp2 = levels_from_entry(entry, ma60)
        if entry <= stop:
            counts["skipped_below_stop"] += 1
            continue
        if (entry - stop) / entry > 0.038:
            counts["skipped_risk_gate"] += 1
            continue
        sim = sim_primary(df_ind, last_bars, i, i + 1, entry, stop, tp1, tp2) \
            if arm == "primary" else sim_literal(df_ind, i + 1, entry, stop)
        if sim["censored"]:
            counts["censored"] += 1
            continue
        trades.append({
            "entry_time": df_ind.index[i + 1].strftime("%Y-%m-%d %H:%M"),
            "entry": entry, "net_pct": sim["net_pct"], "exit_reason": sim["exit_reason"],
            "exit_time": df_ind.index[sim["exit_idx"]].strftime("%Y-%m-%d %H:%M"),
        })
    return trades, counts


def apply_portfolio(cands):
    """按進場時間排序、5 槽位、同股不重疊（breakout_lab.run_book 語義）。
    cands 須含 code / entry_time / exit_time。回傳 (admitted, counts)。"""
    admitted = []
    active = {}  # code -> exit_time 字串
    counts = {"skipped_stock_busy": 0, "skipped_portfolio_full": 0}
    for c in sorted(cands, key=lambda x: (x["entry_time"], x["code"])):
        code = c["code"]
        if code in active and active[code] >= c["entry_time"]:
            counts["skipped_stock_busy"] += 1
            continue
        open_now = [s for s, until in active.items() if until >= c["entry_time"]]
        if len(open_now) >= MAX_SLOTS:
            counts["skipped_portfolio_full"] += 1
            continue
        admitted.append(c)
        active[code] = c["exit_time"]
    return admitted, counts


def summarize(trades):
    """trades 為已完成交易（entry_time 順序為組合曲線順序，與 backtest.py 一致）。"""
    if not trades:
        return {"n": 0, "win_rate": 0.0, "mean_pct": 0.0, "avg_win_pct": 0.0,
                "avg_loss_pct": 0.0, "rr": 0.0, "total_sum_pct": 0.0,
                "cum_slot20_pct": 0.0, "mdd_compounded_pct": 0.0, "mdd_pp_arith": 0.0,
                "h1": {"n": 0, "mean_pct": 0.0, "total_pct": 0.0, "win_rate": 0.0},
                "h2": {"n": 0, "mean_pct": 0.0, "total_pct": 0.0, "win_rate": 0.0},
                "by_reason": {}, "by_year": {}}
    rets = [t["net_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = abs(float(np.mean(losses))) if losses else 0.0
    equity, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        equity *= (1 + SLOT_WEIGHT * r / 100.0)
        peak = max(peak, equity)
        mdd = min(mdd, (equity / peak - 1) * 100)
    curve, pk, mdd_pp = [0.0], 0.0, 0.0
    for t in sorted(trades, key=lambda x: x["exit_time"]):
        curve.append(curve[-1] + t["net_pct"])
    for v in curve:
        pk = max(pk, v)
        mdd_pp = min(mdd_pp, v - pk)

    def half(sub):
        if not sub:
            return {"n": 0, "mean_pct": 0.0, "total_pct": 0.0, "win_rate": 0.0}
        rs = [t["net_pct"] for t in sub]
        return {"n": len(sub), "mean_pct": round(float(np.mean(rs)), 4),
                "total_pct": round(sum(rs), 2),
                "win_rate": round(sum(1 for r in rs if r > 0) / len(rs) * 100, 2)}

    h1 = [t for t in trades if t["entry_time"] < SPLIT_DATE]
    h2 = [t for t in trades if t["entry_time"] >= SPLIT_DATE]
    by_reason = {}
    for t in trades:
        by_reason.setdefault(t["exit_reason"], []).append(t["net_pct"])
    by_year = {}
    for t in trades:
        by_year.setdefault(t["entry_time"][:4], []).append(t["net_pct"])
    return {
        "n": len(trades),
        "win_rate": round(len(wins) / len(rets) * 100, 2),
        "mean_pct": round(float(np.mean(rets)), 4),
        "avg_win_pct": round(avg_win, 4), "avg_loss_pct": round(-avg_loss, 4),
        "rr": round(avg_win / avg_loss, 4) if wins and losses else 0.0,
        "total_sum_pct": round(sum(rets), 2),
        "cum_slot20_pct": round((equity - 1) * 100, 2),
        "mdd_compounded_pct": round(mdd, 2),
        "mdd_pp_arith": round(mdd_pp, 2),
        "h1": half(h1), "h2": half(h2),
        "by_reason": {k: {"n": len(v), "mean_pct": round(float(np.mean(v)), 4)}
                      for k, v in by_reason.items()},
        "by_year": {y: {"n": len(v), "mean_pct": round(float(np.mean(v)), 4),
                        "total_pct": round(sum(v), 2)} for y, v in sorted(by_year.items())},
    }


# ---------------------------------------------------------------- data
def load_stock(code):
    path = os.path.join(DATA_DIR, f"fugle_30k_{code}.csv.gz")
    if not os.path.exists(path):
        return None, 0
    df = pd.read_csv(path, index_col=0, parse_dates=True, compression="gzip")
    if df.index.tz is None:
        df.index = df.index.tz_localize("Asia/Taipei")
    else:
        df.index = df.index.tz_convert("Asia/Taipei")
    return df.sort_index(), len(df)


# ---------------------------------------------------------------- selftest
def selftest():
    # 1. 型態：兩次翻正、第一次 DIF<0、最後一次即訊號根 -> True；變體皆 False
    # 翻正點：k=2（第一次，DIF<0）與 k=30（訊號根，DIF>0），中間回到負值
    osc = np.array([-0.5, -0.4, 0.1, 0.05, -0.1] + [-0.2] * 24 + [-0.1, 0.2], dtype=float)
    dif = np.array([-1.0, -0.9, -0.5, -0.4, -0.3] + [-0.2] * 24 + [-0.1, 0.4], dtype=float)
    assert len(osc) == 31 and is_s3_pattern(osc, dif) is True
    dif_bad_first = dif.copy(); dif_bad_first[2] = 0.5
    assert is_s3_pattern(osc, dif_bad_first) is False  # 第一次翻正 DIF>=0
    osc_one = np.array([-0.5] * 30 + [0.2], dtype=float)
    assert is_s3_pattern(osc_one, dif) is False  # 僅一次翻正
    osc_not_last = np.array([-0.5, -0.4, 0.1, 0.05, -0.1, -0.05, 0.2, -0.1] + [0.0] * 23, dtype=float)
    assert is_s3_pattern(osc_not_last, dif) is False  # 最後一次翻正不是訊號根
    # 2. 主判臂出場分支（平坦合成序列，抄 backtest_30k selftest 的控制法）
    days = pd.bdate_range("2026-01-02", periods=20)
    bars = []
    for d in days:
        for hh, mm in [(9, 0), (9, 30), (10, 0), (10, 30), (11, 0), (11, 30),
                       (12, 0), (12, 30), (13, 0)]:
            bars.append(pd.Timestamp(d.year, d.month, d.day, hh, mm, tz="Asia/Taipei"))
    idx = pd.DatetimeIndex(bars)
    base = pd.DataFrame({"Open": 100.0, "High": 100.3, "Low": 99.7, "Close": 100.0,
                         "Volume": 1000}, index=idx)
    df_ind = calc_indicators(base)
    assert df_ind is not None and len(df_ind) > 70
    last_bars = {i for i in range(len(df_ind))
                 if i == len(df_ind) - 1 or df_ind.index[i + 1].date() != df_ind.index[i].date()}
    sig = {"close": 100.0, "stop": 98.0, "tp1": 103.5, "tp2": 108.0}
    e = 69  # 訊號根；進場根 e+1
    df2 = df_ind.copy()
    df2.iloc[e + 1, df2.columns.get_loc("Low")] = 97.0
    r = sim_primary(df2, last_bars, e, e + 1, 100.0, 98.0, 103.5, 108.0)
    assert r["exit_reason"] == "觸及硬停損清倉" and abs(r["net_pct"] - (-2.585)) < 0.01, r
    df3 = df_ind.copy()
    df3.iloc[e + 1, df3.columns.get_loc("High")] = 104.0
    df3.iloc[e + 2, df3.columns.get_loc("Low")] = 100.4
    df3.iloc[e + 2, df3.columns.get_loc("Close")] = 100.4
    r = sim_primary(df3, last_bars, e, e + 1, 100.0, 98.0, 103.5, 108.0)
    assert r["exit_reason"] == "TP1後保本止盈清倉" and abs(r["net_pct"] - 1.415) < 0.01, r
    df4 = df_ind.copy()
    df4.iloc[e + 1, df4.columns.get_loc("High")] = 109.0
    r = sim_primary(df4, last_bars, e, e + 1, 100.0, 98.0, 103.5, 108.0)
    assert r["exit_reason"] == "達陣TP2波段目標清倉" and abs(r["net_pct"] - 7.415) < 0.01, r
    print("selftest 全過：S3 型態 4 例 + 主判臂停損/TP1保本/TP2 分支")
    return 0


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=RESULTS_FILE)
    ap.add_argument("--codes", default=None, help="逗號分隔，僅跑指定檔（本地輕量檢查用）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    codes = [c.strip() for c in args.codes.split(",")] if args.codes else CODES
    per_stock = {}
    cands_primary, cands_literal, cands_base = [], [], []
    tot = {"primary": {"signals": 0, "skipped_no_next_bar": 0, "skipped_gap_up": 0,
                       "skipped_below_stop": 0, "skipped_risk_gate": 0, "censored": 0},
           "literal": {"signals": 0, "skipped_no_next_bar": 0, "skipped_gap_up": 0,
                       "skipped_below_stop": 0, "skipped_risk_gate": 0, "censored": 0},
           "baseline": {"signals": 0, "skipped_no_next_bar": 0, "skipped_gap_up": 0,
                        "skipped_below_stop": 0, "skipped_risk_gate": 0, "censored": 0}}
    for code in codes:
        df30, n_bars = load_stock(code)
        if df30 is None:
            print(f"  {code} 凍結資料缺失，跳過（Actions 不應發生）")
            per_stock[code] = {"bars": 0, "signals": 0, "error": "missing_data"}
            continue
        df_ind = calc_indicators(df30)
        daily = daily_from_30k(df30)
        if df_ind is None or daily is None:
            print(f"  {code} 指標不足，跳過")
            per_stock[code] = {"bars": n_bars, "signals": 0, "error": "insufficient"}
            continue
        sigs = collect_s3_signals(df_ind, daily)
        per_stock[code] = {"bars": n_bars, "signals": len(sigs),
                           "first_bar": str(df_ind.index[0]), "last_bar": str(df_ind.index[-1])}
        print(f"  {code} {n_bars} 根 S3 訊號 {len(sigs)}", flush=True)
        for arm, dest, key in (("primary", cands_primary, "primary"),
                               ("literal", cands_literal, "literal")):
            tr, ct = build_trades(df_ind, sigs, arm)
            for k in tot[key]:
                tot[key][k] += ct[k]
            for t in tr:
                t["code"] = code
                dest.append(t)
        tr_b, ct_b = build_random_trades(df_ind, len(sigs), "primary")
        for k in tot["baseline"]:
            tot["baseline"][k] += ct_b[k]
        for t in tr_b:
            t["code"] = code
            cands_base.append(t)
    adm_primary, pf_primary = apply_portfolio(cands_primary)
    adm_literal, pf_literal = apply_portfolio(cands_literal)
    adm_base, pf_base = apply_portfolio(cands_base)
    adm_primary = sorted(adm_primary, key=lambda x: x["entry_time"])
    adm_literal = sorted(adm_literal, key=lambda x: x["entry_time"])
    adm_base = sorted(adm_base, key=lambda x: x["entry_time"])
    sum_primary = summarize(adm_primary)
    sum_literal = summarize(adm_literal)
    sum_base = summarize(adm_base)
    # 判決（預註冊 §6，主判臂；診斷臂不參與）
    g1 = sum_primary["rr"] >= 1.8
    g2 = sum_primary["total_sum_pct"] > 0 and sum_primary["cum_slot20_pct"] > 0
    g3 = sum_primary["h1"]["mean_pct"] > 0 and sum_primary["h2"]["mean_pct"] > 0
    g4 = sum_primary["mean_pct"] > sum_base["mean_pct"] and sum_primary["rr"] > sum_base["rr"]
    if g1 and g2 and g3 and g4:
        verdict = "PASS_PRODUCTION_CANDIDATE"
    elif sum_primary["total_sum_pct"] <= 0:
        verdict = "REJECT"
    else:
        verdict = "LEDGER_CANDIDATE"
    results = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "prereg": "DLINE_S3_PREREG_2026-10-24.md",
        "source": "fugle_30k_frozen_cache", "start": START, "end": END,
        "universe_codes": codes, "universe_n": len(codes),
        "split_date": SPLIT_DATE, "seed_random_baseline": SEED,
        "costs": {"buy": BUY_COST, "sell": SELL_COST, "roundtrip": BUY_COST + SELL_COST},
        "slots": MAX_SLOTS, "slot_weight": SLOT_WEIGHT,
        "counts": {"primary": {**tot["primary"], **pf_primary},
                   "literal_config": {**tot["literal"], **pf_literal},
                   "baseline_random": {**tot["baseline"], **pf_base}},
        "primary": sum_primary, "literal_config": sum_literal, "baseline_random": sum_base,
        "gates": {"G1_rr_ge_1.8": g1, "G2_cum_positive": g2, "G3_halves_positive": g3,
                  "G4_beats_baseline": g4},
        "verdict": verdict,
        "per_stock": per_stock,
        "trades_primary": adm_primary, "trades_literal": adm_literal,
        "caveats": [
            "Fugle 分 K 自 2023-05-23 起，無 2020 崩盤與 2022 空頭，判決效力打折。",
            "日線多頭濾網沿用事件研究口徑：以含當日收盤的日線計算；訊號根若非當日最後一根，"
            "該濾網在進場時刻（次根開盤）尚未完全可知，屬同日資訊限制，生產化前需以落後一日線重驗。",
            "主判臂成本會計原樣沿用 30K 帳本（全單一次往返扣除），比主系統逐段計略寬鬆。",
            "Universe 為現今 38 檔成功者（非 point-in-time），仍有倖存者偏誤，僅供相對比較。",
            "日線 S3 值（67 筆、68.7%、+3.37%）為概念對照，持有期尺度不同，非過關門檻。",
        ],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    for name, s in (("PRIMARY", sum_primary), ("LITERAL_CONFIG", sum_literal),
                    ("BASELINE_RANDOM", sum_base)):
        print(f"{name}: {s['n']} 筆 勝率 {s['win_rate']}% 平均 {s['mean_pct']:+.4f}% "
              f"RR {s['rr']} 累計和 {s['total_sum_pct']:+.2f}% 槽位20%複利 {s['cum_slot20_pct']:+.2f}% "
              f"MDD {s['mdd_compounded_pct']:.2f}% H1 {s['h1']['mean_pct']:+.4f}% "
              f"H2 {s['h2']['mean_pct']:+.4f}%")
    print(f"判決: {verdict}  gates G1={g1} G2={g2} G3={g3} G4={g4}")
    print(f"結果已寫入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
