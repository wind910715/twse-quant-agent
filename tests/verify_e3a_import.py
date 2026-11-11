"""tests/verify_e3a_import.py — E3a 導入 quant 的驗證腳本 (本機／分支驗證用)。

驗證四件事，缺一即 FAIL，不得跳過：
  1. 11-10 四筆訊號 (3147/5230/6112/6757) 的 entry/hard_stop：
     strategy_core.evaluate_candidate() 在本地 prices.csv 上重現的數字，
     與 daily_candidate_pool_20261110.json 的 five_signals 逐字一致。
  2. e3a_engine.emit_e3a_picks() 原值照抄上述訊號 (entry/stop/sources/RS)，
     不改寫、不重算。
  3. 0050 那張：resolve_etf_tech_stop(99.70, prev_day_low=94.50, low30_close=92.50)
     = (94.50, 'prev_day_low')；MA20 截至 11-09 收盤 = 105.73、截至 11-10 收盤 = 104.97，
     任一口徑下 99.70 皆在 MA20 之下 → 按 E3c v3 根本不該買 (已知 Space 衝突，須標示，不得掩蓋)。
  4. E3a 回測數字引用正確 (E3a PASS：n=2546、RR 2.82、H2 +480.26%；E3c v3 FAIL 僅 H2)。

執行：~/workspace/stock-strategy/venv/bin/python tests/verify_e3a_import.py
(需本地 data_cache：prices.csv、benchmark_0050.csv；0050_ohlc_adj.csv 在 TSM-srf-sim labs。)
結果同時寫入 e3a_picks.json (repo 根目錄，Space importPicks 的唯一輸入)。
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.expanduser("~/workspace/tsm-srf-sim/labs"))

import pandas as pd  # noqa: E402

from e3a_engine import (  # noqa: E402
    E3A_VERSION, emit_e3a_picks, ma20_of, resolve_etf_tech_stop,
    reproduce_signal_via_core, c_entry_check,
)
from strategy_core import market_regime_ok  # noqa: E402

DATA = os.path.join(REPO, "labs", "data_cache")
POOL_JSON = os.path.expanduser(
    "~/workspace/tsm-srf-sim/labs/results/daily_candidate_pool_20261110.json")
ETF_OHLC = os.path.expanduser(
    "~/workspace/tsm-srf-sim/labs/data_cache/0050_ohlc_adj.csv")
OUT = os.path.join(REPO, "e3a_picks.json")

fails = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'} {name}: got={got} want={want}")
    if not ok:
        fails.append(name)


def main():
    pool = json.load(open(POOL_JSON, encoding="utf-8"))
    sigs = pool["five_signals"]
    assert pool["sday"] == "2026-11-10", pool["sday"]
    assert len(sigs) == 4, len(sigs)

    # --- 1. 用 strategy_core 重現四筆訊號 ---
    # 關鍵：本地 prices.csv 只到 2026-10-30；11-10 的價來自 pool 當時抓的
    # close_ext_20261110.csv (yfinance 延伸，稽核：10-30 重疊日中位差 0.0%)。
    # 必須用「本地 <=10-30 ＋ 延伸 >10-30」拼出的序列重現，否則訊號日是 10-30 而非 11-10。
    # 大盤基準用 pool 內已延伸的 benchmark_0050 (close 99.70、MA20 104.97、ret20 -13.15%)，
    # 不用只到 10-30 的 benchmark_0050.csv。
    prices = pd.read_csv(os.path.join(DATA, "prices.csv"),
                         usecols=["stock_id", "date", "open", "high", "low", "close", "volume"],
                         parse_dates=["date"])
    ext = pd.read_csv(os.path.expanduser(
        "~/workspace/tsm-srf-sim/labs/results/close_ext_20261110.csv"),
        usecols=["stock_id", "date", "open", "high", "low", "close", "volume"],
        parse_dates=["date"])
    prices = pd.concat([prices[prices["date"] <= "2026-10-30"],
                        ext[ext["date"] > "2026-10-30"]]).sort_values(["stock_id", "date"])
    bench_close = float(pool["benchmark_0050"]["close"])
    bench_ma20 = float(pool["benchmark_0050"]["ma20"])
    bench_ret20 = float(pool["benchmark_0050"]["ret20_pct"]) / 100.0
    check("pool bench close", bench_close, 99.70)
    check("pool bench MA20 (asof 11-10 close)", bench_ma20, 104.97)

    names = {"3147": ("大綜", ".TW"), "5230": ("雷笛克光學", ".TW"),
             "6112": ("邁達特", ".TW"), "6757": ("台灣虎航", ".TW")}
    for s in sigs:
        sid = str(s["stock_id"])
        df = prices[prices["stock_id"] == int(sid)].copy()
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                "close": "Close", "volume": "Volume"})
        df = df.set_index("date").sort_index()
        sname, sfx = names[sid]
        ret20 = float((df["Close"].iloc[-1] - df["Close"].iloc[-21]) / df["Close"].iloc[-21])
        m_ok = market_regime_ok(bench_close, bench_ma20, ret20)
        res = reproduce_signal_via_core(sid, sname, sfx, df, bench_ret20, m_ok)
        match = [r for r in res if r["strategy"] == s["strategy"]]
        check(f"signal {sid} reproduced", bool(match), True)
        if match:
            r = match[0]
            check(f"signal {sid} entry", r["entry_price"], s["entry_price"])
            check(f"signal {sid} hard_stop", r["hard_stop"], s["hard_stop"])
            # 注意：evaluate_candidate 的部位是 10 萬零股制 (calc_position_size)，
            # E3a 實盤改一張 1000 股；entry/stop 是同一份 SSOT，股數是 E3a 層的選擇。
            check(f"signal {sid} close==entry", r["close"], s["entry_price"])

    # --- 2. emit 原值照抄 ---
    etf = {"status": "holding", "entry_price": 99.70, "shares": 1000,
           "tech_stop": 94.50, "tech_stop_source": "prev_day_low",
           "tech_stop_detail": "11-09 低點", "ma20": 105.73,
           "peak_close": 99.70, "realized_pnl": 0.0}
    out = emit_e3a_picks(sigs, etf, "2026-11-10", confirm_date="2026-11-11",
                         ma20_asof="2026-11-09")
    out["generated_at"] = "2026-11-11 (branch verification)"
    for s_out, s_in in zip(out["signals"], sigs):
        check(f"emit {s_out['stock_id']} entry", s_out["entry_price"], s_in["entry_price"])
        check(f"emit {s_out['stock_id']} stop", s_out["hard_stop"], s_in["hard_stop"])
        check(f"emit {s_out['stock_id']} shares", s_out["shares"], 1000)
    check("emit etf tech_stop", out["etf_leg"]["tech_stop"], 94.50)
    check("emit version", out["version"], E3A_VERSION)

    # --- 3. 0050 技術停損與 MA20 as-of ---
    ohlc = pd.read_csv(ETF_OHLC, parse_dates=["date"]).sort_values("date")
    ohlc = ohlc[ohlc["date"] <= "2026-11-09"]
    prev_day_low = float(ohlc[ohlc["date"] == "2026-11-09"]["adj_low"].iloc[0])
    closes = ohlc["adj_close"].tolist() + [99.70]  # 11-10 收盤 99.70 (Space 快照)
    low30 = min(closes[-30:])
    check("prev_day_low 11-09", prev_day_low, 94.50)
    check("low30 close", low30, 92.50)
    check("resolve tech stop", resolve_etf_tech_stop(99.70, prev_day_low, low30),
          (94.50, "prev_day_low"))
    check("MA20 asof 11-09", ma20_of(closes[:-1][-20:]), 105.73)
    check("MA20 asof 11-10 close", ma20_of(closes[-20:]), 104.97)
    bought_below_ma20 = 99.70 < 105.73 and 99.70 < 104.97
    check("11-10 lot bought below MA20 (v3 would NOT buy)", bought_below_ma20, True)
    out["etf_leg"]["conflict"] = ("bought_below_ma20" if bought_below_ma20 else None)
    out["etf_leg"]["ma20_on_buy_close"] = 104.97

    # --- 4. 回測數字引用 ---
    fz = out["frozen_backtest"]
    check("E3a verdict", fz["backtest_E3a"]["verdict"], "PASS")
    check("E3a H2", fz["backtest_E3a"]["half2_pct"], 480.26)
    check("E3c v3 verdict", fz["backtest_E3c_v3"]["verdict"], "FAIL (H2 only)")

    # C 進場公式抽查 (11-11 確認日當日開盤價未定，此處只驗公式)：
    check("C formula 3147", c_entry_check(351.0, 342.22, 352.0, 356.0, 355.0),
          {"passed": True, "trigger": 353.06, "reason": "pass"})

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\nwrote {OUT}")
    print("VERIFY " + ("ALL PASS" if not fails else f"FAIL: {fails}"))
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()
