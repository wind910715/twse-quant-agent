import os
import json
import datetime
import requests
import numpy as np
import pandas as pd
import yfinance as yf

# ==========================================
# 1. 全局風控與【超額報酬 Alpha 分%數移動停利】設定 (SSOT)
# ==========================================
CONFIG = {
    "ACCOUNT_EQUITY": 3_000_000,   # 預設總資金 300 萬元
    "RISK_PER_TRADE": 0.01,        # 單筆最大容許虧損 1% (30,000 元)
    "MAX_POS_RATIO": 0.25,         # 單筆最大持倉上限 25% (750,000 元)
    "MIN_MV20_LOTS": 1000,         # 20日均量門檻 >= 1,000 張
    "MIN_PRICE": 25.0,             # 最低股價門檻 >= 25 元
    "MAX_STOP_PCT": 0.032,         # 初始硬性停損上限 -3.2% (壓低單筆最大虧損)
    # 分%數階梯停利與移動停利參數
    "BE_ACTIVATE_PCT": 0.035,      # 漲達 +3.5% 啟動跨日保本 (防守底線上移至 買進價+1.0%)
    "TP1_TARGET_PCT": 0.065,       # 停利一 (TP1)：+6.5% 減碼 30%，餘部收盤守 10MA / 最高收盤回落 4.5%
    "TP2_TARGET_PCT": 0.140,       # 停利二 (TP2)：+14.0% 再減碼 30%，鎖利底線 +7.0%，餘部收盤守 10MA
    "TP3_TARGET_PCT": 0.240,       # 波段目標 (TP3)：+24.0% 再減碼 20%，最後 20% 尾倉沿 10MA 抱滿波段
}

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
    if (entry - stop) / entry > CONFIG["MAX_STOP_PCT"]:
        stop = round(entry * (1.0 - 0.030), 2)

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
        "adjusted_stop": stop,
        "stop_pct": stop_pct,
        "max_loss_twd": int(lots * 1000 * risk_per_share),
        "capital_used": capital_used,
        "capital_pct": capital_pct
    }

def calc_pct_trailing_exit_plan(entry: float, hard_stop: float, total_lots: int, ind: dict) -> dict:
    """
    【超額報酬 Alpha 分%數停利 ＋ 收盤 10MA 吊燈移動停利】：
    - 右側進場觸發：隔日開盤後須站上 max(昨收, 今開*1.003) 確認紅K轉強才買進
    - 跨日保本 (+3.5%)：觸及後防守底線上移至 買進價+1.0%
    - 停利一 TP1 (+6.5%)：減碼 30%，盤中保底 +2.0%，波段倉以「收盤跌破 10MA 或最高收盤回落 4.5%」移動停利
    - 停利二 TP2 (+14.0%)：再減碼 30%，盤中保底 +7.0%，波段倉沿 10MA 移動停利
    - 目標價 TP3 (+24.0%)：再減碼 20%，最後 20% 尾倉沿 10MA 抱完大主升段
    """
    entry_low = round(entry, 2)
    entry_high = round(entry * 1.025, 2)

    be_trigger_price = round(entry * (1.0 + CONFIG["BE_ACTIVATE_PCT"]), 2)
    be_lock_price = round(entry * 1.010, 2)

    tp1_price = round(entry * (1.0 + CONFIG["TP1_TARGET_PCT"]), 2)
    tp2_price = round(entry * (1.0 + CONFIG["TP2_TARGET_PCT"]), 2)
    tp3_price = round(entry * (1.0 + CONFIG["TP3_TARGET_PCT"]), 2)

    if total_lots >= 3:
        tp1_lots = max(1, int(round(total_lots * 0.30)))
        tp2_lots = max(1, int(round(total_lots * 0.30)))
        tp3_lots = max(0, total_lots - tp1_lots - tp2_lots)
    elif total_lots == 2:
        tp1_lots, tp2_lots, tp3_lots = 1, 1, 0
    else:
        tp1_lots, tp2_lots, tp3_lots = 1, 0, 0

    tp1_pct = int(round((tp1_lots / max(total_lots, 1)) * 100))
    tp2_pct = int(round((tp2_lots / max(total_lots, 1)) * 100))
    tp3_pct = max(0, 100 - tp1_pct - tp2_pct)

    return {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "be_trigger_price": be_trigger_price,
        "be_lock_price": be_lock_price,
        "tp1_price": tp1_price,
        "tp1_reduce_pct": tp1_pct,
        "tp1_reduce_lots": tp1_lots,
        "tp1_trail_drawdown": 0.045,
        "tp2_price": tp2_price,
        "tp2_reduce_pct": tp2_pct,
        "tp2_reduce_lots": tp2_lots,
        "tp2_trail_drawdown": 0.045,
        "tp3_price": tp3_price,
        "tp3_remain_pct": tp3_pct,
        "tp3_remain_lots": tp3_lots,
        "tp3_trail_drawdown": 0.050,
        "dynamic_trailing_stop": hard_stop
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
    df["BB_WIDTH"] = (df["BB_UP"] - df["BB_LOW"]) / df["MA20"]

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
    stock_ret_40d = (float(cur["Close"]) - float(df.iloc[-41]["Close"])) / max(float(df.iloc[-41]["Close"]), 1.0)
    rs_score = round((0.6 * stock_ret_20d + 0.4 * stock_ret_40d) * 100, 2)
    rs_strong = (stock_ret_20d >= bench_ret_20d + 0.015) and (rs_score >= 3.0)

    ud_vol_ratio = calc_up_down_vol_ratio(df.iloc[-10:])
    if not rs_strong or ud_vol_ratio < 1.25:
        return []

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
        "ud_vol_ratio": ud_vol_ratio,
        "rs_score": rs_score
    }

    last3 = df.iloc[-3:]
    last5 = df.iloc[-5:]
    prev_10_high = df.iloc[-11:-1]["High"].max()
    prev_15_high = df.iloc[-16:-1]["High"].max()

    f_net = chip_info.get("foreign_net", 0)
    it_net = chip_info.get("it_net", 0)
    inst_net = chip_info.get("total_inst", f_net + it_net)
    chip_positive = (it_net > 0) or (f_net > 0) or (inst_net >= 0)

    trend_up = (cur["Close"] > cur["MA20"] > cur["MA60"]) and \
               (cur["MA20"] > df.iloc[-5]["MA20"]) and (cur["MA60"] > df.iloc[-5]["MA60"])
    candle_pos = (cur["Close"] - cur["Low"]) / max(cur["High"] - cur["Low"], 0.01)

    matched = []

    # S0: VCP 右側收斂突破 (近4日窄幅壓縮 <= 4.2% + 今日紅K突破前4日高點或收在最高)
    amp_4d = (df.iloc[-5:-1]["High"].max() - df.iloc[-5:-1]["Low"].min()) / max(df.iloc[-5:-1]["Low"].min(), 1)
    vdu_ok = (df.iloc[-5:-1]["vol_lots"] < 0.65 * cur["MV20"]).any()
    if trend_up and (amp_4d <= 0.042) and vdu_ok and (cur["Close"] >= df.iloc[-5:-1]["High"].max() * 0.998) and \
       (cur["Close"] > cur["Open"]) and (candle_pos >= 0.65) and (55 <= cur["RSI14"] <= 72) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(last5["Low"].min() * 0.996, entry * 0.970)), 2)
        matched.append(("S0_VCP收斂", entry, stop))

    # S1: 口袋支點拉回 (回測 10MA/20MA 量縮後帶量紅K突破昨高，勝率王牌)
    near_ma = (-0.010 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.022) or \
              (-0.008 <= (cur["Close"] - cur["MA20"]) / cur["MA20"] <= 0.020)
    prev10 = df.iloc[-11:-1]
    down_vols = prev10[prev10["Close"] < prev10["Open"]]["vol_lots"]
    max_down_vol = down_vols.max() if not down_vols.empty else 0
    if trend_up and near_ma and (cur["Close"] > prev["High"]) and (cur["Close"] > cur["Open"]) and \
       (cur["vol_lots"] > max_down_vol > 0) and (54 <= cur["RSI14"] <= 72) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(min(cur["Low"], cur["MA20"]) * 0.995, entry * 0.970)), 2)
        matched.append(("S1_口袋支點拉回", entry, stop))

    # S2: 布林帶寬壓縮突破 (John Carter Squeeze：均線糾結壓縮後首根突破長紅)
    avg_bb_w = float(df.iloc[-9:-1]["BB_WIDTH"].mean())
    if trend_up and (avg_bb_w <= 0.13) and (cur["Close"] > prev_15_high) and \
       (0 <= (cur["Close"] - cur["MA20"]) / cur["MA20"] <= 0.06) and (candle_pos >= 0.70) and \
       (1.35 * cur["MV20"] <= cur["vol_lots"] <= 3.2 * cur["MV20"]) and (cur["MACD_HIST"] > prev["MACD_HIST"] > 0) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(cur["Low"], cur["MA10"]) * 0.995), 2)
        matched.append(("S2_主升段3浪", entry, stop))

    # S3: 威科夫多頭洗盤破底翻 (嚴格收緊：假跌破10MA/前低後，當日帶量實體長紅 >= 1.8% 一舉收復 5MA 與 10MA)
    sup_10 = df.iloc[-12:-2]["Low"].min()
    low_2d = df.iloc[-3:-1]["Low"].min()
    had_spring = (sup_10 * 0.968 <= low_2d < sup_10 * 0.998)
    strong_reclaim = (cur["Close"] > sup_10) and (cur["Close"] >= cur["MA5"]) and (cur["Close"] >= cur["MA10"]) and \
                     (cur["Close"] > prev["High"]) and (cur["Close"] >= cur["Open"] * 1.018) and \
                     (candle_pos >= 0.72) and (cur["vol_lots"] >= 1.25 * cur["MV5"])
    if trend_up and had_spring and strong_reclaim and (ud_vol_ratio >= 1.35) and (53 <= cur["RSI14"] <= 68) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(cur["Low"] * 0.996, entry * 0.970)), 2)
        matched.append(("S3_破底翻2B", entry, stop))

    # S4: 歐尼爾威力跳空 / 缺口突破 (跳空開高或帶量突破10日平台高點且收在最高前25%)
    has_power_gap = ((cur["Open"] >= prev["Close"] * 1.012) and (cur["Close"] > prev_10_high)) or \
                    ((cur["Low"] > prev["High"]) and (cur["Close"] >= prev_10_high))
    if trend_up and has_power_gap and (candle_pos >= 0.72) and (cur["Close"] > cur["Open"] * 1.01) and \
       (0 <= (cur["Close"] - cur["MA20"]) / cur["MA20"] <= 0.075) and (cur["vol_lots"] >= 1.5 * cur["MV20"]) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(cur["Low"] * 0.995, entry * 0.970)), 2)
        matched.append(("S4_威力跳空", entry, stop))

    # S5: 法人強勢鎖碼共振 (四線多頭排列 + 連2日價漲量增 + 創5日高點但緊貼10MA)
    two_red = (cur["Close"] > cur["Open"]) and (prev["Close"] >= prev["Open"]) and (cur["Close"] >= last5["High"].max() * 0.995)
    if trend_up and (cur["MA5"] >= cur["MA10"] >= cur["MA20"]) and two_red and (ud_vol_ratio >= 1.45) and \
       (0.005 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.032) and (56 <= cur["RSI14"] <= 70) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(cur["MA10"] * 0.992, entry * 0.970)), 2)
        matched.append(("S5_法人量價共振", entry, stop))

    results = []
    trade_d = df.index[-1].strftime("%Y-%m-%d")
    for strat, entry, raw_stop in matched:
        pos = calc_position_size(entry, raw_stop)
        if pos["lots"] > 0:
            adj_stop = pos["adjusted_stop"]
            exit_plan = calc_pct_trailing_exit_plan(entry, adj_stop, pos["lots"], ind_pack)
            results.append({
                "date": trade_d,
                "stock_id": sid,
                "stock_name": sname,
                "strategy": strat,
                "close": round(float(cur["Close"]), 2),
                "entry_price": entry,
                "hard_stop": adj_stop,
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
            "highest_price": sig["entry_price"],
            "entry_low": ep["entry_low"],
            "entry_high": ep["entry_high"],
            "be_trigger_price": ep["be_trigger_price"],
            "be_lock_price": ep["be_lock_price"],
            "hard_stop": sig["hard_stop"],
            "stop_pct": sig["stop_pct"],
            "dynamic_trailing_stop": ep["dynamic_trailing_stop"],
            "tp1_price": ep["tp1_price"],
            "tp1_reduce_pct": ep["tp1_reduce_pct"],
            "tp1_reduce_lots": ep["tp1_reduce_lots"],
            "tp1_trail_drawdown": ep["tp1_trail_drawdown"],
            "tp2_price": ep["tp2_price"],
            "tp2_reduce_pct": ep["tp2_reduce_pct"],
            "tp2_reduce_lots": ep["tp2_reduce_lots"],
            "tp2_trail_drawdown": ep["tp2_trail_drawdown"],
            "tp3_price": ep["tp3_price"],
            "tp3_remain_pct": ep["tp3_remain_pct"],
            "tp3_remain_lots": ep["tp3_remain_lots"],
            "tp3_trail_drawdown": ep["tp3_trail_drawdown"],
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
            "be_locked": False,
            "tp1_alerted": False,
            "tp2_alerted": False,
            "tp3_alerted": False,
            "reversal_alerted": False,
            "status": current_status
        }

    final_list = list(active_map.values())
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(final_list, f, ensure_ascii=False, indent=2)
    return final_list

def build_simplified_tg_report(payload: dict, trade_date: str) -> str:
    lines = [
        f"📌【台股五大指數成分股｜超額報酬 Alpha 戰報｜{trade_date} 21:00】",
        f"• 範圍：台灣50、中型100、富櫃50、富櫃200、MSCI ({payload.get('universe_quarter', '')})"
    ]
    sigs = payload.get("new_signals", [])
    if not sigs:
        lines.append("\n🛡️ 今日無符合條件之新標的，建議保留現金或續抱移動停利部位。")
    else:
        for s in sigs[:8]:
            ep = s["exit_plan"]
            lines.append(
                f"\n🎯 {s['stock_id']} {s['stock_name']}｜{s['strategy']}\n"
                f"• 進場點建議：明日開盤後轉強突破 {s['entry_price']} 元 (上限 {ep['entry_high']} 元，開低走低不買)\n"
                f"• 初始停損點：{s['hard_stop']} 元 ({s['stop_pct']}%｜3日不漲破5MA主動汰弱)\n"
                f"• 分%數停利 ＆ 動態移動停利：\n"
                f"  - 保本啟動 (+3.5%)：達 {ep['be_trigger_price']} 元 ➔ 防守上移至 {ep['be_lock_price']} 元 (+1.0%)\n"
                f"  - 停利一 (+6.5%)：達 {ep['tp1_price']} 元 ➔ 減碼 {ep['tp1_reduce_pct']}% (賣 {ep['tp1_reduce_lots']} 張)，餘部收盤守10MA\n"
                f"  - 停利二 (+14.0%)：達 {ep['tp2_price']} 元 ➔ 再減碼 {ep['tp2_reduce_pct']}% (賣 {ep['tp2_reduce_lots']} 張)，鎖利底線 +7%\n"
                f"  - 目標價 (+24.0%)：達 {ep['tp3_price']} 元 ➔ 尾倉 {ep['tp3_remain_pct']}% ({ep['tp3_remain_lots']} 張) 沿 10MA 抱到底\n"
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

    raw_candidates.sort(key=lambda x: x["indicators_backend"]["rs_score"], reverse=True)
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
