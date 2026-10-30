import os
import json
import datetime
import requests
import numpy as np
import pandas as pd
import yfinance as yf

# v2: 策略邏輯/指標/部位/出場計畫全部來自 strategy_core (與 backtest 共用同一份, SSOT)
from strategy_core import (
    CONFIG, STRATEGY_NAMES, calc_rsi, calc_up_down_vol_ratio, calc_volume_poc_and_max_bar,
    calc_cdp_levels, calc_position_size, calc_exit_plan as calc_pct_trailing_exit_plan,
    prepare_indicators, chip_is_positive, market_regime_ok, evaluate_candidate,
)

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

# (calc_* / evaluate_candidate 已移至 strategy_core.py, 此處不再重複實作)

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
                        chip_map[sid] = {"name": name, "foreign_net": foreign_net, "it_net": it_net, "total_inst": total_inst, "has_data": True}
                break
        except Exception:
            continue
    return trade_date_str, chip_map

def update_active_positions(new_signals: list) -> list:
    """
    【每日晚間 21:00 鎖定明日監控名單】：
    1. 清除舊版殘留未經過盤中買進確認 (entered_intraday != True) 的幽靈持倉，根除「沒通知買進卻通知停損」之問題！
    2. 清除前一日未觸發進場的過期 WATCHING 名單，只鎖定今晚最新選出的 WATCHING 標的與真正已進場的 HOLDING 標的。
    """
    existing = []
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    # 只保留真正由盤中觸發買進 (entered_intraday == True) 且仍在 HOLDING 的真實持倉
    active_map = {
        item["stock_id"]: item
        for item in existing
        if item.get("status") == "HOLDING" and item.get("entered_intraday", False) is True
    }

    for sig in new_signals:
        sid = sig["stock_id"]
        if sid in active_map:
            continue
        ep = sig["exit_plan"]
        ind = sig["indicators_backend"]
        active_map[sid] = {
            "stock_id": sid,
            "stock_name": sig["stock_name"],
            "ex_prefix": sig.get("ex_prefix", "tse"),
            "strategy": sig["strategy"],
            "signal_date": sig["date"],
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
            "tp3_reduce_lots": ep.get("tp3_reduce_lots", 0),
            "tp3_reduce_pct": ep.get("tp3_reduce_pct", 0),
            "tail_lots": ep.get("tail_lots", 0),
            "tp3_remain_pct": ep["tp3_remain_pct"],
            "tp3_remain_lots": ep["tp3_remain_lots"],
            "tp3_trail_drawdown": ep["tp3_trail_drawdown"],
            "poc_price": ind["poc_price"],
            "max_vol_low": ind["max_vol_low"],
            "cdp_al": ind["cdp_al"],
            "ma5": ind["ma5"],
            "ma10": ind["ma10"],
            "ma20": ind["ma20"],
            "suggested_lots": sig["suggested_lots"],
            "remaining_lots": sig["suggested_lots"],
            "capital_used": sig["capital_used"],
            "capital_pct": sig["capital_pct"],
            "max_loss_twd": sig["max_loss_twd"],
            "entered_intraday": False,
            "entry_alerted": False,
            "invalidated_alerted": False,
            "be_locked": False,
            "tp1_alerted": False,
            "tp2_alerted": False,
            "tp3_alerted": False,
            "status": "WATCHING"
        }

    final_list = list(active_map.values())
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(final_list, f, ensure_ascii=False, indent=2)
    return final_list

def build_simplified_tg_report(payload: dict, trade_date: str) -> str:
    lines = [
        f"📌【台股五大指數成分股｜21:00 選股鎖定戰報｜{trade_date}】",
        f"• 範圍：台灣50、中型100、富櫃50、富櫃200、MSCI ({payload.get('universe_quarter', '')})"
    ]
    sigs = payload.get("new_signals", [])
    if not sigs:
        lines.append("\n🛡️ 今晚無符合高勝率條件之新鎖定標的，明日盤中將持續監控既有持倉。")
    else:
        lines.append(f"🔒 已鎖定以下 {len(sigs)} 檔標的，明日 09:00 開盤起與【台北證交所/櫃買中心】即時連線監控：")
        for s in sigs[:8]:
            ep = s["exit_plan"]
            lines.append(
                f"\n🎯 {s['stock_id']} {s['stock_name']}｜對應策略：{s['strategy']}\n"
                f"• 盤中進場觸發價：突破 {s['entry_price']} 元 (追價上限 {ep['entry_high']} 元，開低破位不買)\n"
                f"• 初始停損點：{s['hard_stop']} 元 ({s['stop_pct']}%｜3日不漲破5MA主動汰弱)\n"
                f"• 分批停利與目標價：\n"
                f"  - 停利一 (+6.0%)：{ep['tp1_price']} 元 (減碼 {ep['tp1_reduce_pct']}% / 賣 {ep['tp1_reduce_lots']} 張，底線鎖 +1.5%)\n"
                f"  - 停利二 (+14.0%)：{ep['tp2_price']} 元 (減碼 {ep['tp2_reduce_pct']}% / 賣 {ep['tp2_reduce_lots']} 張，底線鎖 +8.0%)\n"
                f"  - 目標價 (+25.0%)：{ep['tp3_price']} 元 (尾倉 {ep['tp3_remain_pct']}% / {ep['tp3_remain_lots']} 張以最高收盤回落6%抱到底)\n"
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

    tickers = ["0050.TW", "^TWII"] + [f"{sid}{sfx}" for sid, (_, sfx) in universe.items()]
    raw_candidates = []
    bench_ret_20d = 0.0

    try:
        # v3.7: 期間改抓 1 年，C21 需 100 日最高價＋間隔回溯（5 個月在邊界上太緊）；
        # S0/S1/S2 只看尾段視窗，拉長歷史不改變其訊號。
        data = yf.download(tickers, period="1y", group_by="ticker", threads=True, progress=False)
        bench_close, bench_ma20 = None, None
        if "0050.TW" in data.columns.get_level_values(0):
            b_df = data["0050.TW"].dropna(subset=["Close"])
            if len(b_df) > 21:
                bench_ret_20d = (float(b_df["Close"].iloc[-1]) - float(b_df["Close"].iloc[-21])) / float(b_df["Close"].iloc[-21])
                bench_close = float(b_df["Close"].iloc[-1])
                bench_ma20 = float(b_df["Close"].rolling(20).mean().iloc[-1])
        # v3.7: 加權收盤序列供 C02 用；抓不到時 C02 自動跳過，不影響 S0/S1/S2/C21。
        twii_series = None
        if "^TWII" in data.columns.get_level_values(0):
            twii_series = data["^TWII"]["Close"].dropna()
            if twii_series.empty:
                twii_series = None

        for sid, (sname, sfx) in universe.items():
            t_sym = f"{sid}{sfx}"
            if t_sym not in data.columns.get_level_values(0):
                continue
            # v2: 無法人資料 (如上櫃 T86 查不到) 明確標記 has_data=False, 不再自動通過濾網
            c_info = chip_map.get(sid, {"name": sname, "foreign_net": 0, "it_net": 0, "total_inst": 0, "has_data": False})
            # v2: 大盤濾網進實盤, 與 backtest 共用 market_regime_ok()
            m_ok = True
            s_df = data[t_sym].dropna(subset=["Close"])
            if bench_close is not None and len(s_df) > 21:
                s_ret20 = (float(s_df["Close"].iloc[-1]) - float(s_df["Close"].iloc[-21])) / float(s_df["Close"].iloc[-21])
                m_ok = market_regime_ok(bench_close, bench_ma20, s_ret20)
            sigs = evaluate_candidate(data[t_sym], sid, sname, sfx, c_info, bench_ret_20d, market_ok=m_ok, bench_series=twii_series)
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
