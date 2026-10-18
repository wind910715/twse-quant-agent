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

def simulate_institutional_trailing_trade(df: pd.DataFrame, i: int, actual_entry: float, raw_stop: float) -> tuple[float, int]:
    """
    【機構級分%數預掛停利 ＋ 跨日收盤確認移動停利 ＋ 幽靈 5 日汰弱引擎】：
    1. 徹底根除「同根K棒早盤低點 (f_low) 誤觸當日新設保本線」之致命 Bug！
       每一天的迴圈，必須先檢驗「昨日已生效之防守線」，收盤後才升級「明日生效之移動停利線」。
    2. 分 % 數限價停利 (趁盤中衝高直接落袋)：
       - 漲達 +6.5% (TP1)：減碼 35% 部位，鎖利底線上移至 +3.0%
       - 漲達 +13.0% (TP2)：再減碼 35% 部位，鎖利底線上移至 +8.0%
       - 漲達 +22.0% (TP3)：再減碼 15% 部位，鎖利底線上移至 +14.0%
    3. 波段部位移動停利：以「收盤價跌破 max(鎖利底線, 最高收盤價回落 4%, 10MA)」出場，不被盤中洗盤下影線洗掉。
    4. 幽靈法則一 (5日時間停損)：若買進滿 5 天從未漲過 +2.5%，且第 5 天收盤跌破買進價與 10MA，主動小賠出場，大幅降低平均虧損！
    """
    hard_stop = max(raw_stop, actual_entry * (1.0 - 0.036))
    be_trigger = actual_entry * 1.038
    tp1_p = actual_entry * 1.065
    tp2_p = actual_entry * 1.130
    tp3_p = actual_entry * 1.220

    future_bars = df.iloc[i + 1:min(i + 28, len(df))]
    highest_high = actual_entry
    highest_close = actual_entry

    # 昨日已生效之盤中硬性底線與收盤移動停利線
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
        f_ma10 = float(fbar["MA10"])

        # ==========================================================
        # 步驟 1：先檢驗「昨日已生效之盤中底線 (active_hard_floor)」是否被跌破
        # (注意：此處用的是昨日設定好的底線，絕不會拿今日早盤低點去撞今日下午才剛調高的保本線！)
        # ==========================================================
        if f_open <= active_hard_floor:
            realized_pct += rem_weight * ((f_open - actual_entry) / actual_entry)
            rem_weight = 0.0
            break
        if f_low <= active_hard_floor:
            realized_pct += rem_weight * ((active_hard_floor - actual_entry) / actual_entry)
            rem_weight = 0.0
            break

        # ==========================================================
        # 步驟 2：檢驗當日盤中高點是否觸及預掛之 +6.5% / +13.0% / +22.0% 限價停利單
        # ==========================================================
        if f_high > highest_high:
            highest_high = f_high
        if f_close > highest_close:
            highest_close = f_close

        if not hit_be and highest_high >= be_trigger:
            hit_be = True

        if not hit_tp1 and f_high >= tp1_p:
            hit_tp1 = True
            realized_pct += 0.35 * 0.065
            rem_weight = round(rem_weight - 0.35, 2)

        if hit_tp1 and not hit_tp2 and f_high >= tp2_p:
            hit_tp2 = True
            realized_pct += 0.35 * 0.130
            rem_weight = round(rem_weight - 0.35, 2)

        if hit_tp2 and not hit_tp3 and f_high >= tp3_p:
            hit_tp3 = True
            realized_pct += 0.15 * 0.220
            rem_weight = round(rem_weight - 0.15, 2)

        # ==========================================================
        # 步驟 3：收盤時檢驗「移動停利線 (Close-based Trailing Stop)」與「幽靈 5 日時間停損」
        # ==========================================================
        # A. 若已啟動移動停利，且今日「收盤價」正式跌破昨日生效之移動停利線 ➔ 收盤獲利了結
        if hit_be and f_close <= active_close_trail:
            realized_pct += rem_weight * ((f_close - actual_entry) / actual_entry)
            rem_weight = 0.0
            break

        # B. 幽靈法則一 (5日不漲主動汰弱)：滿 5~7 天從未觸及 +3.8%，且收盤跌破買進價與 10MA ➔ 立即砍掉死魚單
        if (day_num >= 5) and (not hit_be) and (f_close < actual_entry * 0.992) and (f_close < f_ma10):
            realized_pct += rem_weight * ((f_close - actual_entry) / actual_entry)
            rem_weight = 0.0
            break

        # ==========================================================
        # 步驟 4：收盤後，更新「明日生效」之鎖利底線與收盤移動停利線 (只上不下)
        # ==========================================================
        if hit_tp3:
            active_hard_floor = max(active_hard_floor, actual_entry * 1.140)
            active_close_trail = max(active_close_trail, actual_entry * 1.150, highest_close * 0.955, f_ma10)
        elif hit_tp2:
            active_hard_floor = max(active_hard_floor, actual_entry * 1.080)
            active_close_trail = max(active_close_trail, actual_entry * 1.085, highest_close * 0.960, f_ma10)
        elif hit_tp1:
            active_hard_floor = max(active_hard_floor, actual_entry * 1.030)
            active_close_trail = max(active_close_trail, actual_entry * 1.035, highest_close * 0.962, f_ma10 * 0.995)
        elif hit_be:
            active_hard_floor = max(active_hard_floor, actual_entry * 1.012)
            active_close_trail = max(active_close_trail, actual_entry * 1.015, f_ma10 * 0.990)

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
        df["RSI3"] = calc_rsi(df["Close"], 3)
        df["RET20"] = df["Close"].pct_change(20)

        std20 = df["Close"].rolling(20).std()
        df["BB_UP"] = df["MA20"] + 2.0 * std20
        df["BB_LOW"] = df["MA20"] - 2.0 * std20
        df["BB_WIDTH"] = (df["BB_UP"] - df["BB_LOW"]) / df["MA20"]

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
                if (b_row["Close"] < b_row["B_MA20"] * 0.985) and (cur["RET20"] < 0.02):
                    market_ok = False
                if cur["RET20"] < b_row["B_RET20"]:
                    rs_ok = False

            ud_ratio = calc_up_down_vol_ratio(df.iloc[i - 9:i + 1])
            last3 = df.iloc[i - 2:i + 1]
            last5 = df.iloc[i - 4:i + 1]
            prev15 = df.iloc[i - 15:i]

            trend_up = (cur["Close"] > cur["MA20"] > cur["MA60"]) and \
                       (cur["MA20"] > df.iloc[i - 4]["MA20"]) and (cur["MA60"] >= df.iloc[i - 4]["MA60"])

            triggered_list = []

            # S0: VCP 右側量縮收斂 (波幅 <= 4.5% + 緊貼 10MA 乖離 <= 3%)
            amp_5d = (last5["High"].max() - last5["Low"].min()) / max(last5["Low"].min(), 1)
            if market_ok and rs_ok and ud_ratio >= 1.25 and trend_up and (cur["Close"] >= cur["MA5"]) and \
               (amp_5d <= 0.045) and (last3["vol_lots"] < 0.60 * cur["MV20"]).any() and \
               (0 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.03) and (52 <= cur["RSI14"] <= 70):
                triggered_list.append(("S0_VCP收斂", float(cur["Close"]), float(min(last5["Low"].min(), cur["MA20"]) * 0.995), "pullback"))

            # S1: 口袋支點拉回 (回測 10MA/20MA 量縮後出量紅K)
            near_ma = (-0.012 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.020) or \
                      (-0.010 <= (cur["Close"] - cur["MA20"]) / cur["MA20"] <= 0.018)
            prev10 = df.iloc[i - 10:i]
            down_vols = prev10[prev10["Close"] < prev10["Open"]]["vol_lots"]
            max_down_vol = down_vols.max() if not down_vols.empty else 0
            if market_ok and rs_ok and ud_ratio >= 1.30 and trend_up and near_ma and \
               (cur["Close"] > cur["Open"]) and (cur["vol_lots"] > max_down_vol > 0) and (52 <= cur["RSI14"] <= 70):
                triggered_list.append(("S1_口袋支點拉回", float(cur["Close"] * 0.998), float(min(cur["Low"], cur["MA20"]) * 0.994), "pullback"))

            # S2: 布林帶寬壓縮突破 (John Carter Squeeze：前8日帶寬 <= 12.5% 後首根突破紅K，不追高乖離股)
            avg_bb_w = float(df.iloc[i - 8:i]["BB_WIDTH"].mean())
            candle_pos = (cur["Close"] - cur["Low"]) / max(cur["High"] - cur["Low"], 0.01)
            if market_ok and rs_ok and ud_ratio >= 1.30 and trend_up and (avg_bb_w <= 0.125) and \
               (cur["Close"] > prev15["High"].max()) and (0 <= (cur["Close"] - cur["MA20"]) / cur["MA20"] <= 0.055) and \
               (candle_pos >= 0.65) and (1.35 * cur["MV20"] <= cur["vol_lots"] <= 3.0 * cur["MV20"]) and \
               (cur["MACD_HIST"] > prev["MACD_HIST"] > 0):
                triggered_list.append(("S2_主升段3浪", float(cur["Close"]), float(max(cur["Low"], cur["MA10"]) * 0.994), "pullback"))

            # S3: 強勢股超賣破底翻 (Larry Connors RSI-3 <= 32 極度超賣後首根長紅收復昨高)
            min_rsi3 = float(df.iloc[i - 2:i]["RSI3"].min())
            bull_60 = (cur["Close"] > cur["MA60"]) and (cur["MA60"] > df.iloc[i - 10]["MA60"])
            reclaim_turn = (cur["Close"] > prev["High"]) and (cur["Close"] > cur["Open"] * 1.012) and (cur["Close"] >= cur["MA20"] * 0.985)
            if market_ok and rs_ok and bull_60 and (min_rsi3 <= 32.0) and reclaim_turn and (ud_ratio >= 1.15):
                triggered_list.append(("S3_破底翻2B", float(cur["Close"]), float(df.iloc[i - 2:i + 1]["Low"].min() * 0.995), "pullback"))

            # S4: 箱型壓縮鎖碼跳空 (前12日箱型振幅 <= 10% 後首根跳空收高鎖碼)
            pre_box_amp = (df.iloc[i - 12:i]["High"].max() - df.iloc[i - 12:i]["Low"].min()) / max(df.iloc[i - 12:i]["Low"].min(), 1)
            has_gap = (cur["Open"] > prev["Close"] * 1.015) and (cur["Low"] >= prev["High"] * 0.998) and (cur["Close"] > prev15["High"].max())
            if market_ok and rs_ok and trend_up and (pre_box_amp <= 0.10) and has_gap and (candle_pos >= 0.72) and \
               (0 <= (cur["Close"] - cur["MA20"]) / cur["MA20"] <= 0.065) and (cur["vol_lots"] >= 1.5 * cur["MV20"]) and (ud_ratio >= 1.25):
                triggered_list.append(("S4_威力跳空", float(cur["Close"]), float(cur["Low"] * 0.994), "momentum"))

            # S5: 法人低乖離鎖碼共振 (緊貼 10MA 2.2% 內 + 高量價比 >= 1.40)
            if market_ok and rs_ok and trend_up and (0.02 <= cur["RET20"] <= 0.12) and (ud_ratio >= 1.40) and \
               (cur["Close"] >= cur["MA5"] >= cur["MA10"]) and (0 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.022) and \
               (cur["Close"] > cur["Open"]) and (54 <= cur["RSI14"] <= 66):
                triggered_list.append(("S5_法人量價共振", float(cur["Close"]), float(cur["MA20"] * 0.995), "pullback"))

            next_bar = df.iloc[i + 1]
            for strat_name, limit_entry, stop_p, mode in triggered_list:
                if i < cooldown_by_strat[strat_name] or limit_entry <= stop_p:
                    continue
                if float(next_bar["Open"]) <= stop_p:
                    continue

                if mode == "momentum":
                    if float(next_bar["Open"]) > limit_entry * 1.022:
                        continue
                    actual_entry = float(next_bar["Open"]) if float(next_bar["Low"]) > limit_entry else limit_entry
                else:
                    if float(next_bar["Low"]) > limit_entry * 1.005:
                        continue
                    actual_entry = min(float(next_bar["Open"]), limit_entry) if float(next_bar["Open"]) <= limit_entry else limit_entry

                net_ret_pct, is_win = simulate_institutional_trailing_trade(df, i, actual_entry, stop_p)
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
        f"📈【週六 09:00 機構級移動停利回測報告｜{datetime.date.today().strftime('%Y-%m-%d')}】",
        f"• 標的範圍：{summary.get('universe_scope', '五大指數成分股')} (每季自動調整)",
        f"• 停利機制：+3.8%隔日保本 ➔ +6.5%減碼35%(底線鎖+3%) ➔ +13%減碼35%(底線鎖+8%) ➔ 尾倉收盤守10MA/回落4% ＋ 5日不漲汰弱",
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
                contents=f"請根據以下台股六大策略(S0~S5)最新回測數據，用 3 句話給主人下週操作重點與資金配置建議：{json.dumps(summary, ensure_ascii=False)}"
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
