import os
import json
import datetime
import requests
import numpy as np
import pandas as pd
import yfinance as yf

# ==========================================
# 1. 全局風控與高賺賠比參數 (Single Source of Truth)
# ==========================================
CONFIG = {
    "ACCOUNT_EQUITY": 3_000_000,   # 預設總資金 300 萬元
    "RISK_PER_TRADE": 0.01,        # 單筆最大容許虧損 1% (30,000 元)
    "MAX_POS_RATIO": 0.25,         # 單筆最大持倉上限 25% (750,000 元)
    "MIN_MV20_LOTS": 1200,         # 20日均量門檻 >= 1,200 張
    "MIN_PRICE": 25.0,             # 最低股價門檻 >= 25 元
    "MAX_STOP_PCT": 0.038,         # 【寧缺勿濫鐵律】單筆初始停損幅度上限 3.8% (超過則放棄不進場)
}

SMART_BROKERS = ["摩根大通", "台灣摩根士丹利", "美商高盛", "瑞士信貸", "瑞銀", "美林", "富邦-建國", "凱基-信義", "元大-城中"]
DAY_TRADE_BROKERS = ["凱基-台北", "凱基-松山", "群益金鼎-大安", "富邦-嘉義", "元大-土城永寧", "富邦-虎尾"]

POSITIONS_FILE = "active_positions.json"
REPORT_FILE = "latest_report.json"
UNIVERSE_CACHE_FILE = "universe_cache.json"

FALLBACK_UNIVERSE = {
    # 台灣50 & MSCI 台灣核心成分股 (上市 .TW)
    "2330": ("台積電", ".TW"), "2317": ("鴻海", ".TW"), "2454": ("聯發科", ".TW"), "2308": ("台達電", ".TW"),
    "2382": ("廣達", ".TW"), "2881": ("富邦金", ".TW"), "2882": ("國泰金", ".TW"), "2891": ("中信金", ".TW"),
    "2303": ("聯電", ".TW"), "3711": ("日月光投控", ".TW"), "2886": ("兆豐金", ".TW"), "2884": ("玉山金", ".TW"),
    "2412": ("中華電", ".TW"), "3231": ("緯創", ".TW"), "6669": ("緯穎", ".TW"), "2357": ("華碩", ".TW"),
    "2603": ("長榮", ".TW"), "2345": ("智邦", ".TW"), "3008": ("大立光", ".TW"), "2327": ("國巨", ".TW"),
    "2892": ("第一金", ".TW"), "2885": ("元大金", ".TW"), "5880": ("合庫金", ".TW"), "2880": ("華南金", ".TW"),
    "3034": ("聯詠", ".TW"), "2379": ("瑞昱", ".TW"), "2395": ("研華", ".TW"), "3017": ("奇鋐", ".TW"),
    "3661": ("世芯-KY", ".TW"), "2383": ("台光電", ".TW"), "1216": ("統一", ".TW"), "2002": ("中鋼", ".TW"),
    # 中型100 代表成分股 (上市 .TW)
    "3037": ("欣興", ".TW"), "2368": ("金像電", ".TW"), "2356": ("英業達", ".TW"), "2376": ("技嘉", ".TW"),
    "2609": ("陽明", ".TW"), "2615": ("萬海", ".TW"), "2618": ("長榮航", ".TW"), "2610": ("華航", ".TW"),
    "1519": ("華城", ".TW"), "1513": ("中興電", ".TW"), "1503": ("士電", ".TW"), "1504": ("東元", ".TW"),
    "3443": ("創意", ".TW"), "3533": ("嘉澤", ".TW"), "2059": ("川湖", ".TW"), "6274": ("台燿", ".TW"),
    "3044": ("健鼎", ".TW"), "2409": ("友達", ".TW"), "3481": ("群創", ".TW"), "3189": ("景碩", ".TW"),
    "8046": ("南電", ".TW"), "6285": ("啟碁", ".TW"), "3450": ("聯鈞", ".TW"), "2313": ("華通", ".TW"),
    "2449": ("京元電子", ".TW"), "6415": ("矽力*-KY", ".TW"), "5269": ("祥碩", ".TW"), "3036": ("文曄", ".TW"),
    # 富櫃50 & 富櫃200 代表成分股 (上櫃 .TWO)
    "6488": ("環球晶", ".TWO"), "5347": ("世界", ".TWO"), "8069": ("元太", ".TWO"), "3529": ("力旺", ".TWO"),
    "5274": ("信驊", ".TWO"), "3324": ("雙鴻", ".TWO"), "8299": ("群聯", ".TWO"), "6223": ("旺矽", ".TWO"),
    "3105": ("穩懋", ".TWO"), "6147": ("頎邦", ".TWO"), "4979": ("華星光", ".TWO"), "3363": ("上詮", ".TWO"),
    "3163": ("波若威", ".TWO"), "3264": ("欣銓", ".TWO"), "5483": ("中美晶", ".TWO"), "6510": ("精測", ".TWO"),
    "6187": ("萬潤", ".TWO"), "3680": ("家登", ".TWO"), "8086": ("宏捷科", ".TWO"), "4966": ("譜瑞-KY", ".TWO"),
    "6446": ("藥華醫藥", ".TWO"), "1565": ("精華", ".TWO"), "8436": ("大江", ".TWO"), "6741": ("91APP*-KY", ".TWO")
}

def get_current_quarter_tag() -> str:
    now = datetime.date.today()
    return f"{now.year}-M{now.month:02d}"

def build_dynamic_index_universe() -> dict:
    q_tag = get_current_quarter_tag()
    if os.path.exists(UNIVERSE_CACHE_FILE):
        try:
            with open(UNIVERSE_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("quarter_tag") == q_tag and len(cached.get("stocks", {})) >= 120:
                return {k: tuple(v) for k, v in cached["stocks"].items()}
        except Exception:
            pass

    universe = dict(FALLBACK_UNIVERSE)
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r_twse = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", headers=headers, timeout=10).json()
        twse_rows = []
        for item in r_twse:
            sid = str(item.get("Code", "")).strip()
            sname = str(item.get("Name", "")).strip()
            if len(sid) == 4 and sid.isdigit() and not sid.startswith("00"):
                tv = float(str(item.get("TradeValue", "0")).replace(",", "") or 0)
                close_p = float(str(item.get("ClosingPrice", "0")).replace(",", "") or 0)
                if close_p >= CONFIG["MIN_PRICE"]:
                    twse_rows.append((sid, sname, tv))
        twse_rows.sort(key=lambda x: x[2], reverse=True)
        for sid, sname, _ in twse_rows[:150]:
            universe[sid] = (sname, ".TW")
    except Exception:
        pass

    try:
        r_tpex = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", headers=headers, timeout=10).json()
        tpex_rows = []
        for item in r_tpex:
            sid = str(item.get("SecuritiesCompanyCode", "")).strip()
            sname = str(item.get("CompanyName", "")).strip()
            if len(sid) == 4 and sid.isdigit() and not sid.startswith("00"):
                tv = float(str(item.get("TransactionAmount", "0")).replace(",", "") or 0)
                close_s = str(item.get("Close", "0")).replace(",", "")
                close_p = float(close_s) if close_s.replace(".", "", 1).isdigit() else 0.0
                if close_p >= CONFIG["MIN_PRICE"]:
                    tpex_rows.append((sid, sname, tv))
        tpex_rows.sort(key=lambda x: x[2], reverse=True)
        for sid, sname, _ in tpex_rows[:120]:
            universe[sid] = (sname, ".TWO")
    except Exception:
        pass

    try:
        with open(UNIVERSE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"quarter_tag": q_tag, "updated_at": str(datetime.date.today()), "stocks": universe}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return universe

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calc_up_down_vol_ratio(df_10: pd.DataFrame) -> float:
    up_days = df_10[df_10["Close"] >= df_10["Open"]]["vol_lots"]
    down_days = df_10[df_10["Close"] < df_10["Open"]]["vol_lots"]
    up_avg = float(up_days.mean()) if not up_days.empty else 1.0
    down_avg = float(down_days.mean()) if not down_days.empty else max(up_avg * 0.5, 1.0)
    return round(up_avg / max(down_avg, 1.0), 2)

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
        "max_vol_low": round(float(max_bar["Low"]), 2)
    }

def calc_cdp_levels(high: float, low: float, close: float) -> dict:
    cdp = (high + low + 2.0 * close) / 4.0
    rng = high - low
    return {
        "cdp": round(cdp, 2),
        "ah": round(cdp + rng, 2),
        "nh": round(2 * cdp - low, 2),
        "nl": round(2 * cdp - high, 2),
        "al": round(cdp - rng, 2)
    }

def calc_position_size(entry: float, stop: float) -> dict:
    if entry <= stop or entry <= 0:
        return {"lots": 0, "stop_pct": 0.0, "max_loss_twd": 0, "capital_used": 0, "capital_pct": 0.0}
    stop_dist_ratio = (entry - stop) / entry
    # 【寧缺勿濫鐵律】：若停損距離超過 3.8% 或小於 1.2%，代表風險結構不佳，直接不進場 (回傳 0 張)
    if stop_dist_ratio > CONFIG["MAX_STOP_PCT"]:
        return {"lots": 0, "stop_pct": 0.0, "max_loss_twd": 0, "capital_used": 0, "capital_pct": 0.0}

    friction = entry * 0.005
    risk_per_share = (entry - stop) + friction
    max_risk_twd = CONFIG["ACCOUNT_EQUITY"] * CONFIG["RISK_PER_TRADE"]
    raw_shares = max_risk_twd / risk_per_share
    max_cap_shares = (CONFIG["ACCOUNT_EQUITY"] * CONFIG["MAX_POS_RATIO"]) / entry
    lots = max(1, int(min(raw_shares, max_cap_shares) // 1000))
    stop_pct = round(((stop - entry) / entry) * 100, 2)
    capital_used = int(lots * 1000 * entry)
    capital_pct = round((capital_used / CONFIG["ACCOUNT_EQUITY"]) * 100, 1)
    return {
        "lots": lots,
        "stop_pct": stop_pct,
        "max_loss_twd": int(lots * 1000 * risk_per_share),
        "capital_used": capital_used,
        "capital_pct": capital_pct
    }

def calc_multi_stage_exit_plan(strategy: str, entry: float, hard_stop: float, total_lots: int, ind: dict) -> dict:
    """
    【高賺賠比三階段分批停利架構 (目標賺賠比 2.5:1 ~ 4.0:1)】：
    - 保本觸發線 (+1.4R)：盤中突破此價位後，隔日硬性停損自動上移至成本+0.6%
    - 停利一 TP1 (+2.2R)：先減碼 30% 落袋為安 (賺約 +6.5% ~ +8%)
    - 目標價 TP2 (+4.0R)：再減碼 30% 鎖定主升段暴利 (賺約 +12% ~ +16%)
    - 波段尾倉 (40%)：沿 10MA / 籌碼POC 動態防守，抱滿完整趨勢波段
    """
    risk_r = max(entry - hard_stop, entry * 0.02)
    entry_low = round(max(hard_stop * 1.012, min(ind["ma5"], ind["cdp_nl"], entry * 0.99)), 2)
    entry_high = round(entry * 1.012, 2)

    candidates_support = [ind["max_vol_low"], ind["bb_mid"], ind["cdp_al"], ind["ma10"]]
    valid_sups = [s for s in candidates_support if hard_stop <= s < entry * 0.985]
    raw_trailing = round(max(valid_sups), 2) if valid_sups else hard_stop

    tp1_price = round(entry + 2.2 * risk_r, 2)
    tp2_price = round(max(entry + 4.0 * risk_r, tp1_price * 1.06), 2)

    if total_lots >= 3:
        tp1_lots = max(1, int(round(total_lots * 0.30)))
        tp2_lots = max(1, int(round(total_lots * 0.30)))
        tp3_lots = total_lots - tp1_lots - tp2_lots
    elif total_lots == 2:
        tp1_lots, tp2_lots, tp3_lots = 1, 0, 1
    else:
        tp1_lots, tp2_lots, tp3_lots = 0, 1, 0

    tp1_pct = int(round((tp1_lots / max(total_lots, 1)) * 100))
    tp2_pct = int(round((tp2_lots / max(total_lots, 1)) * 100))
    tp3_pct = max(0, 100 - tp1_pct - tp2_pct)

    return {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "breakeven_trigger": round(entry + 1.4 * risk_r, 2),
        "tp1_price": tp1_price,
        "tp1_reduce_pct": tp1_pct,
        "tp1_reduce_lots": tp1_lots,
        "tp2_price": tp2_price,
        "tp2_reduce_pct": tp2_pct,
        "tp2_reduce_lots": tp2_lots,
        "tp3_remain_pct": tp3_pct,
        "tp3_remain_lots": tp3_lots,
        "dynamic_trailing_stop": raw_trailing
    }

def evaluate_candidate(df: pd.DataFrame, sid: str, sname: str, chip_info: dict, bench_ret_20d: float) -> list:
    if df is None or len(df) < 65:
        return []
    df = df.dropna(subset=["Close", "High", "Low", "Open", "Volume"]).copy()
    if len(df) < 65:
        return []

    df["vol_lots"] = df["Volume"] / 1000.0
    for p in [5, 10, 20, 60]:
        df[f"MA{p}"] = df["Close"].rolling(p).mean()
    df["MV5"] = df["vol_lots"].rolling(5).mean()
    df["MV20"] = df["vol_lots"].rolling(20).mean()
    df["RSI14"] = calc_rsi(df["Close"], 14)

    std20 = df["Close"].rolling(20).std()
    df["BB_UP"] = df["MA20"] + 2.0 * std20
    df["BB_LOW"] = df["MA20"] - 2.0 * std20

    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26
    df["MACD"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD_HIST"] = df["DIF"] - df["MACD"]

    cur = df.iloc[-1]
    prev = df.iloc[-2]
    if pd.isna(cur["MA60"]) or cur["MV20"] < CONFIG["MIN_MV20_LOTS"] or cur["Close"] < CONFIG["MIN_PRICE"]:
        return []

    stock_ret_20d = (float(cur["Close"]) - float(df.iloc[-21]["Close"])) / max(float(df.iloc[-21]["Close"]), 1.0)
    rs_strong = stock_ret_20d >= (bench_ret_20d + 0.015)
    ud_vol_ratio = calc_up_down_vol_ratio(df.iloc[-10:])

    vol_profile = calc_volume_poc_and_max_bar(df.iloc[-60:])
    cdp_info = calc_cdp_levels(float(cur["High"]), float(cur["Low"]), float(cur["Close"]))

    ind_pack = {
        "ma5": round(float(cur["MA5"]), 2),
        "ma10": round(float(cur["MA10"]), 2),
        "ma20": round(float(cur["MA20"]), 2),
        "ma60": round(float(cur["MA60"]), 2),
        "bb_up": round(float(cur["BB_UP"]), 2),
        "bb_mid": round(float(cur["MA20"]), 2),
        "bb_low": round(float(cur["BB_LOW"]), 2),
        "poc_price": vol_profile["poc_price"],
        "max_vol_high": vol_profile["max_vol_high"],
        "max_vol_low": vol_profile["max_vol_low"],
        "cdp": cdp_info["cdp"],
        "cdp_ah": cdp_info["ah"],
        "cdp_nh": cdp_info["nh"],
        "cdp_nl": cdp_info["nl"],
        "cdp_al": cdp_info["al"],
        "swing_high_20": round(float(df.iloc[-20:]["High"].max()), 2),
        "swing_high_60": round(float(df.iloc[-60:]["High"].max()), 2),
        "ud_vol_ratio": ud_vol_ratio
    }

    last3 = df.iloc[-3:]
    last5 = df.iloc[-5:]
    prev_15_high = df.iloc[-16:-1]["High"].max()
    pre_amp_10d = (df.iloc[-11:-1]["High"].max() - df.iloc[-11:-1]["Low"].min()) / max(df.iloc[-11:-1]["Low"].min(), 1)

    f_net = chip_info.get("foreign_net", 0)
    it_net = chip_info.get("it_net", 0)
    inst_net = chip_info.get("total_inst", f_net + it_net)
    chip_positive = (it_net > 0) or (f_net > 0) or (inst_net > 0)

    # 嚴格多頭排列：5MA >= 10MA >= 20MA > 60MA 且均線同步上揚
    strict_trend_up = (cur["Close"] >= cur["MA5"] >= cur["MA10"] >= cur["MA20"] > cur["MA60"]) and \
                      (cur["MA20"] > df.iloc[-5]["MA20"]) and (cur["MA60"] > df.iloc[-5]["MA60"])

    matched = []

    # S0: VCP 右側極度收斂 (波幅 <= 4.2% + 窒息量 + 緊貼 10MA 讓停損壓在 3% 內)
    amp_5d = (last5["High"].max() - last5["Low"].min()) / max(last5["Low"].min(), 1)
    vdu_3d = (last3["vol_lots"] < 0.55 * cur["MV20"]).any()
    if strict_trend_up and rs_strong and ud_vol_ratio >= 1.35 and (cur["Close"] >= ind_pack["poc_price"]) and \
       (amp_5d <= 0.042) and vdu_3d and (54 <= cur["RSI14"] <= 70) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(last5["Low"].min() * 0.996, entry * 0.968)), 2)
        matched.append(("S0_VCP收斂", entry, stop))

    # S1: 口袋支點拉回 (首次回測 10MA/20MA 量縮後帶量紅K點火，勝率與賺賠比王牌)
    near_ma10 = (0.0 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.02)
    prev10 = df.iloc[-11:-1]
    down_vols = prev10[prev10["Close"] < prev10["Open"]]["vol_lots"]
    max_down_vol = down_vols.max() if not down_vols.empty else 0
    if strict_trend_up and rs_strong and ud_vol_ratio >= 1.40 and near_ma10 and \
       (cur["Close"] > cur["Open"]) and (cur["vol_lots"] > max_down_vol > 0) and (55 <= cur["RSI14"] <= 72) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(min(cur["Low"], cur["MA10"]) * 0.994, entry * 0.968)), 2)
        matched.append(("S1_口袋支點拉回", entry, stop))

    # S2: 主升段第 3 浪突破 (前10日極窄整理 <= 8.5% 後首度帶量突破)
    if strict_trend_up and rs_strong and ud_vol_ratio >= 1.40 and (pre_amp_10d <= 0.085) and \
       (cur["Close"] > prev_15_high) and (1.5 * cur["MV20"] <= cur["vol_lots"] <= 2.8 * cur["MV20"]) and \
       (60 <= cur["RSI14"] <= 73) and (cur["MACD_HIST"] > prev["MACD_HIST"] > 0) and chip_positive:
        entry = round(float((cur["Close"] + prev_15_high) / 2.0), 2)
        stop = round(float(max(cur["Low"], cur["MA10"]) * 0.994), 2)
        matched.append(("S2_主升段3浪", entry, stop))

    # S3: 威科夫破底翻 2B (嚴格限制：必須在 60MA 之上，假跌破後當日帶量紅K同時收復前低與 20MA)
    sup_15 = df.iloc[-17:-2]["Low"].min()
    low_2d = df.iloc[-3:-1]["Low"].min()
    had_spring = (sup_15 * 0.97 <= low_2d < sup_15 * 0.998)
    reclaim_strong = (cur["Close"] > sup_15) and (cur["Close"] > cur["MA20"]) and (cur["Close"] >= cur["MA5"]) and \
                     (cur["Close"] > cur["Open"] * 1.012) and (cur["vol_lots"] >= 1.3 * cur["MV5"])
    if rs_strong and (cur["Close"] > cur["MA60"]) and (cur["MA60"] > df.iloc[-10]["MA60"]) and had_spring and reclaim_strong and (ud_vol_ratio >= 1.25) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(low_2d * 0.996, entry * 0.966)), 2)
        matched.append(("S3_破底翻2B", entry, stop))

    # S4: 威力跳空缺口 (底部或平台壓縮 <= 8.5% 後跳空突破 15 日高點)
    candle_pos = (cur["Close"] - cur["Low"]) / max(cur["High"] - cur["Low"], 0.01)
    has_power_gap = (cur["Open"] > prev["Close"] * 1.02) and (cur["Low"] > prev["High"] * 1.004) and (cur["Close"] > prev_15_high)
    if strict_trend_up and rs_strong and (pre_amp_10d <= 0.085) and has_power_gap and (candle_pos >= 0.70) and (cur["vol_lots"] >= 1.8 * cur["MV20"]) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(cur["Low"] * 0.996, entry * 0.965)), 2)
        matched.append(("S4_威力跳空", entry, stop))

    # S5: 法人量價共振 (要求四線多頭 + 量價比 >= 1.50 + 緊貼 5MA/10MA 不超過 2.2%)
    if strict_trend_up and (stock_ret_20d >= bench_ret_20d + 0.03) and (ud_vol_ratio >= 1.50) and \
       (it_net > 0 and f_net >= 0) and (0 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.022) and (56 <= cur["RSI14"] <= 68):
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(cur["MA20"] * 0.995, entry * 0.968)), 2)
        matched.append(("S5_法人量價共振", entry, stop))

    results = []
    trade_d = df.index[-1].strftime("%Y-%m-%d")
    for strat, entry, stop in matched:
        pos = calc_position_size(entry, stop)
        if pos["lots"] > 0:
            exit_plan = calc_multi_stage_exit_plan(strat, entry, stop, pos["lots"], ind_pack)
            results.append({
                "date": trade_d,
                "stock_id": sid,
                "stock_name": sname,
                "strategy": strat,
                "close": round(float(cur["Close"]), 2),
                "entry_price": entry,
                "hard_stop": stop,
                "stop_pct": pos["stop_pct"],
                "suggested_lots": pos["lots"],
                "capital_used": pos["capital_used"],
                "capital_pct": pos["capital_pct"],
                "max_loss_twd": pos["max_loss_twd"],
                "indicators_backend": ind_pack,
                "exit_plan": exit_plan
            })
    return results

def fetch_twse_institutional() -> tuple[str, dict]:
    chip_map = {}
    trade_date_str = datetime.date.today().strftime("%Y-%m-%d")
    headers = {"User-Agent": "Mozilla/5.0"}
    for offset in range(0, 7):
        dt = datetime.date.today() - datetime.timedelta(days=offset)
        if dt.weekday() >= 5:
            continue
        d_str = dt.strftime("%Y%m%d")
        try:
            url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d_str}&selectType=ALLBUT0999&response=json"
            r = requests.get(url, headers=headers, timeout=8).json()
            if r.get("stat") == "OK" and r.get("data"):
                trade_date_str = dt.strftime("%Y-%m-%d")
                for row in r["data"]:
                    sid = str(row[0]).strip()
                    name = str(row[1]).strip()
                    if len(sid) == 4 and sid.isdigit():
                        foreign_net = int(str(row[4]).replace(",", "")) // 1000
                        it_net = int(str(row[10]).replace(",", "")) // 1000
                        total_inst = int(str(row[-1]).replace(",", "")) // 1000
                        chip_map[sid] = {"name": name, "foreign_net": foreign_net, "it_net": it_net, "total_inst": total_inst}
                break
        except Exception:
            continue
    return trade_date_str, chip_map

def update_active_positions(new_signals: list) -> list:
    existing = []
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    active_map = {item["stock_id"]: item for item in existing if item.get("status") in ["HOLDING", "WATCHING"]}
    for sig in new_signals:
        sid = sig["stock_id"]
        ep = sig["exit_plan"]
        ind = sig["indicators_backend"]
        current_status = active_map[sid]["status"] if sid in active_map else "WATCHING"
        active_map[sid] = {
            "stock_id": sid,
            "stock_name": sig["stock_name"],
            "strategy": sig["strategy"],
            "entry_date": sig["date"],
            "entry_price": sig["entry_price"],
            "entry_low": ep["entry_low"],
            "entry_high": ep["entry_high"],
            "breakeven_trigger": ep["breakeven_trigger"],
            "hard_stop": sig["hard_stop"],
            "stop_pct": sig["stop_pct"],
            "dynamic_trailing_stop": ep["dynamic_trailing_stop"],
            "tp1_price": ep["tp1_price"],
            "tp1_reduce_pct": ep["tp1_reduce_pct"],
            "tp1_reduce_lots": ep["tp1_reduce_lots"],
            "tp2_price": ep["tp2_price"],
            "tp2_reduce_pct": ep["tp2_reduce_pct"],
            "tp2_reduce_lots": ep["tp2_reduce_lots"],
            "tp3_remain_pct": ep["tp3_remain_pct"],
            "tp3_remain_lots": ep["tp3_remain_lots"],
            "poc_price": ind["poc_price"],
            "max_vol_low": ind["max_vol_low"],
            "cdp_al": ind["cdp_al"],
            "ma5": ind["ma5"],
            "ma10": ind["ma10"],
            "suggested_lots": sig["suggested_lots"],
            "remaining_lots": sig["suggested_lots"],
            "capital_used": sig["capital_used"],
            "capital_pct": sig["capital_pct"],
            "max_loss_twd": sig["max_loss_twd"],
            "entry_alerted": (current_status == "HOLDING"),
            "tp1_alerted": False,
            "tp2_alerted": False,
            "reversal_alerted": False,
            "status": current_status
        }

    final_list = list(active_map.values())
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(final_list, f, ensure_ascii=False, indent=2)
    return final_list

def build_simplified_tg_report(payload: dict, trade_date: str) -> str:
    lines = [
        f"📌【台股五大指數成分股｜高賺賠比戰術簡報｜{trade_date} 21:00】",
        f"• 標的池：台灣50、中型100、富櫃50、富櫃200、MSCI ({payload.get('universe_quarter', '')})"
    ]
    sigs = payload.get("new_signals", [])
    if not sigs:
        lines.append("\n🛡️ 今日無符合「停損<=3.8% 且高賺賠比>=2.5:1」之嚴選標的，寧缺勿濫、空手觀望。")
    else:
        for s in sigs[:8]:
            ep = s["exit_plan"]
            lines.append(
                f"\n🎯 {s['stock_id']} {s['stock_name']}｜{s['strategy']}\n"
                f"• 進場點建議：{ep['entry_low']} ～ {s['entry_price']} 元\n"
                f"• 硬性停損點：{s['hard_stop']} 元 ({s['stop_pct']}%｜極窄防守)\n"
                f"• 分批停利與目標價：\n"
                f"  - 停利一 (TP1, 2.2R)：{ep['tp1_price']} 元 (減碼 {ep['tp1_reduce_pct']}% / 賣 {ep['tp1_reduce_lots']} 張)\n"
                f"  - 目標價 (TP2, 4.0R)：{ep['tp2_price']} 元 (減碼 {ep['tp2_reduce_pct']}% / 賣 {ep['tp2_reduce_lots']} 張)\n"
                f"  - 尾倉防守線：{ep['dynamic_trailing_stop']} 元 (剩餘 {ep['tp3_remain_pct']}% / {ep['tp3_remain_lots']} 張抱波段)\n"
                f"• 總部位控管：建議買進 {s['suggested_lots']} 張 (動用 {s['capital_used']//10000} 萬 / 佔總資金 {s['capital_pct']}%，最大風險 {s['max_loss_twd']:,} 元)"
            )
    return "\n".join(lines)

def send_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i in range(0, len(text), 3800):
        try:
            requests.post(url, json={"chat_id": chat_id, "text": text[i:i+3800]}, timeout=10)
        except Exception:
            pass

def main():
    trade_date, chip_map = fetch_twse_institutional()
    universe = build_dynamic_index_universe()

    tickers = ["0050.TW"] + [f"{sid}{sfx}" for sid, (_, sfx) in universe.items()]
    raw_candidates = []
    bench_ret_20d = 0.0

    try:
        data = yf.download(tickers, period="5mo", group_by="ticker", threads=True, progress=False)
        if "0050.TW" in data.columns.get_level_values(0):
            b_df = data["0050.TW"].dropna(subset=["Close"])
            if len(b_df) > 21:
                bench_ret_20d = (float(b_df["Close"].iloc[-1]) - float(b_df["Close"].iloc[-21])) / float(b_df["Close"].iloc[-21])

        for sid, (sname, sfx) in universe.items():
            t_sym = f"{sid}{sfx}"
            if t_sym not in data.columns.get_level_values(0):
                continue
            c_info = chip_map.get(sid, {"name": sname, "foreign_net": 0, "it_net": 0, "total_inst": 0})
            sigs = evaluate_candidate(data[t_sym], sid, sname, c_info, bench_ret_20d)
            raw_candidates.extend(sigs)
    except Exception as e:
        print(f"⚠️ 批次下載異常: {e}")

    raw_candidates.sort(key=lambda x: x["indicators_backend"]["ud_vol_ratio"], reverse=True)
    final_signals = raw_candidates[:8]

    active_list = update_active_positions(final_signals)
    report_payload = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "latest_trade_date": trade_date,
        "universe_quarter": get_current_quarter_tag(),
        "universe_count": len(universe),
        "new_signals": final_signals,
        "active_positions": active_list
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report_payload, f, ensure_ascii=False, indent=2)

    tg_text = build_simplified_tg_report(report_payload, trade_date)
    print("\n" + tg_text + "\n")
    send_telegram(tg_text)

if __name__ == "__main__":
    main()
