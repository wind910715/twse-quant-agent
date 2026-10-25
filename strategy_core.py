"""
strategy_core.py — S0~S5 策略的單一真相來源 (SSOT) v2

設計原則 (2026-10-23 統一回測修正版):
1. screener.py (實盤選股) 與 backtest.py (回測) 必須呼叫同一個 evaluate_candidate()，
   不允許兩邊各寫一份策略邏輯。差異只能來自「輸入資料不同」，且必須被明確標示
   (例如回測無歷史法人資料 -> require_chip=False 且每筆交易標記 chip_unverified)。
2. 停損不做假: 不再把結構停損夾進 -1.8%~-2.6%。停損距離是多少就用多少算部位；
   若 1 張就超過資金上限或風險預算，回傳 lots=0 並附 reason，不再強制買 1 張。
3. 法人濾網不再「無資料自動通過」: has_data=False 時 require_chip=True 直接不給訊號。
4. 大盤濾網進實盤: screener 與 backtest 共用 market_regime_ok()。
5. 出場計畫欄位明確化: TP3 有獨立的 tp3_reduce_lots / tp3_reduce_pct 與 tail_lots，
   monitor.py 不再用「賣剩餘 50%」這種推測寫法。
"""
import numpy as np
import pandas as pd

CONFIG = {
    "ACCOUNT_EQUITY": 3_000_000,
    "RISK_PER_TRADE": 0.012,        # 單筆最大容許虧損 1.2%
    "MAX_POS_RATIO": 0.35,          # 單筆最大持倉上限 35%
    "MAX_CONCURRENT_POSITIONS": 5,  # 組合同時持倉上限 (回測與實盤共用)
    "MIN_MV20_LOTS": 1000,
    "MIN_PRICE": 25.0,
    "REQUIRE_CHIP_DATA": True,      # 實盤: 無法人資料就不給訊號 (上櫃需另接 TPEx 法人資料源)
    # 交易成本 (逐筆分批賣出都計): 買入手續費 0.1425%; 賣出 = 手續費 0.1425% + 證交稅 0.3%
    "BUY_COST_RATE": 0.001425,
    "SELL_COST_RATE": 0.004425,
    "ROUNDTRIP_FRICTION_EST": 0.005,  # 僅用於部位 sizing 的每股風險預估
    # 三階停利 (SSOT, screener/backtest/monitor 共用)
    "TP1_TARGET_PCT": 0.060, "TP1_REDUCE_RATIO": 0.25, "TP1_LOCK_PCT": 0.015, "TP1_TRAIL_DD": 0.055,
    "TP2_TARGET_PCT": 0.140, "TP2_REDUCE_RATIO": 0.35, "TP2_LOCK_PCT": 0.080, "TP2_TRAIL_DD": 0.055,
    "TP3_TARGET_PCT": 0.250, "TP3_REDUCE_RATIO": 0.20, "TP3_LOCK_PCT": 0.160, "TP3_TRAIL_DD": 0.060,
    "MAX_HOLD_DAYS": 55,
}

# v3.3 決策 (使用者指示): 不適用策略直接刪除, 只留通過標準者 (小賠大賺/小賠小賺, 期望值為正)。
# v3.4 (2026-10-24, 使用者拍板): 併入 BRK20_突破量增 (breakout_lab 組合級驗證過關:
#   BRK_ONLY 375 筆 RR 2.14, MERGED RR 2.10 > S_ONLY 1.95, 累計 +398.6% vs +41.1%, MDD -7.36%)。
#   已刪除: S1 口袋支點拉回、S2 主升段3浪、S5 強勢回檔反彈 (理由見 CHANGES_V32.md / CHANGES_V33.md)。
# v3.5 (2026-10-24, 使用者拍板): S0 收斂門檻放寬為 amp_4d ≤0.060 (S0-V1, 預註冊優化實驗過關:
#   長視窗 62 筆、勝率 50.0%、平均 +2.10%、RR 2.51); S4 威力跳空剃除 (三種放寬變體全數平均轉負)。
# v3.6 (2026-10-25, 使用者拍板): 策略重新命名為 S0–S2（邏輯、門檻、優先序全不動）：
#   S0 VCP收斂（留任）、S3 破底翻2B → S1、BRK20 突破量增 → S2。
#   現行: S0 VCP收斂 / S1 破底翻2B / S2 突破量增。
#   另定：盤中觸及觸發價即買進（monitor 原有行為，不加尾盤確認）；觸發當下 monitor
#   立即發 TG 通知（含完整進出場劇本），不批次、不延遲。
STRATEGY_NAMES = [
    "S0_VCP收斂", "S1_破底翻2B", "S2_突破量增",
]


# ---------------------------------------------------------------- indicators
def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_up_down_vol_ratio(df_10: pd.DataFrame) -> float:
    up_days = df_10[df_10["Close"] >= df_10["Open"]]["vol_lots"]
    down_days = df_10[df_10["Close"] < df_10["Open"]]["vol_lots"]
    up_avg = float(up_days.mean()) if not up_days.empty else 1.0
    down_avg = float(down_days.mean()) if not down_days.empty else max(up_avg * 0.5, 1.0)
    return round(up_avg / max(down_avg, 1.0), 2)


def prepare_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """在 df (OHLCV) 上補齊所有策略共用指標。screener/backtest 共用，不可各自計算。"""
    df = df.copy()
    df["vol_lots"] = df["Volume"] / 1000.0
    for p in [5, 10, 20, 60]:
        df[f"MA{p}"] = df["Close"].rolling(p).mean()
    df["MV5"] = df["vol_lots"].rolling(5).mean()
    df["MV20"] = df["vol_lots"].rolling(20).mean()
    df["RSI14"] = calc_rsi(df["Close"], 14)
    std20 = df["Close"].rolling(20).std()
    df["BB_UP"] = df["MA20"] + 2.0 * std20
    df["BB_LOW"] = df["MA20"] - 2.0 * std20
    df["BB_WIDTH"] = (df["BB_UP"] - df["BB_LOW"]) / df["MA20"]
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26
    df["MACD"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD_HIST"] = df["DIF"] - df["MACD"]
    return df


def calc_volume_poc_and_max_bar(df_60: pd.DataFrame) -> dict:
    typical_price = (df_60["High"] + df_60["Low"] + df_60["Close"]) / 3.0
    vols = df_60["vol_lots"].values
    p_min, p_max = typical_price.min(), typical_price.max()
    if p_max <= p_min:
        poc_price = float(df_60["Close"].iloc[-1])
    else:
        bins = np.linspace(p_min, p_max, 16)
        inds = np.clip(np.digitize(typical_price.values, bins) - 1, 0, 14)
        bin_vols = np.bincount(inds, weights=vols, minlength=15)
        best_bin = int(np.argmax(bin_vols))
        poc_price = round(float((bins[best_bin] + bins[best_bin + 1]) / 2.0), 2)
    df_20 = df_60.iloc[-20:]
    max_bar = df_20.loc[df_20["vol_lots"].idxmax()]
    return {
        "poc_price": poc_price,
        "max_vol_high": round(float(max_bar["High"]), 2),
        "max_vol_low": round(float(max_bar["Low"]), 2),
    }


def calc_cdp_levels(high: float, low: float, close: float) -> dict:
    cdp = (high + low + 2.0 * close) / 4.0
    rng = high - low
    return {
        "cdp": round(cdp, 2), "ah": round(cdp + rng, 2), "nh": round(2 * cdp - low, 2),
        "nl": round(2 * cdp - high, 2), "al": round(cdp - rng, 2),
    }


# ---------------------------------------------------------------- gates
def chip_is_positive(chip_info: dict | None) -> bool:
    """
    法人濾網 v2: 必須真的有資料，且三大法人合計買超 > 0，且外資或投信至少一方買超。
    v1 的 bug: 無資料預設 0 也能通過 (total >= 0)，上櫃股永遠自動通過。
    """
    if not chip_info or not chip_info.get("has_data", False):
        return False
    foreign = chip_info.get("foreign_net", 0)
    invest_trust = chip_info.get("it_net", 0)
    total = chip_info.get("total_inst", foreign + invest_trust)
    return bool(total > 0 and (foreign > 0 or invest_trust > 0))


def market_regime_ok(bench_close: float, bench_ma20: float, stock_ret_20d: float) -> bool:
    """
    大盤濾網 (screener 與 backtest 共用同一函數):
    0050 跌破月線 1.2% 以上且個股 20 日漲幅 < 3% 時，不發新訊號。
    """
    if bench_ma20 is None or pd.isna(bench_ma20) or bench_ma20 <= 0:
        return True  # 無大盤資料時不擋，但呼叫端應記錄
    return not ((bench_close < bench_ma20 * 0.988) and (stock_ret_20d < 0.03))


# ---------------------------------------------------------------- sizing / exit plan
def calc_position_size(entry: float, stop: float) -> dict:
    """
    v2 修正:
    - 不再把停損夾進固定區間: adjusted_stop 永遠等於結構停損 stop。
    - 不再強制至少 1 張: 若 1 張就超過資金上限 (35%) 或風險預算 (1.2%)，
      回傳 lots=0 + reason="one_lot_exceeds_limits"，呼叫端應跳過該訊號
      (高價股改用零股是另一個產品決策，不在程式裡偷偷硬買整張)。
    """
    base = {"lots": 0, "stop_pct": 0.0, "max_loss_twd": 0, "capital_used": 0,
            "capital_pct": 0.0, "adjusted_stop": stop, "reason": ""}
    if entry <= 0 or stop <= 0 or entry <= stop:
        base["reason"] = "invalid_stop"
        return base
    risk_per_share = (entry - stop) + entry * CONFIG["ROUNDTRIP_FRICTION_EST"]
    max_risk_twd = CONFIG["ACCOUNT_EQUITY"] * CONFIG["RISK_PER_TRADE"]
    raw_shares = max_risk_twd / risk_per_share
    max_cap_shares = (CONFIG["ACCOUNT_EQUITY"] * CONFIG["MAX_POS_RATIO"]) / entry
    lots = int(min(raw_shares, max_cap_shares) // 1000)
    if lots < 1:
        one_lot_capital_pct = round(1000 * entry / CONFIG["ACCOUNT_EQUITY"] * 100, 1)
        one_lot_loss = int(1000 * risk_per_share)
        base.update({
            "reason": "one_lot_exceeds_limits",
            "one_lot_capital_pct": one_lot_capital_pct,
            "one_lot_max_loss_twd": one_lot_loss,
            "stop_pct": round(((stop - entry) / entry) * 100, 2),
        })
        return base
    base.update({
        "lots": lots,
        "adjusted_stop": stop,
        "stop_pct": round(((stop - entry) / entry) * 100, 2),
        "max_loss_twd": int(lots * 1000 * risk_per_share),
        "capital_used": int(lots * 1000 * entry),
        "capital_pct": round((lots * 1000 * entry / CONFIG["ACCOUNT_EQUITY"]) * 100, 1),
        "reason": "ok",
    })
    return base


def calc_exit_plan(entry: float, hard_stop: float, total_lots: int) -> dict:
    """三階停利計畫 (SSOT)。TP3 減碼張數獨立欄位，不再與尾倉混在一起。"""
    tp1_price = round(entry * (1 + CONFIG["TP1_TARGET_PCT"]), 2)
    tp2_price = round(entry * (1 + CONFIG["TP2_TARGET_PCT"]), 2)
    tp3_price = round(entry * (1 + CONFIG["TP3_TARGET_PCT"]), 2)
    if total_lots >= 4:
        tp1_lots = max(1, int(round(total_lots * CONFIG["TP1_REDUCE_RATIO"])))
        tp2_lots = max(1, int(round(total_lots * CONFIG["TP2_REDUCE_RATIO"])))
        tp3_lots = max(1, int(round(total_lots * CONFIG["TP3_REDUCE_RATIO"])))
        tail_lots = max(0, total_lots - tp1_lots - tp2_lots - tp3_lots)
    elif total_lots == 3:
        tp1_lots, tp2_lots, tp3_lots, tail_lots = 1, 1, 1, 0
    elif total_lots == 2:
        tp1_lots, tp2_lots, tp3_lots, tail_lots = 1, 1, 0, 0
    else:
        tp1_lots, tp2_lots, tp3_lots, tail_lots = 1, 0, 0, 0

    def pct(n):
        return int(round(n / max(total_lots, 1) * 100))

    return {
        "entry_low": round(entry, 2),
        "entry_high": round(entry * 1.028, 2),
        "be_trigger_price": tp1_price,
        "be_lock_price": round(entry * (1 + CONFIG["TP1_LOCK_PCT"]), 2),
        "tp1_price": tp1_price, "tp1_reduce_pct": pct(tp1_lots), "tp1_reduce_lots": tp1_lots,
        "tp1_trail_drawdown": CONFIG["TP1_TRAIL_DD"],
        "tp2_price": tp2_price, "tp2_reduce_pct": pct(tp2_lots), "tp2_reduce_lots": tp2_lots,
        "tp2_trail_drawdown": CONFIG["TP2_TRAIL_DD"],
        "tp3_price": tp3_price, "tp3_reduce_pct": pct(tp3_lots), "tp3_reduce_lots": tp3_lots,
        "tp3_remain_pct": pct(tp3_lots + tail_lots), "tp3_remain_lots": tp3_lots + tail_lots,
        "tail_lots": tail_lots, "tail_pct": pct(tail_lots),
        "tp3_trail_drawdown": CONFIG["TP3_TRAIL_DD"],
        "dynamic_trailing_stop": hard_stop,
    }


# ---------------------------------------------------------------- strategy evaluation (SSOT)
def evaluate_candidate(df_raw: pd.DataFrame, sid: str, sname: str, sfx: str,
                       chip_info: dict | None, bench_ret_20d: float,
                       market_ok: bool = True, require_chip: bool | None = None) -> list:
    """
    唯一策略評估函數。df_raw 為「截至訊號日為止」的 OHLCV (最後一列 = 訊號日)，
    回測以 df.iloc[:i+1] 切片呼叫、實盤以最新資料呼叫，邏輯完全相同。

    require_chip=None 時使用 CONFIG["REQUIRE_CHIP_DATA"] (實盤預設 True)。
    回測因無歷史法人資料傳入 False，所有訊號會被標記 chip_unverified=True，
    報表必須揭露，不能假裝測過法人濾網。
    """
    if require_chip is None:
        require_chip = CONFIG["REQUIRE_CHIP_DATA"]
    if df_raw is None or len(df_raw) < 65:
        return []
    df = df_raw.dropna(subset=["Close", "High", "Low", "Open", "Volume"])
    if len(df) < 65:
        return []
    df = prepare_indicators(df)

    cur = df.iloc[-1]
    prev = df.iloc[-2]
    if pd.isna(cur["MA60"]) or cur["MV20"] < CONFIG["MIN_MV20_LOTS"] or cur["Close"] < CONFIG["MIN_PRICE"]:
        return []

    stock_ret_20d = (float(cur["Close"]) - float(df.iloc[-21]["Close"])) / max(float(df.iloc[-21]["Close"]), 1.0)
    stock_ret_40d = (float(cur["Close"]) - float(df.iloc[-41]["Close"])) / max(float(df.iloc[-41]["Close"]), 1.0)
    rs_score = round((0.6 * stock_ret_20d + 0.4 * stock_ret_40d) * 100, 2)
    rs_strong = (stock_ret_20d >= bench_ret_20d + 0.015) and (rs_score >= 3.5)

    ud_vol_ratio = calc_up_down_vol_ratio(df.iloc[-10:])
    s_gates_ok = rs_strong and ud_vol_ratio >= 1.30
    if not market_ok:
        return []

    # S2 (v3.4 併入，原 BRK20): 20 日突破+量增。驗證規格獨立於 S 家族的 rs_strong/ud 閘門,
    # 僅受 market_ok、基本有效性與法人閘門約束 (與 breakout_lab 驗證口徑一致)。
    prev_20_high = df.iloc[-21:-1]["High"].max()
    brk_hit = (float(cur["Close"]) > float(prev_20_high)) and \
              (cur["vol_lots"] >= 1.5 * cur["MV20"]) and (cur["Close"] > cur["MA60"])

    chip_positive = chip_is_positive(chip_info)
    chip_unverified = not (chip_info and chip_info.get("has_data", False))
    if require_chip and not chip_positive:
        return []

    vol_profile = calc_volume_poc_and_max_bar(df.iloc[-60:])
    cdp_info = calc_cdp_levels(float(cur["High"]), float(cur["Low"]), float(cur["Close"]))
    ind_pack = {
        "ma5": round(float(cur["MA5"]), 2), "ma10": round(float(cur["MA10"]), 2),
        "ma20": round(float(cur["MA20"]), 2), "ma60": round(float(cur["MA60"]), 2),
        "bb_up": round(float(cur["BB_UP"]), 2), "bb_mid": round(float(cur["MA20"]), 2),
        "bb_low": round(float(cur["BB_LOW"]), 2),
        "poc_price": vol_profile["poc_price"],
        "max_vol_high": vol_profile["max_vol_high"], "max_vol_low": vol_profile["max_vol_low"],
        "cdp": cdp_info["cdp"], "cdp_ah": cdp_info["ah"], "cdp_nh": cdp_info["nh"],
        "cdp_nl": cdp_info["nl"], "cdp_al": cdp_info["al"],
        "swing_high_20": round(float(df.iloc[-20:]["High"].max()), 2),
        "swing_high_60": round(float(df.iloc[-60:]["High"].max()), 2),
        "ud_vol_ratio": ud_vol_ratio, "rs_score": rs_score,
    }

    last5 = df.iloc[-5:]
    prev_4_high = df.iloc[-5:-1]["High"].max()
    prev_10_high = df.iloc[-11:-1]["High"].max()
    prev_15_high = df.iloc[-16:-1]["High"].max()

    trend_up = (cur["Close"] > cur["MA20"] > cur["MA60"]) and \
               (cur["MA20"] > df.iloc[-5]["MA20"]) and (cur["MA60"] > df.iloc[-5]["MA60"])
    candle_pos = (cur["Close"] - cur["Low"]) / max(cur["High"] - cur["Low"], 0.01)

    matched = []

    # S0: VCP 收斂帶量點火突破
    amp_4d = (df.iloc[-5:-1]["High"].max() - df.iloc[-5:-1]["Low"].min()) / max(df.iloc[-5:-1]["Low"].min(), 1)
    vdu_ok = (df.iloc[-5:-1]["vol_lots"] < 0.65 * cur["MV20"]).any()
    # v3.1: 量縮收緊與 MA20 延伸上限經回測證實有害 (S0 交易 5->1, S3 11->2, 後半段轉負), 已退回 v2 條件。
    # v3.5: 收斂門檻放寬為 0.060 (S0-V1, 預註冊優化實驗過關, 其餘 S0 門檻不動)。
    if s_gates_ok and trend_up and (amp_4d <= 0.060) and vdu_ok and (cur["Close"] > prev_4_high) and \
       (cur["Close"] > cur["Open"] * 1.012) and (cur["vol_lots"] >= 1.20 * cur["MV5"]) and \
       (candle_pos >= 0.70) and (55 <= cur["RSI14"] <= 72):
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(last5["Low"].min() * 0.996, entry * 0.975)), 2)
        matched.append(("S0_VCP收斂", entry, stop))

    # S1: 強勢洗盤破底翻 (2B，原 S3)
    dipped_ma10 = (df.iloc[-4:-1]["Low"] < df.iloc[-4:-1]["MA10"] * 0.996).any()
    power_reclaim = (cur["Close"] > prev_4_high) and (cur["Close"] >= cur["MA5"]) and \
                    (cur["Close"] >= cur["MA10"]) and (cur["Close"] >= cur["Open"] * 1.025) and \
                    (candle_pos >= 0.75) and (cur["vol_lots"] >= 1.35 * cur["MV5"])
    if s_gates_ok and trend_up and dipped_ma10 and power_reclaim and (ud_vol_ratio >= 1.40) and (55 <= cur["RSI14"] <= 68):
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(cur["Low"] * 0.996, entry * 0.975)), 2)
        matched.append(("S1_破底翻2B", entry, stop))

    # S2_突破量增 (v3.4 併入，原 BRK20): 優先序最低 (同日同股與 S 策略同發時 S 優先, 與驗證版 MERGED 口徑一致)。
    # 停損 = max(訊號日低點 x0.995, 進場 x0.975); 驗證版以隔日 trigger 計地板, 差異僅出現在
    # 跳空且結構停損較淺的子集 (<=0.3%), 已於 CHANGES_V34.md 揭露。
    if brk_hit:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(cur["Low"] * 0.995, entry * 0.975)), 2)
        matched.append(("S2_突破量增", entry, stop))

    results = []
    trade_d = df.index[-1].strftime("%Y-%m-%d") if hasattr(df.index[-1], "strftime") else str(df.index[-1])
    ex_prefix = "otc" if sfx == ".TWO" else "tse"
    for strat, entry, raw_stop in matched:
        pos = calc_position_size(entry, raw_stop)
        if pos["lots"] <= 0:
            continue  # v2: 部位不合格 (含高價股 1 張超限) 直接跳過, 不硬買
        exit_plan = calc_exit_plan(entry, pos["adjusted_stop"], pos["lots"])
        results.append({
            "date": trade_d, "stock_id": sid, "stock_name": sname, "ex_prefix": ex_prefix,
            "strategy": strat, "close": round(float(cur["Close"]), 2),
            "entry_price": entry, "hard_stop": pos["adjusted_stop"], "stop_pct": pos["stop_pct"],
            "suggested_lots": pos["lots"], "capital_used": pos["capital_used"],
            "capital_pct": pos["capital_pct"], "max_loss_twd": pos["max_loss_twd"],
            "chip_unverified": chip_unverified,
            "indicators_backend": ind_pack, "exit_plan": exit_plan,
        })
    return results


# ---------------------------------------------------------------- exit simulation (backtest SSOT)
def simulate_exit(df_ind: pd.DataFrame, entry_idx: int, actual_entry: float, raw_stop: float) -> dict:
    """
    出場模擬 v2 (df_ind 需先經 prepare_indicators):
    - 成本逐段計: 每一段賣出 (TP1/TP2/TP3/最終出場) 都扣 SELL_COST_RATE, 買入扣一次 BUY_COST_RATE。
      v1 只扣一筆固定 0.5%, 低估分批賣出的稅費。
    - 移動停利以「截至前一根 K 為止的最高收盤」計算, 避免同根 K 先創高再用新停利自我觸發的lookahead。
    - 資料不足以走完 MAX_HOLD_DAYS 且部位未出場 -> censored=True, 呼叫端必須剔除不計勝率
      (v1 直接用最後收盤結算, 把未完成交易當已實現)。
    回傳: {net_ret_pct, win, exit_idx, exit_reason, censored}
    """
    sell_cost = CONFIG["SELL_COST_RATE"]
    hard_stop = raw_stop
    tp1_p = actual_entry * (1 + CONFIG["TP1_TARGET_PCT"])
    tp2_p = actual_entry * (1 + CONFIG["TP2_TARGET_PCT"])
    tp3_p = actual_entry * (1 + CONFIG["TP3_TARGET_PCT"])
    r1, r2, r3 = CONFIG["TP1_REDUCE_RATIO"], CONFIG["TP2_REDUCE_RATIO"], CONFIG["TP3_REDUCE_RATIO"]

    last_idx = min(entry_idx + CONFIG["MAX_HOLD_DAYS"], len(df_ind) - 1)
    window_complete = (entry_idx + CONFIG["MAX_HOLD_DAYS"]) <= (len(df_ind) - 1)
    future = df_ind.iloc[entry_idx:last_idx + 1]

    highest_close_prev = actual_entry
    active_hard_floor = hard_stop
    active_close_trail = hard_stop
    hit_tp1 = hit_tp2 = hit_tp3 = False
    realized = 0.0
    rem = 1.0
    exit_idx = last_idx
    exit_reason = "time_exit" if window_complete else "censored"
    closed = False

    def settle(weight: float, exit_price: float) -> float:
        return weight * (((exit_price - actual_entry) / actual_entry) - sell_cost)

    for offset, (idx_pos, fbar) in enumerate(future.iterrows()):
        day_num = offset + 1
        f_open, f_high, f_low, f_close = float(fbar["Open"]), float(fbar["High"]), float(fbar["Low"]), float(fbar["Close"])
        f_ma5 = float(fbar["MA5"]) if not pd.isna(fbar["MA5"]) else f_close

        # 1) 停損/移動停利先於停利檢查 (保守順序, 與 v1 相同)
        if day_num >= 2:
            if f_open <= active_hard_floor:
                realized += settle(rem, f_open); rem = 0.0
                exit_idx, exit_reason, closed = entry_idx + offset, "gap_stop" if not hit_tp1 else "trail_exit", True
                break
            if f_low <= active_hard_floor:
                realized += settle(rem, active_hard_floor); rem = 0.0
                exit_idx, exit_reason, closed = entry_idx + offset, "hard_stop" if not hit_tp1 else "trail_exit", True
                break
            if hit_tp1 and f_close <= active_close_trail:
                realized += settle(rem, f_close); rem = 0.0
                exit_idx, exit_reason, closed = entry_idx + offset, "trail_close", True
                break
        else:
            if f_close <= hard_stop:
                realized += settle(rem, hard_stop); rem = 0.0
                exit_idx, exit_reason, closed = entry_idx + offset, "hard_stop_day1", True
                break

        # 2) 分批停利 (以除權息調整後價格的高點觸發, 成交價 = 目標價)
        if not hit_tp1 and f_high >= tp1_p:
            hit_tp1 = True; realized += settle(r1, tp1_p); rem = round(rem - r1, 4)
        if hit_tp1 and not hit_tp2 and f_high >= tp2_p:
            hit_tp2 = True; realized += settle(r2, tp2_p); rem = round(rem - r2, 4)
        if hit_tp2 and not hit_tp3 and f_high >= tp3_p:
            hit_tp3 = True; realized += settle(r3, tp3_p); rem = round(rem - r3, 4)

        # 3) 3 日不漲汰弱
        if (day_num >= 3) and (not hit_tp1) and (highest_close_prev < actual_entry * 1.025) and \
           (f_close < actual_entry * 0.995) and (f_close < f_ma5):
            realized += settle(rem, f_close); rem = 0.0
            exit_idx, exit_reason, closed = entry_idx + offset, "weak_3d", True
            break

        # 4) 更新停利/停損水位 (只用已完成的這根 K, 供下一根使用)
        if f_close > highest_close_prev:
            highest_close_prev = f_close
        if hit_tp3:
            active_hard_floor = max(active_hard_floor, actual_entry * (1 + CONFIG["TP3_LOCK_PCT"]))
            active_close_trail = max(active_close_trail, actual_entry * 1.170,
                                     highest_close_prev * (1 - CONFIG["TP3_TRAIL_DD"]))
        elif hit_tp2:
            active_hard_floor = max(active_hard_floor, actual_entry * (1 + CONFIG["TP2_LOCK_PCT"]))
            active_close_trail = max(active_close_trail, actual_entry * 1.085,
                                     highest_close_prev * (1 - CONFIG["TP2_TRAIL_DD"]))
        elif hit_tp1:
            active_hard_floor = max(active_hard_floor, actual_entry * (1 + CONFIG["TP1_LOCK_PCT"]))
            active_close_trail = max(active_close_trail, actual_entry * 1.025,
                                     highest_close_prev * (1 - CONFIG["TP1_TRAIL_DD"]))
        elif highest_close_prev >= actual_entry * 1.045:
            active_hard_floor = max(active_hard_floor, actual_entry * 1.010)
        exit_idx = entry_idx + offset

    if rem > 0 and not closed:
        if not window_complete:
            return {"net_ret_pct": None, "win": None, "exit_idx": last_idx,
                    "exit_reason": "censored", "censored": True}
        last_c = float(future.iloc[-1]["Close"])
        realized += settle(rem, last_c)
        rem = 0.0
        exit_reason = "time_exit"

    net_ret_pct = round((realized - CONFIG["BUY_COST_RATE"]) * 100, 2)
    return {"net_ret_pct": net_ret_pct, "win": 1 if net_ret_pct > 0 else 0,
            "exit_idx": exit_idx, "exit_reason": exit_reason, "censored": False}
