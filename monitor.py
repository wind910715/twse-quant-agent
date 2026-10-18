import os
import time
import json
import datetime
import requests
import yfinance as yf
from google import genai

POSITIONS_FILE = "active_positions.json"

def get_realtime_price(stock_id: str) -> float:
    """優先從證交所 MIS 抓取即時成交價，若失敗則自動切換 Yahoo Finance 即時報價"""
    for prefix in ["tse", "otc"]:
        try:
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={prefix}_{stock_id}.tw"
            r = requests.get(url, timeout=4).json()
            if r.get("msgArray"):
                z = r["msgArray"][0].get("z", "-")
                if z != "-" and float(z) > 0:
                    return float(z)
        except Exception:
            pass
    # 備援：Yahoo Finance 即時報價
    for suffix in [".TW", ".TWO"]:
        try:
            ticker = yf.Ticker(f"{stock_id}{suffix}")
            price = ticker.fast_info.get("lastPrice")
            if price and price > 0:
                return round(float(price), 2)
        except Exception:
            pass
    return -1.0

def send_urgent_alert(alert_data: dict):
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    prompt = f"""
    主人注意！盤中即時風控系統偵測到持倉標的觸發【{alert_data['trigger_type']}】條件！
    數據明細：{json.dumps(alert_data, ensure_ascii=False)}
    請用最精煉、醒目的格式（3行內）發出盤中緊急執行通知：包含股票代號名稱、當前觸發價、原設定防守/目標價、建議立即執行動作（如：市價全數停損出場 / 獲利達標減碼50%鎖利）。
    """
    resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"🚨【盤中即時停損/停利警報】🚨\n{resp.text}"}
        )

def check_positions_once() -> bool:
    if not os.path.exists(POSITIONS_FILE):
        return False
    with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
        positions = json.load(f)
    
    modified = False
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for pos in positions:
        if pos.get("status") != "HOLDING":
            continue
        sid = pos["stock_id"]
        live_price = get_realtime_price(sid)
        if live_price <= 0:
            continue
            
        # 1. 檢查是否跌破硬性停損 (Hard Stop)
        if live_price <= pos["hard_stop"]:
            pos["status"] = "STOPPED_OUT"
            pos["exit_price"] = live_price
            pos["exit_time"] = now_str
            modified = True
            send_urgent_alert({
                "trigger_type": "⛔ 硬性停損觸發 (Hard Stop)",
                "stock": f"{sid} {pos['stock_name']}",
                "strategy": pos["strategy"],
                "live_price": live_price,
                "hard_stop": pos["hard_stop"],
                "lots": pos["suggested_lots"]
            })
        # 2. 檢查是否達到第一停利目標 (Target Price)
        elif live_price >= pos["target_price"] and not pos.get("tp1_alerted", False):
            pos["tp1_alerted"] = True
            # 達標後自動將硬性停損上移至成本價 (保本停損 Breakeven Stop)
            pos["hard_stop"] = max(pos["hard_stop"], pos["entry_price"])
            modified = True
            send_urgent_alert({
                "trigger_type": "🎯 第一停利目標達標 (Target Reached)",
                "stock": f"{sid} {pos['stock_name']}",
                "strategy": pos["strategy"],
                "live_price": live_price,
                "target_price": pos["target_price"],
                "new_breakeven_stop": pos["hard_stop"]
            })
        # 3. 若已達第一停利，檢查是否跌破 10MA 移動停利線
        elif pos.get("tp1_alerted", False) and live_price < pos.get("trailing_ma10", 0):
            pos["status"] = "TRAILING_PROFIT_EXIT"
            pos["exit_price"] = live_price
            pos["exit_time"] = now_str
            modified = True
            send_urgent_alert({
                "trigger_type": "💰 跌破10MA移動停利出場",
                "stock": f"{sid} {pos['stock_name']}",
                "live_price": live_price,
                "trailing_ma10": pos["trailing_ma10"]
            })

    if modified:
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
    return modified

if __name__ == "__main__":
    # 在單次 5 分鐘排程內，每 60 秒巡邏一次（共巡邏 4 次），實現 1 分鐘級別即時警報
    for i in range(4):
        check_positions_once()
        if i < 3:
            time.sleep(55)
