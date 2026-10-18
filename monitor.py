import os
import time
import json
import datetime
import requests
import yfinance as yf

POSITIONS_FILE = "active_positions.json"

def get_realtime_price(stock_id: str) -> float:
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
    msg = (
        f"🚨【盤中即時停損/停利警報】🚨\n"
        f"• 觸發訊號：{alert_data['trigger_type']}\n"
        f"• 股票標的：{alert_data['stock']} ({alert_data.get('strategy', '')})\n"
        f"• 當前成交價：{alert_data['live_price']} 元\n"
        f"• 設定觸發價：{alert_data['ref_price']} 元\n"
        f"• 建議行動：{alert_data['action']}"
    )
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg},
                timeout=8
            )
        except Exception as e:
            print(f"Telegram 警報發送異常: {e}")

def check_positions_once() -> bool:
    if not os.path.exists(POSITIONS_FILE):
        return False
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            positions = json.load(f)
    except Exception:
        return False

    modified = False
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for pos in positions:
        if pos.get("status") != "HOLDING":
            continue
        sid = pos["stock_id"]
        live_price = get_realtime_price(sid)
        if live_price <= 0:
            continue

        if live_price <= pos["hard_stop"]:
            pos["status"] = "STOPPED_OUT"
            pos["exit_price"] = live_price
            pos["exit_time"] = now_str
            modified = True
            send_urgent_alert({
                "trigger_type": "⛔ 硬性停損觸發 (Hard Stop)",
                "stock": f"{sid} {pos['stock_name']}",
                "strategy": pos.get("strategy", ""),
                "live_price": live_price,
                "ref_price": pos["hard_stop"],
                "action": f"請立即於券商 App 市價停損全數出場 ({pos.get('suggested_lots', 1)} 張)"
            })
        elif live_price >= pos["target_price"] and not pos.get("tp1_alerted", False):
            pos["tp1_alerted"] = True
            pos["hard_stop"] = max(pos["hard_stop"], pos["entry_price"])
            modified = True
            send_urgent_alert({
                "trigger_type": "🎯 第一停利目標達標 (Target Reached)",
                "stock": f"{sid} {pos['stock_name']}",
                "strategy": pos.get("strategy", ""),
                "live_price": live_price,
                "ref_price": pos["target_price"],
                "action": f"建議先減碼 50% 鎖住獲利，剩餘部位保本停損已自動上移至 {pos['hard_stop']} 元"
            })
        elif pos.get("tp1_alerted", False) and live_price < pos.get("trailing_ma10", 0):
            pos["status"] = "TRAILING_PROFIT_EXIT"
            pos["exit_price"] = live_price
            pos["exit_time"] = now_str
            modified = True
            send_urgent_alert({
                "trigger_type": "💰 跌破 10MA 移動停利出場",
                "stock": f"{sid} {pos['stock_name']}",
                "strategy": pos.get("strategy", ""),
                "live_price": live_price,
                "ref_price": pos["trailing_ma10"],
                "action": "波段尾倉跌破 10 日均線，建議全數獲利了結"
            })

    if modified:
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
    return modified

if __name__ == "__main__":
    for i in range(4):
        check_positions_once()
        if i < 3:
            time.sleep(55)
