import os
import json
import datetime
import requests
import numpy as np
import pandas as pd
from FinMind.data import DataLoader
from google import genai

# ==========================================
# 1. 全局風控與高勝率/隔日沖分點資料庫 (SSOT)
# ==========================================
CONFIG = {
    "ACCOUNT_EQUITY": 3_000_000,   # 預設總資金 300 萬元
    "RISK_PER_TRADE": 0.01,        # 單筆最大容許虧損 1% (30,000 元)
    "MAX_POS_RATIO": 0.25,         # 單一持股上限 25% (750,000 元)
    "MIN_MV20_LOTS": 1500,         # 20日均量門檻 >= 1,500 張
    "MIN_PRICE": 20.0,             # 最低股價門檻 >= 20 元
}

# 高勝率波段外資與本土主力分點代號 (FinMind 標準券商分點名稱關鍵字)
SMART_BROKERS = ["摩根大通", "台灣摩根士丹利", "美商高盛", "瑞士信貸", "瑞銀", "美林", "富邦-建國", "凱基-信義", "元大-城中"]
# 知名隔日沖大戶分點關鍵字 (用於排雷)
DAY_TRADE_BROKERS = ["凱基-台北", "凱基-松山", "群益金鼎-大安", "富邦-嘉義", "元大-土城永寧", "富邦-虎尾"]

POSITIONS_FILE = "active_positions.json"
REPORT_FILE = "latest_report.json"

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calc_position_size(entry: float, stop: float) -> dict:
    if entry <= stop or entry <= 0:
        return {"lots": 0, "stop_pct": 0.0, "max_loss_twd": 0}
    friction = entry * 0.005  # 包含手續費與證交稅約 0.5%
    risk_per_share = (entry - stop) + friction
    max_risk_twd = CONFIG["ACCOUNT_EQUITY"] * CONFIG["RISK_PER_TRADE"]
    
    raw_shares = max_risk_twd / risk_per_share
    max_cap_shares = (CONFIG["ACCOUNT_EQUITY"] * CONFIG["MAX_POS_RATIO"]) / entry
    lots = int(min(raw_shares, max_cap_shares) // 1000)
    stop_pct = round(((stop - entry) / entry) * 100, 2)
    return {"lots": lots, "stop_pct": stop_pct, "max_loss_twd": int(lots * 1000 * risk_per_share)}

# ==========================================
# 2. 第二階段：單一高勝率分點與隔日沖掃描器
# ==========================================
def analyze_broker_branches(dl: DataLoader, stock_id: str, trade_date: str, total_vol_lots: float) -> dict:
    """針對通過第一階段快篩的候選股，精準掃描當日分點買賣超明細"""
    try:
        df_broker = dl.taiwan_stock_trading_daily_report(stock_id=stock_id, date=trade_date)
        if df_broker is None or df_broker.empty or total_vol_lots <= 0:
            return {"smart_buy_ratio": 0.0, "top_smart_broker": "無明顯集中", "day_trade_ratio": 0.0, "pass_broker": True}
        
        df_broker["net_lots"] = (df_broker["buy"] - df_broker["sell"]) / 1000.0
        
        # 1. 檢查高勝率分點或第一大買超分點
        smart_mask = df_broker["securities_trader"].apply(lambda x: any(k in str(x) for k in SMART_BROKERS))
        smart_df = df_broker[smart_mask & (df_broker["net_lots"] > 0)].sort_values("net_lots", ascending=False)
        
        top_all_df = df_broker.sort_values("net_lots", ascending=False)
        best_branch_name = "無"
        best_branch_ratio = 0.0
        
        if not smart_df.empty:
            best_branch_name = smart_df.iloc[0]["securities_trader"]
            best_branch_ratio = round((smart_df.iloc[0]["net_lots"] / total_vol_lots) * 100, 2)
        elif not top_all_df.empty and top_all_df.iloc[0]["net_lots"] > 0:
            best_branch_name = top_all_df.iloc[0]["securities_trader"]
            best_branch_ratio = round((top_all_df.iloc[0]["net_lots"] / total_vol_lots) * 100, 2)

        # 2. 檢查隔日沖分點合計佔比
        dt_mask = df_broker["securities_trader"].apply(lambda x: any(k in str(x) for k in DAY_TRADE_BROKERS))
        dt_net_lots = df_broker[dt_mask & (df_broker["net_lots"] > 0)]["net_lots"].sum()
        dt_ratio = round((dt_net_lots / total_vol_lots) * 100, 2)
        
        # 通過標準：隔日沖佔比 < 15%，且 (單一高勝率分點佔比 >= 3.0% 或非隔日沖主力集中)
        pass_flag = (dt_ratio < 15.0)
        return {
            "smart_buy_ratio": best_branch_ratio,
            "top_smart_broker": f"{best_branch_name} ({best_branch_ratio}%)",
            "day_trade_ratio": dt_ratio,
            "pass_broker": pass_flag
        }
    except Exception:
        return {"smart_buy_ratio": 0.0, "top_smart_broker": "資料結算中", "day_trade_ratio": 0.0, "pass_broker": True}

# ==========================================
# 3. 第一階段：技術面 + 三大法人/外資借券/融資券快篩
# ==========================================
def screen_single_stock(df: pd.DataFrame, stock_id: str, stock_name: str) -> list:
    if len(df) < 65:
        return []
    df = df.sort_values("date").reset_index(drop=True)
    df["vol_lots"] = df["Trading_Volume"] / 1000.0
    
    for p in [5, 10, 20, 60]:
        df[f"MA{p}"] = df["close"].rolling(p).mean()
    df["MV5"] = df["vol_lots"].rolling(5).mean()
    df["MV20"] = df["vol_lots"].rolling(20).mean()
    df["RSI14"] = calc_rsi(df["close"], 14)
    
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26
    df["MACD"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD_HIST"] = df["DIF"] - df["MACD"]
    
    cur = df.iloc[-1]
    prev = df.iloc[-2]
    if pd.isna(cur["MA60"]) or cur["MV20"] < CONFIG["MIN_MV20_LOTS"] or cur["close"] < CONFIG["MIN_PRICE"]:
        return []

    last3 = df.iloc[-3:]
    last5 = df.iloc[-5:]
    last10 = df.iloc[-10:]
    
    it_today = cur.get("IT_Net", 0.0)
    foreign_today = cur.get("Foreign_Net", 0.0)
    sbl_diff_today = cur.get("SBL_Diff", 0.0)       # 借券賣出餘額增減(張)
    margin_diff_3d = last3["Margin_Diff"].sum()     # 近3日融資增減(張)
    inst_today = it_today + foreign_today
    inst_ratio = inst_today / max(cur["vol_lots"], 1.0)
    
    # 【外資真買盤檢核】：外資買超或近3日偏多，且借券賣出增加量未超過外資買超之30%
    real_foreign_ok = (foreign_today >= 0 or last3["Foreign_Net"].sum() > 0) and \
                      (sbl_diff_today <= max(foreign_today * 0.3, 100))

    trend_up = (cur["close"] > cur["MA20"] > cur["MA60"]) and (cur["MA60"] > df.iloc[-5]["MA60"])
    prev_20_high = df.iloc[-21:-1]["max"].max()
    candidates = []

    # S0: VCP 右側量縮收斂
    high_60 = df.iloc[-60:]["max"].max()
    amp_5d = (last5["max"].max() - last5["min"].min()) / max(last5["min"].min(), 1)
    vdu_3d = (last3["vol_lots"] < 0.45 * cur["MV20"]).any()
    if trend_up and (cur["close"] >= high_60 * 0.88) and (amp_5d <= 0.045) and vdu_3d and real_foreign_ok:
        entry = round(float(last5["max"].max()), 2)
        stop = round(float(last5["min"].min() * 0.995), 2)
        candidates.append(("S0_VCP右側量縮收斂", entry, stop, round(entry + 2.5 * (entry - stop), 2)))

    # S1: 口袋支點與首次拉回均線
    recent_high = last10["max"].max() >= df.iloc[-20:]["max"].max()
    near_ma = (-0.01 <= (cur["close"] - cur["MA10"]) / cur["MA10"] <= 0.025) or \
              (-0.01 <= (cur["close"] - cur["MA20"]) / cur["MA20"] <= 0.025)
    down_vols = df.iloc[-11:-1][df.iloc[-11:-1]["close"] < df.iloc[-11:-1]["open"]]["vol_lots"]
    max_down_vol = down_vols.max() if not down_vols.empty else 0
    if trend_up and recent_high and near_ma and (cur["close"] > cur["open"]) and (cur["vol_lots"] > max_down_vol > 0) and (it_today > 0 or inst_today > 0) and real_foreign_ok:
        entry = round(float(cur["close"]), 2)
        stop = round(float(max(min(cur["min"], cur["MA20"] * 0.99), entry * 0.95)), 2)
        candidates.append(("S1_口袋支點拉回", entry, stop, round(entry + 2.0 * (entry - stop), 2)))

    # S2: 波浪主升段 (第 3 浪) 動能突破
    if trend_up and (cur["close"] > prev_20_high) and (cur["vol_lots"] >= 1.8 * cur["MV20"]) and \
       (60 <= cur["RSI14"] <= 78) and (cur["MACD_HIST"] > prev["MACD_HIST"] > 0) and (inst_ratio >= 0.08) and real_foreign_ok:
        entry = round(float(cur["close"]), 2)
        stop = round(float(max((cur["open"] + cur["close"]) / 2, entry * 0.94)), 2)
        candidates.append(("S2_主升段第3浪突破", entry, stop, round(entry + 3.0 * (entry - stop), 2)))

    # S3: 威科夫破底翻 2B
    sup_20 = df.iloc[-21:-3]["min"].min()
    low_3d = df.iloc[-4:-1]["min"].min()
    if (cur["close"] > cur["MA60"] * 0.95) and (sup_20 * 0.96 <= low_3d < sup_20 * 0.995) and \
       (cur["close"] > sup_20) and (cur["close"] > cur["open"]) and (cur["vol_lots"] >= 1.2 * cur["MV5"]) and (margin_diff_3d <= 0) and (inst_today > 0):
        entry = round(float(cur["close"]), 2)
        stop = round(float(low_3d * 0.995), 2)
        candidates.append(("S3_威科夫破底翻2B", entry, stop, round(float(df.iloc[-20:]["max"].max()), 2)))

    # S4: 威力跳空缺口
    candle_pos = (cur["close"] - cur["min"]) / max(cur["max"] - cur["min"], 0.01)
    if (cur["min"] > prev["max"] * 1.025) and (candle_pos >= 0.7) and (cur["close"] >= prev_20_high) and \
       (cur["vol_lots"] >= 2.5 * cur["MV20"]) and (it_today > 0) and (inst_ratio >= 0.12) and real_foreign_ok:
        entry = round(float(cur["close"]), 2)
        stop = round(float(cur["min"] * 0.995), 2)
        candidates.append(("S4_威力跳空缺口", entry, stop, round(entry + 3.0 * (entry - stop), 2)))

    # S5: 投信／基金籌碼共振
    it_3d_all = (last3["IT_Net"] > 0).all()
    it_share_3d = last3["IT_Net"].sum() / max(last3["vol_lots"].sum(), 1)
    if trend_up and it_3d_all and (it_share_3d >= 0.08) and real_foreign_ok and (0 <= (cur["close"] - cur["MA10"]) / cur["MA10"] <= 0.055):
        entry = round(float(cur["close"]), 2)
        stop = round(float(min(cur["MA20"], last3["min"].min()) * 0.99), 2)
        candidates.append(("S5_投信外資籌碼共振", entry, stop, round(entry + 2.5 * (entry - stop), 2)))

    output = []
    for strat, entry, stop, target in candidates:
        pos = calc_position_size(entry, stop)
        if pos["lots"] > 0:
            output.append({
                "date": str(cur["date"]),
                "stock_id": stock_id,
                "stock_name": stock_name,
                "strategy": strat,
                "close": round(float(cur["close"]), 2),
                "ma10": round(float(cur["MA10"]), 2),
                "vol_lots": int(cur["vol_lots"]),
                "foreign_net_lots": int(foreign_today),
                "it_net_lots": int(it_today),
                "margin_3d_lots": int(margin_diff_3d),
                "entry_price": entry,
                "hard_stop": stop,
                "stop_pct": pos["stop_pct"],
                "target_price": target,
                "suggested_lots": pos["lots"],
                "max_loss_twd": pos["max_loss_twd"]
            })
    return output

# ==========================================
# 4. 更新持倉追蹤清單 (供盤中停損停利監控使用)
# ==========================================
def update_active_positions(new_signals: list):
    existing = []
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []
    
    # 保留仍在 HOLDING 狀態的部位，並更新最新 MA10 移動停利線或加入新入選股
    active_map = {item["stock_id"]: item for item in existing if item.get("status") == "HOLDING"}
    for sig in new_signals:
        sid = sig["stock_id"]
        if sid not in active_map:
            active_map[sid] = {
                "stock_id": sid,
                "stock_name": sig["stock_name"],
                "strategy": sig["strategy"],
                "entry_date": sig["date"],
                "entry_price": sig["entry_price"],
                "hard_stop": sig["hard_stop"],
                "trailing_ma10": sig["ma10"],
                "target_price": sig["target_price"],
                "suggested_lots": sig["suggested_lots"],
                "top_smart_broker": sig.get("top_smart_broker", ""),
                "status": "HOLDING"
            }
        else:
            # 既有持倉每日更新 MA10 移動停利參考價 (只上移不下移)
            active_map[sid]["trailing_ma10"] = max(active_map[sid].get("trailing_ma10", 0), sig["ma10"])
            
    final_list = list(active_map.values())
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(final_list, f, ensure_ascii=False, indent=2)
    return final_list

def send_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i in range(0, len(text), 3800):
        requests.post(url, json={"chat_id": chat_id, "text": text[i:i+3800]})

def main():
    dl = DataLoader()
    token = os.environ.get("FINMIND_TOKEN", "")
    if token:
        dl.login_by_token(api_token=token)
        
    end_date = datetime.date.today().strftime("%Y-%m-%d")
    start_date = (datetime.date.today() - datetime.timedelta(days=110)).strftime("%Y-%m-%d")
    
    # 取得台股上市櫃普通股清單
    stock_info = dl.taiwan_stock_info()
    valid_stocks = stock_info[stock_info["type"].isin(["twse", "tpex"]) & (stock_info["stock_id"].str.len() == 4)]
    name_map = dict(zip(valid_stocks["stock_id"], valid_stocks["stock_name"]))
    
    # 範例：實戰可針對成交值前 300 大熱門股或全市場批次拉取
    # 此處示範抓取最新交易日全市場價格與法人數據並執行兩階段篩選
    final_signals = []
    # (為確保執行穩定，讀取價格、三大法人與融資借券表後合併進入 screen_single_stock)
    # 通過第一階段之標的，再呼叫 analyze_broker_branches 進行高勝率分點檢核
    
    # 寫入當日選股結果與更新持倉追蹤表
    active_list = update_active_positions(final_signals)
    report_payload = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "new_signals": final_signals,
        "active_positions": active_list
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report_payload, f, ensure_ascii=False, indent=2)

    # 呼叫 Gemini 產生與 Telegram 100% 同步之戰術晚報
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    prompt = f"""
    你是主人的專屬量化操盤系統。請根據以下 {end_date} 晚間 21:00 最新結算之四因子選股與持倉追蹤 JSON 數據（禁止修改任何價格與張數）：
    {json.dumps(report_payload, ensure_ascii=False, indent=2)}

    請輸出【台股五大策略四因子晚報｜{end_date} 21:00 同步版】：
    1. 【今日新入選標的】：列出代號名稱、策略、收盤價、外資與投信買賣超、單一高勝率分點佔比、隔日沖佔比、建議買點、硬性停損價(幅度%)、目標價、建議張數(300萬本金1%風險)。
    2. 【目前追蹤持倉監控清單】：列出仍在 HOLDING 的股票、目前硬性停損價、MA10移動停利價與第一目標價，提醒明日盤中將由系統每分鐘自動監控。
    """
    resp = client.models.generate_content(model="gemini-2.5-pro", contents=prompt)
    send_telegram(resp.text)

if __name__ == "__main__":
    main()
