"""factor_rotation.py — 月營收 + RS 雙因子月輪動名單 (每月自動產出, 只產名單不下單)
驗證依據 (2026-10-23, 本機 33 期): 雙因子 (營收年增百分位 + RS120 百分位 取平均) 前 30 等權月輪動
累計 +308.9% vs 全體等權 +213.6%, 期均超額 +1.48%, 勝期 72%, 三年皆正超額。
凍結規格: 每月 11 日 (營收公告截止後) 以當時已公告的最新月營收計年增率; RS = 近 120 交易日報酬;
兩者百分位 (0-100) 平均取前 30。universe 與 screener 相同 (動態指數成分)。
執行節奏: workflow 於每月 11 日的平日晚間 run 執行 (11 日逢週末則順延至週一); 也可手動執行。
輸出: factor_rotation.json + Telegram 名單。使用者看名單自行決定是否跟單, 本程式不下單。
"""
import os
import json
import traceback
import urllib.request
from datetime import datetime

import pandas as pd
import requests
import yfinance as yf

try:
    from screener import build_dynamic_index_universe
except Exception:
    from strategy_core import build_dynamic_index_universe  # type: ignore

RESULTS_FILE = "factor_rotation.json"
TOP_N = 30


def send_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("(no TG env, skip)")
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print("TG failed:", e)


def latest_revenue_ym(now: datetime) -> int:
    """point-in-time: 營收於次月 10 日前公告。now.day>=11 -> 上個月已全數公告; 否則用上上個月。回傳 ym = year*12+month"""
    y, m = now.year, now.month
    back = 1 if now.day >= 11 else 2
    m -= back
    while m <= 0:
        m += 12
        y -= 1
    return y * 12 + m


def fetch_revenue_yoy(sid: str, token: str, target_ym: int):
    start = f"{(target_ym - 14) // 12}-{(target_ym - 14) % 12 + 1:02d}-01"
    params = {"dataset": "TaiwanStockMonthRevenue", "data_id": sid, "start_date": start}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get("https://api.finmindtrade.com/api/v4/data", params=params, headers=headers, timeout=30)
    rows = r.json().get("data", [])
    if not rows:
        return None
    rev = {}
    for row in rows:
        rev[int(row["revenue_year"]) * 12 + int(row["revenue_month"])] = float(row["revenue"])
    cur, prev = rev.get(target_ym), rev.get(target_ym - 12)
    if not cur or not prev:
        return None
    return (cur / prev - 1) * 100


def main():
    now = datetime.now()
    target_ym = latest_revenue_ym(now)
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    universe = build_dynamic_index_universe()
    print(f"universe={len(universe)} target revenue ym={target_ym}", flush=True)

    # RS120
    tickers = [f"{sid}{sfx}" for sid, (_, sfx) in universe.items()]
    data = yf.download(tickers, period="8mo", group_by="ticker", threads=True, progress=False)
    rs = {}
    for t in tickers:
        try:
            df = data[t].dropna(subset=["Close"])
        except Exception:
            continue
        if len(df) >= 121:
            rs[t.split(".")[0]] = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-121]) - 1) * 100
    print(f"RS ok: {len(rs)}", flush=True)

    # 營收年增
    yoy = {}
    for sid in universe:
        try:
            v = fetch_revenue_yoy(sid, token, target_ym)
        except Exception:
            v = None
        if v is not None:
            yoy[sid] = v
    print(f"YoY ok: {len(yoy)}", flush=True)

    both = [sid for sid in universe if sid in rs and sid in yoy]
    df = pd.DataFrame({"sid": both,
                       "yoy": [yoy[s] for s in both],
                       "rs": [rs[s] for s in both]})
    df["pct_yoy"] = df["yoy"].rank(pct=True) * 100
    df["pct_rs"] = df["rs"].rank(pct=True) * 100
    df["combo"] = (df["pct_yoy"] + df["pct_rs"]) / 2
    df = df.sort_values("combo", ascending=False).reset_index(drop=True)
    picks = []
    for _, row in df.head(TOP_N).iterrows():
        sid = row["sid"]
        picks.append({"sid": sid, "name": universe[sid][0],
                      "yoy_pct": round(float(row["yoy"]), 1), "rs120_pct": round(float(row["rs"]), 1),
                      "combo": round(float(row["combo"]), 1)})

    prev_picks = []
    if os.path.exists(RESULTS_FILE):
        try:
            prev_picks = [p["sid"] for p in json.load(open(RESULTS_FILE)).get("picks", [])]
        except Exception:
            prev_picks = []
    new_sids = [p["sid"] for p in picks]
    added = [s for s in new_sids if s not in prev_picks]
    dropped = [s for s in prev_picks if s not in new_sids]

    out = {"generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
           "revenue_month": f"{target_ym // 12}-{target_ym % 12 + 1:02d}" if target_ym % 12 else f"{target_ym // 12 - 1}-12",
           "universe_n": len(both), "picks": picks, "added": added, "dropped": dropped}
    # revenue_month 修正: ym = year*12+month, month = ym - (ym//12)*12; ym%12==0 代表 12 月
    mm = target_ym - (target_ym // 12) * 12
    out["revenue_month"] = f"{target_ym // 12}-{mm:02d}" if mm else f"{target_ym // 12 - 1}-12"
    with open(RESULTS_FILE, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    lines = [f"📋 雙因子月輪動名單 ({out['revenue_month']} 營收, 前 {TOP_N} 檔等權)"]
    lines.append(" / ".join(f"{p['sid']} {p['name']}" for p in picks[:10]))
    lines.append(" / ".join(f"{p['sid']} {p['name']}" for p in picks[10:20]))
    lines.append(" / ".join(f"{p['sid']} {p['name']}" for p in picks[20:]))
    if prev_picks:
        lines.append(f"新進 {len(added)}: {' '.join(added) if added else '無'}")
        lines.append(f"剔除 {len(dropped)}: {' '.join(dropped) if dropped else '無'}")
    lines.append("驗證: 2024-2026 累計 +308.9% vs 全體 +213.6% (雙因子百分位平均, 每月輪動)。名單僅供參考, 是否跟單由你決定。")
    send_telegram("\n".join(lines))
    print("\n".join(lines))
    print("DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
