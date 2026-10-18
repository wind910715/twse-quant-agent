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

def simulate_high_rr_trade(df: pd.DataFrame, i: int, actual_entry: float, stop_p: float) -> tuple[float, int]:
    """
    【高賺賠比波段模擬引擎 (修復同根K棒誤觸保本Bug)】：
    1. 嚴格先檢驗「前一日已生效之移動停損」，再檢驗當日是否創高觸發 TP1/TP2，隔日才上移防守線！
    2. +1.4R 觸發隔日保本防線 (成本+0.6%)；+2.2R (TP1) 減碼 30%；+4.0R (TP2) 再減碼 30%；
       剩餘 40% 波段尾倉沿 10MA/20MA 動態防守，最長抱滿 25 個交易日吃盡主升段！
    """
    risk_r = max(actual_entry - stop_p, actual_entry * 0.018)
    be_trigger = actual_entry + 1.4 * risk_r
    tp1_p = actual_entry + 2.2 * risk_r
    tp2_p = actual_entry + 4.0 * risk_r

    future_bars = df.iloc[i + 1:min(i + 26, len(df))]
    hit_be = False
    hit_tp1 = False
    hit_tp2 = False
    realized_pct = 0.0
    rem_weight = 1.0
    active_stop = stop_p

    for bar_idx, (_, fbar) in enumerate(future_bars.iterrows()):
        f_open = float(fbar["Open"])
        f_high = float(fbar["High"])
        f_low = float(fbar["Low"])
        f_close = float(fbar["Close"])
        f_ma10 = float(fbar["MA10"])
        f_ma20 = float(fbar["MA20"])

        # 第一步：先用「昨日已生效之 active_stop」檢查今天是否跌破停損/移動停利
        if f_low <= active_stop:
            exit_p = min(f_open, active_stop) if bar_idx > 0 else active_stop
            realized_pct += rem_weight * ((exit_p - actual_entry) / actual_entry)
            rem_weight = 0.0
            break

        # 第二步：檢查當日高點是否觸及 +1.4R 保本線、TP1 (2.2R) 與 TP2 (4.0R)
        next_stop = active_stop
        if not hit_be and f_high >= be_trigger:
            hit_be = True
            next_stop = max(next_stop, actual_entry * 1.006)

        if not hit_tp1 and f_high >= tp1_p:
            hit_tp1 = True
            realized_pct += 0.30 * ((tp1_p - actual_entry) / actual_entry)
            rem_weight = round(rem_weight - 0.30, 2)
            next_stop = max(next_stop, actual_entry * 1.01)

        if hit_tp1 and not hit_tp2 and f_high >= tp2_p:
            hit_tp2 = True
            realized_pct += 0.30 * ((tp2_p - actual_entry) / actual_entry)
            rem_weight = round(rem_weight - 0.30, 2)
            next_stop = max(next_stop, f_ma10 * 0.995)

        # 第三步：收盤後更新明日生效之動態移動停利線 (只上移不下移)
        if hit_tp2 and f_close > f_ma10:
            next_stop = max(next_stop, f_ma10 * 0.995)
        elif hit_tp1 and f_close > f_ma20:
            next_stop = max(next_stop, min(f_ma10, f_ma20) * 0.995)

        active_stop = max(active_stop, next_stop)

    if rem_weight > 0 and not future_bars.empty:
        last_c = float(future_bars.iloc[-1]["Close"])
        realized_pct += rem_weight * ((last_c - actual_entry) / actual_entry)

    net_ret_pct = round((realized_pct - 0.005) * 100, 2)
    return net_ret_pct, (1 if net_ret_pct > 0 else 0)

def run_portfolio_backtest() -> dict:
    universe = build_dynamic_index_universe()
    sample_items = list(universe.items())[:130]
    tickers = ["0050.TW"] + [f"{sid}{sfx}" for sid, (_, sfx) in sample_items]
    data = yf.download(tickers, period="8mo", group_by="ticker", threads=True, progress=False)

    bench_df = None
    if "0050.TW" in data.columns.get_level_values(0):
        bench_df = data["0050.TW"].dropna(subset=["Close"]).copy()
        bench_df["B_MA20"] = bench_df["Close"].rolling(20).mean()
        bench_df["B_RET20"] = bench_df["Close"].pct_change(20)

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
        df["RET20"] = df["Close"].pct_change(20)

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
            if pd.isna(cur["MA60"]) or cur["MV20"] < 1200 or cur["Close"] < 25:
                continue

            dt_idx = df.index[i]
            rs_strong = True
            rs_super = True
            market_bull = True
            if bench_df is not None and dt_idx in bench_df.index:
                b_row = bench_df.loc[dt_idx]
                if b_row["Close"] < b_row["B_MA20"] * 0.99:
                    market_bull = False
                if cur["RET20"] < (b_row["B_RET20"] + 0.015):
                    rs_strong = False
                if cur["RET20"] < (b_row["B_RET20"] + 0.035):
                    rs_super = False

            ud_ratio = calc_up_down_vol_ratio(df.iloc[i - 9:i + 1])
            last3 = df.iloc[i - 2:i + 1]
            last5 = df.iloc[i - 4:i + 1]
            prev15 = df.iloc[i - 15:i]
            pre_amp_10d = (df.iloc[i - 10:i]["High"].max() - df.iloc[i - 10:i]["Low"].min()) / max(df.iloc[i - 10:i]["Low"].min(), 1)

            strict_trend = (cur["Close"] >= cur["MA5"] >= cur["MA10"] >= cur["MA20"] > cur["MA60"]) and \
                           (cur["MA20"] > df.iloc[i - 4]["MA20"]) and (cur["MA60"] > df.iloc[i - 4]["MA60"])

            triggered_list = []

            # S0: VCP 右側極度收斂 (極窄停損 <= 3.5%)
            amp_5d = (last5["High"].max() - last5["Low"].min()) / max(last5["Low"].min(), 1)
            if market_bull and rs_strong and ud_ratio >= 1.35 and strict_trend and (amp_5d <= 0.042) and \
               (last3["vol_lots"] < 0.55 * cur["MV20"]).any() and (54 <= cur["RSI14"] <= 70):
                e_p = float(cur["Close"])
                s_p = float(max(last5["Low"].min() * 0.996, e_p * 0.968))
                triggered_list.append(("S0_VCP收斂", e_p, s_p, "pullback"))

            # S1: 口袋支點拉回 (回測 10MA 點火)
            prev10 = df.iloc[i - 10:i]
            down_vols = prev10[prev10["Close"] < prev10["Open"]]["vol_lots"]
            max_down_vol = down_vols.max() if not down_vols.empty else 0
            if market_bull and rs_strong and ud_ratio >= 1.40 and strict_trend and \
               (0.0 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.02) and (cur["Close"] > cur["Open"]) and \
               (cur["vol_lots"] > max_down_vol > 0) and (55 <= cur["RSI14"] <= 72):
                e_p = float(cur["Close"])
                s_p = float(max(min(cur["Low"], cur["MA10"]) * 0.994, e_p * 0.968))
                triggered_list.append(("S1_口袋支點拉回", e_p, s_p, "pullback"))

            # S2: 主升段第 3 浪突破 (前10日極度壓縮 <= 8.5% 後帶量突破)
            if market_bull and rs_strong and ud_ratio >= 1.40 and strict_trend and (pre_amp_10d <= 0.085) and \
               (cur["Close"] > prev15["High"].max()) and (1.5 * cur["MV20"] <= cur["vol_lots"] <= 2.8 * cur["MV20"]) and \
               (60 <= cur["RSI14"] <= 73) and (cur["MACD_HIST"] > prev["MACD_HIST"] > 0):
                e_p = float((cur["Close"] + prev15["High"].max()) / 2.0)
                s_p = float(max(cur["Low"], cur["MA10"]) * 0.994)
                triggered_list.append(("S2_主升段3浪", e_p, s_p, "pullback"))

            # S3: 威科夫破底翻 2B (嚴選季線之上、假跌破後當日長紅同時站回前低與 20MA)
            sup_15 = df.iloc[i - 16:i - 1]["Low"].min()
            low_2d = df.iloc[i - 2:i + 1]["Low"].min()
            had_spring = (sup_15 * 0.972 <= low_2d < sup_15 * 0.998)
            reclaim_strong = (cur["Close"] > sup_15) and (cur["Close"] > cur["MA20"]) and (cur["Close"] >= cur["MA5"]) and \
                             (cur["Close"] > cur["Open"] * 1.015) and (cur["vol_lots"] >= 1.3 * cur["MV5"])
            if market_bull and rs_strong and (cur["Close"] > cur["MA60"]) and (cur["MA60"] > df.iloc[i - 10]["MA60"]) and \
               had_spring and reclaim_strong and (ud_ratio >= 1.30) and (50 <= cur["RSI14"] <= 66):
                e_p = float(cur["Close"])
                s_p = float(max(low_2d * 0.996, e_p * 0.968))
                triggered_list.append(("S3_破底翻2B", e_p, s_p, "pullback"))

            # S4: 威力跳空缺口 (壓縮後跳空突破 15 日高點)
            candle_pos = (cur["Close"] - cur["Low"]) / max(cur["High"] - cur["Low"], 0.01)
            has_gap = (cur["Open"] > prev["Close"] * 1.02) and (cur["Low"] > prev["High"] * 1.003) and (cur["Close"] > prev15["High"].max())
            if market_bull and rs_strong and strict_trend and (pre_amp_10d <= 0.085) and has_gap and \
               (candle_pos >= 0.70) and (cur["vol_lots"] >= 1.8 * cur["MV20"]) and (ud_ratio >= 1.35):
                e_p = float(cur["Close"])
                s_p = float(max(cur["Low"] * 0.996, e_p * 0.966))
                triggered_list.append(("S4_威力跳空", e_p, s_p, "momentum"))

            # S5: 法人量價共振 (要求超強 RS + 量價比 >= 1.55 + 緊貼 10MA 2% 內)
            if market_bull and rs_super and strict_trend and (ud_ratio >= 1.55) and \
               (0 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.02) and (cur["Close"] > cur["Open"]) and (56 <= cur["RSI14"] <= 68):
                e_p = float(cur["Close"])
                s_p = float(max(cur["MA20"] * 0.995, e_p * 0.968))
                triggered_list.append(("S5_法人量價共振", e_p, s_p, "pullback"))

            next_bar = df.iloc[i + 1]
            for strat_name, limit_entry, stop_p, mode in triggered_list:
                if i < cooldown_by_strat[strat_name] or limit_entry <= stop_p:
                    continue
                # 【寧缺勿濫鐵律】：停損幅度 > 3.8% 一律放棄不進場
                if ((limit_entry - stop_p) / limit_entry) > 0.038:
                    continue
                if float(next_bar["Open"]) <= stop_p:
                    continue

                if mode == "momentum":
                    if float(next_bar["Open"]) > limit_entry * 1.025:
                        continue
                    actual_entry = float(next_bar["Open"]) if float(next_bar["Low"]) > limit_entry else limit_entry
                else:
                    if float(next_bar["Low"]) > limit_entry * 1.006:
                        continue
                    actual_entry = min(float(next_bar["Open"]), limit_entry) if float(next_bar["Open"]) <= limit_entry else limit_entry

                net_ret_pct, is_win = simulate_high_rr_trade(df, i, actual_entry, stop_p)
                trades.append({
                    "stock_id": sid,
                    "strategy": strat_name,
                    "entry_date": df.index[i].strftime("%Y-%m-%d"),
                    "net_return_pct": net_ret_pct,
                    "win": is_win
                })
                cooldown_by_strat[strat_name] = i + 10

    df_t = pd.DataFrame(trades)
    if df_t.empty:
        return {"total_trades": 0, "win_rate": 0.0, "rr_ratio": 0.0, "mdd_pct": 0.0, "cum_return_pct": 0.0, "by_strategy": {}}

    df_t = df_t.sort_values("entry_date").reset_index(drop=True)
    # 依 25% 標準倉位計算組合複利報酬與 MDD
    df_t["port_impact"] = df_t["net_return_pct"] * 0.25
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
        f"📈【週六 09:00 高賺賠比回測健檢報告｜{datetime.date.today().strftime('%Y-%m-%d')}】",
        f"• 標的範圍：{summary.get('universe_scope', '五大指數成分股')} (每季自動調整)",
        f"• 總交易筆數：{summary['total_trades']} 筆 (寧缺勿濫｜停損嚴控<=3.8%)",
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
            lines.append(f" - {s_name}：期間無符合極窄停損高賺賠比訊號 (0筆｜寧缺勿濫)")

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"請根據以下台股六大策略(S0~S5)高賺賠比最新回測數據，用 3 句話給主人下週操作重點與資金配置建議：{json.dumps(summary, ensure_ascii=False)}"
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
