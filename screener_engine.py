#!/usr/bin/env python3
"""
台股技術分析選股與行動燈號核心引擎 v5.1（對應 index.html 前端邏輯 1-to-1）
==========================================================================
- 來源規則：TradingView Pine Script v6 (S01-S02-S03-S04-S06.pine)
- 輸出項目：行動燈號 (LONG / WATCH / WAIT)、S02 RS、S04 黃金雲帶、S01 九轉、
           20日壓力/支撐、EMA20、60MA、台股跳動檔位對齊之 ENTRY / SL / TP 與 R:R
- v5.1 優化：
  1. 加入大盤指數缺值/零值前值填補與序列長度防呆
  2. 加入台股升降單位 (Tick Size) 自動對齊（SL 向下取檔、TP 向上取檔）
  3. 支援選用三大法人淨買賣超欄位 (inst_total_lots) 標註籌碼輔助狀態
"""

import math
import re

ORDINARY_STOCK_RE = re.compile(r"^[1-9]\d{3}$")


def is_ordinary_tw_stock(code: str) -> bool:
    """僅保留 4 碼上市/上櫃普通股，排除 ETF (00xx)、ETN (02xx)、權證與特別股"""
    return bool(ORDINARY_STOCK_RE.match(str(code).strip()))


def get_tw_tick_size(price: float) -> float:
    """依台灣證券交易所升降單位規則回傳價格對應跳動檔位"""
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def align_tw_tick(price: float, mode: str = "nearest") -> float:
    """將價格對齊台股跳動檔位（mode: nearest / floor / ceil）"""
    if price is None or price <= 0:
        return 0.0
    tick = get_tw_tick_size(price)
    steps = price / tick
    if mode == "floor":
        aligned = math.floor(steps + 1e-9) * tick
    elif mode == "ceil":
        aligned = math.ceil(steps - 1e-9) * tick
    else:
        aligned = round(steps) * tick
    return round(aligned, 2)


def _sanitize_taiex(taiex):
    """對大盤指數序列進行缺值/零值前值填補（若開頭缺值則回填首個有效值）"""
    cleaned = [None if (v is None or v <= 0) else float(v) for v in taiex]
    first_valid = next((v for v in cleaned if v is not None), None)
    if first_valid is None:
        raise ValueError("taiex 序列無任何有效正值，無法計算 S02 RS")
    last = first_valid
    out = []
    for v in cleaned:
        if v is None:
            out.append(last)
        else:
            last = v
            out.append(v)
    return out


def calc_ema(values, period: int):
    k = 2.0 / (period + 1)
    out, ema = [], None
    for v in values:
        ema = v if ema is None else (v * k + ema * (1.0 - k))
        out.append(ema)
    return out


def calc_s01_nine_turn(closes, lookback: int = 4):
    bull, bear = [], []
    u = d = 0
    for i, c in enumerate(closes):
        if i >= lookback:
            u = u + 1 if c > closes[i - lookback] else 0
            d = d + 1 if c < closes[i - lookback] else 0
        bull.append(u)
        bear.append(d)
    return bull, bear


def evaluate_stock(highs, lows, closes, taiex, high_52w=None, inst_total_lots=None):
    n = len(closes)
    if n == 0:
        raise ValueError("closes 不可為空")
    if not (len(highs) == len(lows) == len(taiex) == n):
        raise ValueError(f"序列長度不一致: highs={len(highs)}, lows={len(lows)}, closes={n}, taiex={len(taiex)}")

    safe_taiex = _sanitize_taiex(taiex)
    c = float(closes[-1])

    # S02 RS 相對強度
    rs = [float(cl) / bx for cl, bx in zip(closes, safe_taiex)]
    rs_p = 50 if n >= 50 else min(20, n)
    rs_ma = sum(rs[-rs_p:]) / rs_p
    rs_pct = ((rs[-1] / rs_ma - 1.0) * 100.0) if rs_ma > 0 else 0.0
    rs_strong = rs[-1] > rs_ma
    rs_nh20 = rs[-1] >= max(rs[-min(20, n):])

    # S04 黃金雲帶 & 60MA
    em5 = calc_ema(closes, 5)
    em20 = calc_ema(closes, 20)
    gold_cloud = em5[-1] > em20[-1]
    ema5_up = em5[-1] > em5[-2] if n >= 2 else False
    ma60 = sum(closes[-60:]) / 60.0 if n >= 60 else None

    # S01 神奇九轉
    bull, bear = calc_s01_nine_turn(closes, lookback=4)
    td_recent = max(bear[-min(5, n):]) >= 6

    # 短線壓力 / 支撐 (20D High / Low)
    hi20 = max(highs[-min(20, n):])
    lo20 = min(lows[-min(20, n):])

    # 行動燈號 (LONG / WATCH / WAIT)
    if rs_strong and (gold_cloud or (ema5_up and (rs_nh20 or c >= em20[-1]))):
        action = "LONG"
    elif rs_strong and (ema5_up or td_recent):
        action = "WATCH"
    else:
        action = "WAIT"

    # 參考風控點位 ENTRY / SL / TP & R:R（對齊台股升降單位）
    entry = align_tw_tick(c, "nearest")
    raw_floor = max(lo20, em20[-1] * 0.985) if em20[-1] < c else lo20 * 0.99
    sl_raw = min(entry * 0.96, raw_floor)
    if sl_raw < entry * 0.88:
        sl_raw = entry * 0.92
    sl = align_tw_tick(sl_raw, "floor")
    if sl >= entry:
        sl = align_tw_tick(entry - get_tw_tick_size(entry), "floor")

    risk = max(entry - sl, entry * 0.02)
    tp_raw = hi20 if hi20 >= entry * 1.04 else (entry + risk * 2.0)
    if tp_raw < entry + risk * 1.5:
        tp_raw = entry + risk * 2.0
    tp = align_tw_tick(tp_raw, "ceil")
    rr = (tp - entry) / risk if risk > 0 else 0.0

    result = {
        "action": action,
        "close": c,
        "rs_pct": round(rs_pct, 1),
        "rs_ma_period": rs_p,
        "rs_nh20": rs_nh20,
        "gold_cloud": gold_cloud,
        "ema5_up": ema5_up,
        "s01_bull": bull[-1],
        "s01_bear": bear[-1],
        "td_recent": td_recent,
        "resistance_20d": hi20,
        "support_20d": lo20,
        "ema20": round(em20[-1], 2),
        "ma60": round(ma60, 2) if ma60 else None,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": round(rr, 2),
        "ratio_52w": round(c / high_52w * 100.0, 1) if (high_52w and high_52w > 0) else None,
    }
    if inst_total_lots is not None:
        result["inst_total_lots"] = int(inst_total_lots)
        result["inst_support"] = int(inst_total_lots) > 0
    return result


if __name__ == "__main__":
    # 內建單元煙霧測試（含跳動檔位對齊與大盤缺值防呆）
    assert is_ordinary_tw_stock("2330") and is_ordinary_tw_stock("3491")
    assert not is_ordinary_tw_stock("0050") and not is_ordinary_tw_stock("020001")
    assert align_tw_tick(3822.48, "floor") == 3820.0
    assert align_tw_tick(4505.04, "ceil") == 4510.0
    assert align_tw_tick(97.93, "nearest") == 97.9

    sample_c = [100 + i * 0.5 for i in range(60)]
    sample_b = [20000 - i * 10 for i in range(60)]
    sample_b[10] = 0  # 測試大盤缺值自動前值填補
    res = evaluate_stock(
        [x + 1 for x in sample_c],
        [x - 1 for x in sample_c],
        sample_c,
        sample_b,
        high_52w=140.0,
        inst_total_lots=520,
    )
    assert res["action"] == "LONG" and res["gold_cloud"] is True and res["rr"] >= 1.5
    assert res["inst_support"] is True
    print("screener_engine.py v5.1 self-test OK:", res)
