import os
import json
import datetime
import requests
import numpy as np
import pandas as pd
import yfinance as yf
from screener import build_dynamic_index_universe, calc_rsi, calc_up_down_vol_ratio

BACKTEST_FILE = "latest_backtest.json"

ALL_STRATEGIES = [
    "S0_VCP收斂",
    "S1_口袋支點拉回",
    "S2_主升段3浪",
    "S3_破底翻2B",
    "S4_威力跳空",
    "S5_法人量價共振"
]

def simulate_pct_trailing_trade(df: pd.DataFrame, i: int, actual_entry: float, raw_stop: float) -> tuple[float, int]:
    """
    【分 % 數階梯停利 ＋ 最高價回落動態移動停利 (Peak-Pullback Trailing Stop) 模擬引擎】：
    - 初始硬性停損：結構支撐 (最寬上限 -4.2%)
    - 漲達 +3.2%：啟動保本移動停利，防守線拉升至 買進價 * 1.010 (扣除0.5%稅費後必賺)
    - 漲達 +5.5% (TP1)：先減碼 30% 落袋，剩餘 70% 啟動【最高價回落 3.0% (Peak * 0.970)】移動停利
    - 漲達 +11.0% (TP2)：再減碼 30% 落袋，剩餘 40% 啟動【最高價回落 3.5% (Peak * 0.965) 或 10MA】移動停利
    - 漲達 +18.0% (TP3)：再減碼 20% 落袋，最後 20% 尾倉沿【最高價回落 4.0% (Peak * 0.960) 或 10MA】抱滿波段
    """
    stop_p = max(raw_stop, actual_entry * (1.0 - 0.042))
    be_trigger = actual_entry * 1.032
    tp1_p = actual_entry * 1.055
    tp2_p = actual_entry * 1.110
    tp3_p = actual_entry * 1.180

    future_bars = df.iloc[i + 1:min(i + 26, len(df))]
    highest_peak = actual_entry
    active_trail_stop = stop_p

    hit_be = False
    hit_tp1 = False
    hit_tp2 = False
    hit_tp3 = False
    realized_pct = 0.0
    rem_weight = 1.0

    for _, fbar in future_bars.iterrows():
        f_open = float(fbar["Open"])
        f_high = float(fbar["High"])
        f_low = float(fbar["Low"])
        f_close = float(fbar["Close"])
        f_ma10 = float(fbar["MA10"])

        # 1. 若開盤直接跳空跌破當前移動停利/停損線，以開盤價出場
        if f_open <= active_trail_stop:
            realized_pct += rem_weight * ((f_open - actual_entry) / actual_entry)
            rem_weight = 0.0
            break

        # 2. 更新波段最高價 (High-Water Mark) 並檢驗分 % 數停利階梯
        if f_high > highest_peak:
            highest_peak = f_high

        # 階梯 A：觸及 +3.2% 啟動保本移動停利
        if not hit_be and highest_peak >= be_trigger:
            hit_be = True
            active_trail_stop = max(active_trail_stop, actual_entry * 1.010)

        # 階梯 B：觸及 +5.5% (TP1) 減碼 30%，啟動最高價回落 3.0% 移動停利
        if not hit_tp1 and highest_peak >= tp1_p:
            hit_tp1 = True
            realized_pct += 0.30 * 0.055
            rem_weight = round(rem_weight - 0.30, 2)
            active_trail_stop = max(active_trail_stop, actual_entry * 1.022, highest_peak * 0.970)

        # 階梯 C：觸及 +11.0% (TP2) 再減碼 30%，啟動最高價回落 3.5% 與 10MA 移動停利
        if hit_tp1 and not hit_tp2 and highest_peak >= tp2_p:
            hit_tp2 = True
            realized_pct += 0.30 * 0.110
            rem_weight = round(rem_weight - 0.30, 2)
            active_trail_stop = max(active_trail_stop, actual_entry * 1.065, highest_peak * 0.965, f_ma10)

        # 階梯 D：觸及 +18.0% (TP3) 再減碼 20%，最後 20% 尾倉沿最高價回落 4.0% 與 10MA 移動停利
        if hit_tp2 and not hit_tp3 and highest_peak >= tp3_p:
            hit_tp3 = True
            realized_pct += 0.20 * 0.180
            rem_weight = round(rem_weight - 0.20, 2)
            active_trail_stop = max(active_trail_stop, actual_entry * 1.120, highest_peak * 0.960, f_ma10)

        # 隨最高價持續墊高，動態上推移動停利線 (只上不下)
        if hit_tp3:
            active_trail_stop = max(active_trail_stop, highest_peak * 0.960, f_ma10)
        elif hit_tp2:
            active_trail_stop = max(active_trail_stop, highest_peak * 0.965, f_ma10)
        elif hit_tp1:
            active_trail_stop = max(active_trail_stop, highest_peak * 0.970)

        # 3. 檢查當日是否觸發移動停利或硬性停損
        # 若當日尚未觸發保本，盘中跌破硬性停損即出場；若已啟動移動停利，收盤跌破或盤中反轉跌破移動停利線即獲利出場
        if not hit_be and f_low <= active_trail_stop:
            realized_pct += rem_weight * ((active_trail_stop - actual_entry) / actual_entry)
            rem_weight = 0.0
            break
        elif hit_be and (f_close <= active_trail_stop or f_low <= active_trail_stop * 0.992):
            exit_p = active_trail_stop if f_low <= active_trail_stop else f_close
            realized_pct += rem_weight * ((exit_p - actual_entry) / actual_entry)
            rem_weight = 0.0
            break

    if rem_weight > 0 and not future_bars.empty:
        last_c = float(future_bars.iloc[-1]["Close"])
        realized_pct += rem_weight * ((last_c - actual_entry) / actual_entry)

    net_ret_pct = round((realized_pct - 0.005) * 100, 2)
    return net_ret_pct, (1 if net_ret_pct > 0 else 0)

def run_portfolio_backtest() -> dict:
    universe = build_dynamic_index_universe()
    sample_items = list(universe.items())[:140]
    tickers = ["0050.TW"] + [f"{sid}{sfx}" for sid, (_, sfx) in sample_items]
    data = yf.download(tickers, period="8mo", group_by="ticker", threads=True, progress=False)

    bench_df = None
    if "0050.TW" in data.columns.get_level_values(0):
        bench_df = data["0050.TW"].dropna(subset=["Close"]).copy()
        bench_df["B_MA20"] = bench_df["Close"].rolling(20).mean()
        bench_df["B_RET15"] = bench_df["Close"].pct_change(15)

    trades = []
    for sid, (sname, sfx) in sample_items:
        t_sym = f"{sid}{sfx}"
        if t_sym not in data.columns.get_level_values(0):
            continue
        df = data[t_sym].dropna(subset=["Close", "High", "Low", "Open", "Volume"]).copy()
        if len(df) < 105:
            continue

        df["vol_lots"] = df["Volume"] / 1000.0
        for p in [5, 10, 20, 60]:
            df[f"MA{p}"] = df["Close"].rolling(p).mean()
        df["MV5"] = df["vol_lots"].rolling(5).mean()
        df["MV20"] = df["vol_lots"].rolling(20).mean()
        df["RSI14"] = calc_rsi(df["Close"], 14)
        df["RET15"] = df["Close"].pct_change(15)

        ema12 = df["Close"].ewm(span=12, adjust=False).mean()
        ema26 = df["Close"].ewm(span=26, adjust=False).mean()
        df["DIF"] = ema12 - ema26
        df["MACD"] = df["DIF"].ewm(span=9, adjust=False).mean()
        df["MACD_HIST"] = df["DIF"] - df["MACD"]

        start_idx = max(60, len(df) - 85)
        end_idx = len(df) - 8
        cooldown_by_strat = {s: -1 for s in ALL_STRATEGIES}

        for i in range(start_idx, end_idx):
            cur = df.iloc[i]
            prev = df.iloc[i - 1]
            if pd.isna(cur["MA60"]) or cur["MV20"] < 1000 or cur["Close"] < 25:
                continue

            dt_idx = df.index[i]
            rs_ok = True
            market_ok = True
            if bench_df is not None and dt_idx in bench_df.index:
                b_row = bench_df.loc[dt_idx]
                if (b_row["Close"] < b_row["B_MA20"] * 0.985) and (cur["RET15"] < 0.01):
                    market_ok = False
                if cur["RET15"] < b_row["B_RET15"]:
                    rs_ok = False

            ud_ratio = calc_up_down_vol_ratio(df.iloc[i - 9:i + 1])
            last3 = df.iloc[i - 2:i + 1]
            last5 = df.iloc[i - 4:i + 1]
            prev15 = df.iloc[i - 15:i]

            trend_up = (cur["Close"] > cur["MA20"] > cur["MA60"]) and \
                       (cur["MA20"] > df.iloc[i - 4]["MA20"]) and (cur["MA60"] >= df.iloc[i - 4]["MA60"])

            triggered_list = []

            # S0: VCP 右側量縮收斂
            amp_5d = (last5["High"].max() - last5["Low"].min()) / max(last5["Low"].min(), 1)
            if market_ok and rs_ok and ud_ratio >= 1.25 and trend_up and (cur["Close"] >= cur["MA5"]) and \
               (amp_5d <= 0.050) and (last3["vol_lots"] < 0.62 * cur["MV20"]).any() and (52 <= cur["RSI14"] <= 72):
                triggered_list.append(("S0_VCP收斂", float((cur["Close"] + cur["MA5"]) / 2.0), float(min(last5["Low"].min(), cur["MA20"]) * 0.994), "pullback"))

            # S1: 口袋支點拉回 (回測 10MA/20MA 量縮後出量紅K)
            near_ma = (-0.010 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.022) or \
                      (-0.008 <= (cur["Close"] - cur["MA20"]) / cur["MA20"] <= 0.020)
            if market_ok and rs_ok and ud_ratio >= 1.25 and trend_up and near_ma and \
               (cur["Close"] > cur["Open"]) and (cur["vol_lots"] > 1.25 * cur["MV5"]) and (53 <= cur["RSI14"] <= 72):
                triggered_list.append(("S1_口袋支點拉回", float(cur["Close"] * 0.996), float(max(min(cur["Low"], cur["MA20"]) * 0.992, cur["Close"] * 0.958)), "pullback"))

            # S2: 主升段第 3 浪突破
            if market_ok and rs_ok and ud_ratio >= 1.25 and trend_up and (cur["Close"] > prev15["High"].max()) and \
               (1.4 * cur["MV20"] <= cur["vol_lots"] <= 3.2 * cur["MV20"]) and (58 <= cur["RSI14"] <= 75) and \
               (cur["MACD_HIST"] > prev["MACD_HIST"] > 0):
                triggered_list.append(("S2_主升段3浪", float(cur["Close"]), float(max((cur["Open"] + cur["Close"]) / 2.0, cur["MA10"]) * 0.992), "momentum"))

            # S3: 威科夫破底翻 2B (多頭趨勢中假跌破前低後帶量長紅站回 10MA)
            sup_12 = df.iloc[i - 13:i - 1]["Low"].min()
            low_2d = df.iloc[i - 2:i + 1]["Low"].min()
            had_spring = (sup_12 * 0.965 <= low_2d < sup_12 * 0.998)
            reclaim_ok = (cur["Close"] > sup_12) and (cur["Close"] >= cur["MA10"]) and (cur["Close"] > cur["Open"] * 1.015) and (cur["vol_lots"] >= 1.2 * cur["MV5"])
            if market_ok and trend_up and had_spring and reclaim_ok and (ud_ratio >= 1.20) and (50 <= cur["RSI14"] <= 68):
                triggered_list.append(("S3_破底翻2B", float(cur["Close"] * 0.996), float(max(low_2d * 0.994, cur["Close"] * 0.958)), "pullback"))

            # S4: 威力跳空缺口
            candle_pos = (cur["Close"] - cur["Low"]) / max(cur["High"] - cur["Low"], 0.01)
            has_gap = (cur["Open"] > prev["Close"] * 1.015) and (cur["Low"] > prev["High"]) and (cur["Close"] >= prev15["High"].max() * 0.99)
            if market_ok and rs_ok and trend_up and has_gap and (candle_pos >= 0.65) and (cur["vol_lots"] >= 1.6 * cur["MV20"]) and (ud_ratio >= 1.25):
                triggered_list.append(("S4_威力跳空", float(cur["Close"]), float(max(cur["Low"] * 0.994, cur["Close"] * 0.958)), "momentum"))

            # S5: 法人量價共振 (鎖定初升段起漲：15日漲幅介於 +2%~+12% 未過熱、量價比 >= 1.35 且貼近 10MA)
            if market_ok and rs_ok and trend_up and (0.02 <= cur["RET15"] <= 0.12) and (ud_ratio >= 1.35) and \
               (cur["Close"] >= cur["MA5"] >= cur["MA10"]) and (0 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.025) and \
               (cur["Close"] > cur["Open"]) and (54 <= cur["RSI14"] <= 68):
                triggered_list.append(("S5_法人量價共振", float((cur["Close"] + cur["MA5"]) / 2.0), float(max(cur["MA20"] * 0.994, cur["Close"] * 0.960)), "pullback"))

            next_bar = df.iloc[i + 1]
            for strat_name, limit_entry, stop_p, mode in triggered_list:
                if i < cooldown_by_strat[strat_name] or limit_entry <= stop_p:
                    continue
                if float(next_bar["Open"]) <= stop_p:
                    continue

                if mode == "momentum":
                    if float(next_bar["Open"]) > limit_entry * 1.028:
                        continue
                    actual_entry = float(next_bar["Open"]) if float(next_bar["Low"]) > limit_entry else limit_entry
                else:
                    if float(next_bar["Low"]) > limit_entry * 1.008:
                        continue
                    actual_entry = min(float(next_bar["Open"]), limit_entry) if float(next_bar["Open"]) <= limit_entry else limit_entry

                net_ret_pct, is_win = simulate_pct_trailing_trade(df, i, actual_entry, stop_p)
                trades.append({
                    "stock_id": sid,
                    "strategy": strat_name,
                    "entry_date": df.index[i].strftime("%Y-%m-%d"),
                    "net_return_pct": net_ret_pct,
                    "win": is_win
                })
                cooldown_by_strat[strat_name] = i + 8

    df_t = pd.DataFrame(trades)
    if df_t.empty:
        return {"total_trades": 0, "win_rate": 0.0, "rr_ratio": 0.0, "mdd_pct": 0.0, "cum_return_pct": 0.0, "by_strategy": {}}

    df_t = df_t.sort_values("entry_date").reset_index(drop=True)
    df_t["port_impact"] = df_t["net_return_pct"] * 0.20
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
    for s_name in ALL_STRATEGIES:
        grp = df_t[df_t["strategy"] == s_name]
        if not grp.empty:
            by_strat[s_name] = {
                "trades": int(len(grp)),
                "win_rate": round(float(grp["win"].mean() * 100), 1),
                "avg_return_pct": round(float(grp["net_return_pct"].mean()), 2)
            }
        else:
            by_strat[s_name] = {"trades": 0, "win_rate": 0.0, "avg_return_pct": 0.0}

    summary = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "universe_scope": "台灣50 + 中型100 + 富櫃50 + 富櫃200 + MSCI成分股",
        "total_trades": int(len(df_t)),
        "win_rate": round(float(df_t["win"].mean() * 100), 1),
        "rr_ratio": rr_ratio,
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(-avg_loss, 2),
        "cum_return_pct": cum_ret_pct,
        "mdd_pct": mdd_pct,
        "by_strategy": by_strat
    }
    with open(BACKTEST_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary

def send_weekly_backtest_tg(summary: dict):
    lines = [
        f"📈【週六 09:00 分%數移動停利回測報告｜{datetime.date.today().strftime('%Y-%m-%d')}】",
        f"• 標的範圍：{summary.get('universe_scope', '五大指數成分股')} (每季自動調整)",
        f"• 停利機制：+3.2%保本 ➔ +5.5%減碼30%(回落3%移動停利) ➔ +11%減碼30%(回落3.5%移動停利) ➔ +18%減碼20%",
        f"• 總交易筆數：{summary['total_trades']} 筆",
        f"• 整體真實勝率：{summary['win_rate']}%",
        f"• 平均盈虧比：{summary['rr_ratio']} : 1 (平均獲利 {summary.get('avg_win_pct', 0):+.2f}% / 平均虧損 {summary.get('avg_loss_pct', 0):+.2f}%)",
        f"• 組合累計報酬率：{summary['cum_return_pct']:+.2f}%",
        f"• 最大回撤 (MDD)：{summary['mdd_pct']:.2f}%",
        "\n📊【各策略細項表現 (S0 ~ S5 完整檢視)】："
    ]
    for s_name in ALL_STRATEGIES:
        v = summary.get("by_strategy", {}).get(s_name, {"trades": 0, "win_rate": 0.0, "avg_return_pct": 0.0})
        if v["trades"] > 0:
            lines.append(f" - {s_name}：勝率 {v['win_rate']}% ({v['trades']}筆)｜單筆平均 {v['avg_return_pct']:+.2f}%")
        else:
            lines.append(f" - {s_name}：期間無觸發訊號 (0筆)")

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"請根據以下台股六大策略(S0~S5)分%數移動停利最新回測數據，用 3 句話給主人下週操作重點與資金配置建議：{json.dumps(summary, ensure_ascii=False)}"
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
