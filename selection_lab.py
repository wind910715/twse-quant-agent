"""selection_lab.py — 進場選擇濾網實驗 (GitHub Actions 執行)
背景: RR 實驗已證出場參數在高原 (R0 RR 1.93), 槓桿轉向進場選擇。本實驗事前註冊三個濾網,
只用訊號日當下可得資訊, 與 S_ONLY 基準 (breakout_lab.run_book 同口徑) 對照:
  F1_NO_WIDE_ATR: 剔除 ATR%(14) 落在該股自身過去 250 日分布前 25% 寬的訊號 (停損距離過寬傷 RR)
  F2_RS_POS:      剔除訊號日 RS120 (120 日報酬) < 0 的訊號 (相對弱勢不接)
  F3_NO_CHASE:    剔除確認日開盤跳空 > +1.5% 的進場 (追價)
過關條件 (事前宣告): RR 高於基準 且 累計 >= 基準 -2pp 且 後半段平均 > 0。三者全過才建議採用該濾網。
"""
import json
from datetime import datetime

import numpy as np
import pandas as pd

from strategy_core import prepare_indicators
from breakout_lab import load_data, collect_s, run_book, send_telegram

RESULTS_FILE = "selection_lab_results.json"


def add_features(prepped, cands):
    cache = {}
    for c in cands:
        sid = c["sid"]
        df = prepped[sid]
        if sid not in cache:
            high, low, close = df["High"], df["Low"], df["Close"]
            prev_close = close.shift(1)
            tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
            atr_pct = tr.rolling(14).mean() / close * 100
            cache[sid] = (df, atr_pct, close)
        df, atr_pct, close = cache[sid]
        ei = df.index.get_loc(c["entry_idx_date"])
        si = ei - 1  # 訊號日
        ap = float(atr_pct.iloc[si])
        window = atr_pct.iloc[max(0, si - 250):si].dropna()
        c["atr_pctile"] = float((window < ap).mean() * 100) if len(window) >= 60 else 50.0
        c["rs120"] = float(close.iloc[si] / close.iloc[si - 120] - 1) * 100 if si >= 120 else 0.0
        c["gap_pct"] = float(df["Open"].iloc[ei] / close.iloc[si] - 1) * 100
    return cands


FILTERS = {
    "F1_NO_WIDE_ATR": lambda c: c["atr_pctile"] <= 75,
    "F2_RS_POS": lambda c: c["rs120"] >= 0,
    "F3_NO_CHASE": lambda c: c["gap_pct"] <= 1.5,
}


def main():
    print("loading data (3y)...", flush=True)
    data = load_data()
    print(f"stocks={len(data['stocks'])}", flush=True)
    prepped = {sid: prepare_indicators(df) for sid, (_, _, df) in data["stocks"].items()}
    s_cands = collect_s(data)
    print(f"S candidates: {len(s_cands)}", flush=True)
    s_cands = add_features(prepped, s_cands)

    results = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    results["BASE"] = run_book(prepped, s_cands)
    for name, keep in FILTERS.items():
        sub = [c for c in s_cands if keep(c)]
        results[name] = run_book(prepped, sub)
        results[name]["kept"] = len(sub)
        print(f"{name}: kept {len(sub)}/{len(s_cands)}", flush=True)
    base = results["BASE"]
    for name in FILTERS:
        r = results[name]
        results[name]["pass"] = bool(
            r.get("n") and base.get("n")
            and r["rr"] is not None and base["rr"] is not None and r["rr"] > base["rr"]
            and r["cum_slot20"] >= base["cum_slot20"] - 2
            and r["h2_avg"] > 0)
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    def fmt(tag, r):
        if not r.get("n"):
            return f"{tag}: 無交易"
        return (f"{tag}: {r['n']} 筆 勝率 {r['win']}% 平均 {r['avg']:+.2f}% RR {r['rr']} "
                f"累計 {r['cum_slot20']:+.2f}% 後半 {r['h2_avg']:+.2f}%")
    lines = ["🔎 進場選擇濾網實驗 (3 年, S 策略集)"]
    lines.append(fmt("BASE", base))
    for name in FILTERS:
        lines.append(fmt(name, results[name]) + (" ✅過關" if results[name]["pass"] else " ✖"))
    send_telegram("\n".join(lines))
    print("\n".join(lines))
    print("DONE")


if __name__ == "__main__":
    main()
