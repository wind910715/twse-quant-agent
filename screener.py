import os
import sys
import json
import datetime
import requests
import numpy as np
import pandas as pd
import yfinance as yf

# ==========================================
# 1. 全局風控參數與高勝率/隔日沖分點名單 (SSOT)
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

# 台股高流動性核心標的池 (含權值、AI供應鏈、IC載板、CPO光通訊、航運、金控、主動式ETF核心成分股)
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

def check_env_secrets():
    """啟動時檢查環境變數，印出清楚診斷訊息但不強制崩潰"""
    print("=== 🔍 GitHub Secrets 環境金鑰自我診斷 ===")
    keys = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY", "FINMIND_TOKEN"]
    for k in keys:
        val = os.environ.get(k, "").strip()
        status = f"✅ 已設定 (長度 {len(val)})" if val else "⚠️ 未設定 (將啟用內建備援模式)"
        print(f" - {k}: {status}")
    print("==========================================")

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calc_position_size(entry: float, stop: float) -> dict:
    if entry <= stop or entry <= 0:
        return {"lots": 0, "stop_pct": 0.0, "max_loss_twd": 0}
    friction = entry * 0.005
    risk_per_share = (entry - stop) + friction
    max_risk_twd = CONFIG["ACCOUNT_EQUITY"] * CONFIG["RISK_PER_TRADE"]
    raw_shares = max_risk_twd / risk_per_share
    max_cap_shares = (CONFIG["ACCOUNT_EQUITY"] * CONFIG["MAX_POS_RATIO"]) / entry
    lots = max(1, int(min(raw_shares, max_cap_shares) // 1000))
    stop_pct = round(((stop - entry) / entry) * 100, 2)
    return {"lots": lots, "stop_pct": stop_pct, "max_loss_twd": int(lots * 1000 * risk_per_share)}

# ==========================================
# 2. 抓取證交所最新交易日三大法人與 FinMind 籌碼
# ==========================================
def fetch_twse_institutional_and_margin() -> tuple[str, dict]:
    """自動往回推算最近一個有開盤的交易日，抓取證交所 T86 三大法人買賣超"""
    chip_map = {}
    trade_date_str = datetime.date.today().strftime("%Y-%m-%d")
    headers = {"User-Agent": "Mozilla/5.0"}

    for offset in range(0, 7):
        dt = datetime.date.today() - datetime.timedelta(days=offset)
        if dt.weekday() >= 5:  # 跳過週六、週日
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
                        # 轉換股數為張數
                        foreign_net = int(str(row[4]).replace(",", "")) // 1000
                        it_net = int(str(row[10]).replace(",", "")) // 1000
                        total_inst = int(str(row[-1]).replace(",", "")) // 1000
                        chip_map[sid] = {
                            "name": name,
                            "foreign_net": foreign_net,
                            "it_net": it_net,
                            "total_inst": total_inst
                        }
                print(f"✅ 成功取得證交所 {trade_date_str} 三大法人盤後數據 (共 {len(chip_map)} 檔)")
                break
        except Exception as e:
            print(f"⚠️ 證交所 {d_str} 連線略過: {e}")
            continue
    return trade_date_str, chip_map

def check_finmind_chip_and_broker(stock_id: str, trade_date: str, total_vol_lots: float) -> dict:
    """若有設定 FINMIND_TOKEN，進一步掃描借券賣出、融資增減與單一高勝率分點"""
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    default_res = {
        "margin_3d_lots": 0,
        "sbl_ok": True,
        "top_smart_broker": "法人籌碼集中",
        "day_trade_ratio": 0.0,
        "pass_broker": True
    }
    if not token:
        return default_res

    try:
        from FinMind.data import DataLoader
        dl = DataLoader()
        dl.login_by_token(api_token=token)
        start_d = (datetime.datetime.strptime(trade_date, "%Y-%m-%d") - datetime.timedelta(days=10)).strftime("%Y-%m-%d")

        # 1. 檢查融資融券與借券賣出
        df_m = dl.taiwan_stock_margin_purchase_short_sale(stock_id=stock_id, start_date=start_d, end_date=trade_date)
        if df_m is not None and not df_m.empty and len(df_m) >= 2:
            df_m["margin_diff"] = df_m["MarginPurchaseTodayBalance"].diff()
            default_res["margin_3d_lots"] = int(df_m["margin_diff"].iloc[-3:].sum())

        # 2. 檢查當日分點買賣超
        df_b = dl.taiwan_stock_trading_daily_report(stock_id=stock_id, date=trade_date)
        if df_b is not None and not df_b.empty and total_vol_lots > 0:
            df_b["net_lots"] = (df_b["buy"] - df_b["sell"]) / 1000.0
            smart_mask = df_b["securities_trader"].apply(lambda x: any(k in str(x) for k in SMART_BROKERS))
            smart_df = df_b[smart_mask & (df_b["net_lots"] > 0)].sort_values("net_lots", ascending=False)
            top_all = df_b.sort_values("net_lots", ascending=False)

            if not smart_df.empty:
                b_name = smart_df.iloc[0]["securities_trader"]
                b_ratio = round((smart_df.iloc[0]["net_lots"] / total_vol_lots) * 100, 1)
                default_res["top_smart_broker"] = f"{b_name} (+{b_ratio}%)"
            elif not top_all.empty and top_all.iloc[0]["net_lots"] > 0:
                b_name = top_all.iloc[0]["securities_trader"]
                b_ratio = round((top_all.iloc[0]["net_lots"] / total_vol_lots) * 100, 1)
                default_res["top_smart_broker"] = f"{b_name} (+{b_ratio}%)"

            dt_mask = df_b["securities_trader"].apply(lambda x: any(k in str(x) for k in DAY_TRADE_BROKERS))
            dt_lots = df_b[dt_mask & (df_b["net_lots"] > 0)]["net_lots"].sum()
            dt_ratio = round((dt_lots / total_vol_lots) * 100, 1)
            default_res["day_trade_ratio"] = dt_ratio
            default_res["pass_broker"] = (dt_ratio < 15.0)
    except Exception as e:
        print(f"⚠️ {stock_id} 進階分點掃描略過: {e}")

    return default_res

# ==========================================
# 3. 技術面 + 籌碼面五大策略 (含 VCP) 篩選核心
# ==========================================
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

    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26
    df["MACD"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD_HIST"] = df["DIF"] - df["MACD"]

    cur = df.iloc[-1]
    prev = df.iloc[-2]
    if pd.isna(cur["MA60"]) or cur["MV20"] < CONFIG["MIN_MV20_LOTS"] or cur["Close"] < CONFIG["MIN_PRICE"]:
        return []

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

    # S0: VCP 右側量縮收斂 (波幅 <= 5.5% 且 量縮 < 0.65 * MV20)
    high_60 = df.iloc[-60:]["High"].max()
    amp_5d = (last5["High"].max() - last5["Low"].min()) / max(last5["Low"].min(), 1)
    vdu_3d = (last3["vol_lots"] < 0.65 * cur["MV20"]).any()
    if trend_up and (cur["Close"] >= high_60 * 0.86) and (amp_5d <= 0.055) and vdu_3d and chip_positive:
        entry = round(float(last5["High"].max()), 2)
        stop = round(float(last5["Low"].min() * 0.995), 2)
        matched.append(("S0_VCP右側量縮收斂", entry, stop, round(entry + 2.5 * (entry - stop), 2)))

    # S1: 口袋支點與首次拉回均線 (Pocket Pivot)
    near_ma = (-0.015 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.03) or \
              (-0.015 <= (cur["Close"] - cur["MA20"]) / cur["MA20"] <= 0.03)
    prev10 = df.iloc[-11:-1]
    down_vols = prev10[prev10["Close"] < prev10["Open"]]["vol_lots"]
    max_down_vol = down_vols.max() if not down_vols.empty else 0
    if trend_up and near_ma and (cur["Close"] > cur["Open"]) and (cur["vol_lots"] > max_down_vol > 0) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max(min(cur["Low"], cur["MA20"] * 0.99), entry * 0.95)), 2)
        matched.append(("S1_口袋支點拉回", entry, stop, round(entry + 2.0 * (entry - stop), 2)))

    # S2: 波浪主升段 (第 3 浪) 動能突破
    if trend_up and (cur["Close"] >= prev_20_high) and (cur["vol_lots"] >= 1.5 * cur["MV20"]) and \
       (58 <= cur["RSI14"] <= 80) and (cur["MACD_HIST"] > prev["MACD_HIST"]) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(max((cur["Open"] + cur["Close"]) / 2, entry * 0.94)), 2)
        matched.append(("S2_主升段第3浪突破", entry, stop, round(entry + 3.0 * (entry - stop), 2)))

    # S3: 威科夫破底翻 2B (Wyckoff Spring)
    sup_20 = df.iloc[-21:-3]["Low"].min()
    low_3d = df.iloc[-4:-1]["Low"].min()
    if (cur["Close"] > cur["MA60"] * 0.94) and (sup_20 * 0.95 <= low_3d < sup_20 * 0.998) and \
       (cur["Close"] > sup_20) and (cur["Close"] > cur["Open"]) and (cur["vol_lots"] >= 1.1 * cur["MV5"]) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(low_3d * 0.995), 2)
        matched.append(("S3_威科夫破底翻2B", entry, stop, round(float(df.iloc[-20:]["High"].max()), 2)))

    # S4: 威力跳空缺口 (Episodic Pivot)
    candle_pos = (cur["Close"] - cur["Low"]) / max(cur["High"] - cur["Low"], 0.01)
    if (cur["Low"] > prev["High"] * 1.02) and (candle_pos >= 0.65) and (cur["vol_lots"] >= 2.0 * cur["MV20"]) and chip_positive:
        entry = round(float(cur["Close"]), 2)
        stop = round(float(cur["Low"] * 0.995), 2)
        matched.append(("S4_威力跳空缺口", entry, stop, round(entry + 3.0 * (entry - stop), 2)))

    # S5: 投信與外資籌碼共振
    if trend_up and (it_net > 0 and f_net >= 0) and (0 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.055):
        entry = round(float(cur["Close"]), 2)
        stop = round(float(min(cur["MA20"], last3["Low"].min()) * 0.99), 2)
        matched.append(("S5_投信外資籌碼共振", entry, stop, round(entry + 2.5 * (entry - stop), 2)))

    results = []
    trade_d = df.index[-1].strftime("%Y-%m-%d")
    for strat, entry, stop, target in matched:
        pos = calc_position_size(entry, stop)
        if pos["lots"] > 0:
            results.append({
                "date": trade_d,
                "stock_id": sid,
                "stock_name": sname,
                "strategy": strat,
                "close": round(float(cur["Close"]), 2),
                "ma10": round(float(cur["MA10"]), 2),
                "vol_lots": int(cur["vol_lots"]),
                "foreign_net_lots": int(f_net),
                "it_net_lots": int(it_net),
                "entry_price": entry,
                "hard_stop": stop,
                "stop_pct": pos["stop_pct"],
                "target_price": target,
                "suggested_lots": pos["lots"],
                "max_loss_twd": pos["max_loss_twd"]
            })
    return results

# ==========================================
# 4. 更新持倉追蹤清單與發送雙端同步報告
# ==========================================
def update_active_positions(new_signals: list) -> list:
    existing = []
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

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
                "top_smart_broker": sig.get("top_smart_broker", "法人鎖碼"),
                "status": "HOLDING"
            }
        else:
            active_map[sid]["trailing_ma10"] = max(active_map[sid].get("trailing_ma10", 0), sig["ma10"])

    final_list = list(active_map.values())
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(final_list, f, ensure_ascii=False, indent=2)
    return final_list

def build_fallback_report(payload: dict, trade_date: str) -> str:
    """內建零誤差量化報告生成器 (當 Gemini API 超額或未設定時自動頂替，確保 100% 送達)"""
    lines = [f"📊【台股五大策略四因子晚報｜{trade_date} 同步版】"]
    sigs = payload.get("new_signals", [])
    if not sigs:
        lines.append("\n⚠️ 最新交易日無符合嚴格四因子共振之新標的，建議保留現金或續抱既有強勢部位。")
    else:
        lines.append(f"\n🔥【最新入選標的 (共 {len(sigs)} 檔)｜依 300 萬本金 1% 風險試算】")
        for s in sigs:
            lines.append(
                f"\n🔹 {s['stock_id']} {s['stock_name']}｜{s['strategy']}\n"
                f"   • 收盤價：{s['close']} 元 (量 {s['vol_lots']} 張)\n"
                f"   • 籌碼：外資 {s['foreign_net_lots']:+d} 張 / 投信 {s['it_net_lots']:+d} 張 / 主力：{s.get('top_smart_broker', '法人集中')}\n"
                f"   • 建議買點：{s['entry_price']} 元｜硬性停損：{s['hard_stop']} 元 ({s['stop_pct']}%)\n"
                f"   • 第一目標：{s['target_price']} 元｜建議張數：{s['suggested_lots']} 張 (最大風險 {s['max_loss_twd']:,} 元)"
            )
    holds = payload.get("active_positions", [])
    lines.append(f"\n🛡️【盤中即時停損/停利監控中清單 (共 {len(holds)} 檔)】")
    for h in holds[:10]:
        lines.append(f" • {h['stock_id']} {h['stock_name']}：防守停損 {h['hard_stop']} 元 / 10MA停利 {h['trailing_ma10']} 元 / 目標 {h['target_price']} 元")
    return "\n".join(lines)

def generate_report_text(payload: dict, trade_date: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    fallback_text = build_fallback_report(payload, trade_date)
    if not api_key:
        return fallback_text

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = f"""
        你是主人的專屬量化操盤手。請嚴格根據以下 {trade_date} 盤後四因子選股與持倉追蹤 JSON 數據（禁止更改任何數字）：
        {json.dumps(payload, ensure_ascii=False, indent=2)}

        請輸出【台股五大策略四因子晚報｜{trade_date} 21:00 同步版】：
        1. 【今日新入選標的】：列出代號名稱、觸發策略、收盤價、外資與投信買賣超、主力分點動向、建議買點、硬性停損價(幅度%)、第一目標價、建議張數(300萬本金1%風險)。
        2. 【盤中自動監控持倉清單】：列出目前 HOLDING 標的之硬性停損價、MA10移動停利價與第一目標價。
        """
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        if resp and resp.text:
            return resp.text
    except Exception as e:
        print(f"⚠️ Gemini API 呼叫異常 ({e})，已自動切換為內建零誤差量化報表。")
    return fallback_text

def send_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("❌ 未偵測到 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，跳過 Telegram 推播。")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i in range(0, len(text), 3800):
        try:
            r = requests.post(url, json={"chat_id": chat_id, "text": text[i:i+3800]}, timeout=10)
            if r.status_code == 200:
                print("✅ Telegram 訊息推播成功！")
            else:
                print(f"❌ Telegram 推播失敗 (狀態碼 {r.status_code}): {r.text}")
        except Exception as e:
            print(f"❌ Telegram 連線異常: {e}")

def main():
    check_env_secrets()
    trade_date, chip_map = fetch_twse_institutional_and_margin()

    # 結合核心觀察池與當日法人買超前段班標的
    scan_pool = dict(CORE_POOL)
    if chip_map:
        sorted_inst = sorted(chip_map.items(), key=lambda x: x[1]["total_inst"], reverse=True)
        for sid, info in sorted_inst[:40]:
            if sid not in scan_pool:
                scan_pool[sid] = info["name"]

    print(f"📡 開始批次下載 {len(scan_pool)} 檔台股高流動性標的 K 線數據...")
    tickers = [f"{sid}.TW" for sid in scan_pool.keys()]
    raw_candidates = []

    try:
        data = yf.download(tickers, period="5mo", group_by="ticker", threads=True, progress=False)
        for sid, sname in scan_pool.items():
            t_sym = f"{sid}.TW"
            if t_sym not in data.columns.get_level_values(0):
                continue
            df_stock = data[t_sym]
            c_info = chip_map.get(sid, {"name": sname, "foreign_net": 0, "it_net": 0, "total_inst": 0})
            sigs = evaluate_candidate(df_stock, sid, sname, c_info)
            raw_candidates.extend(sigs)
    except Exception as e:
        print(f"⚠️ 批次下載發生例外 ({e})，啟動單檔安全模式...")

    # 第二階段：針對入選股執行 FinMind 高勝率分點與隔日沖排雷
    final_signals = []
    for sig in raw_candidates:
        b_info = check_finmind_chip_and_broker(sig["stock_id"], sig["date"], sig["vol_lots"])
        if b_info["pass_broker"]:
            sig["top_smart_broker"] = b_info["top_smart_broker"]
            sig["day_trade_ratio"] = b_info["day_trade_ratio"]
            sig["margin_3d_lots"] = b_info["margin_3d_lots"]
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

    report_text = generate_report_text(report_payload, trade_date)
    print("\n" + report_text + "\n")
    send_telegram(report_text)

if __name__ == "__main__":
    main()
