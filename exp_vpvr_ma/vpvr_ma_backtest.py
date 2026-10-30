#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
量＋均線策略回測引擎 v1（VPVR 支撐版）。
凍結規格：量價均線策略_凍結規格_v1.md（2026-10-30 使用者確認凍結）。
本程式為純回測：只下載公開日線價量、計算指標、模擬交易，不含任何外部通知、
不含任何憑證、不含任何交易指令。

實作選擇（規格未明訂處，事前寫死、測完不改）：
- 價格與量：yfinance 日線，與主系統 backtest.py 同呼叫（不指定 auto_adjust）。
- VPVR 分箱：箱寬 = 1% x 收盤_T；每根日線的全日成交量歸入收盤價所在分箱。
- 均箱量 = 窗口總量 / 有量分箱數；HVN = 量 >= 1.5 倍均箱量之箱。
- 價值區：自 POC 箱出發，逐次向相鄰箱中量較大的一側擴張（量相等時先向下擴張），
  直到累計量 >= 70% 窗口總量或無相鄰箱可擴；VAL 為價值區最低箱之下緣，
  VAH 為價值區最高箱之上緣。支撐區含價值區最低箱（即 VAL 所在箱），
  壓力區含價值區最高箱（即 VAH 所在箱）。
- VPVR 窗口須滿 60 根：訊號日前 60 個交易日資料不足者跳過該訊號日
  （暖機自 2025-09-01 起，故 2025 年 11 月上旬訊號多未滿 60 根，屬規格內排除，
  計入 coverage 的 skipped_insufficient_vpvr）。
- ATR14 = 訊號日含 T 向前 14 日真實波幅簡單平均。
- 布林標準差採樣本標準差（pandas std，ddof=1）；MACD 為 EMA12/EMA26（adjust=False）
  之差，Signal 為 DIF 的 9 日 EMA（adjust=False）。
- 兩半切法：以 2330 在交易窗口內的交易日序列對半切（前半 D[:n//2]、後半 D[n//2:]），
  交易按進場日歸屬前半或後半；切點日期寫入結果檔。
- 累計淨報酬 = 已實現交易淨報酬 % 之和（非複利）；MDD 以該累計曲線（按出場日排序）
  的峰值回落幅度計（百分點，報正數）。
- 窗口末未觸停損／停利者採剔除不計（censored），每組報剔除筆數。
- 全贏組合（無虧損筆）RR 記為通過（RR 概念上為無限大），PF 同理；
  無贏筆組合 RR = 0、PF = 0。
- 第二階段門檻：三個單濾網臂（CDP／布林／MACD）各至少有一組停損倍數通過六項，
  才計算底＋三者全上臂；否則門檻關閉，全上臂不計算、不列入候選。

用法：
  完整回測（GitHub Actions）：python exp_vpvr_ma/vpvr_ma_backtest.py
  本機冒煙（僅 1-2 檔，確認可跑與欄位齊全，不當正式結論）：
      python exp_vpvr_ma/vpvr_ma_backtest.py --smoke --out /tmp/vpvr_smoke.json
"""

import argparse
import json
import math
import os
import sys

import numpy as np
import pandas as pd
import yfinance as yf

VERSION = "v1"
TICKERS = {
    "2330": "2330.TW",
    "6669": "6669.TW",
    "8299": "8299.TWO",
    "8046": "8046.TW",
    "2376": "2376.TW",
    "3711": "3711.TW",
    "2308": "2308.TW",
    "6488": "6488.TWO",
    "2409": "2409.TW",
    "2327": "2327.TW",
    "0050": "0050.TW",
}
FETCH_START = "2025-09-01"
FETCH_END = "2026-10-31"  # exclusive，含 2026-10-30
TRADE_START = pd.Timestamp("2025-11-01")
TRADE_END = pd.Timestamp("2026-10-30")
MA_TREND = 20
MA_PULLBACK = 10
VPVR_WINDOW = 60
BIN_PCT = 0.01
HVN_MULT = 1.5
VALUE_AREA_PCT = 0.70
ATR_LEN = 14
STOP_MULTS = [0.5, 1.0, 1.5, 2.0]
TARGET_MULT = 2.0
MAX_HOLD = 20
COST_PCT = 0.585

ARMS = ["P1_MA10", "VPVR_BASE", "VPVR_CDP", "VPVR_BB", "VPVR_MACD", "VPVR_ALL"]
FILTER_ARMS = ["VPVR_CDP", "VPVR_BB", "VPVR_MACD"]


def true_range(df):
    prev_close = df["Close"].shift(1)
    hl = df["High"] - df["Low"]
    hc = (df["High"] - prev_close).abs()
    lc = (df["Low"] - prev_close).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1)


def vpvr_support_bins(window_df, close_t):
    """回傳 (support_bins, resistance_bins, poc_price, val, vah)。
    support_bins / resistance_bins 為 (low, high) 區間 list；無支撐區時 support_bins 為空。
    window_df：訊號日前 60 根日線（不含 T），須含 Close、Volume。
    """
    w = BIN_PCT * close_t
    if w <= 0:
        return [], [], None, None, None
    vol_by_bin = {}
    for _, row in window_df.iterrows():
        c = float(row["Close"])
        v = float(row["Volume"])
        if math.isnan(c) or math.isnan(v) or v <= 0:
            continue
        k = math.floor(c / w)
        vol_by_bin[k] = vol_by_bin.get(k, 0.0) + v
    if not vol_by_bin:
        return [], [], None, None, None
    total_vol = sum(vol_by_bin.values())
    n_bins = len(vol_by_bin)
    mean_bin_vol = total_vol / n_bins
    hvn_bins = {k for k, v in vol_by_bin.items() if v >= HVN_MULT * mean_bin_vol}
    poc_bin = max(vol_by_bin, key=lambda k: vol_by_bin[k])
    # 價值區擴張：自 POC 出發，向量較大的一側擴張（相等先向下）。
    va_bins = {poc_bin}
    cum = vol_by_bin[poc_bin]
    lo = hi = poc_bin
    target = VALUE_AREA_PCT * total_vol
    while cum < target:
        up_vol = vol_by_bin.get(hi + 1, 0.0)
        dn_vol = vol_by_bin.get(lo - 1, 0.0)
        if up_vol == 0.0 and dn_vol == 0.0:
            break
        if dn_vol >= up_vol:
            lo -= 1
            va_bins.add(lo)
            cum += dn_vol
        else:
            hi += 1
            va_bins.add(hi)
            cum += up_vol
    val = min(va_bins) * w
    vah = (max(va_bins) + 1) * w
    poc_price = (poc_bin + 0.5) * w
    support = set()
    resistance = set()
    for k in hvn_bins:
        if k * w <= close_t:
            support.add(k)
        else:
            resistance.add(k)
    if min(va_bins) * w <= close_t:
        support.add(min(va_bins))
    if max(va_bins) * w > close_t:
        resistance.add(max(va_bins))
    support_bins = sorted((k * w, (k + 1) * w) for k in support)
    resistance_bins = sorted((k * w, (k + 1) * w) for k in resistance)
    return support_bins, resistance_bins, poc_price, val, vah


def build_signals(df):
    """回傳 signals: dict arm -> list of dict；另回傳 coverage 資訊。"""
    df = df.copy()
    df["MA20"] = df["Close"].rolling(MA_TREND).mean()
    df["MA10"] = df["Close"].rolling(MA_PULLBACK).mean()
    df["TR"] = true_range(df)
    df["ATR14"] = df["TR"].rolling(ATR_LEN).mean()
    df["BB_std"] = df["Close"].rolling(MA_TREND).std(ddof=1)
    df["BB_up"] = df["MA20"] + 2.0 * df["BB_std"]
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    sig = dif.ewm(span=9, adjust=False).mean()
    df["MACD_DIF"] = dif
    df["MACD_SIG"] = sig
    df["CDP_prev"] = (df["High"].shift(1) + df["Low"].shift(1)
                      + 2.0 * df["Close"].shift(1)) / 4.0

    signals = {arm: [] for arm in ARMS}
    skipped_vpvr = 0
    n_in_window = 0
    for i in range(len(df)):
        date = df.index[i]
        if date < TRADE_START or date > TRADE_END:
            continue
        n_in_window += 1
        row = df.iloc[i]
        close_t = float(row["Close"])
        low_t = float(row["Low"])
        ma20 = row["MA20"]
        ma10 = row["MA10"]
        atr = row["ATR14"]
        if pd.isna(ma20) or pd.isna(ma10) or pd.isna(atr):
            continue
        trend_ok = close_t > ma20
        # P1 對照：MA20 趨勢＋MA10 拉回。
        if trend_ok and low_t <= ma10:
            signals["P1_MA10"].append(i)
        # VPVR 底：須滿 60 根前窗。
        if i < VPVR_WINDOW:
            skipped_vpvr += 1
            continue
        window_df = df.iloc[i - VPVR_WINDOW:i]
        support_bins, _, _, _, _ = vpvr_support_bins(window_df, close_t)
        touched = any(low_t <= b_high and close_t >= b_low
                      for b_low, b_high in support_bins)
        if trend_ok and touched:
            signals["VPVR_BASE"].append(i)
            cdp = row["CDP_prev"]
            if not pd.isna(cdp) and close_t > cdp:
                signals["VPVR_CDP"].append(i)
            bb_up = row["BB_up"]
            if not pd.isna(bb_up) and close_t <= bb_up:
                signals["VPVR_BB"].append(i)
            if not pd.isna(row["MACD_DIF"]) and not pd.isna(row["MACD_SIG"]) \
                    and row["MACD_DIF"] > row["MACD_SIG"]:
                signals["VPVR_MACD"].append(i)
            if (not pd.isna(cdp) and close_t > cdp
                    and not pd.isna(bb_up) and close_t <= bb_up
                    and not pd.isna(row["MACD_DIF"])
                    and not pd.isna(row["MACD_SIG"])
                    and row["MACD_DIF"] > row["MACD_SIG"]):
                signals["VPVR_ALL"].append(i)
    return df, signals, {"window_days": n_in_window, "skipped_insufficient_vpvr": skipped_vpvr}


def simulate_trades(df, signal_idx, stop_mult):
    """模擬一組訊號在某停損倍數下的交易。回傳 (trades, censored)。"""
    trades = []
    censored = 0
    for i in signal_idx:
        sig_date = df.index[i]
        atr = float(df.iloc[i]["ATR14"])
        if i + 1 >= len(df):
            continue
        entry_date = df.index[i + 1]
        if entry_date > TRADE_END:
            continue
        entry = float(df.iloc[i + 1]["Open"])
        if math.isnan(entry) or entry <= 0:
            continue
        stop_dist = atr * stop_mult
        stop_px = entry - stop_dist
        target_px = entry + TARGET_MULT * stop_dist
        exit_px = None
        exit_date = None
        outcome = None
        last_j = None
        for j in range(i + 1, min(i + 1 + MAX_HOLD, len(df))):
            last_j = j
            low_j = float(df.iloc[j]["Low"])
            high_j = float(df.iloc[j]["High"])
            if low_j <= stop_px:
                exit_px, exit_date, outcome = stop_px, df.index[j], "stop"
                break
            if high_j >= target_px:
                exit_px, exit_date, outcome = target_px, df.index[j], "target"
                break
        if outcome is None:
            if last_j is not None and (last_j - (i + 1) + 1) >= MAX_HOLD:
                exit_px = float(df.iloc[last_j]["Close"])
                exit_date = df.index[last_j]
                outcome = "timeout"
            else:
                censored += 1
                continue
        gross = (exit_px - entry) / entry * 100.0
        net = gross - COST_PCT
        trades.append({
            "signal_date": str(sig_date.date()),
            "entry_date": str(entry_date.date()),
            "exit_date": str(exit_date.date()),
            "entry": round(entry, 4),
            "exit": round(exit_px, 4),
            "outcome": outcome,
            "gross_pct": round(gross, 4),
            "net_pct": round(net, 4),
        })
    return trades, censored


def group_metrics(trades, censored, half_days_first):
    """計算一組交易的完整指標與六項判定。half_days_first：前半交易日集合（日期字串）。"""
    nets = [t["net_pct"] for t in trades]
    n = len(nets)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    if losses:
        rr = avg_win / abs(avg_loss) if avg_win > 0 else 0.0
    else:
        rr = None  # 無虧損：RR 概念上無限大，判通過
    expectancy = float(np.mean(nets)) if n else 0.0
    sum_win = float(sum(wins)) if wins else 0.0
    sum_loss = float(sum(losses)) if losses else 0.0
    pf = (sum_win / abs(sum_loss)) if sum_loss < 0 else (None if sum_win > 0 else 0.0)
    cumulative = float(sum(nets)) if n else 0.0
    # MDD：按出場日排序的累計曲線峰值回落（百分點）。
    mdd = 0.0
    if trades:
        ordered = sorted(trades, key=lambda t: (t["exit_date"], t["entry_date"]))
        peak = 0.0
        cum = 0.0
        for t in ordered:
            cum += t["net_pct"]
            peak = max(peak, cum)
            mdd = max(mdd, peak - cum)
    # 兩半。
    def half_stats(sub):
        sub_nets = [t["net_pct"] for t in sub]
        s_wins = [x for x in sub_nets if x > 0]
        s_losses = [x for x in sub_nets if x <= 0]
        s_avg_win = float(np.mean(s_wins)) if s_wins else 0.0
        if s_losses:
            s_rr = s_avg_win / abs(float(np.mean(s_losses))) if s_avg_win > 0 else 0.0
        else:
            s_rr = None
        return {
            "n": len(sub_nets),
            "net_sum": round(float(sum(sub_nets)), 4) if sub_nets else 0.0,
            "rr": (round(s_rr, 4) if s_rr is not None else None),
        }
    first = [t for t in trades if t["entry_date"] in half_days_first]
    second = [t for t in trades if t["entry_date"] not in half_days_first]
    fh = half_stats(first)
    sh = half_stats(second)
    # 每檔貢獻。
    per_ticker = {}
    for t in trades:
        per_ticker[t["ticker"]] = per_ticker.get(t["ticker"], 0.0) + t["net_pct"]
    per_ticker = {k: round(v, 4) for k, v in per_ticker.items()}
    if cumulative > 0:
        max_contrib = max((v / cumulative) for v in per_ticker.values()) if per_ticker else 0.0
    else:
        max_contrib = None
    rr_pass = (rr is None) or (rr >= 1.5)
    second_rr_pass = (sh["rr"] is None and sh["n"] > 0 and sh["net_sum"] > 0) \
        or (sh["rr"] is not None and sh["rr"] >= 1.0)
    criteria = {
        "n_ge_30": n >= 30,
        "rr_ge_1.5": bool(rr_pass),
        "win_rate_ge_45": win_rate >= 45.0,
        "expectancy_gt_0": expectancy > 0,
        "cumulative_gt_0_and_mdd_le_20": (cumulative > 0 and mdd <= 20.0),
        "halves_and_contrib": bool(
            fh["net_sum"] > 0 and sh["net_sum"] > 0 and second_rr_pass
            and max_contrib is not None and max_contrib <= 0.50),
    }
    return {
        "n": n,
        "censored": censored,
        "wins": len(wins),
        "win_rate": round(win_rate, 2),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "rr": (round(rr, 4) if rr is not None else None),
        "expectancy": round(expectancy, 4),
        "pf": (round(pf, 4) if pf is not None else None),
        "cumulative": round(cumulative, 4),
        "mdd": round(mdd, 4),
        "first_half": fh,
        "second_half": sh,
        "per_ticker_net": per_ticker,
        "max_ticker_contrib": (round(max_contrib, 4) if max_contrib is not None else None),
        "criteria": criteria,
        "pass": all(criteria.values()),
    }


def fetch_all(codes):
    symbols = [TICKERS[c] for c in codes]
    data = yf.download(symbols, start=FETCH_START, end=FETCH_END,
                       group_by="ticker", threads=True, progress=False)
    frames = {}
    for code, sym in zip(codes, symbols):
        try:
            if isinstance(data.columns, pd.MultiIndex):
                df = data[sym].copy()
            else:
                df = data.copy()
        except KeyError:
            frames[code] = None
            continue
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df = df.dropna(subset=["Close"])
        try:
            df.index = pd.to_datetime(df.index).tz_localize(None)
        except (TypeError, AttributeError):
            df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        frames[code] = df if len(df) else None
    return frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true",
                    help="冒煙模式：僅 2330 與 0050，不當正式結論")
    ap.add_argument("--out", default=None, help="結果 JSON 輸出路徑（預設為正式路徑）")
    ap.add_argument("--report", default=None, help="報告 MD 輸出路徑（預設為正式路徑）")
    args = ap.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    if args.smoke:
        codes = ["2330", "0050"]
        out_path = args.out or "/tmp/vpvr_smoke.json"
        report_path = args.report
    else:
        codes = list(TICKERS.keys())
        out_path = args.out or os.path.join(base_dir, "vpvr_ma_results.json")
        report_path = args.report or os.path.join(base_dir, "vpvr_ma_report.md")

    frames = fetch_all(codes)
    coverage = {}
    for code in codes:
        df = frames.get(code)
        if df is None:
            coverage[code] = {"rows": 0, "error": "no data"}
            continue
        win = df[(df.index >= TRADE_START) & (df.index <= TRADE_END)]
        coverage[code] = {
            "rows": int(len(df)),
            "first_date": str(df.index[0].date()),
            "last_date": str(df.index[-1].date()),
            "window_days": int(len(win)),
            "error": None,
        }

    # 兩半切點：以 2330（或首檔有資料者）在窗口內的交易日序列對半切。
    ref = frames.get("2330")
    if ref is None:
        ref = next((frames[c] for c in codes if frames.get(c) is not None), None)
    ref_days = sorted(str(d.date()) for d in ref.index
                      if TRADE_START <= d <= TRADE_END) if ref is not None else []
    half_n = len(ref_days) // 2
    first_half_days = set(ref_days[:half_n])
    split_info = {
        "reference": "2330",
        "total_window_days": len(ref_days),
        "first_half_end": ref_days[half_n - 1] if half_n else None,
        "second_half_start": ref_days[half_n] if half_n < len(ref_days) else None,
    }

    all_signals = {}
    all_frames = {}
    for code in codes:
        df = frames.get(code)
        if df is None:
            continue
        df2, signals, cov_extra = build_signals(df)
        all_frames[code] = df2
        coverage[code].update(cov_extra)
        for arm in ARMS:
            all_signals.setdefault(arm, {})[code] = signals[arm]

    signal_counts = {arm: {code: len(all_signals[arm].get(code, []))
                          for code in codes if code in all_frames}
                     for arm in ARMS}

    def run_arm(arm):
        groups = {}
        for mult in STOP_MULTS:
            trades = []
            censored = 0
            for code in codes:
                df = all_frames.get(code)
                if df is None:
                    continue
                t, c = simulate_trades(df, all_signals[arm].get(code, []), mult)
                for tr in t:
                    tr["ticker"] = code
                trades.extend(t)
                censored += c
            metrics = group_metrics(trades, censored, first_half_days)
            groups[str(mult)] = {"metrics": metrics, "trades": trades}
        return groups

    results_arms = {}
    for arm in ["P1_MA10", "VPVR_BASE", "VPVR_CDP", "VPVR_BB", "VPVR_MACD"]:
        results_arms[arm] = run_arm(arm)

    # 第二階段門檻：三個單濾網臂各至少一組停損通過六項，才計算全上臂。
    gate = {}
    for arm in FILTER_ARMS:
        gate[arm] = any(results_arms[arm][str(m)]["metrics"]["pass"] for m in STOP_MULTS)
    gate_open = all(gate.values())
    if gate_open:
        results_arms["VPVR_ALL"] = run_arm("VPVR_ALL")

    groups_out = []
    for arm, arm_groups in results_arms.items():
        for mult in STOP_MULTS:
            g = arm_groups[str(mult)]
            groups_out.append({
                "arm": arm,
                "stop_mult": mult,
                "metrics": g["metrics"],
                "trades": g["trades"],
            })

    result = {
        "version": VERSION,
        "spec": "量價均線策略_凍結規格_v1.md",
        "params": {
            "tickers": TICKERS,
            "fetch_start": FETCH_START,
            "fetch_end": FETCH_END,
            "trade_start": str(TRADE_START.date()),
            "trade_end": str(TRADE_END.date()),
            "ma_trend": MA_TREND,
            "ma_pullback": MA_PULLBACK,
            "vpvr_window": VPVR_WINDOW,
            "bin_pct": BIN_PCT,
            "hvn_mult": HVN_MULT,
            "value_area_pct": VALUE_AREA_PCT,
            "atr_len": ATR_LEN,
            "stop_mults": STOP_MULTS,
            "target_mult": TARGET_MULT,
            "max_hold": MAX_HOLD,
            "cost_pct": COST_PCT,
            "gate_open": gate_open,
            "gate_detail": gate,
        },
        "coverage": coverage,
        "signal_counts": signal_counts,
        "halves_split": split_info,
        "groups": groups_out,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    if report_path:
        write_report(result, report_path)
    print(f"version={VERSION} groups={len(groups_out)} gate_open={gate_open} out={out_path}")
    for g in groups_out:
        m = g["metrics"]
        print(f"{g['arm']} stop={g['stop_mult']} n={m['n']} win={m['win_rate']}% "
              f"rr={m['rr']} exp={m['expectancy']} cum={m['cumulative']} "
              f"mdd={m['mdd']} pass={m['pass']}")


def write_report(result, path):
    groups = result["groups"]
    def sort_key(g):
        m = g["metrics"]
        rr = m["rr"] if m["rr"] is not None else 999.0
        return (-rr, -m["expectancy"])
    groups_sorted = sorted(groups, key=sort_key)
    lines = []
    lines.append(f"# 量＋均線策略回測報告 v{result['version']}（VPVR 支撐版）")
    lines.append("")
    lines.append(f"規格：{result['spec']}；窗口：{result['params']['trade_start']}～"
                 f"{result['params']['trade_end']}；成本每筆扣 {result['params']['cost_pct']}%；"
                 f"兩半切點：前半迄 {result['halves_split']['first_half_end']}，"
                 f"後半起 {result['halves_split']['second_half_start']}。")
    lines.append(f"第二階段門檻（單濾網三臂各至少一組通過才開）："
                 f"{'開啟' if result['params']['gate_open'] else '未開啟'}"
                 f"（{result['params']['gate_detail']}）。")
    lines.append("判定：六項全過才算通過；任一項未過即不通過；不得挑單一指標最高者硬稱找到。")
    lines.append("")
    lines.append("| 組別 | 停損倍數 | n | 剔除 | 勝率% | 平均贏% | 平均輸% | RR | 期望% | PF | 累計% | MDD | 前半淨% | 後半淨% | 後半RR | 最大檔貢獻 | 判定 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for g in groups_sorted:
        m = g["metrics"]
        rr = "無虧損" if m["rr"] is None else m["rr"]
        pf = "無虧損" if m["pf"] is None else m["pf"]
        sh_rr = "無虧損" if m["second_half"]["rr"] is None else m["second_half"]["rr"]
        mc = m["max_ticker_contrib"] if m["max_ticker_contrib"] is not None else "-"
        lines.append(f"| {g['arm']} | {g['stop_mult']} | {m['n']} | {m['censored']} | "
                     f"{m['win_rate']} | {m['avg_win']} | {m['avg_loss']} | {rr} | "
                     f"{m['expectancy']} | {pf} | {m['cumulative']} | {m['mdd']} | "
                     f"{m['first_half']['net_sum']} | {m['second_half']['net_sum']} | {sh_rr} | "
                     f"{mc} | {'通過' if m['pass'] else '未過'} |")
    lines.append("")
    passing = [g for g in groups if g["metrics"]["pass"]]
    lines.append(f"## 判定結果：{'有 ' + str(len(passing)) + ' 組通過' if passing else '無組合通過'}")
    if passing:
        for g in passing:
            lines.append(f"- 通過：{g['arm']} 停損 {g['stop_mult']} 倍（n={g['metrics']['n']}，"
                         f"RR={g['metrics']['rr']}，期望={g['metrics']['expectancy']}，"
                         f"累計={g['metrics']['cumulative']}）。")
    else:
        lines.append("無組合通過六項，不得採納。最接近組合（按 RR 排序前三）：")
        for g in groups_sorted[:3]:
            m = g["metrics"]
            failed = [k for k, v in m["criteria"].items() if not v]
            lines.append(f"- {g['arm']} 停損 {g['stop_mult']} 倍：n={m['n']}，RR={m['rr']}，"
                         f"期望={m['expectancy']}，累計={m['cumulative']}；未過項目：{failed}。")
    lines.append("")
    lines.append("## 覆蓋與排除")
    for code, cov in result["coverage"].items():
        lines.append(f"- {code}：{cov}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
