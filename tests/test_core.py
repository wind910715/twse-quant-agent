"""v2 核心單元測試: python -m pytest tests/test_core.py -q"""
import numpy as np
import pandas as pd
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy_core import (
    calc_position_size, calc_exit_plan, chip_is_positive, market_regime_ok,
    evaluate_candidate, prepare_indicators, simulate_exit, CONFIG,
)


# ---------- position sizing ----------
def test_high_price_stock_not_forced_one_lot():
    r = calc_position_size(2000, 1950)
    assert r["lots"] == 0 and r["reason"] == "one_lot_exceeds_limits", r
    assert r["one_lot_capital_pct"] > CONFIG["MAX_POS_RATIO"] * 100

def test_structural_stop_not_clamped():
    r = calc_position_size(100, 95.0)   # 結構停損 -5%, v1 會偷偷夾成 -2.6%
    assert r["adjusted_stop"] == 95.0 and r["stop_pct"] == -5.0, r
    r2 = calc_position_size(100, 99.5)  # -0.5% 也不夾成 -1.8%
    assert r2["adjusted_stop"] == 99.5, r2

def test_normal_sizing_within_limits():
    r = calc_position_size(100, 97.5)
    assert r["lots"] >= 1 and r["capital_pct"] <= 35.0
    assert r["max_loss_twd"] <= 3_000_000 * CONFIG["RISK_PER_TRADE"] * 1.02

def test_invalid_stop():
    assert calc_position_size(100, 101)["lots"] == 0


# ---------- gates ----------
def test_chip_missing_data_fails():
    assert chip_is_positive(None) is False
    assert chip_is_positive({"foreign_net": 0, "it_net": 0, "total_inst": 0}) is False  # v1 會通過
    assert chip_is_positive({"has_data": True, "foreign_net": 500, "it_net": -100, "total_inst": 400}) is True
    assert chip_is_positive({"has_data": True, "foreign_net": 500, "it_net": 0, "total_inst": -50}) is False

def test_market_regime():
    assert market_regime_ok(100, 105, 0.01) is False   # 大盤破月線且個股弱
    assert market_regime_ok(100, 105, 0.05) is True    # 個股夠強仍可
    assert market_regime_ok(106, 105, 0.0) is True


# ---------- exit plan ----------
def test_exit_plan_tp3_fields():
    p = calc_exit_plan(100, 97.5, 10)
    assert p["tp1_reduce_lots"] + p["tp2_reduce_lots"] + p["tp3_reduce_lots"] + p["tail_lots"] == 10, p
    assert p["tp3_reduce_lots"] == 2 and p["tail_lots"] == 2


# ---------- synthetic breakout: chip & market gates ----------
def make_s4_gap_df(n=75):
    """緩漲 -> 10日箱型 -> 最後一天跳空大漲, 設計給 S4 觸發。"""
    rng = np.random.default_rng(7)
    closes = []
    price = 60.0
    for i in range(n - 11):
        price *= 1.006  # 緩漲建立趨勢與 40 日 RS
        closes.append(price)
    box_top = closes[-1]
    for i in range(10):  # 箱型: 小幅震盪, 上漲日量大於下跌日
        closes.append(box_top * (1.004 if i % 2 == 0 else 0.998))
    rows = []
    for i, c in enumerate(closes):
        o = c * (0.999 if i % 2 == 0 else 1.001)
        rows.append([o, c * 1.004, c * 0.996, c, 2_400_000 if c >= o else 1_200_000])
    # 最後一天: 跳空開高 +3.5% 收漲
    prev_c = closes[-1]
    o = prev_c * 1.035; c = prev_c * 1.055
    rows.append([o, c * 1.002, o * 0.999, c, 6_000_000])
    idx = pd.bdate_range("2026-06-01", periods=len(rows))
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)

def test_gates_block_and_unblock():
    df = make_s4_gap_df()
    chip_ok = {"has_data": True, "foreign_net": 800, "it_net": 200, "total_inst": 1000}
    # 無法人資料 + require_chip=True (實盤預設) -> 不給訊號
    assert evaluate_candidate(df, "9999", "測試", ".TW", None, bench_ret_20d=-0.02, market_ok=True) == []
    # 大盤濾網擋下 -> 不給訊號
    assert evaluate_candidate(df, "9999", "測試", ".TW", chip_ok, bench_ret_20d=-0.02, market_ok=False) == []
    # 兩道門都開 -> 至少要能觸發一個策略 (驗證合成資料真的符合型態)
    sigs = evaluate_candidate(df, "9999", "測試", ".TW", chip_ok, bench_ret_20d=-0.02, market_ok=True)
    assert len(sigs) >= 1, "synthetic S4 pattern should trigger at least one strategy"
    assert all(s["suggested_lots"] >= 1 for s in sigs)
    print("triggered:", [s["strategy"] for s in sigs])


# ---------- simulate_exit ----------
def make_exit_df(closes):
    rows = []
    for c in closes:
        rows.append([c, c * 1.001, c * 0.999, c, 2_000_000])
    idx = pd.bdate_range("2026-06-01", periods=len(rows))
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)
    return prepare_indicators(df)

def test_simulate_stop_loss():
    df = make_exit_df([100] * 65 + [100, 99, 97, 96, 95] + [95] * 60)
    r = simulate_exit(df, 65, 100.0, 97.5)
    assert r["censored"] is False and r["win"] == 0 and r["net_ret_pct"] < 0, r

def test_simulate_censored_when_data_too_short():
    df = make_exit_df([100] * 65 + [101, 102, 103])  # 進場後資料不足 55 日且未出場
    r = simulate_exit(df, 65, 100.0, 90.0)
    assert r["censored"] is True, r

def test_simulate_tp_profit_positive():
    closes = [100] * 65 + [102, 104, 107, 110, 115, 120, 128] + [126] * 60
    df = make_exit_df(closes)
    r = simulate_exit(df, 65, 100.0, 97.0)
    assert r["censored"] is False and r["net_ret_pct"] > 3.0, r
