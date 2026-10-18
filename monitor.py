import os
import time
import json
import datetime
import requests
import yfinance as yf

POSITIONS_FILE = "active_positions.json"

def get_realtime_quote(stock_id: str) -> dict:
    """從證交所 MIS 或 Yahoo Finance 取得盤中即時【成交價、開盤價、當日最高、當日最低、昨收】"""
    for prefix in ["tse", "otc"]:
        try:
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={prefix}_{stock_id}.tw"
            r = requests.get(url, timeout=4).json()
            if r.get("msgArray"):
                m = r["msgArray"][0]
                z = m.get("z", "-")
                # 若當下無搓合成交價，取最佳委買價(b)第一檔輔助判斷
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

def check_market_flash_crash() -> dict:
    """偵測台股大盤 (^TWII / 0050) 是否發生盤中突發重挫變盤"""
    try:
        fi = yf.Ticker("^TWII").fast_info
        last_p = float(fi.get("lastPrice", 0))
        prev_p = float(fi.get("previousClose", 0))
        day_high = float(fi.get("dayHigh", last_p))
        if last_p > 0 and prev_p > 0:
            chg_pct = round(((last_p - prev_p) / prev_p) * 100, 2)
            drop_from_high = round(((last_p - day_high) / day_high) * 100, 2)
            # 若大盤單日跌幅 >= 1.5% 或從日內高點急殺超過 1.5%
            if chg_pct <= -1.5 or drop_from_high <= -1.5:
                return {"is_crash": True, "index": round(last_p, 1), "chg_pct": chg_pct, "drop_high": drop_from_high}
    except Exception:
        pass
    return {"is_crash": False}

def send_telegram_alert(title: str, details: list):
    msg = f"🚨【{title}】🚨\n" + "\n".join(details)
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
    mkt = check_market_flash_crash()

    for pos in positions:
        if pos.get("status") != "HOLDING":
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

        total_lots = pos.get("suggested_lots", 1)
        rem_lots = pos.get("remaining_lots", total_lots)
        hard_stop = pos.get("hard_stop", 0)
        dyn_stop = pos.get("dynamic_trailing_stop", hard_stop)
        cdp_al = pos.get("cdp_al", hard_stop)
        poc_p = pos.get("poc_price", hard_stop)

        # ====================================================
        # 情境 1：⛔ 觸發硬性停損 (Hard Stop) ➔ 100% 全數砍倉
        # ====================================================
        if live <= hard_stop:
            pos["status"] = "STOPPED_OUT"
            pos["exit_price"] = live
            pos["exit_time"] = now_str
            pos["remaining_lots"] = 0
            modified = True
            send_telegram_alert("⛔ 硬性停損觸發｜立即100%清倉", [
                f"• 標的：{sid} {sname} ({pos.get('strategy', '')})",
                f"• 當前成交價：{live} 元 (跌破硬性停損 {hard_stop} 元)",
                f"• 關鍵支撐失守：已跌破籌碼POC ({poc_p}元) 與結構防線",
                f"• ⚡ 執行指令：請立即以市價減碼 100%（全數賣出剩餘 {rem_lots} 張）！"
            ])
            continue

        # ====================================================
        # 情境 2：⚡ 盤中突發變盤偵測 (開高走低 A 轉 / 跌破 CDP_AL 極限值 / 大盤急殺破位)
        # ====================================================
        intraday_surge = ((day_high - prev_c) / max(prev_c, 1)) >= 0.03
        pullback_from_high = ((day_high - live) / max(day_high, 1)) >= 0.035
        turned_black = (live < day_open) and (live < prev_c)
        broke_cdp_al = (live <= cdp_al) and (live < pos.get("max_vol_low", live))

        if (not pos.get("reversal_alerted", False)) and ((intraday_surge and pullback_from_high and turned_black) or broke_cdp_al or (mkt["is_crash"] and live < day_open)):
            pos["reversal_alerted"] = True
            cut_lots = max(1, int(round(rem_lots * 0.50)))
            pos["remaining_lots"] = max(0, rem_lots - cut_lots)
            # 變盤後自動收緊停損至當日低點或大量低點
            pos["hard_stop"] = round(max(hard_stop, min(q["low"] * 0.995, live * 0.985)), 2)
            modified = True
            reason_str = "早盤衝高逾3%後急殺翻黑(標準A轉出貨)" if (intraday_surge and pullback_from_high) else f"瞬間貫穿CDP恐慌極限值AL({cdp_al}元)或大盤重挫({mkt.get('chg_pct', 0)}%)"
            send_telegram_alert("⚡ 盤中突發變盤警報｜緊急防禦減碼 50%", [
                f"• 標的：{sid} {sname} (日高 {day_high}元 ➔ 現價 {live}元)",
                f"• 變盤原因：{reason_str}",
                f"• 指標位置：20日大量低點 {pos.get('max_vol_low')}元 / CDP_AL {cdp_al}元 / 布林中軌 {pos.get('bb_mid')}元",
                f"• ⚡ 執行指令：建議立即減碼 50%（先賣出 {cut_lots} 張，剩餘 {pos['remaining_lots']} 張防守線收緊至 {pos['hard_stop']} 元）！"
            ])
            continue

        # ====================================================
        # 情境 3：🎯 第一階段停利 (TP1：布林上軌 / CDP_AH / 前高壓力)
        # ====================================================
        tp1_p = pos.get("tp1_price", 999999)
        if live >= tp1_p and not pos.get("tp1_alerted", False):
            pos["tp1_alerted"] = True
            sell_lots = min(rem_lots, pos.get("tp1_reduce_lots", 1))
            sell_pct = pos.get("tp1_reduce_pct", 40)
            pos["remaining_lots"] = max(0, rem_lots - sell_lots)
            # 達標後，將動態防守線提升至【保本價、籌碼POC、10MA】三者之最有利位置
            new_defense = round(max(pos["entry_price"], pos.get("poc_price", 0), pos.get("ma10", 0) * 0.99), 2)
            pos["dynamic_trailing_stop"] = min(new_defense, round(live * 0.97, 2))
            modified = True
            send_telegram_alert(f"🎯 第一階段停利(TP1)達標｜減碼 {sell_pct}% ({sell_lots}張)", [
                f"• 標的：{sid} {sname} ({pos.get('strategy', '')})",
                f"• 當前成交價：{live} 元 (突破 TP1 目標 {tp1_p} 元)",
                f"• 技術共振：觸及布林上軌 ({pos.get('bb_up')}元) / CDP最高值AH ({pos.get('cdp_ah')}元) 短壓區",
                f"• 💰 執行指令：請立即減碼 {sell_pct}%（獲利賣出 {sell_lots} 張，保留 {pos['remaining_lots']} 張衝刺波段）！",
                f"• 🛡️ 防守升級：剩餘部位動態停利線已自動上移至 {pos['dynamic_trailing_stop']} 元（結合保本價與籌碼POC {poc_p}元）"
            ])
            continue

        # ====================================================
        # 情境 4：🚀 第二階段停利 (TP2：波段 3R 目標 / 大壓力區)
        # ====================================================
        tp2_p = pos.get("tp2_price", 999999)
        if live >= tp2_p and not pos.get("tp2_alerted", False):
            pos["tp2_alerted"] = True
            sell_lots = min(rem_lots, pos.get("tp2_reduce_lots", 1))
            sell_pct = pos.get("tp2_reduce_pct", 30)
            pos["remaining_lots"] = max(0, rem_lots - sell_lots)
            # 尾倉防守線緊鎖 5MA 與 CDP_NL
            pos["dynamic_trailing_stop"] = round(max(pos["dynamic_trailing_stop"], pos.get("ma5", live * 0.97), pos.get("cdp_nl", 0)), 2)
            if pos["remaining_lots"] == 0:
                pos["status"] = "TAKE_PROFIT_COMPLETED"
            modified = True
            send_telegram_alert(f"🚀 第二階段波段停利(TP2)達標｜再減碼 {sell_pct}% ({sell_lots}張)", [
                f"• 標的：{sid} {sname}",
                f"• 當前成交價：{live} 元 (突破波段 TP2 目標 {tp2_p} 元)",
                f"• 💰 執行指令：請再減碼 {sell_pct}%（賣出 {sell_lots} 張），剩餘尾倉 {pos['remaining_lots']} 張改守 5MA/CDP_NL ({pos['dynamic_trailing_stop']} 元)！"
            ])
            continue

        # ====================================================
        # 情境 5：💰 跌破五維度動態防守線 (已過 TP1 後回檔跌破 POC/均線/布林中軌)
        # ====================================================
        if pos.get("tp1_alerted", False) and live <= pos.get("dynamic_trailing_stop", 0):
            pos["status"] = "TRAILING_PROFIT_EXIT"
            pos["exit_price"] = live
            pos["exit_time"] = now_str
            exit_lots = pos.get("remaining_lots", 1)
            pos["remaining_lots"] = 0
            modified = True
            send_telegram_alert("💰 觸發五維度動態移動停利｜尾倉全數入袋", [
                f"• 標的：{sid} {sname}",
                f"• 當前成交價：{live} 元 (跌破綜合動態防守價 {pos['dynamic_trailing_stop']} 元)",
                f"• 參考依據：{pos.get('trailing_desc', '綜合均線與籌碼POC防守')}",
                f"• 💰 執行指令：波段尾倉 {exit_lots} 張請全數獲利了結出清！"
            ])

    if modified:
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
    return modified

if __name__ == "__main__":
    # 配合 GitHub Actions 每 5 分鐘喚醒一次，內部每 30 秒高頻巡邏 9 次 (涵蓋完整 4.5 分鐘無死角)
    print("🛡️ 啟動盤中每 30 秒高頻五維度監控與突發變盤偵測...")
    for i in range(9):
        check_positions_once()
        if i < 8:
            time.sleep(30)
