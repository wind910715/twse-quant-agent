import os
import json
import datetime
import requests
import numpy as np
import pandas as pd
import yfinance as yf
from screener import CORE_POOL, calc_rsi

BACKTEST_FILE = "latest_backtest.json"

def run_portfolio_backtest() -> dict:
    """針對核心標的池執行近 90 個交易日滾動回測，驗證五大策略之勝率、盈虧比與最大回撤(MDD)"""
    tickers = [f"{sid}.TW" for sid in CORE_POOL.keys()]
    data = yf.download(tickers, period="8mo", group_by="ticker", threads=True, progress=False)

    trades = []
    for sid, sname in CORE_POOL.items():
        t_sym = f"{sid}.TW"
        if t_sym not in data.columns.get_level_values(0):
            continue
        df = data[t_sym].dropna(subset=["Close", "High", "Low", "Open", "Volume"]).copy()
        if len(df) < 100:
            continue

        df["vol_lots"] = df["Volume"] / 1000.0
        for p in [5, 10, 20, 60]:
            df[f"MA{p}"] = df["Close"].rolling(p).mean()
        df["MV5"] = df["vol_lots"].rolling(5).mean()
        df["MV20"] = df["vol_lots"].rolling(20).mean()
        df["RSI14"] = calc_rsi(df["Close"], 14)

        # 回測最近 60 個交易日之訊號表現 (預留往後 15 日觀察出場結果)
        start_idx = max(60, len(df) - 75)
        end_idx = len(df) - 5
        cooldown_until = -1

        for i in range(start_idx, end_idx):
            if i < cooldown_until:
                continue
            cur = df.iloc[i]
            prev = df.iloc[i - 1]
            last3 = df.iloc[i - 2:i + 1]
            last5 = df.iloc[i - 4:i + 1]
            prev20 = df.iloc[i - 20:i]

            if cur["MV20"] < 1000 or cur["Close"] < 20:
                continue

            trend_up = (cur["Close"] > cur["MA20"] > cur["MA60"])
            strat_hit = None
            entry_p = float(cur["Close"])
            stop_p = float(cur["Low"] * 0.96)

            # 檢驗五大策略觸發
            amp_5d = (last5["High"].max() - last5["Low"].min()) / max(last5["Low"].min(), 1)
            if trend_up and amp_5d <= 0.055 and (last3["vol_lots"] < 0.65 * cur["MV20"]).any():
                strat_hit = "S0_VCP收斂"
                stop_p = float(last5["Low"].min() * 0.995)
            elif trend_up and (-0.01 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.025) and (cur["Close"] > cur["Open"]) and (cur["vol_lots"] > 1.2 * cur["MV5"]):
                strat_hit = "S1_口袋支點拉回"
                stop_p = float(min(cur["Low"], cur["MA20"]) * 0.99)
            elif trend_up and (cur["Close"] > prev20["High"].max()) and (cur["vol_lots"] >= 1.5 * cur["MV20"]) and (58 <= cur["RSI14"] <= 78):
                strat_hit = "S2_主升段3浪"
                stop_p = float((cur["Open"] + cur["Close"]) / 2.0)
            elif (cur["Close"] > cur["MA60"] * 0.95) and ( df.iloc[i-3:i]["Low"].min() < prev20["Low"].min() ) and (cur["Close"] > prev20["Low"].min()):
                strat_hit = "S3_破底翻2B"
                stop_p = float(df.iloc[i-3:i]["Low"].min() * 0.995)
            elif (cur["Low"] > prev["High"] * 1.02) and (cur["vol_lots"] >= 2.0 * cur["MV20"]):
                strat_hit = "S4_威力跳空"
                stop_p = float(cur["Low"] * 0.995)

            if not strat_hit or entry_p <= stop_p:
                continue

            risk_r = max(entry_p - stop_p, entry_p * 0.02)
            tp1_p = entry_p + 1.8 * risk_r
            tp2_p = entry_p + 3.0 * risk_r

            # 模擬未來 15 個交易日之分批停利與停損結果 (40% TP1 + 30% TP2 + 30% 尾倉)
            future_bars = df.iloc[i + 1:min(i + 16, len(df))]
            hit_tp1 = False
            hit_tp2 = False
            realized_pct = 0.0
            rem_weight = 1.0
            trail_stop = stop_p

            for _, fbar in future_bars.iterrows():
                if fbar["Low"] <= trail_stop:
                    realized_pct += rem_weight * ((trail_stop - entry_p) / entry_p)
                    rem_weight = 0.0
                    break
                if not hit_tp1 and fbar["High"] >= tp1_p:
                    hit_tp1 = True
                    realized_pct += 0.40 * ((tp1_p - entry_p) / entry_p)
                    rem_weight -= 0.40
                    trail_stop = entry_p  # 觸發 TP1 後上移至保本價
                if hit_tp1 and not hit_tp2 and fbar["High"] >= tp2_p:
                    hit_tp2 = True
                    realized_pct += 0.30 * ((tp2_p - entry_p) / entry_p)
                    rem_weight -= 0.30
                    trail_stop = float(fbar["MA10"])

            if rem_weight > 0 and not future_bars.empty:
                last_c = float(future_bars.iloc[-1]["Close"])
                realized_pct += rem_weight * ((last_c - entry_p) / entry_p)

            net_ret_pct = round((realized_pct - 0.005) * 100, 2)  # 扣除 0.5% 稅費摩擦成本
            trades.append({
                "stock_id": sid,
                "strategy": strat_hit,
                "entry_date": df.index[i].strftime("%Y-%m-%d"),
                "net_return_pct": net_ret_pct,
                "win": 1 if net_ret_pct > 0 else 0
            })
            cooldown_until = i + 8

    df_t = pd.DataFrame(trades)
    if df_t.empty:
        return {"total_trades": 0, "win_rate": 0.0, "mdd_pct": 0.0, "cum_return_pct": 0.0, "by_strategy": {}}

    # 以單筆投入 15% 資金計算組合權益曲線與最大回撤 (MDD)
    df_t = df_t.sort_values("entry_date").reset_index(drop=True)
    df_t["port_impact"] = df_t["net_return_pct"] * 0.15
    equity_curve = 100.0 * (1.0 + df_t["port_impact"] / 100.0).cumprod()
    peak = equity_curve.cummax()
    dd = ((equity_curve - peak) / peak) * 100.0
    mdd_pct = round(float(dd.min()), 2)
    cum_ret_pct = round(float(equity_curve.iloc[-1] - 100.0), 2)

    wins = df_t[df_t["net_return_pct"] > 0]["net_return_pct"]
    losses = df_t[df_t["net_return_pct"] <= 0]["net_return_pct"]
    avg_win = float(wins.mean()) if not wins.empty else 0.0
    avg_loss = abs(float(losses.mean())) if not losses.empty else 1.0
    rr_ratio = round(avg_win / max(avg_loss, 0.1), 2)

    by_strat = {}
    for s_name, grp in df_t.groupby("strategy"):
        by_strat[s_name] = {
            "trades": int(len(grp)),
            "win_rate": round(float(grp["win"].mean() * 100), 1),
            "avg_return_pct": round(float(grp["net_return_pct"].mean()), 2)
        }

    summary = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_trades": int(len(df_t)),
        "win_rate": round(float(df_t["win"].mean() * 100), 1),
        "rr_ratio": rr_ratio,
        "cum_return_pct": cum_ret_pct,
        "mdd_pct": mdd_pct,
        "by_strategy": by_strat
    }
    with open(BACKTEST_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary

def send_weekly_backtest_tg(summary: dict):
    lines = [
        f"📈【週六自動回測績效健檢報告｜{datetime.date.today().strftime('%Y-%m-%d')}】",
        f"• 回測樣本：近 90 日核心高流動性標的池 (含分批停利與 0.5% 稅費)",
        f"• 總交易筆數：{summary['total_trades']} 筆",
        f"• 整體真實勝率：{summary['win_rate']}%",
        f"• 平均盈虧比：{summary['rr_ratio']} : 1",
        f"• 組合累計報酬率：{summary['cum_return_pct']:+.2f}%",
        f"• 最大回撤 (MDD)：{summary['mdd_pct']:.2f}%",
        "\n📊【各策略細項表現】："
    ]
    for k, v in summary.get("by_strategy", {}).items():
        lines.append(f" - {k}：勝率 {v['win_rate']}% ({v['trades']}筆)｜單筆平均 {v['avg_return_pct']:+.2f}%")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    text = "\n".join(lines)
    print(text)
    if token and chat_id:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=10)

if __name__ == "__main__":
    res = run_portfolio_backtest()
    send_weekly_backtest_tg(res)
