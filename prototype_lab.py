#!/usr/bin/env python3
"""prototype_lab.py — 候選策略原型實驗室 (v1, 2026-10-23)

在週六回測 job 中於 backtest.py 之後執行, 結果寫 prototype_results.json 並發 Telegram。
候選 (標準: 只接受「小賠大賺」與「小賠小賺」, 期望值須勝過同期基準):
  P1  Connors RSI(2) 均值回歸 — 趨勢上 (Close>MA200) + RSI(2)<10, 隔日開盤進,
      站回 MA5 出場, 外加硬停損 -5% (原版無停損, 不符小賠標準故補上), 最長 10 日。
  P1b 布林穿透變體 — 觸發改為收盤跌破布林下軌 (20, 2σ), 其餘同 P1 (A/B 對照)。
  P2  PEAD 營收驚喜漂移 — FinMind 月營收 (point-in-time: 營收月 M 於 M+1 月 11 日後可用),
      每月以年增率排名取前 30 且年增加速者, 持有 20/40/60 日, 硬停損 -8%。
  P3  RS 線突破 — RS (個股/0050) 創 60 日新高 + 股價突破 20 日高 + 量增, 隔日確認進場,
      跌破 MA10 出場, 硬停損 -5%, 最長 25 日; 對照組為無 RS 條件的同型突破。
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
    data = yf.download(tickers, period="2y", group_by="ticker", threads=True, progress=False)
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


# ---------------------------------------------------------------- P1 RSI(2)
def sim_mean_reversion(df: pd.DataFrame, trig: pd.Series, max_hold: int = 10) -> list:
    """trig: 布林 Series (訊號日)。隔日開盤進, 站回 MA5 出, 硬停損 -5%, 最長 max_hold 日。同股不重疊。"""
    close, open_, low = df["Close"], df["Open"], df["Low"]
    ma5 = close.rolling(5).mean()
    rets = []
    i = 0
    n = len(df)
    while i < n - 2:
        if not bool(trig.iloc[i]):
            i += 1
            continue
        j = i + 1
        if float(open_.iloc[j]) <= float(close.iloc[i]) * 0.905:  # 跌停鎖死近似: 隔日開盤近跌停, 視為買不到
            i += 1
            continue
        entry = float(open_.iloc[j])
        stop = entry * 0.95
        exit_px = None
        last_k = min(j + max_hold, n - 1)
        k = j
        while k <= last_k:
            if float(low.iloc[k]) <= stop:
                exit_px = float(open_.iloc[k]) if float(open_.iloc[k]) < stop else stop
                break
            if float(close.iloc[k]) > float(ma5.iloc[k]):
                exit_px = float(open_.iloc[k + 1]) if k + 1 < n else float(close.iloc[k])
                k = min(k + 1, n - 1)
                break
            if k == last_k:
                exit_px = float(close.iloc[k])
                break
            k += 1
        if exit_px is not None:
            rets.append(net_ret(entry, exit_px))
            i = k + 1
        else:
            i += 1
    return rets


def proto_rsi2(stocks: dict) -> dict:
    p1, p1b, base = [], [], []
    for sid, df in stocks.items():
        if len(df) < 230:
            continue
        d = df.copy()
        d["MA200"] = d["Close"].rolling(200).mean()
        d["MA20"] = d["Close"].rolling(20).mean()
        d["STD20"] = d["Close"].rolling(20).std()
        d["RSI2"] = calc_rsi(d["Close"], 2)
        trend = d["Close"] > d["MA200"]
        trig1 = (trend & (d["RSI2"] < 10)).fillna(False)
        lower = d["MA20"] - 2 * d["STD20"]
        trig1b = (trend & (d["Close"] < lower)).fillna(False)
        p1.extend(sim_mean_reversion(d, trig1))
        p1b.extend(sim_mean_reversion(d, trig1b))
        # 基準: 同趨勢條件下任一日的 5 日持有 (無停損), 每 5 日取樣避免重疊膨脹
        cl, op = d["Close"], d["Open"]
        for i in range(210, len(d) - 6, 5):
            if bool(trend.iloc[i]):
                base.append(net_ret(float(op.iloc[i + 1]), float(cl.iloc[i + 5])))
    return {"P1_RSI2": stats(p1), "P1b_BB穿透": stats(p1b), "BASE_趨勢內5日持有": stats(base)}


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


# ---------------------------------------------------------------- P3 RS 線突破
def proto_rs(stocks: dict, bench: pd.DataFrame) -> dict:
    bclose = bench["Close"]
    rs_rets, plain_rets = [], []
    for sid, df in stocks.items():
        if len(df) < 130:
            continue
        d = df.copy()
        aligned = bclose.reindex(d.index).ffill()
        rs = d["Close"] / aligned
        rs_high60 = rs.rolling(60).max()
        prev20_high = d["High"].shift(1).rolling(20).max()
        ma60 = d["Close"].rolling(60).mean()
        ma10 = d["Close"].rolling(10).mean()
        mv20 = d["Volume"].rolling(20).mean()
        n = len(d)
        i = 61
        while i < n - 2:
            breakout = (float(d["Close"].iloc[i]) > float(prev20_high.iloc[i])
                        and float(d["Volume"].iloc[i]) >= 1.5 * float(mv20.iloc[i])
                        and float(d["Close"].iloc[i]) > float(ma60.iloc[i]))
            if not breakout:
                i += 1
                continue
            is_rs = float(rs.iloc[i]) >= float(rs_high60.iloc[i]) - 1e-12
            j = i + 1
            stop0 = float(d["Low"].iloc[i]) * 0.995
            if float(d["Open"].iloc[j]) > float(d["Close"].iloc[i]) * 1.028 or float(d["Open"].iloc[j]) <= stop0:
                i += 1
                continue
            trigger = max(float(d["Close"].iloc[i]), float(d["Open"].iloc[j]) * 1.003)
            if float(d["High"].iloc[j]) < trigger or float(d["Close"].iloc[j]) < float(d["Open"].iloc[j]) * 0.998:
                i += 1
                continue
            entry = trigger
            stop = max(stop0, entry * 0.95)
            exit_px, last_k = None, min(j + 25, n - 1)
            k = j
            while k <= last_k:
                if float(d["Low"].iloc[k]) <= stop:
                    exit_px = float(d["Open"].iloc[k]) if float(d["Open"].iloc[k]) < stop else stop
                    break
                if float(d["Close"].iloc[k]) < float(ma10.iloc[k]) and k > j:
                    exit_px = float(d["Open"].iloc[k + 1]) if k + 1 < n else float(d["Close"].iloc[k])
                    k = min(k + 1, n - 1)
                    break
                if k == last_k:
                    exit_px = float(d["Close"].iloc[k])
                    break
                k += 1
            if exit_px is not None:
                (rs_rets if is_rs else plain_rets).append(net_ret(entry, exit_px))
                i = k + 1
            else:
                i += 1
    return {"P3_RS線突破": stats(rs_rets), "P3對照_無RS突破": stats(plain_rets)}


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
        lines.append(f"股票池 {len(stocks)} 檔 / 資料 2 年")
    except Exception as e:
        lines.append(f"⚠️ 資料載入失敗: {e}")
        results["error"] = str(e)
        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        send_telegram("\n".join(lines))
        return

    for name, fn in [("P1", lambda: proto_rsi2(stocks)),
                     ("P2", lambda: proto_pead(stocks)),
                     ("P3", lambda: proto_rs(stocks, bench))]:
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
