import os
import time
import json
import datetime
import requests
import yfinance as yf

POSITIONS_FILE = "active_positions.json"

def get_realtime_quote(stock_id: str) -> dict:
    for prefix in ["tse", "otc"]:
        try:
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={prefix}_{stock_id}.tw"
            r = requests.get(url, timeout=4).json()
            if r.get("msgArray"):
                m = r["msgArray"][0]
                z = m.get("z", "-")
                if z == "-" and m.get("b"):
                    z = m["b"].split("_")[0]
                if z != "-" and float(z) > 0:
                    price = float(z)
                    open_p = float(m.get("o", price)) if m.get("o", "-") != "-" else price
                    high_p = float(m.get("h", price)) if m.get("h", "-") != "-" else price
                    low_p = float(m.get("l", price)) if m.get("l", "-") != "-" else price
                    yest_p = float(m.get("y", price)) if m.get("y", "-") != "-" else price
                    return {"price": price, "open": open_p, "high": high_p, "low": low_p, "prev_close": yest_p}
        except Exception:
            pass

    for suffix in [".TW", ".TWO"]:
        try:
            fi = yf.Ticker(f"{stock_id}{suffix}").fast_info
            price = fi.get("lastPrice")
            if price and price > 0:
                return {
                    "price": round(float(price), 2),
                    "open": round(float(fi.get("open", price)), 2),
                    "high": round(float(fi.get("dayHigh", price)), 2),
                    "low": round(float(fi.get("dayLow", price)), 2),
                    "prev_close": round(float(fi.get("previousClose", price)), 2)
                }
        except Exception:
            pass
    return {}

def send_telegram_alert(title: str, details: list):
    msg = f"🚨【{title}】\n" + "\n".join(details)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg},
                timeout=8
            )
        except Exception:
            pass

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
        status = pos.get("status", "")
        if status not in ["WATCHING", "HOLDING"]:
            continue

        sid = pos["stock_id"]
        sname = pos["stock_name"]
        q = get_realtime_quote(sid)
        if not q:
            continue

        live = q["price"]
        day_high = q["high"]
        day_open = q["open"]

        # 階段 A：盤中偵測【右側紅 K 轉強觸價買點】(WATCHING ➔ HOLDING)
        if status == "WATCHING" and not pos.get("entry_alerted", False):
            trigger_buy = round(max(pos["entry_price"], day_open * 1.003), 2)
            e_high = pos.get("entry_high", pos["entry_price"] * 1.025)
            if (trigger_buy <= live <= e_high) and (live >= day_open * 1.003) and (live > pos["hard_stop"]):
                pos["status"] = "HOLDING"
                pos["entry_alerted"] = True
                pos["actual_entry_price"] = live
                pos["highest_price"] = max(live, day_high)
                pos["entry_time"] = now_str
                modified = True
                send_telegram_alert("🟢 盤中右側轉強買點觸發｜啟動超額報酬移動停利", [
                    f"• 標的：{sid} {sname} ({pos.get('strategy', '')})",
                    f"• 觸發成交價：{live} 元 (突破轉強觸發價 {trigger_buy} 元)",
                    f"• 總部位建議：買進 {pos['suggested_lots']} 張 (佔總資金 {pos.get('capital_pct', 15)}%)",
                    f"• 初始停損點：{pos['hard_stop']} 元 ({pos.get('stop_pct', -3.0)}%)",
                    f"• 分批停利點：+6.5%({pos['tp1_price']}元)減碼30% ➔ +14%({pos['tp2_price']}元)減碼30% ➔ +24%({pos['tp3_price']}元)"
                ])
                continue

        if pos.get("status") != "HOLDING":
            continue

        entry_p = pos.get("actual_entry_price", pos["entry_price"])
        rem_lots = pos.get("remaining_lots", pos.get("suggested_lots", 1))
        highest_p = max(pos.get("highest_price", entry_p), live, day_high)
        if highest_p > pos.get("highest_price", 0):
            pos["highest_price"] = highest_p
            modified = True

        trail_stop = pos.get("dynamic_trailing_stop", pos["hard_stop"])

        # 1. 檢驗是否跌破先前已生效之防守底線
        if live <= trail_stop:
            is_profit_exit = pos.get("be_locked", False) or pos.get("tp1_alerted", False)
            pos["status"] = "TRAILING_PROFIT_EXIT" if is_profit_exit else "STOPPED_OUT"
            pos["remaining_lots"] = 0
            modified = True
            title_str = "💰 觸發鎖利底線/移動停利｜獲利出清" if is_profit_exit else "⛔ 觸發初始硬性停損｜立即清倉"
            gain_pct = round(((live - entry_p) / entry_p) * 100, 2)
            send_telegram_alert(title_str, [
                f"• 標的：{sid} {sname}｜現價：{live} 元 ({gain_pct:+.2f}%)",
                f"• 波段最高價：{highest_p} 元｜防守線：{trail_stop} 元",
                f"• 行動建議：請立即將剩餘 {rem_lots} 張全數出清！"
            ])
            continue

        # 2. 漲達 +3.5% 啟動保本鎖利 (+1.0%)
        if live >= pos.get("be_trigger_price", entry_p * 1.035) and not pos.get("be_locked", False):
            pos["be_locked"] = True
            lock_p = pos.get("be_lock_price", round(entry_p * 1.010, 2))
            pos["dynamic_trailing_stop"] = max(trail_stop, lock_p)
            modified = True
            send_telegram_alert("🛡️ 漲達 +3.5% 啟動保本鎖利", [
                f"• 標的：{sid} {sname}｜現價：{live} 元",
                f"• 防守升級：盤中保本底線已上移至 {pos['dynamic_trailing_stop']} 元 (買進成本+1.0%)"
            ])

        # 3. 停利一 (TP1: +6.5%) 減碼 30%，盤中底線設為 +1.8%，餘部收盤守 10MA
        if live >= pos.get("tp1_price", 999999) and not pos.get("tp1_alerted", False):
            pos["tp1_alerted"] = True
            sell_lots = min(rem_lots, pos.get("tp1_reduce_lots", 1))
            sell_pct = pos.get("tp1_reduce_pct", 30)
            pos["remaining_lots"] = max(0, rem_lots - sell_lots)
            pos["dynamic_trailing_stop"] = round(max(entry_p * 1.018, pos.get("ma10", 0) * 0.985), 2)
            modified = True
            send_telegram_alert(f"🎯 停利一(+6.5%)達標｜減碼 {sell_pct}% ({sell_lots}張)", [
                f"• 標的：{sid} {sname}｜現價：{live} 元 (達 +6.5% 停利點 {pos['tp1_price']} 元)",
                f"• 行動建議：獲利賣出 {sell_lots} 張，剩餘 {pos['remaining_lots']} 張收盤守 10MA (盤中保底 {pos['dynamic_trailing_stop']} 元)"
            ])
            continue

        # 4. 停利二 (TP2: +14.0%) 再減碼 30%，鎖利底線上移至 +7.0%
        if live >= pos.get("tp2_price", 999999) and not pos.get("tp2_alerted", False):
            pos["tp2_alerted"] = True
            sell_lots = min(rem_lots, pos.get("tp2_reduce_lots", 1))
            sell_pct = pos.get("tp2_reduce_pct", 30)
            pos["remaining_lots"] = max(0, rem_lots - sell_lots)
            pos["dynamic_trailing_stop"] = round(max(entry_p * 1.070, highest_p * 0.950, pos.get("ma10", 0)), 2)
            modified = True
            send_telegram_alert(f"🚀 停利二(+14.0%)達標｜再減碼 {sell_pct}% ({sell_lots}張)", [
                f"• 標的：{sid} {sname}｜現價：{live} 元 (達 +14.0% 停利點 {pos['tp2_price']} 元)",
                f"• 行動建議：再獲利賣出 {sell_lots} 張，尾倉 {pos['remaining_lots']} 張防守線上移至 {pos['dynamic_trailing_stop']} 元 (+7%保底)"
            ])
            continue

        # 5. 波段目標價 (TP3: +24.0%)
        if live >= pos.get("tp3_price", 999999) and not pos.get("tp3_alerted", False):
            pos["tp3_alerted"] = True
            sell_lots = max(1, int(round(rem_lots * 0.50)))
            pos["remaining_lots"] = max(0, rem_lots - sell_lots)
            pos["dynamic_trailing_stop"] = round(max(entry_p * 1.150, highest_p * 0.950, pos.get("ma10", 0)), 2)
            modified = True
            send_telegram_alert(f"👑 波段目標(+24.0%)達標｜減碼尾倉 ({sell_lots}張)", [
                f"• 標的：{sid} {sname}｜現價：{live} 元",
                f"• 行動建議：賣出 {sell_lots} 張，最後尾倉緊守 {pos['dynamic_trailing_stop']} 元抱到底！"
            ])

    if modified:
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
    return modified

if __name__ == "__main__":
    for i in range(9):
        check_positions_once()
        if i < 8:
            time.sleep(30)
