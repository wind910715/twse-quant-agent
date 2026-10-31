"""14:00 收盤推播：今日進出場總攬＋目前持倉標的。

進出場來自 monitor 盤中寫入的 trade_events.jsonl（以台北日期篩選今日）；
持倉來自 active_positions.json（舊制紀錄以 migrate_position_record 在記憶體內轉換）。
"""
import json
import os

from notify import send_telegram
from strategy_core import migrate_position_record, taipei_today

POSITIONS_FILE = "active_positions.json"
TRADE_EVENTS_FILE = "trade_events.jsonl"

EVENT_LABEL = {
    "ENTRY": "進場",
    "STOPPED_OUT": "停損出清",
    "TRAILING_PROFIT_EXIT": "吊燈停利出清",
    "TP1": "TP1 減碼",
    "TP2": "TP2 減碼",
    "TP3": "TP3 減碼",
}


def _fmt_money(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _gain_str(gain) -> str:
    if gain is None:
        return ""
    try:
        return f"，{float(gain):+.2f}%"
    except Exception:
        return ""


def build_summary_text(today: str, entries: list, exits: list, holding: list) -> str:
    lines = ["🌇 14:00 收盤｜今日進出總攬＋持倉"]
    lines.append(f"【今日進場】（{len(entries)} 筆）")
    if not entries:
        lines.append("• 無")
    for e in entries:
        lines.append(
            f"• {e.get('time', '')} {e['stock_id']} {e['stock_name']}｜{e.get('strategy', '')}"
            f"｜進場 {e.get('price', '')}｜買 {e.get('shares', 0)} 股"
        )
    lines.append(f"【今日出場】（{len(exits)} 筆）")
    if not exits:
        lines.append("• 無")
    for e in exits:
        lines.append(
            f"• {e.get('time', '')} {e['stock_id']} {e['stock_name']}｜{EVENT_LABEL.get(e.get('event', ''), e.get('event', ''))}"
            f" {e.get('shares', 0)} 股（進場 {e.get('entry_price', '')} → {e.get('price', '')}{_gain_str(e.get('gain_pct'))}）"
            f"｜剩 {e.get('remaining_shares', 0)} 股"
        )
    lines.append(f"【目前持倉】（{len(holding)} 檔）")
    if not holding:
        lines.append("• 無")
    for p in holding:
        lines.append(
            f"• {p['stock_id']} {p['stock_name']}｜{p.get('strategy', '')}"
            f"｜進場 {p.get('actual_entry_price', p['entry_price'])}"
            f"｜停損 {p.get('dynamic_trailing_stop', p['hard_stop'])}"
            f"｜TP1 {p.get('tp1_price', '')}／TP2 {p.get('tp2_price', '')}／TP3 {p.get('tp3_price', '')}"
            f"｜剩 {p.get('remaining_shares', 0)} 股"
        )
    return "\n".join(lines)


def main():
    today = taipei_today()
    entries, exits = [], []
    if os.path.exists(TRADE_EVENTS_FILE):
        try:
            with open(TRADE_EVENTS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if str(e.get("date", "")) != today:
                        continue
                    if e.get("event") == "ENTRY":
                        entries.append(e)
                    else:
                        exits.append(e)
        except Exception:
            entries, exits = [], []
    positions = []
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for p in raw:
                m = migrate_position_record(p)
                if m is not None:
                    positions.append(m)
        except Exception:
            positions = []
    holding = [p for p in positions
               if p.get("status") == "HOLDING" and p.get("entered_intraday")]
    send_telegram(build_summary_text(today, entries, exits, holding))


if __name__ == "__main__":
    main()
