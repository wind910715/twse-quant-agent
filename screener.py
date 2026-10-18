import os
import json
import datetime
import requests
import numpy as np
import pandas as pd
import yfinance as yf

# ==========================================
# 1. 全局風控與部位控管參數 (Single Source of Truth)
# ==========================================
CONFIG = {
    "ACCOUNT_EQUITY": 3_000_000,   # 預設總資金 300 萬元
    "RISK_PER_TRADE": 0.01,        # 單筆最大容許虧損 1% (30,000 元)
    "MAX_POS_RATIO": 0.25,         # 單筆最大持倉上限 25% (750,000 元)
    "MIN_MV20_LOTS": 1000,         # 20日均量門檻 >= 1,000 張
    "MIN_PRICE": 20.0,             # 最低股價門檻 >= 20 元
}

SMART_BROKERS = ["摩根大通", "台灣摩根士丹利", "美商高盛", "瑞士信貸", "瑞銀", "美林", "富邦-建國", "凱基-信義", "元大-城中"]
DAY_TRADE_BROKERS = ["凱基-台北", "凱基-松山", "群益金鼎-大安", "富邦-嘉義", "元大-土城永寧", "富邦-虎尾"]

CORE_POOL = {
    "2330": "台積電", "2454": "聯發科", "2317": "鴻海", "2308": "台達電", "2382": "廣達",
    "2383": "台光電", "3711": "日月光投控", "3037": "欣興", "3189": "景碩", "8046": "南電",
    "2409": "友達", "3481": "群創", "3017": "奇鋐", "3324": "雙鴻", "3661": "世芯-KY",
    "3443": "創意", "6669": "緯穎", "3231": "緯創", "2356": "英業達", "2376": "技嘉",
    "2345": "智邦", "6285": "啟碁", "3450": "聯鈞", "4979": "華星光", "3363": "上詮",
    "2603": "長榮", "2609": "陽明", "2615": "萬海", "2618": "長榮航", "2610": "華航",
    "2881": "富邦金", "2882": "國泰金", "2891": "中信金", "2886": "兆豐金", "5880": "合庫金",
    "1513": "中興電", "1519": "華城", "1503": "士電", "1504": "東元", "2368": "金像電",
    "6274": "台燿", "3044": "健鼎", "2327": "國巨", "3008": "大立光", "3034": "聯詠",
    "2379": "瑞昱", "6415": "矽力*-KY", "5269": "祥碩", "3533": "嘉澤", "2059": "川湖"
}

POSITIONS_FILE = "active_positions.json"
REPORT_FILE = "latest_report.json"

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calc_volume_poc_and_max_bar(df_60: pd.DataFrame) -> dict:
    """後台詳細計算：60日籌碼最大量密集區(POC)與近20日最大量K棒高低點"""
    typical_price = (df_60["High"] + df_60["Low"] + df_60["Close"]) / 3.0
    vols = df_60["vol_lots"].values
    p_min, p_max = typical_price.min(), typical_price.max()
    if p_max <= p_min:
        poc_price = float(df_60["Close"].iloc[-1])
    else:
        bins = np.linspace(p_min, p_max, 16)
        inds = np.digitize(typical_price.values, bins) - 1
        inds = np.clip(inds, 0, 14)
        bin_vols = np.bincount(inds, weights=vols, minlength=15)
        best_bin = int(np.argmax(bin_vols))
        poc_price = round(float((bins[best_bin] + bins[best_bin + 1]) / 2.0), 2)

    df_20 = df_60.iloc[-20:]
    max_idx = df_20["vol_lots"].idxmax()
    max_bar = df_20.loc[max_idx]
    return {
        "poc_price": poc_price,
        "max_vol_high": round(float(max_bar["High"]), 2),
        "max_vol_low": round(float(max_bar["Low"]), 2),
        "max_vol_lots": int(max_bar["vol_lots"])
    }

def calc_cdp_levels(high: float, low: float, close: float) -> dict:
    """後台詳細計算：明日盤中 CDP 五關價"""
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
    """計算總部位控管建議：買進張數、動用資金、佔總資金比例、最大風險金額"""
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
    """後台綜合五維度指標(POC、支撐壓力、均線、布林、CDP)，輸出精簡進出場與減碼參數"""
    risk_r = max(entry - hard_stop, entry * 0.025)

    # 計算理想低接下緣與突破上限 (供隔日盤中進場偵測使用)
    entry_low = round(max(hard_stop * 1.015, min(ind["ma10"], ind["cdp_nl"], entry * 0.99)), 2)
    entry_high = round(entry * 1.015, 2)

    # 綜合動態防守線
    candidates_support = [ind["max_vol_low"], ind["bb_mid"], ind["cdp_al"]]
    if "S2_" in strategy or "S4_" in strategy:
        raw_trailing = round(max(min(ind["ma10"], entry * 0.975), hard_stop), 2)
    else:
        valid_sups = [s for s in candidates_support if hard_stop <= s < entry * 0.985]
        raw_trailing = round(max(valid_sups), 2) if valid_sups else hard_stop

    # 第一停利點 (TP1) 與 波段目標價 (TP2)
    tp1_candidates = [ind["bb_up"], ind["cdp_ah"], ind["swing_high_20"], entry + 1.8 * risk_r]
    valid_tp1 = [p for p in tp1_candidates if p >= entry * 1.035]
    tp1_price = round(min(valid_tp1) if valid_tp1 else entry + 1.8 * risk_r, 2)
    tp2_price = round(max(entry + 3.0 * risk_r, ind["swing_high_60"], tp1_price * 1.05), 2)

    # 分批減碼張數與比例
    if total_lots >= 3:
        tp1_lots = max(1, int(round(total_lots * 0.40)))
        tp2_lots = max(1, int(round((total_lots - tp1_lots) * 0.50)))
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

def check_finmind_broker(stock_id: str, trade_date: str, total_vol_lots: float) -> dict:
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    res = {"top_smart_broker": "法人籌碼集中", "day_trade_ratio": 0.0, "pass_broker": True}
    if not token:
        return res
    try:
        from FinMind.data import DataLoader
        dl = DataLoader()
        dl.login_by_token(api_token=token)
        df_b = dl.taiwan_stock_trading_daily_report(stock_id=stock_id, date=trade_date)
        if df_b is not None and not df_b.empty and total_vol_lots > 0:
            df_b["net_lots"] = (df_b["buy"] - df_b["sell"]) / 1000.0
            smart_mask = df_b["securities_trader"].apply(lambda x: any(k in str(x) for k in SMART_BROKERS))
            smart_df = df_b[smart_mask & (df_b["net_lots"] > 0)].sort_values("net_lots", ascending=False)
            if not smart_df.empty:
                b_name = smart_df.iloc[0]["securities_trader"]
                b_ratio = round((smart_df.iloc[0]["net_lots"] / total_vol_lots) * 100, 1)
                res["top_smart_broker"] = f"{b_name} (+{b_ratio}%)"
            dt_mask = df_b["securities_trader"].apply(lambda x: any(k in str(x) for k in DAY_TRADE_BROKERS))
            dt_lots = df_b[dt_mask & (df_b["net_lots"] > 0)]["net_lots"].sum()
            dt_ratio = round((dt_lots / total_vol_lots) * 100, 1)
            res["day_trade_ratio"] = dt_ratio
            res["pass_broker"] = (dt_ratio < 15.0)
    except Exception:
        pass
    return res

def evaluate_candidate(df: pd.DataFrame, sid: str, sname: str, chip_info: dict) -> list:
    if df is None or len(df) < 60:
        return []
    df = df.dropna(subset=["Close", "High", "Low", "Open", "Volume"]).copy()
    if len(df) < 60:
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

    vol_profile = calc_volume_poc_and_max_bar(df.iloc[-60:])
    cdp_info = calc_cdp_levels(float(cur["High"]), float(cur["Low"]), float(cur["Close"]))

    # 後台保留完整五維度座標 (寫入 JSON 供 Gemini 與盤中引擎使用)
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
    }

    last3 = df.iloc[-3:]
    last5 = df.iloc[-5:]
    last10 = df.iloc[-10:]
    prev_20_high = df.iloc[-21:-1]["High"].max()

    f_net = chip_info.get("foreign_net", 0)
    it_net = chip_info.get("it_net", 0)
    inst_net = chip_info.get("total_inst", f_net + it_net)
    chip_positive = (it_net > 0) or (f_net > 0) or (inst_net >= 0)
    trend_up = (cur["Close"] > cur["MA20"] > cur["MA60"]) and (cur["MA60"] >= df.iloc[-5]["MA60"] * 0.998)

    matched = []
    # S0: VCP 右側量縮收斂
    amp_5d = (last5["High"].max() - last5["Low"].min()) / max(last5["Low"].min(), 1)
    vdu_3d = (last3["vol_lots"] < 0.65 * cur["MV20"]).any()
    if trend_up and (cur["Close"] >= ind_pack["swing_high_60"] * 0.86) and (amp_5d <= 0.055) and vdu_3d and chip_positive:
        entry = round(float(last5["High"].max()), 2)
        stop = round(float(min(last5["Low"].min(), ind_pack["poc_price"]) * 0.995), 2)
        matched.append(("S0_VCP右側量縮收斂", entry, stop))

    # S1: 口袋支點拉回
    near_ma = (-0.015 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.03) or \
              (-0.015 <= (cur["Close"] - cur["MA20"]) / cur["MA20"] <= 0.03)
    prev10 = df.iloc[-11:-1]
    down_vols = prev10[prev10["Close"] < prev10["Open"]]["vol_lots"]
    max_down_vol = down_vols.max() if not down_vols.empty else 0
    if trend_up and (last10["High"].max() >= ind_pack["swing_high_20"]) and near_ma and (cur["Close"] > cur["Open"]) and (cur["vol_lots"] > max_down_vol > 0) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(min(cur["Low"], cur["MA20"] * 0.99), entry * 0.95)), 2)
        matched.append(("S1_口袋支點拉回", entry, stop))

    # S2: 主升段第 3 浪突破
    if trend_up and (cur["Close"] >= prev_20_high) and (cur["vol_lots"] >= 1.5 * cur["MV20"]) and \
       (58 <= cur["RSI14"] <= 80) and (cur["MACD_HIST"] > prev["MACD_HIST"]) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max((cur["Open"] + cur["Close"]) / 2, entry * 0.94)), 2)
        matched.append(("S2_主升段第3浪突破", entry, stop))

    # S3: 威科夫破底翻 2B
    sup_20 = df.iloc[-21:-3]["Low"].min()
    low_3d = df.iloc[-4:-1]["Low"].min()
    if (cur["Close"] > cur["MA60"] * 0.94) and (sup_20 * 0.95 <= low_3d < sup_20 * 0.998) and \
       (cur["Close"] > sup_20) and (cur["Close"] > cur["Open"]) and (cur["vol_lots"] >= 1.1 * cur["MV5"]) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(low_3d * 0.995), 2)
        matched.append(("S3_威科夫破底翻2B", entry, stop))

    # S4: 威力跳空缺口
    candle_pos = (cur["Close"] - cur["Low"]) / max(cur["High"] - cur["Low"], 0.01)
    if (cur["Low"] > prev["High"] * 1.02) and (candle_pos >= 0.65) and (cur["vol_lots"] >= 2.0 * cur["MV20"]) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(cur["Low"] * 0.995), 2)
        matched.append(("S4_威力跳空缺口", entry, stop))

    # S5: 投信外資籌碼共振
    if trend_up and (it_net > 0 and f_net >= 0) and (0 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.055):
        entry = round(float(cur["Close"]), 2)
        stop = round(float(min(cur["MA20"], last3["Low"].min()) * 0.99), 2)
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

def update_active_positions(new_signals: list) -> list:
    existing = []
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    # 保留已持倉(HOLDING)與觀察中(WATCHING)標的
    active_map = {item["stock_id"]: item for item in existing if item.get("status") in ["HOLDING", "WATCHING"]}
    for sig in new_signals:
        sid = sig["stock_id"]
        ep = sig["exit_plan"]
        ind = sig["indicators_backend"]
        # 若原本已 HOLDING 則維持 HOLDING 並更新動態防守線；若為新入選股則設為 WATCHING 等待隔日盤中觸發買點
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
    """前台 Telegram 極簡推播格式：隱藏繁雜指標，只給精確進出場、目標價與總部位控管"""
    lines = [f"📌【台股戰術簡報｜{trade_date} 晚間 21:00】"]
    sigs = payload.get("new_signals", [])
    if not sigs:
        lines.append("\n今日無符合高標準共振之新標的，建議保留現金觀望。")
    else:
        for s in sigs:
            ep = s["exit_plan"]
            lines.append(
                f"\n🎯 {s['stock_id']} {s['stock_name']}｜{s['strategy']}\n"
                f"• 進場點建議：{ep['entry_low']} ～ {s['entry_price']} 元 (突破 {s['entry_price']} 元確認)\n"
                f"• 硬性停損點：{s['hard_stop']} 元 ({s['stop_pct']}%)\n"
                f"• 分批停利與目標價：\n"
                f"  - 停利一 (TP1)：{ep['tp1_price']} 元 (減碼 {ep['tp1_reduce_pct']}% / 賣 {ep['tp1_reduce_lots']} 張)\n"
                f"  - 目標價 (TP2)：{ep['tp2_price']} 元 (減碼 {ep['tp2_reduce_pct']}% / 賣 {ep['tp2_reduce_lots']} 張)\n"
                f"  - 尾倉防守線：{ep['dynamic_trailing_stop']} 元 (剩餘 {ep['tp3_remain_pct']}% / {ep['tp3_remain_lots']} 張跌破出清)\n"
                f"• 總部位控管：建議買進 {s['suggested_lots']} 張 (動用 {s['capital_used']//10000} 萬 / 佔總資金 {s['capital_pct']}%，最大風險 {s['max_loss_twd']:,} 元)"
            )
    lines.append("\n🤖 明日 09:00 起盤中將每 30 秒自動偵測【買點觸發】與【停利停損】並即時通知。")
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
    scan_pool = dict(CORE_POOL)
    if chip_map:
        sorted_inst = sorted(chip_map.items(), key=lambda x: x[1]["total_inst"], reverse=True)
        for sid, info in sorted_inst[:40]:
            if sid not in scan_pool:
                scan_pool[sid] = info["name"]

    tickers = [f"{sid}.TW" for sid in scan_pool.keys()]
    raw_candidates = []
    try:
        data = yf.download(tickers, period="5mo", group_by="ticker", threads=True, progress=False)
        for sid, sname in scan_pool.items():
            t_sym = f"{sid}.TW"
            if t_sym not in data.columns.get_level_values(0):
                continue
            c_info = chip_map.get(sid, {"name": sname, "foreign_net": 0, "it_net": 0, "total_inst": 0})
            sigs = evaluate_candidate(data[t_sym], sid, sname, c_info)
            raw_candidates.extend(sigs)
    except Exception as e:
        print(f"⚠️ 批次下載異常: {e}")

    final_signals = []
    for sig in raw_candidates:
        b_info = check_finmind_broker(sig["stock_id"], sig["date"], sig.get("vol_lots", 1500))
        if b_info["pass_broker"]:
            sig["top_smart_broker"] = b_info["top_smart_broker"]
            final_signals.append(sig)

    active_list = update_active_positions(final_signals)
    report_payload = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "latest_trade_date": trade_date,
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
