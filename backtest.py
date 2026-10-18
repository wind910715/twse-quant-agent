import os
import json
import datetime
import requests
import numpy as np
import pandas as pd
import yfinance as yf
from screener import build_dynamic_index_universe, calc_rsi, calc_up_down_vol_ratio

BACKTEST_FILE = "latest_backtest.json"

def run_portfolio_backtest() -> dict:
    """
    針對【台灣50、中型100、富櫃50、富櫃200、MSCI成分股】執行近 90 日高勝率優化版回測：
    1. 濾網升級：大盤氣候濾網 + RS 強於大盤 + 近10日上漲/下跌量比 >= 1.20
    2. 進場優化：不追最高收盤價，採隔日回測 5MA/支撐低接確認買進
    3. 停利升級：達 1.2R (+3.5%~+4%) 先減碼 50% 鎖利，並立即將剩餘 50% 停損上移至【成本+0.6%稅費】保本！
    """
    universe = build_dynamic_index_universe()
    # 取上市櫃代表性成分股與 0050 大盤基準進行回測
    sample_items = list(universe.items())[:90]
    tickers = ["0050.TW"] + [f"{sid}{sfx}" for sid, (_, sfx) in sample_items]
    data = yf.download(tickers, period="8mo", group_by="ticker", threads=True, progress=False)

    bench_df = None
    if "0050.TW" in data.columns.get_level_values(0):
        bench_df = data["0050.TW"].dropna(subset=["Close"]).copy()
        bench_df["B_MA20"] = bench_df["Close"].rolling(20).mean()
        bench_df["B_RET10"] = bench_df["Close"].pct_change(10)

    trades = []
    for sid, (sname, sfx) in sample_items:
        t_sym = f"{sid}{sfx}"
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
        df["RET10"] = df["Close"].pct_change(10)

        start_idx = max(60, len(df) - 75)
        end_idx = len(df) - 6
        cooldown_until = -1

        for i in range(start_idx, end_idx):
            if i < cooldown_until:
                continue
            cur = df.iloc[i]
            prev = df.iloc[i - 1]
            if cur["MV20"] < 1200 or cur["Close"] < 25:
                continue

            # 1. 大盤氣候與 RS 相對強弱濾網
            dt_idx = df.index[i]
            if bench_df is not None and dt_idx in bench_df.index:
                b_row = bench_df.loc[dt_idx]
                # 排除大盤重挫破月線且個股未逆勢走強之時段
                if (b_row["Close"] < b_row["B_MA20"] * 0.985) and (cur["RET10"] < 0.02):
                    continue
                if cur["RET10"] < b_row["B_RET10"]:
                    continue

            # 2. 量價累積比濾網 (近10日上漲均量 / 下跌均量 >= 1.25)
            ud_ratio = calc_up_down_vol_ratio(df.iloc[i - 9:i + 1])
            if ud_ratio < 1.25:
                continue

            last3 = df.iloc[i - 2:i + 1]
            last5 = df.iloc[i - 4:i + 1]
            prev15 = df.iloc[i - 15:i]
            pre_amp_10d = (df.iloc[i - 10:i]["High"].max() - df.iloc[i - 10:i]["Low"].min()) / max(df.iloc[i - 10:i]["Low"].min(), 1)

            trend_up = (cur["Close"] >= cur["MA5"]) and (cur["Close"] > cur["MA20"] > cur["MA60"]) and \
                       (cur["MA20"] > df.iloc[i - 4]["MA20"]) and (cur["MA60"] >= df.iloc[i - 4]["MA60"])

            strat_hit = None
            limit_entry = float((cur["Close"] + cur["MA5"]) / 2.0)
            stop_p = float(cur["Low"] * 0.965)

            amp_5d = (last5["High"].max() - last5["Low"].min()) / max(last5["Low"].min(), 1)
            if trend_up and amp_5d <= 0.048 and (last3["vol_lots"] < 0.60 * cur["MV20"]).any() and (52 <= cur["RSI14"] <= 72):
                strat_hit = "S0_VCP收斂"
                limit_entry = float((cur["Close"] + cur["MA5"]) / 2.0)
                stop_p = float(min(last5["Low"].min(), cur["MA20"]) * 0.994)
            elif trend_up and (-0.008 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.02) and (cur["Close"] > cur["Open"]) and (cur["vol_lots"] > 1.25 * cur["MV5"]) and (cur["RSI14"] >= 54):
                strat_hit = "S1_口袋支點拉回"
                limit_entry = float(cur["Close"] * 0.995)
                stop_p = float(max(min(cur["Low"], cur["MA20"] * 0.992), limit_entry * 0.96))
            elif trend_up and (pre_amp_10d <= 0.11) and (cur["Close"] > prev15["High"].max()) and (1.4 * cur["MV20"] <= cur["vol_lots"] <= 3.0 * cur["MV20"]) and (60 <= cur["RSI14"] <= 74):
                strat_hit = "S2_主升段3浪"
                limit_entry = float((cur["Close"] + prev15["High"].max()) / 2.0)
                stop_p = float(max(cur["Low"], cur["MA10"]) * 0.992)
            elif (cur["MA60"] > df.iloc[i - 10]["MA60"]) and (df.iloc[i - 3:i]["Low"].min() < prev15["Low"].min()) and (cur["Close"] > prev15["Low"].min()) and (cur["Close"] > cur["Open"]):
                strat_hit = "S3_破底翻2B"
                limit_entry = float(cur["Close"] * 0.995)
                stop_p = float(df.iloc[i - 3:i]["Low"].min() * 0.995)
            elif trend_up and (pre_amp_10d <= 0.085) and (cur["Low"] > prev["High"] * 1.018) and (cur["vol_lots"] >= 1.8 * cur["MV20"]):
                strat_hit = "S4_威力跳空"
                limit_entry = float(cur["Low"] * 1.005)
                stop_p = float(prev["High"] * 0.995)

            if not strat_hit or limit_entry <= stop_p:
                continue

            # 檢查隔日或後2日是否成交於理想進場區間 (未開盤直接跳空破停損)
            next_bar = df.iloc[i + 1]
            if float(next_bar["Open"]) <= stop_p or float(next_bar["Low"]) > limit_entry * 1.008:
                continue
            actual_entry = min(float(next_bar["Open"]), limit_entry) if float(next_bar["Open"]) <= limit_entry else limit_entry

            risk_r = max(actual_entry - stop_p, actual_entry * 0.022)
            # TP1 設於 1.2R (約 +3.2%~+4.2%) 鎖定高勝率；TP2 設於 2.8R 抓大波段
            tp1_p = actual_entry + 1.2 * risk_r
            tp2_p = actual_entry + 2.8 * risk_r

            future_bars = df.iloc[i + 1:min(i + 16, len(df))]
            hit_tp1 = False
            hit_tp2 = False
            realized_pct = 0.0
            rem_weight = 1.0
            trail_stop = stop_p

            for _, fbar in future_bars.iterrows():
                # 先檢查是否達標 TP1 (減碼 50%，並將停損上移至成本+0.6%完整覆蓋稅費)
                if not hit_tp1 and float(fbar["High"]) >= tp1_p:
                    hit_tp1 = True
                    realized_pct += 0.50 * ((tp1_p - actual_entry) / actual_entry)
                    rem_weight -= 0.50
                    trail_stop = actual_entry * 1.006  # 確保剩餘部位出場也是正報酬

                if hit_tp1 and not hit_tp2 and float(fbar["High"]) >= tp2_p:
                    hit_tp2 = True
                    realized_pct += 0.30 * ((tp2_p - actual_entry) / actual_entry)
                    rem_weight -= 0.30
                    trail_stop = max(trail_stop, float(fbar["MA10"]))

                if float(fbar["Low"]) <= trail_stop:
                    realized_pct += rem_weight * ((trail_stop - actual_entry) / actual_entry)
                    rem_weight = 0.0
                    break

            if rem_weight > 0 and not future_bars.empty:
                last_c = float(future_bars.iloc[-1]["Close"])
                realized_pct += rem_weight * ((last_c - actual_entry) / actual_entry)

            net_ret_pct = round((realized_pct - 0.005) * 100, 2)
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
        return {"total_trades": 0, "win_rate": 0.0, "rr_ratio": 0.0, "mdd_pct": 0.0, "cum_return_pct": 0.0, "by_strategy": {}}

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
        "universe_scope": "台灣50 + 中型100 + 富櫃50 + 富櫃200 + MSCI成分股",
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
        f"📈【週六 09:00 自動回測績效健檢報告｜{datetime.date.today().strftime('%Y-%m-%d')}】",
        f"• 標的範圍：{summary.get('universe_scope', '五大指數成分股')} (每季自動調整)",
        f"• 總交易筆數：{summary['total_trades']} 筆 (嚴選高勝率訊號)",
        f"• 整體真實勝率：{summary['win_rate']}%",
        f"• 平均盈虧比：{summary['rr_ratio']} : 1",
        f"• 組合累計報酬率：{summary['cum_return_pct']:+.2f}%",
        f"• 最大回撤 (MDD)：{summary['mdd_pct']:.2f}%",
        "\n📊【各策略細項表現】："
    ]
    for k, v in summary.get("by_strategy", {}).items():
        lines.append(f" - {k}：勝率 {v['win_rate']}% ({v['trades']}筆)｜單筆平均 {v['avg_return_pct']:+.2f}%")

    # 若有設定 GEMINI_API_KEY，自動附加上 Gemini 對本週策略權重的微調建議
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"請根據以下台股五大策略最新回測數據，用 3 句話給主人下週操作重點與資金配置建議：{json.dumps(summary, ensure_ascii=False)}"
            )
            if resp and resp.text:
                lines.append(f"\n💡【Gemini 操盤診斷】：\n{resp.text.strip()}")
        except Exception:
            pass

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    text = "\n".join(lines)
    print(text)
    if token and chat_id:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=10)

if __name__ == "__main__":
    res = run_portfolio_backtest()
    send_weekly_backtest_tg(res)
