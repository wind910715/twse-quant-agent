#!/usr/bin/env python3
"""prototype_lab.py — 候選策略原型實驗室 (v2, 2026-10-24 長視窗+剔除版)

在週六回測 job 中於 backtest.py 之後執行, 結果寫 prototype_results.json 並發 Telegram。
資料視窗: 2020-01-01 起 (使用者指定長視窗標準)。
現行候選:
  P2  PEAD 營收驚喜漂移 — FinMind 月營收 (point-in-time: 營收月 M 於 M+1 月 11 日後可用),
      每月以年增率排名取前 30 且年增加速者, 持有 20/40/60 日, 硬停損 -8%。
      (本質為月營收因子的事件型監控; 因子本體已由 factor_rotation.py 每月正式產名單。)
已剔除 (2026-10-24, 常設規則: 無效且無優化路徑者刪除; 完整程式保留於 git 歷史):
  P1/P1b RSI(2)/布林穿透 (2,100 筆輸基準)、P3 RS 線 (無增量價值, 對照組已畢業為 BRK20)、
  P4 漲停隔日沖 (統計過關但期貨買不到, 使用者實務否決)。
口徑: 事件研究 (每筆獨立統計, 不做組合槽位), 成本買 0.1425% / 賣 0.4425% (與主系統一致)。
任何單一原型失敗不影響其他原型; 本腳本永遠 exit 0。
"""
import json
import os
import traceback
from datetime import datetime

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from screener import build_dynamic_index_universe
from strategy_core import calc_rsi

BUY_COST = 0.001425
SELL_COST = 0.004425
RESULT_FILE = "prototype_results.json"


def net_ret(entry: float, exit_px: float) -> float:
    return (exit_px * (1 - SELL_COST)) / (entry * (1 + BUY_COST)) * 100 - 100


def stats(rets: list) -> dict:
    if not rets:
        return {"n": 0, "win": 0.0, "avg": 0.0, "median": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "rr": 0.0}
    r = np.array(rets, dtype=float)
    w = r[r > 0]
    l = r[r <= 0]
    return {
        "n": int(len(r)),
        "win": round(float((r > 0).mean() * 100), 1),
        "avg": round(float(r.mean()), 2),
        "median": round(float(np.median(r)), 2),
        "avg_win": round(float(w.mean()), 2) if len(w) else 0.0,
        "avg_loss": round(float(l.mean()), 2) if len(l) else 0.0,
        "rr": round(float(w.mean() / abs(l.mean())), 2) if len(w) and len(l) and l.mean() != 0 else 0.0,
    }


def fmt(name: str, s: dict) -> str:
    if s["n"] == 0:
        return f"{name}: 無事件"
    return (f"{name}: {s['n']}筆 勝率{s['win']}% 平均{s['avg']:+.2f}% 中位{s['median']:+.2f}% "
            f"賺{s['avg_win']:+.2f}/賠{s['avg_loss']:+.2f} RR{s['rr']}")


# ---------------------------------------------------------------- data
def load_data(limit: int = 0):
    universe = build_dynamic_index_universe()
    items = list(universe.items())
    if limit:
        items = items[:limit]
    tickers = ["0050.TW"] + [f"{sid}{sfx}" for sid, (_, sfx) in items]
    data = yf.download(tickers, start="2020-01-01", group_by="ticker", threads=True, progress=False)
    bench = None
    stocks = {}
    for t in tickers:
        try:
            df = data[t].dropna(subset=["Close"]).copy()
        except Exception:
            continue
        if len(df) < 60:
            continue
        if t == "0050.TW":
            bench = df
        else:
            sid = t.split(".")[0]
            stocks[sid] = df
    return stocks, bench


# ---------------------------------------------------------------- P2 PEAD (月營收)
def fetch_revenue(sid: str, token: str) -> pd.DataFrame:
    params = {"dataset": "TaiwanStockMonthRevenue", "data_id": sid, "start_date": "2022-06-01"}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get("https://api.finmindtrade.com/api/v4/data", params=params, headers=headers, timeout=30)
    j = r.json()
    rows = j.get("data", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["ym"] = df["revenue_year"] * 12 + df["revenue_month"]
    return df.sort_values("ym")


def proto_pead(stocks: dict) -> dict:
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    events = {}  # usable_ym -> list[(sid, yoy, accel)]
    ok = 0
    for sid in stocks:
        try:
            rdf = fetch_revenue(sid, token)
        except Exception:
            continue
        if len(rdf) < 15:
            continue
        ok += 1
        rev = dict(zip(rdf["ym"], rdf["revenue"]))
        for ym, cur_rev in rev.items():
            prev_y = rev.get(ym - 12)
            prev_m_yoy_base = rev.get(ym - 13)
            prev_m_cur = rev.get(ym - 1)
            if not prev_y or not prev_m_yoy_base or not prev_m_cur:
                continue
            yoy = cur_rev / prev_y - 1
            yoy_prev = prev_m_cur / prev_m_yoy_base - 1
            usable_ym = ym + 1  # M 月營收於 M+1 月 10 日前公布, 11 日起可用
            events.setdefault(usable_ym, []).append((sid, yoy, yoy - yoy_prev))
    if ok < len(stocks) * 0.5:
        raise RuntimeError(f"FinMind 營收僅取得 {ok}/{len(stocks)} 檔, 視為不可用")

    def fwd_ret(sid: str, usable_ym: int, hold: int):
        df = stocks[sid]
        y, m = (usable_ym - 1) // 12, (usable_ym - 1) % 12 + 1
        after = pd.Timestamp(year=y, month=m, day=11)
        pos = df.index.searchsorted(after)
        if pos >= len(df) - 1:
            return None
        entry = float(df["Open"].iloc[pos])
        stop = entry * 0.92
        last = min(pos + hold, len(df) - 1)
        for k in range(pos, last + 1):
            if float(df["Low"].iloc[k]) <= stop:
                return net_ret(entry, float(df["Open"].iloc[k]) if float(df["Open"].iloc[k]) < stop else stop)
            if k == last:
                return net_ret(entry, float(df["Close"].iloc[k]))
        return None

    out = {}
    for hold in (20, 40, 60):
        top_rets, all_rets = [], []
        for usable_ym, rows in events.items():
            rows_sorted = sorted(rows, key=lambda x: x[1], reverse=True)
            top = {sid for sid, yoy, acc in rows_sorted[:30] if acc > 0}
            for sid, yoy, acc in rows:
                r = fwd_ret(sid, usable_ym, hold)
                if r is None:
                    continue
                all_rets.append(r)
                if sid in top:
                    top_rets.append(r)
        out[f"P2_PEAD_top30_hold{hold}"] = stats(top_rets)
        out[f"BASE_全部事件_hold{hold}"] = stats(all_rets)
    return out


# ---------------------------------------------------------------- main
def send_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    print(text)
    if token and chat_id:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": text}, timeout=15)
        except Exception:
            pass


def main():
    limit = int(os.environ.get("PROTO_LIMIT", "0") or 0)
    results = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "prototypes": {}}
    lines = ["🧪【候選策略原型實驗室】(每週自動重測, 事件研究口徑, 含成本)"]
    try:
        stocks, bench = load_data(limit)
        if bench is None or not stocks:
            raise RuntimeError("yfinance 資料下載失敗")
        lines.append(f"股票池 {len(stocks)} 檔 / 資料 2020-2026")
    except Exception as e:
        lines.append(f"⚠️ 資料載入失敗: {e}")
        results["error"] = str(e)
        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        send_telegram("\n".join(lines))
        return

    # P4 漲停隔日沖: 2026-10-24 使用者實務否決 (漲停鎖死日期貨買不到, 進場假設不成立), 自實驗室移除, 函數保留備查
    # 2026-10-24 剔除 (常設規則: 無效且無優化路徑者刪除): P1 RSI(2)/P1b (2,100 筆輸基準)、
    # P3 RS 線 (無增量價值; 其對照組已畢業為主系統 BRK20)、P4 漲停隔日沖 (期貨買不到已擱置)。
    for name, fn in [("P2", lambda: proto_pead(stocks))]:
        try:
            out = fn()
            results["prototypes"].update(out)
            for k, s in out.items():
                lines.append(fmt(k, s))
        except Exception as e:
            results["prototypes"][name] = {"error": str(e)}
            lines.append(f"{name}: ⚠️ 執行失敗 ({e})")
            traceback.print_exc()
        lines.append("")

    lines.append("判讀標準: 平均須勝過各自 BASE 對照, 且型態屬小賠大賺/小賠小賺才有資格進主系統候選。")
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
