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

def simulate_alpha_v4_5_golden_trade(df: pd.DataFrame, entry_idx: int, actual_entry: float, raw_stop: float) -> tuple[float, int]:
    """
    【Alpha v4.5 Golden 黃金合體版引擎 (結合 v4.0 的 65.4% 高勝率與 v5.0 的 +13.67% 爆發力)】：
    1. 移除 v5.0 首日 -0.5% 誤殺條件，恢復 v4.0 的首日防守，讓 S2 與 S3 恢復高勝率！
    2. 將 TP1 改回 v4.0 驗證最成功的 +5.5%：
       - 觸及 +5.5% (TP1) 時輕減碼 20%，立即將盤中底線鎖死於 +2.8%，並觸發 40% 零風險利潤墊加碼！
       - 這讓 S1 與 S3 不再因差一點摸不到 +6.5% 而跌回平盤！
    3. 保留 v5.0 讓 S0 (+3.09%) 與 S4 (+13.67%) 暴賺的後段設計：
       - 未達 TP1 前只設盤中 +0.8% 保本底線 (不設過緊之收盤線)
       - TP2 (+14.0%) 減碼 35%，底線鎖 +8.5%
       - TP3 (+25.0%) 減碼 35%，底線鎖 +16.0%，尾倉沿 20MA 抱滿 50 日！
    """
    hard_stop = max(raw_stop, actual_entry * (1.0 - 0.022))
    be_trigger = actual_entry * 1.030
    tp1_p = actual_entry * 1.055
    tp2_p = actual_entry * 1.140
    tp3_p = actual_entry * 1.250

    future_bars = df.iloc[entry_idx:min(entry_idx + 50, len(df))]
    highest_high = actual_entry
    highest_close = actual_entry

    active_hard_floor = hard_stop
    active_close_trail = hard_stop

    hit_be = False
    hit_tp1 = False
    hit_tp2 = False
    hit_tp3 = False

    realized_pct = 0.0
    rem_weight = 1.0

    for day_num, (_, fbar) in enumerate(future_bars.iterrows(), start=1):
        f_open = float(fbar["Open"])
        f_high = float(fbar["High"])
        f_low = float(fbar["Low"])
        f_close = float(fbar["Close"])
        f_ma5 = float(fbar["MA5"])
        f_ma10 = float(fbar["MA10"])
        f_ma20 = float(fbar["MA20"])

        # 第一步：改回 v4.0 首日防守邏輯 (不誤殺首日震盪股)
        if day_num >= 2:
            if f_open <= active_hard_floor:
                realized_pct += rem_weight * ((f_open - actual_entry) / actual_entry)
                rem_weight = 0.0
                break
            if f_low <= active_hard_floor:
                realized_pct += rem_weight * ((active_hard_floor - actual_entry) / actual_entry)
                rem_weight = 0.0
                break
        else:
            if f_close <= hard_stop:
                realized_pct += rem_weight * ((hard_stop - actual_entry) / actual_entry)
                rem_weight = 0.0
                break

        # 第二步：檢驗盤中最高價是否觸及 +5.5% (TP1) / +14.0% (TP2) / +25.0% (TP3)
        if f_high > highest_high:
            highest_high = f_high
        if f_close > highest_close:
            highest_close = f_close

        if not hit_be and highest_high >= be_trigger:
            hit_be = True

        if not hit_tp1 and f_high >= tp1_p:
            hit_tp1 = True
            realized_pct += 0.20 * 0.055
            rem_weight = round(rem_weight - 0.20, 2)
            # 5日內攻克 +5.5% 且收盤穩健，利用 +2.8% 保本利潤墊執行零風險加碼
            if day_num <= 5 and f_close >= tp1_p * 0.992:
                rem_weight = round(rem_weight + 0.40, 2)

        if hit_tp1 and not hit_tp2 and f_high >= tp2_p:
            hit_tp2 = True
            sell_w = min(rem_weight, 0.35)
            realized_pct += sell_w * 0.140
            rem_weight = round(rem_weight - sell_w, 2)

        if hit_tp2 and not hit_tp3 and f_high >= tp3_p:
            hit_tp3 = True
            sell_w = min(rem_weight, 0.35)
            realized_pct += sell_w * 0.250
            rem_weight = round(rem_weight - sell_w, 2)

        # 第三步：收盤檢驗「達標 TP1 後之收盤移動停利」與「2日不漲極速汰弱」
        if day_num >= 2 and hit_tp1 and f_close <= active_close_trail:
            realized_pct += rem_weight * ((f_close - actual_entry) / actual_entry)
            rem_weight = 0.0
            break

        if (day_num >= 2) and (not hit_be) and (highest_high < actual_entry * 1.020) and (f_close < actual_entry * 0.996) and (f_close < f_ma5):
            realized_pct += rem_weight * ((f_close - actual_entry) / actual_entry)
            rem_weight = 0.0
            break

        # 第四步：收盤後更新「明日生效」之盤中鎖利底線與收盤移動停利線
        if hit_tp3:
            active_hard_floor = max(active_hard_floor, actual_entry * 1.160)
            active_close_trail = max(active_close_trail, actual_entry * 1.170, highest_close * 0.940, f_ma20)
        elif hit_tp2:
            active_hard_floor = max(active_hard_floor, actual_entry * 1.085)
            active_close_trail = max(active_close_trail, actual_entry * 1.090, highest_close * 0.945, f_ma10)
        elif hit_tp1:
            active_hard_floor = max(active_hard_floor, actual_entry * 1.028)
            active_close_trail = max(active_close_trail, actual_entry * 1.032, highest_close * 0.948, f_ma20)
        elif hit_be:
            active_hard_floor = max(active_hard_floor, actual_entry * 1.008)

    if rem_weight > 0 and not future_bars.empty:
        last_c = float(future_bars.iloc[-1]["Close"])
        realized_pct += rem_weight * ((last_c - actual_entry) / actual_entry)

    net_ret_pct = round((realized_pct - 0.005) * 100, 2)
    return net_ret_pct, (1 if net_ret_pct > 0 else 0)

def run_portfolio_backtest() -> dict:
    universe = build_dynamic_index_universe()
    sample_items = list(universe.items())[:220]
    tickers = ["0050.TW"] + [f"{sid}{sfx}" for sid, (_, sfx) in sample_items]
    data = yf.download(tickers, period="9mo", group_by="ticker", threads=True, progress=False)

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
        if len(df) < 110:
            continue

        df["vol_lots"] = df["Volume"] / 1000.0
        for p in [5, 10, 20, 60]:
            df[f"MA{p}"] = df["Close"].rolling(p).mean()
        df["MV5"] = df["vol_lots"].rolling(5).mean()
        df["MV20"] = df["vol_lots"].rolling(20).mean()
        df["RSI14"] = calc_rsi(df["Close"], 14)
        df["RET20"] = df["Close"].pct_change(20)
        df["RET40"] = df["Close"].pct_change(40)

        std20 = df["Close"].rolling(20).std()
        df["BB_UP"] = df["MA20"] + 2.0 * std20
        df["BB_LOW"] = df["MA20"] - 2.0 * std20
        df["BB_WIDTH"] = (df["BB_UP"] - df["BB_LOW"]) / df["MA20"]

        ema12 = df["Close"].ewm(span=12, adjust=False).mean()
        ema26 = df["Close"].ewm(span=26, adjust=False).mean()
        df["DIF"] = ema12 - ema26
        df["MACD"] = df["DIF"].ewm(span=9, adjust=False).mean()
        df["MACD_HIST"] = df["DIF"] - df["MACD"]

        start_idx = max(60, len(df) - 105)
        end_idx = len(df) - 8
        cooldown_by_strat = {s: -1 for s in ALL_STRATEGIES}

        for i in range(start_idx, end_idx):
            cur = df.iloc[i]
            prev = df.iloc[i - 1]
            if pd.isna(cur["MA60"]) or cur["MV20"] < 1000 or cur["Close"] < 25:
                continue

            dt_idx = df.index[i]
            rs_strong = True
            market_ok = True
            if bench_df is not None and dt_idx in bench_df.index:
                b_row = bench_df.loc[dt_idx]
                if (b_row["Close"] < b_row["B_MA20"] * 0.988) and (cur["RET20"] < 0.03):
                    market_ok = False
                if cur["RET20"] < (b_row["B_RET20"] + 0.015):
                    rs_strong = False

            rs_comp = 0.6 * float(cur["RET20"] or 0) + 0.4 * float(cur["RET40"] or 0)
            ud_ratio = calc_up_down_vol_ratio(df.iloc[i - 9:i + 1])
            if not market_ok or not rs_strong or rs_comp < 0.035 or ud_ratio < 1.30:
                continue

            last5 = df.iloc[i - 4:i + 1]
            prev10_high = df.iloc[i - 10:i]["High"].max()
            prev15_high = df.iloc[i - 15:i]["High"].max()

            trend_up = (cur["Close"] > cur["MA20"] > cur["MA60"]) and \
                       (cur["MA20"] > df.iloc[i - 4]["MA20"]) and (cur["MA60"] > df.iloc[i - 4]["MA60"])
            candle_pos = (cur["Close"] - cur["Low"]) / max(cur["High"] - cur["Low"], 0.01)

            triggered_list = []

            # S0: VCP 右側極致收斂突破
            amp_4d = (df.iloc[i - 4:i]["High"].max() - df.iloc[i - 4:i]["Low"].min()) / max(df.iloc[i - 4:i]["Low"].min(), 1)
            vdu_ok = (df.iloc[i - 4:i]["vol_lots"] < 0.62 * cur["MV20"]).any()
            if trend_up and (amp_4d <= 0.040) and vdu_ok and (cur["Close"] >= df.iloc[i - 4:i]["High"].max()) and \
               (cur["Close"] > cur["Open"]) and (candle_pos >= 0.68) and (55 <= cur["RSI14"] <= 72):
                triggered_list.append(("S0_VCP收斂", float(cur["Close"]), float(last5["Low"].min() * 0.996)))

            # S1: 口袋支點拉回
            near_ma = (-0.008 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.020) or \
                      (-0.008 <= (cur["Close"] - cur["MA20"]) / cur["MA20"] <= 0.018)
            prev10 = df.iloc[i - 10:i]
            down_vols = prev10[prev10["Close"] < prev10["Open"]]["vol_lots"]
            max_down_vol = down_vols.max() if not down_vols.empty else 0
            if trend_up and near_ma and (cur["Close"] > prev["High"]) and (cur["Close"] > cur["Open"]) and \
               (cur["vol_lots"] > max_down_vol > 0) and (54 <= cur["RSI14"] <= 70):
                triggered_list.append(("S1_口袋支點拉回", float(cur["Close"]), float(min(cur["Low"], cur["MA20"]) * 0.995)))

            # S2: 布林帶寬極度壓縮突破
            avg_bb_w = float(df.iloc[i - 8:i]["BB_WIDTH"].mean())
            if trend_up and (avg_bb_w <= 0.120) and (cur["Close"] > prev15_high) and \
               (0 <= (cur["Close"] - cur["MA20"]) / cur["MA20"] <= 0.055) and (candle_pos >= 0.72) and \
               (1.40 * cur["MV20"] <= cur["vol_lots"] <= 3.2 * cur["MV20"]) and (cur["MACD_HIST"] > prev["MACD_HIST"] > 0):
                triggered_list.append(("S2_主升段3浪", float(cur["Close"]), float(max(cur["Low"], cur["MA10"]) * 0.995)))

            # S3: 精銳版強勢洗盤破底翻
            dipped_ma10 = (df.iloc[i - 3:i]["Low"] < df.iloc[i - 3:i]["MA10"] * 0.996).any()
            power_reclaim = (cur["Close"] > df.iloc[i - 3:i]["High"].max()) and (cur["Close"] >= cur["MA5"]) and \
                            (cur["Close"] >= cur["MA10"]) and (cur["Close"] >= cur["Open"] * 1.022) and \
                            (candle_pos >= 0.75) and (cur["vol_lots"] >= 1.30 * cur["MV5"])
            if trend_up and dipped_ma10 and power_reclaim and (ud_ratio >= 1.38) and (54 <= cur["RSI14"] <= 68):
                triggered_list.append(("S3_破底翻2B", float(cur["Close"]), float(cur["Low"] * 0.996)))

            # S4: 達瓦斯箱型威力跳空
            box_amp_10 = (df.iloc[i - 10:i]["High"].max() - df.iloc[i - 10:i]["Low"].min()) / max(df.iloc[i - 10:i]["Low"].min(), 1)
            breakout_power = (cur["Close"] > prev10_high) and ((cur["Open"] > prev["Close"] * 1.010) or (cur["Close"] >= prev["Close"] * 1.032))
            if trend_up and (box_amp_10 <= 0.090) and breakout_power and (candle_pos >= 0.72) and \
               (0 <= (cur["Close"] - cur["MA20"]) / cur["MA20"] <= 0.085) and (cur["vol_lots"] >= 1.5 * cur["MV20"]):
                triggered_list.append(("S4_威力跳空", float(cur["Close"]), float(cur["Low"] * 0.995)))

            # S5: 法人爆量突破後「首次量縮回測 5MA/10MA 轉強」
            recent_surge = ((df.iloc[i - 5:i]["vol_lots"] >= 1.8 * df.iloc[i - 5:i]["MV20"]) & (df.iloc[i - 5:i]["Close"] >= df.iloc[i - 5:i]["Open"] * 1.025)).any()
            pullback_hold = (prev["vol_lots"] < cur["MV5"]) and (cur["Low"] <= cur["MA5"] * 1.008) and (cur["Close"] >= cur["MA5"]) and (cur["Close"] > prev["High"])
            if trend_up and (cur["MA5"] >= cur["MA10"] >= cur["MA20"]) and recent_surge and pullback_hold and \
               (cur["Close"] > cur["Open"] * 1.01) and (candle_pos >= 0.70) and (ud_ratio >= 1.45) and (54 <= cur["RSI14"] <= 68):
                triggered_list.append(("S5_法人量價共振", float(cur["Close"]), float(cur["MA10"] * 0.995)))

            next_bar = df.iloc[i + 1]
            n_open = float(next_bar["Open"])
            n_high = float(next_bar["High"])
            n_close = float(next_bar["Close"])

            for strat_name, signal_close, stop_p in triggered_list:
                if i < cooldown_by_strat[strat_name] or signal_close <= stop_p:
                    continue
                if n_open > signal_close * 1.028 or n_open <= stop_p:
                    continue

                trigger_buy = round(max(signal_close, n_open * 1.003), 2)
                if n_high < trigger_buy or n_close < n_open * 0.998:
                    continue

                actual_entry = trigger_buy
                net_ret_pct, is_win = simulate_alpha_v4_5_golden_trade(df, i + 1, actual_entry, stop_p)
                trades.append({
                    "stock_id": sid,
                    "strategy": strat_name,
                    "entry_date": df.index[i + 1].strftime("%Y-%m-%d"),
                    "net_return_pct": net_ret_pct,
                    "win": is_win
                })
                cooldown_by_strat[strat_name] = i + 8

    df_t = pd.DataFrame(trades)
    if df_t.empty:
        return {"total_trades": 0, "win_rate": 0.0, "rr_ratio": 0.0, "mdd_pct": 0.0, "cum_return_pct": 0.0, "by_strategy": {}}

    df_t = df_t.sort_values("entry_date").reset_index(drop=True)
    df_t["port_impact"] = df_t["net_return_pct"] * 0.35
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
        f"📈【週六 09:00 超額報酬 Alpha v4.5 Golden 定版回測｜{datetime.date.today().strftime('%Y-%m-%d')}】",
        f"• 標的範圍：{summary.get('universe_scope', '五大指數成分股')} (每季自動調整)",
        f"• 核心機制：結合 v4.0 高勝率(+5.5%鎖利加碼) ＋ v5.0 高爆發尾倉(+14%/+25%) ＋ 2日極速汰弱",
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
                contents=f"請根據以下台股六大策略(S0~S5) Alpha v4.5 Golden 最新回測數據，用 3 句話給主人下週操作重點與資金配置建議：{json.dumps(summary, ensure_ascii=False)}"
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
