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
        prev_c = q["prev_close"]

        # ====================================================
        # 階段 A：盤中偵測【合適進場點觸發】(WATCHING ➔ HOLDING)
        # ====================================================
        if status == "WATCHING" and not pos.get("entry_alerted", False):
            e_low = pos.get("entry_low", pos["entry_price"] * 0.99)
            e_high = pos.get("entry_high", pos["entry_price"] * 1.015)
            # 觸發條件：股價進入建議進場區間 (拉回守住支撐且翻紅，或帶量突破買點) 且高於硬性停損
            if (e_low <= live <= e_high) and (live >= day_open) and (live > pos["hard_stop"]):
                pos["status"] = "HOLDING"
                pos["entry_alerted"] = True
                pos["actual_entry_price"] = live
                pos["entry_time"] = now_str
                modified = True
                send_telegram_alert("🟢 盤中買進訊號觸發｜自動轉入持倉監控", [
                    f"• 標的：{sid} {sname} ({pos.get('strategy', '')})",
                    f"• 觸發成交價：{live} 元 (進入建議買點區間 {e_low}～{pos['entry_price']} 元)",
                    f"• 總部位建議：買進 {pos['suggested_lots']} 張 (佔總資金 {pos.get('capital_pct', 15)}%)",
                    f"• 硬性停損點：{pos['hard_stop']} 元 ({pos.get('stop_pct', -4)}%)",
                    f"• 分批停利點：TP1 {pos['tp1_price']}元 (減碼{pos['tp1_reduce_pct']}%) ／ 目標價TP2 {pos['tp2_price']}元 (減碼{pos['tp2_reduce_pct']}%)"
                ])
                continue

        # ====================================================
        # 階段 B：已持倉標的【停損 / 變盤 / 分批停利】即時偵測
        # ====================================================
        if pos.get("status") != "HOLDING":
            continue

        rem_lots = pos.get("remaining_lots", pos.get("suggested_lots", 1))
        hard_stop = pos.get("hard_stop", 0)
        dyn_stop = pos.get("dynamic_trailing_stop", hard_stop)
        cdp_al = pos.get("cdp_al", hard_stop)

        # 1. 硬性停損觸發 (100% 清倉)
        if live <= hard_stop:
            pos["status"] = "STOPPED_OUT"
            pos["remaining_lots"] = 0
            modified = True
            send_telegram_alert("⛔ 硬性停損觸發｜立即清倉", [
                f"• 標的：{sid} {sname}",
                f"• 當前價：{live} 元 (跌破停損點 {hard_stop} 元)",
                f"• 行動建議：請立即市價賣出 100% 部位 (出清剩餘 {rem_lots} 張)"
            ])
            continue

        # 2. 盤中突發變盤 (開高走低 A 轉或貫穿極限支撐)
        intraday_surge = ((day_high - prev_c) / max(prev_c, 1)) >= 0.03
        pullback_from_high = ((day_high - live) / max(day_high, 1)) >= 0.035
        turned_black = (live < day_open) and (live < prev_c)
        broke_al = (live <= cdp_al) and (live < pos.get("max_vol_low", live))

        if (not pos.get("reversal_alerted", False)) and ((intraday_surge and pullback_from_high and turned_black) or broke_al):
            pos["reversal_alerted"] = True
            cut_lots = max(1, int(round(rem_lots * 0.50)))
            pos["remaining_lots"] = max(0, rem_lots - cut_lots)
            pos["hard_stop"] = round(max(hard_stop, min(q["low"] * 0.995, live * 0.985)), 2)
            modified = True
            send_telegram_alert("⚡ 盤中突發變盤警報｜防禦減碼 50%", [
                f"• 標的：{sid} {sname} (日高 {day_high} ➔ 現價 {live} 元)",
                f"• 行動建議：立即減碼 50% (賣出 {cut_lots} 張)，剩餘 {pos['remaining_lots']} 張停損收緊至 {pos['hard_stop']} 元"
            ])
            continue

        # 3. 第一階段停利 (TP1)
        if live >= pos.get("tp1_price", 999999) and not pos.get("tp1_alerted", False):
            pos["tp1_alerted"] = True
            sell_lots = min(rem_lots, pos.get("tp1_reduce_lots", 1))
            sell_pct = pos.get("tp1_reduce_pct", 40)
            pos["remaining_lots"] = max(0, rem_lots - sell_lots)
            pos["dynamic_trailing_stop"] = round(max(pos["entry_price"], pos.get("poc_price", 0), pos.get("ma10", 0) * 0.99), 2)
            modified = True
            send_telegram_alert(f"🎯 停利一(TP1)達標｜減碼 {sell_pct}% ({sell_lots}張)", [
                f"• 標的：{sid} {sname}｜現價：{live} 元 (達 TP1 {pos['tp1_price']} 元)",
                f"• 行動建議：獲利賣出 {sell_lots} 張，剩餘 {pos['remaining_lots']} 張防守線上移至 {pos['dynamic_trailing_stop']} 元 (目標價看 {pos['tp2_price']} 元)"
            ])
            continue

        # 4. 第二階段目標價停利 (TP2)
        if live >= pos.get("tp2_price", 999999) and not pos.get("tp2_alerted", False):
            pos["tp2_alerted"] = True
            sell_lots = min(rem_lots, pos.get("tp2_reduce_lots", 1))
            sell_pct = pos.get("tp2_reduce_pct", 30)
            pos["remaining_lots"] = max(0, rem_lots - sell_lots)
            pos["dynamic_trailing_stop"] = round(max(pos["dynamic_trailing_stop"], pos.get("ma5", live * 0.97)), 2)
            if pos["remaining_lots"] == 0:
                pos["status"] = "TAKE_PROFIT_COMPLETED"
            modified = True
            send_telegram_alert(f"🚀 目標價(TP2)達標｜再減碼 {sell_pct}% ({sell_lots}張)", [
                f"• 標的：{sid} {sname}｜現價：{live} 元 (達目標價 {pos['tp2_price']} 元)",
                f"• 行動建議：再獲利賣出 {sell_lots} 張，尾倉 {pos['remaining_lots']} 張防守線上移至 {pos['dynamic_trailing_stop']} 元"
            ])
            continue

        # 5. 尾倉跌破動態防守線
        if pos.get("tp1_alerted", False) and live <= pos.get("dynamic_trailing_stop", 0):
            pos["status"] = "TRAILING_PROFIT_EXIT"
            exit_lots = pos.get("remaining_lots", 1)
            pos["remaining_lots"] = 0
            modified = True
            send_telegram_alert("💰 尾倉觸發移動停利｜全數獲利入袋", [
                f"• 標的：{sid} {sname}｜現價：{live} 元 (跌破尾倉防守 {pos['dynamic_trailing_stop']} 元)",
                f"• 行動建議：尾倉 {exit_lots} 張全數獲利出清"
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
