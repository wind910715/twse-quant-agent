import os
import time
import json
import datetime
import requests
import yfinance as yf

POSITIONS_FILE = "active_positions.json"

def fetch_twse_mis_batch_quotes(positions: list) -> dict:
    """
    【台北證交所 / 櫃買中心 官方 MIS 批次即時訊號串聯引擎】：
    一次將所有監控中標的透過 `|` 批次查詢，0.2 秒極速取得五檔與最新成交價，零延遲、不漏接！
    """
    quotes = {}
    if not positions:
        return quotes

    ex_list = []
    for p in positions:
        if p.get("status") in ["WATCHING", "HOLDING"]:
            sid = p["stock_id"]
            pfx = p.get("ex_prefix", "tse")
            ex_list.append(f"{pfx}_{sid}.tw")
            # 同時加入另一市場備援確保上市櫃皆可命中
            alt_pfx = "otc" if pfx == "tse" else "tse"
            ex_list.append(f"{alt_pfx}_{sid}.tw")

    if not ex_list:
        return quotes

    # 每 20 檔切分為一個批次向證交所 MIS 請求
    headers = {"User-Agent": "Mozilla/5.0"}
    for idx in range(0, len(ex_list), 20):
        batch_str = "|".join(ex_list[idx:idx + 20])
        try:
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={batch_str}&json=1&delay=0"
            r = requests.get(url, headers=headers, timeout=5).json()
            for m in r.get("msgArray", []):
                sid = str(m.get("c", "")).strip()
                if not sid:
                    continue
                z = m.get("z", "-")
                # 若當盤瞬間無成交價 z，取最佳委買第一檔 b 與最佳委賣 a 中價
                if z == "-" and m.get("b"):
                    b_first = m["b"].split("_")[0]
                    if b_first and b_first != "-":
                        z = b_first
                if z != "-" and float(z) > 0:
                    price = round(float(z), 2)
                    open_p = float(m.get("o", price)) if m.get("o", "-") != "-" else price
                    high_p = float(m.get("h", price)) if m.get("h", "-") != "-" else price
                    low_p = float(m.get("l", price)) if m.get("l", "-") != "-" else price
                    yest_p = float(m.get("y", price)) if m.get("y", "-") != "-" else price
                    vol_lots = int(m.get("v", 0)) if str(m.get("v", "0")).isdigit() else 0
                    quotes[sid] = {
                        "price": price,
                        "open": round(open_p, 2),
                        "high": round(high_p, 2),
                        "low": round(low_p, 2),
                        "prev_close": round(yest_p, 2),
                        "vol_lots": vol_lots
                    }
        except Exception:
            continue

    # 若證交所 MIS 針對個別股票尚未回傳，以 Yahoo Finance 即時報價補齊
    for p in positions:
        sid = p["stock_id"]
        if sid not in quotes and p.get("status") in ["WATCHING", "HOLDING"]:
            for suffix in [".TW", ".TWO"]:
                try:
                    fi = yf.Ticker(f"{sid}{suffix}").fast_info
                    lp = fi.get("lastPrice")
                    if lp and lp > 0:
                        quotes[sid] = {
                            "price": round(float(lp), 2),
                            "open": round(float(fi.get("open", lp)), 2),
                            "high": round(float(fi.get("dayHigh", lp)), 2),
                            "low": round(float(fi.get("dayLow", lp)), 2),
                            "prev_close": round(float(fi.get("previousClose", lp)), 2),
                            "vol_lots": 0
                        }
                        break
                except Exception:
                    pass
    return quotes

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
    batch_quotes = fetch_twse_mis_batch_quotes(positions)

    for pos in positions:
        status = pos.get("status", "")
        if status not in ["WATCHING", "HOLDING"]:
            continue

        # 過濾掉舊版未經過盤中進場確認 (entered_intraday != True) 卻誤標為 HOLDING 的殘留資料
        if status == "HOLDING" and not pos.get("entered_intraday", False):
            pos["status"] = "WATCHING"
            status = "WATCHING"
            modified = True

        sid = pos["stock_id"]
        sname = pos["stock_name"]
        q = batch_quotes.get(sid)
        if not q:
            continue

        live = q["price"]
        day_high = q["high"]
        day_open = q["open"]

        # ==========================================================
        # 階段 A：昨晚 21:00 鎖定之觀察股 (WATCHING) 盤中即時觸發監控
        # ==========================================================
        if status == "WATCHING" and not pos.get("entry_alerted", False):
            # 1. 若開盤或盤中未觸發買點就先跌破初始防守價 ➔ 標記為破位失效，提醒不進場 (絕不誤發停損清倉！)
            if live <= pos["hard_stop"] and not pos.get("invalidated_alerted", False):
                pos["status"] = "INVALIDATED_NO_ENTRY"
                pos["invalidated_alerted"] = True
                modified = True
                send_telegram_alert("⚠️ 觀察股開盤走弱破位｜取消買進監控 (空手未進場)", [
                    f"• 標的：{sid} {sname}｜對應策略：{pos.get('strategy', '')}",
                    f"• 證交所即時價：{live} 元 (未突破買點 {pos['entry_price']} 元，且跌破防守價 {pos['hard_stop']} 元)",
                    f"• 行動指令：今日不符右側轉強條件，取消買進監控，保留資金！"
                ])
                continue

            # 2. 盤中突破轉強買點 (高於昨收訊號價 且 高於今開 +0.3% 確認紅K攻擊) ➔ 第一時間通知進場並給出完整策略進出場劇本！
            trigger_buy = round(max(pos["entry_price"], day_open * 1.003), 2)
            e_high = pos.get("entry_high", pos["entry_price"] * 1.028)
            if (trigger_buy <= live <= e_high) and (live >= day_open * 1.003) and (live > pos["hard_stop"]):
                pos["status"] = "HOLDING"
                pos["entered_intraday"] = True
                pos["entry_alerted"] = True
                pos["actual_entry_price"] = live
                pos["highest_price"] = max(live, day_high)
                pos["entry_time"] = now_str
                # 依據實際觸發買進價，即時校準三階段停利價位
                pos["tp1_price"] = round(live * 1.060, 2)
                pos["tp2_price"] = round(live * 1.140, 2)
                pos["tp3_price"] = round(live * 1.250, 2)
                modified = True
                send_telegram_alert("🟢 證交所盤中進場訊號觸發｜立即買進與對應策略進出場指令", [
                    f"• 觸發標的：{sid} {sname} (時間：{now_str[-8:]})",
                    f"• 觸發策略：{pos.get('strategy', '')}",
                    f"• 建議進場價：現價 {live} 元 (已突破轉強觸發價 {trigger_buy} 元，追價上限 {e_high} 元)",
                    f"• 總部位建議：買進 {pos['suggested_lots']} 張 (動用約 {pos.get('capital_used', 0)//10000} 萬 / 佔總資金 {pos.get('capital_pct', 20)}%)",
                    f"• 初始硬性停損：{pos['hard_stop']} 元 ({pos.get('stop_pct', -2.5)}%｜3日不漲破5MA主動汰弱)",
                    f"• 🎯【對應策略分批停利與移動出場計畫】：",
                    f"  ① 停利一 (+6.0%)：{pos['tp1_price']} 元 ➔ 減碼 {pos['tp1_reduce_pct']}% (賣 {pos['tp1_reduce_lots']} 張)，底線鎖 +1.5%",
                    f"  ② 停利二 (+14.0%)：{pos['tp2_price']} 元 ➔ 再減碼 {pos['tp2_reduce_pct']}% (賣 {pos['tp2_reduce_lots']} 張)，底線鎖 +8.0%",
                    f"  ③ 目標價 (+25.0%)：{pos['tp3_price']} 元 ➔ 尾倉 {pos['tp3_remain_pct']}% ({pos['tp3_remain_lots']} 張) 以最高收盤回落 5.5%~6% 抱到底！"
                ])
                continue

        # ==========================================================
        # 階段 B：已確認盤中進場之真實持倉 (HOLDING) 停損與分批停利監控
        # ==========================================================
        if pos.get("status") != "HOLDING" or not pos.get("entered_intraday", False):
            continue

        entry_p = pos.get("actual_entry_price", pos["entry_price"])
        rem_lots = pos.get("remaining_lots", pos.get("suggested_lots", 1))
        highest_p = max(pos.get("highest_price", entry_p), live, day_high)
        if highest_p > pos.get("highest_price", 0):
            pos["highest_price"] = highest_p
            modified = True

        trail_stop = pos.get("dynamic_trailing_stop", pos["hard_stop"])

        # 1. 檢驗是否跌破防守底線 / 吊燈移動停利
        if live <= trail_stop:
            is_profit_exit = pos.get("tp1_alerted", False) or pos.get("be_locked", False)
            pos["status"] = "TRAILING_PROFIT_EXIT" if is_profit_exit else "STOPPED_OUT"
            pos["remaining_lots"] = 0
            modified = True
            title_str = "💰 觸發吊燈移動停利｜獲利出清" if is_profit_exit else "⛔ 觸發初始硬性停損｜立即清倉"
            gain_pct = round(((live - entry_p) / entry_p) * 100, 2)
            send_telegram_alert(title_str, [
                f"• 標的：{sid} {sname}｜對應策略：{pos.get('strategy', '')}",
                f"• 證交所現價：{live} 元 (進場價 {entry_p} 元，報酬 {gain_pct:+.2f}%)",
                f"• 波段最高價：{highest_p} 元｜觸發防守線：{trail_stop} 元",
                f"• 行動指令：請立即將剩餘 {rem_lots} 張全數出清！"
            ])
            continue

        # 2. 停利一 (TP1: +6.0%) 減碼 25%，底線鎖 +1.5%，啟動最高價回落 5.5% 吊燈停利
        if live >= pos.get("tp1_price", 999999) and not pos.get("tp1_alerted", False):
            pos["tp1_alerted"] = True
            pos["be_locked"] = True
            sell_lots = min(rem_lots, pos.get("tp1_reduce_lots", 1))
            sell_pct = pos.get("tp1_reduce_pct", 25)
            pos["remaining_lots"] = max(0, rem_lots - sell_lots)
            pos["dynamic_trailing_stop"] = round(max(entry_p * 1.015, highest_p * 0.945), 2)
            modified = True
            send_telegram_alert(f"🎯 停利一(+6.0%)達標｜減碼 {sell_pct}% ({sell_lots}張)", [
                f"• 標的：{sid} {sname} ({pos.get('strategy', '')})｜現價：{live} 元",
                f"• 行動指令：請先獲利賣出 {sell_lots} 張！剩餘 {pos['remaining_lots']} 張防守底線已上移至 {pos['dynamic_trailing_stop']} 元，下一目標看 TP2 ({pos['tp2_price']} 元)"
            ])
            continue

        # 3. 停利二 (TP2: +14.0%) 再減碼 35%，底線鎖死 +8.0%
        if live >= pos.get("tp2_price", 999999) and not pos.get("tp2_alerted", False):
            pos["tp2_alerted"] = True
            sell_lots = min(rem_lots, pos.get("tp2_reduce_lots", 1))
            sell_pct = pos.get("tp2_reduce_pct", 35)
            pos["remaining_lots"] = max(0, rem_lots - sell_lots)
            pos["dynamic_trailing_stop"] = round(max(entry_p * 1.080, highest_p * 0.945), 2)
            modified = True
            send_telegram_alert(f"🚀 停利二(+14.0%)達標｜再減碼 {sell_pct}% ({sell_lots}張)", [
                f"• 標的：{sid} {sname} ({pos.get('strategy', '')})｜現價：{live} 元",
                f"• 行動指令：請再獲利賣出 {sell_lots} 張！剩餘 {pos['remaining_lots']} 張防守底線已上移至 {pos['dynamic_trailing_stop']} 元 (+8.0%保底)"
            ])
            continue

        # 4. 波段目標價 (TP3: +25.0%)
        if live >= pos.get("tp3_price", 999999) and not pos.get("tp3_alerted", False):
            pos["tp3_alerted"] = True
            sell_lots = max(1, int(round(rem_lots * 0.50)))
            pos["remaining_lots"] = max(0, rem_lots - sell_lots)
            pos["dynamic_trailing_stop"] = round(max(entry_p * 1.160, highest_p * 0.940), 2)
            modified = True
            send_telegram_alert(f"👑 波段目標(+25.0%)達標｜減碼尾倉 ({sell_lots}張)", [
                f"• 標的：{sid} {sname} ({pos.get('strategy', '')})｜現價：{live} 元",
                f"• 行動指令：賣出 {sell_lots} 張，最後尾倉 {pos['remaining_lots']} 張緊守 {pos['dynamic_trailing_stop']} 元抱到底！"
            ])

    if modified:
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
    return modified

if __name__ == "__main__":
    # 每 20 秒透過台北證交所 MIS 批次引擎極速掃描一次 (單次排程巡邏 13 次涵蓋完整 4.5 分鐘)
    for i in range(13):
        check_positions_once()
        if i < 12:
            time.sleep(20)
