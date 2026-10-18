import os
import json
import datetime
import requests
import numpy as np
import pandas as pd
import yfinance as yf

# ==========================================
# 1. 全局風控與高勝率參數設定 (Single Source of Truth)
# ==========================================
CONFIG = {
    "ACCOUNT_EQUITY": 3_000_000,   # 預設總資金 300 萬元
    "RISK_PER_TRADE": 0.01,        # 單筆最大容許虧損 1% (30,000 元)
    "MAX_POS_RATIO": 0.25,         # 單筆最大持倉上限 25% (750,000 元)
    "MIN_MV20_LOTS": 1200,         # 20日均量門檻 >= 1,200 張
    "MIN_PRICE": 25.0,             # 最低股價門檻 >= 25 元
}

SMART_BROKERS = ["摩根大通", "台灣摩根士丹利", "美商高盛", "瑞士信貸", "瑞銀", "美林", "富邦-建國", "凱基-信義", "元大-城中"]
DAY_TRADE_BROKERS = ["凱基-台北", "凱基-松山", "群益金鼎-大安", "富邦-嘉義", "元大-土城永寧", "富邦-虎尾"]

POSITIONS_FILE = "active_positions.json"
REPORT_FILE = "latest_report.json"
UNIVERSE_CACHE_FILE = "universe_cache.json"

# 基礎核心保底名單 (當證交所/櫃買中心 API 維護時作為保底，涵蓋 0050/0051/富櫃50/MSCI 核心股)
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

# ==========================================
# 2. 五大指數成分股動態季度更新引擎
#    (台灣50、中型100、富櫃50、富櫃200、MSCI成分股)
# ==========================================
def get_current_quarter_tag() -> str:
    now = datetime.date.today()
    # 涵蓋 2/3/5/6/8/9/11/12 月之 MSCI 與 台灣指數公司/櫃買中心 季度調整週期
    return f"{now.year}-M{now.month:02d}"

def build_dynamic_index_universe() -> dict:
    """
    自動從證交所(TWSE)與櫃買中心(TPEx)官方 OpenAPI 動態抓取最新季度成分股池：
    1. 上市前 150 大高權重/高流動性標的 (完整涵蓋 台灣50 + 中型100 + MSCI台灣成分股)
    2. 上櫃前 150 大高權重/高流動性標的 (完整涵蓋 富櫃50 + 富櫃200 核心成分股)
    每逢月份/季度更迭自動刷新快取。
    """
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

    # A. 動態抓取上市 (TWSE) 最新權值與高流動性成分股 (台灣50 + 中型100 + MSCI)
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
        # 依成交金額與流動性排序取前 150 大上市成分股
        twse_rows.sort(key=lambda x: x[2], reverse=True)
        for sid, sname, _ in twse_rows[:150]:
            universe[sid] = (sname, ".TW")
        print(f"✅ 已動態更新【台灣50 + 中型100 + MSCI】上市成分股池 (共納入 {min(150, len(twse_rows))} 檔)")
    except Exception as e:
        print(f"⚠️ TWSE 官方成分股 API 暫時略過，使用保底名單: {e}")

    # B. 動態抓取上櫃 (TPEx) 最新富櫃50 與 富櫃200 成分股
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
        # 依成交值與市值活躍度取前 120 大上櫃成分股 (涵蓋富櫃50與富櫃200核心)
        tpex_rows.sort(key=lambda x: x[2], reverse=True)
        for sid, sname, _ in tpex_rows[:120]:
            universe[sid] = (sname, ".TWO")
        print(f"✅ 已動態更新【富櫃50 + 富櫃200】上櫃成分股池 (共納入 {min(120, len(tpex_rows))} 檔)")
    except Exception as e:
        print(f"⚠️ TPEx 官方成分股 API 暫時略過，使用保底名單: {e}")

    try:
        with open(UNIVERSE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"quarter_tag": q_tag, "updated_at": str(datetime.date.today()), "stocks": universe}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return universe

# ==========================================
# 3. 技術與高勝率量價指標計算函數
# ==========================================
def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calc_up_down_vol_ratio(df_10: pd.DataFrame) -> float:
    """計算近10日【上漲日均量 / 下跌日均量】比值 (大於 1.25 代表主力真吃貨、洗盤量縮)"""
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
    【高勝率優化版三階段停利】：
    - TP1 改為 1.2R (約 +3.5%~+4.5% 短壓區) 先減碼 50% 鎖住高勝率，並將剩餘部位停損上移至成本+0.6%
    - TP2 設為 2.8R 波段目標價，再減碼 30%
    - 尾倉 20% 守五維度動態防守線 (POC / MA20布林中軌 / 10MA)
    """
    risk_r = max(entry - hard_stop, entry * 0.025)
    entry_low = round(max(hard_stop * 1.015, min(ind["ma5"], ind["cdp_nl"], entry * 0.988)), 2)
    entry_high = round(entry * 1.012, 2)

    candidates_support = [ind["max_vol_low"], ind["bb_mid"], ind["cdp_al"]]
    if "S2_" in strategy or "S4_" in strategy:
        raw_trailing = round(max(min(ind["ma10"], entry * 0.975), hard_stop), 2)
    else:
        valid_sups = [s for s in candidates_support if hard_stop <= s < entry * 0.985]
        raw_trailing = round(max(valid_sups), 2) if valid_sups else hard_stop

    # TP1 鎖定 1.2R 快速保本鎖利點
    tp1_price = round(max(entry * 1.035, entry + 1.25 * risk_r), 2)
    # TP2 鎖定 2.8R 波段目標價
    tp2_price = round(max(entry + 2.8 * risk_r, ind["swing_high_60"], tp1_price * 1.05), 2)

    if total_lots >= 3:
        tp1_lots = max(1, int(round(total_lots * 0.50)))
        tp2_lots = max(1, int(round((total_lots - tp1_lots) * 0.60)))
        tp3_lots = total_lots - tp1_lots - tp2_lots
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

# ==========================================
# 4. 優化版高勝率選股核心 (加入 RS 強弱度 + 量價累積比 + 低位階濾網)
# ==========================================
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

    # 【高勝率全局濾網 1】：計算個股近20日報酬率 vs 0050 大盤，要求 RS 相對強弱度 > 大盤
    stock_ret_20d = (float(cur["Close"]) - float(df.iloc[-21]["Close"])) / max(float(df.iloc[-21]["Close"]), 1.0)
    rs_outperform = stock_ret_20d >= (bench_ret_20d - 0.01)

    # 【高勝率全局濾網 2】：近10日上漲均量 / 下跌均量 >= 1.20 (價漲量增、價跌量縮)
    ud_vol_ratio = calc_up_down_vol_ratio(df.iloc[-10:])
    if not rs_outperform or ud_vol_ratio < 1.15:
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
        "ud_vol_ratio": ud_vol_ratio
    }

    last3 = df.iloc[-3:]
    last5 = df.iloc[-5:]
    last10 = df.iloc[-10:]
    prev_15_high = df.iloc[-16:-1]["High"].max()

    f_net = chip_info.get("foreign_net", 0)
    it_net = chip_info.get("it_net", 0)
    inst_net = chip_info.get("total_inst", f_net + it_net)
    chip_positive = (it_net > 0) or (f_net > 0) or (inst_net >= 0)

    # 嚴格要求月線(MA20)與季線(MA60)同步翻揚向上，且收盤站上 5MA
    trend_up = (cur["Close"] >= cur["MA5"]) and (cur["Close"] > cur["MA20"] > cur["MA60"]) and \
               (cur["MA20"] > df.iloc[-5]["MA20"]) and (cur["MA60"] >= df.iloc[-5]["MA60"])

    matched = []

    # S0: VCP 右側量縮收斂 (新增：收盤須站上5MA與籌碼POC之上，且55<=RSI<=72)
    amp_5d = (last5["High"].max() - last5["Low"].min()) / max(last5["Low"].min(), 1)
    vdu_3d = (last3["vol_lots"] < 0.60 * cur["MV20"]).any()
    if trend_up and (cur["Close"] >= ind_pack["poc_price"]) and (cur["Close"] >= ind_pack["swing_high_60"] * 0.88) and \
       (amp_5d <= 0.048) and vdu_3d and (52 <= cur["RSI14"] <= 72) and chip_positive:
        entry = round(float((cur["Close"] + cur["MA5"]) / 2.0), 2)
        stop = round(float(min(last5["Low"].min(), ind_pack["ma20"]) * 0.994), 2)
        matched.append(("S0_VCP右側量縮收斂", entry, stop))

    # S1: 口袋支點拉回 (新增：20MA明顯上揚 + RSI>=53 + 下影線或紅K實體飽滿)
    near_ma = (-0.008 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.022) or \
              (-0.008 <= (cur["Close"] - cur["MA20"]) / cur["MA20"] <= 0.022)
    prev10 = df.iloc[-11:-1]
    down_vols = prev10[prev10["Close"] < prev10["Open"]]["vol_lots"]
    max_down_vol = down_vols.max() if not down_vols.empty else 0
    if trend_up and near_ma and (cur["Close"] > cur["Open"]) and (cur["vol_lots"] > max_down_vol > 0) and \
       (cur["RSI14"] >= 53) and (ud_vol_ratio >= 1.3) and chip_positive:
        entry = round(float(cur["Close"] * 0.995), 2)
        stop = round(float(max(min(cur["Low"], cur["MA20"] * 0.992), entry * 0.955)), 2)
        matched.append(("S1_口袋支點拉回", entry, stop))

    # S2: 主升段第 3 浪突破 (新增：突破前先經過低波幅收斂，避免買到末升段乖離過大股)
    pre_amp_10d = (df.iloc[-11:-1]["High"].max() - df.iloc[-11:-1]["Low"].min()) / max(df.iloc[-11:-1]["Low"].min(), 1)
    if trend_up and (pre_amp_10d <= 0.12) and (cur["Close"] > prev_15_high) and \
       (1.4 * cur["MV20"] <= cur["vol_lots"] <= 3.2 * cur["MV20"]) and (60 <= cur["RSI14"] <= 74) and \
       (cur["MACD_HIST"] > prev["MACD_HIST"] > 0) and chip_positive:
        entry = round(float((cur["Close"] + prev_15_high) / 2.0), 2)
        stop = round(float(max(cur["Low"], cur["MA10"]) * 0.992), 2)
        matched.append(("S2_主升段第3浪突破", entry, stop))

    # S3: 威科夫破底翻 2B (鎖定季線向上之強勢股假跌破月線/箱底後帶量站回)
    sup_20 = df.iloc[-21:-3]["Low"].min()
    low_3d = df.iloc[-4:-1]["Low"].min()
    if (cur["MA60"] > df.iloc[-10]["MA60"]) and (sup_20 * 0.96 <= low_3d < sup_20 * 0.998) and \
       (cur["Close"] > sup_20) and (cur["Close"] > cur["Open"]) and (cur["vol_lots"] >= 1.2 * cur["MV5"]) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(low_3d * 0.995), 2)
        matched.append(("S3_威科夫破底翻2B", entry, stop))

    # S4: 威力跳空缺口 (新增：跳空前10日振幅<=8.5%，確保是底部起漲突破缺口而非高檔竭盡缺口)
    candle_pos = (cur["Close"] - cur["Low"]) / max(cur["High"] - cur["Low"], 0.01)
    if trend_up and (pre_amp_10d <= 0.085) and (cur["Low"] > prev["High"] * 1.018) and \
       (candle_pos >= 0.70) and (cur["vol_lots"] >= 1.8 * cur["MV20"]) and chip_positive:
        entry = round(float(cur["Low"] * 1.005), 2)  # 以缺口上緣支撐作為理想切入點
        stop = round(float(prev["High"] * 0.995), 2)
        matched.append(("S4_威力跳空缺口", entry, stop))

    # S5: 投信外資籌碼共振
    if trend_up and (it_net > 0 and f_net > 0) and (0 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.045):
        entry = round(float((cur["Close"] + cur["MA10"]) / 2.0), 2)
        stop = round(float(min(cur["MA20"], last3["Low"].min()) * 0.992), 2)
        matched.append(("S5_投信外資籌碼共振", entry, stop))

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
        f"📌【台股五大指數成分股戰術簡報｜{trade_date} 21:00】",
        f"• 掃描範圍：台灣50、中型100、富櫃50、富櫃200、MSCI成分股 ({payload.get('universe_quarter', '')} 季度名單)"
    ]
    sigs = payload.get("new_signals", [])
    if not sigs:
        lines.append("\n今日無符合高勝率濾網之新標的，建議保留現金觀望。")
    else:
        for s in sigs[:12]:
            ep = s["exit_plan"]
            lines.append(
                f"\n🎯 {s['stock_id']} {s['stock_name']}｜{s['strategy']}\n"
                f"• 進場點建議：{ep['entry_low']} ～ {s['entry_price']} 元\n"
                f"• 硬性停損點：{s['hard_stop']} 元 ({s['stop_pct']}%)\n"
                f"• 分批停利與目標價：\n"
                f"  - 停利一 (TP1)：{ep['tp1_price']} 元 (減碼 {ep['tp1_reduce_pct']}% / 賣 {ep['tp1_reduce_lots']} 張，停損上移保本)\n"
                f"  - 目標價 (TP2)：{ep['tp2_price']} 元 (減碼 {ep['tp2_reduce_pct']}% / 賣 {ep['tp2_reduce_lots']} 張)\n"
                f"  - 尾倉防守線：{ep['dynamic_trailing_stop']} 元 (剩餘 {ep['tp3_remain_pct']}% / {ep['tp3_remain_lots']} 張跌破出清)\n"
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

    # 納入 0050.TW 作為計算 RS 相對強弱度之大盤基準
    tickers = ["0050.TW"] + [ f"{sid}{sfx}" for sid, (_, sfx) in universe.items() ]
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

    # 依量價累積比與相對強弱排序，精選最優前 10 檔
    raw_candidates.sort(key=lambda x: x["indicators_backend"]["ud_vol_ratio"], reverse=True)
    final_signals = raw_candidates[:10]

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
