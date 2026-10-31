"""08:00 盤前推播：今日觀察標的＋目前持倉標的。

讀 active_positions.json（昨晚 21:00 screener 鎖定的名單＋既有持倉）。
舊制（張、300 萬）紀錄以 migrate_position_record 在記憶體內轉換，不寫回
（寫回由盤中 monitor 負責）；重算為 0 股的舊觀察紀錄直接剔除。
"""
import json
import os

from notify import send_telegram
from strategy_core import migrate_position_record

POSITIONS_FILE = "active_positions.json"


def _fmt_money(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def build_brief_text(positions: list) -> str:
    watching = [p for p in positions if p.get("status") == "WATCHING"]
    holding = [p for p in positions
               if p.get("status") == "HOLDING" and p.get("entered_intraday")]
    lines = ["🌅 08:00 盤前｜今日觀察＋持倉"]
    lines.append(f"【今日觀察】（{len(watching)} 檔）")
    if not watching:
        lines.append("• 無")
    for p in watching:
        lines.append(
            f"• {p['stock_id']} {p['stock_name']}｜{p.get('strategy', '')}"
            f"｜觸發價 {p['entry_price']}（追價上限 {p.get('entry_high', '')}）"
            f"｜停損 {p['hard_stop']}"
            f"｜買 {p.get('suggested_shares', 0)} 股"
            f"（動用 {_fmt_money(p.get('capital_used', 0))} 元，佔 {p.get('capital_pct', 0)}%）"
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
    lines.append("提醒：零股今日進場者，明日才能出場。")
    return "\n".join(lines)


def main():
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
    send_telegram(build_brief_text(positions))


if __name__ == "__main__":
    main()
