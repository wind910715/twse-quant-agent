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

def simulate_single_trade(df: pd.DataFrame, i: int, actual_entry: float, stop_p: float) -> tuple[float, int]:
    """模擬單筆交易未來 15 日之 1.2R 快速保本鎖利 (50%) + 2.8R 波段目標 (30%) + 尾倉 (20%) 表現"""
    risk_r = max(actual_entry - stop_p, actual_entry * 0.022)
    tp1_p = actual_entry + 1.2 * risk_r
    tp2_p = actual_entry + 2.8 * risk_r

    future_bars = df.iloc[i + 1:min(i + 16, len(df))]
    hit_tp1 = False
    hit_tp2 = False
    realized_pct = 0.0
    rem_weight = 1.0
    trail_stop = stop_p

    for _, fbar in future_bars.iterrows():
        # 1. 觸及 TP1 (1.2R)：先減碼 50% 鎖利，剩餘 50% 停損立刻上移至【成本+0.6%稅費】確保不敗
        if not hit_tp1 and float(fbar["High"]) >= tp1_p:
            hit_tp1 = True
            realized_pct += 0.50 * ((tp1_p - actual_entry) / actual_entry)
            rem_weight -= 0.50
            trail_stop = actual_entry * 1.006

        # 2. 觸及 TP2 (2.8R)：再減碼 30%，尾倉 20% 改守 10MA
        if hit_tp1 and not hit_tp2 and float(fbar["High"]) >= tp2_p:
            hit_tp2 = True
            realized_pct += 0.30 * ((tp2_p - actual_entry) / actual_entry)
            rem_weight -= 0.30
            trail_stop = max(trail_stop, float(fbar["MA10"]))

        # 3. 檢查是否觸及停損或移動停利
        if float(fbar["Low"]) <= trail_stop:
            realized_pct += rem_weight * ((trail_stop - actual_entry) / actual_entry)
            rem_weight = 0.0
            break

    if rem_weight > 0 and not future_bars.empty:
        last_c = float(future_bars.iloc[-1]["Close"])
        realized_pct += rem_weight * ((last_c - actual_entry) / actual_entry)

    net_ret_pct = round((realized_pct - 0.005) * 100, 2)
    return net_ret_pct, (1 if net_ret_pct > 0 else 0)

def run_portfolio_backtest() -> dict:
    """
    針對【台灣50、中型100、富櫃50、富櫃200、MSCI成分股】執行近 90 日高勝率回測
    獨立評估 S0 ~ S5 六大策略，解決 S3(破底翻) 與 S4(威力跳空) 濾網衝突問題
    """
    universe = build_dynamic_index_universe()
    sample_items = list(universe.items())[:120]
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

        start_idx = max(60, len(df) - 80)
        end_idx = len(df) - 6
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
                if (b_row["Close"] < b_row["B_MA20"] * 0.98) and (cur["RET10"] < 0.01):
                    market_ok = False
                if cur["RET10"] < b_row["B_RET10"]:
                    rs_strong = False

            ud_ratio = calc_up_down_vol_ratio(df.iloc[i - 9:i + 1])
            last3 = df.iloc[i - 2:i + 1]
            last5 = df.iloc[i - 4:i + 1]
            prev15 = df.iloc[i - 15:i]
            pre_amp_10d = (df.iloc[i - 10:i]["High"].max() - df.iloc[i - 10:i]["Low"].min()) / max(df.iloc[i - 10:i]["Low"].min(), 1)

            trend_up = (cur["Close"] >= cur["MA5"]) and (cur["Close"] > cur["MA20"] > cur["MA60"]) and \
                       (cur["MA20"] > df.iloc[i - 4]["MA20"]) and (cur["MA60"] >= df.iloc[i - 4]["MA60"])

            triggered_list = []

            # ----------------------------------------------------
            # S0: VCP 右側量縮收斂 (需強勢 RS + 量價比 >= 1.25)
            # ----------------------------------------------------
            amp_5d = (last5["High"].max() - last5["Low"].min()) / max(last5["Low"].min(), 1)
            if market_ok and rs_strong and ud_ratio >= 1.25 and trend_up and amp_5d <= 0.048 and \
               (last3["vol_lots"] < 0.60 * cur["MV20"]).any() and (52 <= cur["RSI14"] <= 72):
                triggered_list.append(("S0_VCP收斂", float((cur["Close"] + cur["MA5"]) / 2.0), float(min(last5["Low"].min(), cur["MA20"]) * 0.994), "pullback"))

            # ----------------------------------------------------
            # S1: 口袋支點拉回 (需強勢 RS + 量價比 >= 1.25)
            # ----------------------------------------------------
            if market_ok and rs_strong and ud_ratio >= 1.25 and trend_up and \
               (-0.008 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.02) and (cur["Close"] > cur["Open"]) and \
               (cur["vol_lots"] > 1.25 * cur["MV5"]) and (cur["RSI14"] >= 54):
                triggered_list.append(("S1_口袋支點拉回", float(cur["Close"] * 0.995), float(max(min(cur["Low"], cur["MA20"] * 0.992), cur["Close"] * 0.96)), "pullback"))

            # ----------------------------------------------------
            # S2: 主升段第 3 浪突破
            # ----------------------------------------------------
            if market_ok and rs_strong and ud_ratio >= 1.20 and trend_up and (pre_amp_10d <= 0.11) and \
               (cur["Close"] > prev15["High"].max()) and (1.4 * cur["MV20"] <= cur["vol_lots"] <= 3.0 * cur["MV20"]) and (60 <= cur["RSI14"] <= 74):
                triggered_list.append(("S2_主升段3浪", float((cur["Close"] + prev15["High"].max()) / 2.0), float(max(cur["Low"], cur["MA10"]) * 0.992), "pullback"))

            # ----------------------------------------------------
            # S3: 威科夫破底翻 2B (專屬逆勢轉強濾網：季線向上 + 假跌破前波箱底後帶量紅K站回 5MA 與箱底)
            # ----------------------------------------------------
            sup_15 = df.iloc[i - 18:i - 2]["Low"].min()
            low_3d = df.iloc[i - 3:i]["Low"].min()
            had_spring = (sup_15 * 0.955 <= low_3d < sup_15 * 0.998)
            reclaim_ok = (cur["Close"] > sup_15) and (cur["Close"] >= cur["MA5"]) and (cur["Close"] > cur["Open"]) and (cur["vol_lots"] >= 1.15 * cur["MV5"])
            if (cur["MA60"] >= df.iloc[i - 8]["MA60"] * 0.998) and (cur["Close"] > cur["MA60"] * 0.96) and had_spring and reclaim_ok and (45 <= cur["RSI14"] <= 68):
                triggered_list.append(("S3_破底翻2B", float(cur["Close"] * 0.998), float(low_3d * 0.994), "direct"))

            # ----------------------------------------------------
            # S4: 威力跳空缺口 (專屬動能濾網：前10日收斂 + 跳空紅K收高 + 隔日不破缺口直接順勢切入)
            # ----------------------------------------------------
            candle_pos = (cur["Close"] - cur["Low"]) / max(cur["High"] - cur["Low"], 0.01)
            has_gap = (cur["Low"] > prev["High"] * 1.012) and (cur["Close"] > prev["Close"] * 1.03)
            if trend_up and (pre_amp_10d <= 0.095) and has_gap and (candle_pos >= 0.65) and (cur["vol_lots"] >= 1.6 * cur["MV20"]) and (ud_ratio >= 1.20):
                triggered_list.append(("S4_威力跳空", float(cur["Close"]), float(cur["Low"] * 0.994), "momentum"))

            # ----------------------------------------------------
            # S5: 投信外資籌碼量價共振 (連3日價漲量增鎖碼 + 貼近 10MA 不追高)
            # ----------------------------------------------------
            up_days_3 = int((last3["Close"] >= last3["Open"]).sum())
            if market_ok and rs_strong and trend_up and (up_days_3 >= 2) and (ud_ratio >= 1.35) and \
               (0 <= (cur["Close"] - cur["MA10"]) / cur["MA10"] <= 0.038) and (55 <= cur["RSI14"] <= 70):
                triggered_list.append(("S5_法人量價共振", float((cur["Close"] + cur["MA5"]) / 2.0), float(min(cur["MA20"], last3["Low"].min()) * 0.993), "pullback"))

            next_bar = df.iloc[i + 1]
            for strat_name, limit_entry, stop_p, mode in triggered_list:
                if i < cooldown_by_strat[strat_name] or limit_entry <= stop_p:
                    continue

                if float(next_bar["Open"]) <= stop_p:
                    continue

                # 依策略模式決定隔日實際進場成交價
                if mode == "momentum":
                    # 跳空動能股：隔日開盤未暴衝超過 +2.5% 且守穩缺口即順勢進場
                    if float(next_bar["Open"]) > limit_entry * 1.025:
                        continue
                    actual_entry = float(next_bar["Open"]) if float(next_bar["Low"]) > limit_entry else limit_entry
                elif mode == "direct":
                    # 破底翻確認股：隔日回測不破支撐即成交
                    if float(next_bar["Low"]) > limit_entry * 1.012:
                        continue
                    actual_entry = min(float(next_bar["Open"]), limit_entry) if float(next_bar["Open"]) <= limit_entry else limit_entry
                else:
                    if float(next_bar["Low"]) > limit_entry * 1.008:
                        continue
                    actual_entry = min(float(next_bar["Open"]), limit_entry) if float(next_bar["Open"]) <= limit_entry else limit_entry

                net_ret_pct, is_win = simulate_single_trade(df, i, actual_entry, stop_p)
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

    # 強制依序輸出 S0 ~ S5 所有策略，絕不漏列
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
        except Exception as e:
            print(f"⚠️ Gemini 診斷略過: {e}")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    text = "\n".join(lines)
    print(text)
    if token and chat_id:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=10)

if __name__ == "__main__":
    res = run_portfolio_backtest()
    send_weekly_backtest_tg(res)
