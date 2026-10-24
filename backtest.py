"""
backtest.py v2 — 與實盤共用 strategy_core 的回測

v1 -> v2 修正 (2026-10-23):
1. 策略邏輯不再自己複製一份: 直接呼叫 strategy_core.evaluate_candidate(df.iloc[:i+1]),
   與 screener.py 完全同一函數。大盤濾網也共用 market_regime_ok()。
2. 法人濾網誠實揭露: 無歷史法人資料, 以 require_chip=False 執行,
   summary 標記 chip_filter_applied=False, 每筆標記 chip_unverified。
3. 組合層級約束: 同時持倉上限 MAX_CONCURRENT_POSITIONS (5 檔), 同一檔股票持倉期間不重複進場,
   同日同股多策略只取第一個。v1 是每筆都以 35% 資金獨立複利, 等於無限槓桿。
   v2 每筆權重 = 1/5 = 20%。
4. 成本逐段計 (strategy_core.simulate_exit): 分批賣出每一筆都扣賣出成本, 不再只扣固定 0.5%。
5. 未走完持有期就遇到資料結尾的交易標記 censored 並剔除, 不再用最後收盤假裝已實現。
6. 揭露限制: universe 仍為「目前」成分/熱門股 (point-in-time 名單未取得),
   summary 明確標記 universe_point_in_time=False, 數字仍有倖存者偏誤, 僅供相對比較。
7. 期間切分: 依進場日筆數切前/後半段分別統計, 讓過度最佳化至少在後半段現形。
"""
import os
import json
import datetime
import requests
import pandas as pd
import yfinance as yf

from screener import build_dynamic_index_universe
from strategy_core import (
    CONFIG, STRATEGY_NAMES as ALL_STRATEGIES, prepare_indicators,
    evaluate_candidate, market_regime_ok, simulate_exit,
)

BACKTEST_FILE = "latest_backtest.json"
MAX_CONCURRENT = CONFIG["MAX_CONCURRENT_POSITIONS"]
SLOT_WEIGHT = 1.0 / MAX_CONCURRENT  # 每筆占組合 20%


def summarize_rows(rows: list) -> dict:
    if not rows:
        return {"trades": 0, "win_rate": 0.0, "avg_return_pct": 0.0}
    wins = sum(r["win"] for r in rows)
    return {
        "trades": len(rows),
        "win_rate": round(wins / len(rows) * 100, 1),
        "avg_return_pct": round(sum(r["net_return_pct"] for r in rows) / len(rows), 2),
    }


def run_portfolio_backtest() -> dict:
    universe = build_dynamic_index_universe()
    sample_items = list(universe.items())[:220]
    tickers = ["0050.TW"] + [f"{sid}{sfx}" for sid, (_, sfx) in sample_items]
    data = yf.download(tickers, start="2020-01-01", group_by="ticker", threads=True, progress=False)

    bench_df = None
    if "0050.TW" in data.columns.get_level_values(0):
        bench_df = data["0050.TW"].dropna(subset=["Close"]).copy()
        bench_df["B_MA20"] = bench_df["Close"].rolling(20).mean()
        bench_df["B_RET20"] = bench_df["Close"].pct_change(20)

    trades = []
    shadow_trades = []  # v3: 同日同股的次要策略訊號也做影子模擬統計 (不占組合資金), 避免次要訊號被優先序吞掉而看不見表現
    censored_count = 0
    skipped_portfolio_full = 0
    skipped_stock_busy = 0
    open_positions = []  # [{stock_id, entry_date, exit_date}]

    for sid, (sname, sfx) in sample_items:
        t_sym = f"{sid}{sfx}"
        if t_sym not in data.columns.get_level_values(0):
            continue
        df_raw = data[t_sym].dropna(subset=["Close", "High", "Low", "Open", "Volume"]).copy()
        if len(df_raw) < 110:
            continue
        df = prepare_indicators(df_raw)

        start_idx = 60
        end_idx = len(df) - 2  # 至少要有隔日 K 做進場確認

        for i in range(start_idx, end_idx):
            dt_idx = df.index[i]
            if bench_df is None or dt_idx not in bench_df.index:
                continue
            b_row = bench_df.loc[dt_idx]
            if pd.isna(b_row["B_MA20"]) or pd.isna(b_row["B_RET20"]):
                continue
            cur_close = float(df["Close"].iloc[i])
            stock_ret20 = (cur_close - float(df["Close"].iloc[i - 20])) / float(df["Close"].iloc[i - 20])
            m_ok = market_regime_ok(float(b_row["Close"]), float(b_row["B_MA20"]), stock_ret20)
            bench_ret20 = float(b_row["B_RET20"])

            # 與實盤同一函數; 切片只到訊號日, 無未來資料
            sigs = evaluate_candidate(df_raw.iloc[:i + 1], sid, sname, sfx,
                                      chip_info=None, bench_ret_20d=bench_ret20,
                                      market_ok=m_ok, require_chip=False)
            if not sigs:
                continue
            sig = sigs[0]  # 同日同股多策略只取優先序第一個, 不重複曝險
            signal_close = sig["entry_price"]
            stop_p = sig["hard_stop"]

            # 隔日進場確認 (與 v1 相同規則, 保留)
            next_bar = df.iloc[i + 1]
            n_open = float(next_bar["Open"])
            n_high = float(next_bar["High"])
            n_close = float(next_bar["Close"])
            if n_open > signal_close * 1.028 or n_open <= stop_p:
                continue
            trigger_buy = round(max(signal_close, n_open * 1.003), 2)
            if n_high < trigger_buy or n_close < n_open * 0.998:
                continue

            entry_date = df.index[i + 1]
            # 組合約束: 同股未出場不重進; 同時持倉未滿才進
            overlapping = [p for p in open_positions if p["entry_date"] <= entry_date < p["exit_date"]]
            if any(p["stock_id"] == sid for p in overlapping):
                skipped_stock_busy += 1
                continue
            if len(overlapping) >= MAX_CONCURRENT:
                skipped_portfolio_full += 1
                continue

            result = simulate_exit(df, i + 1, trigger_buy, stop_p)
            if result["censored"]:
                censored_count += 1
                continue
            exit_date = df.index[result["exit_idx"]]
            trades.append({
                "stock_id": sid,
                "strategy": sig["strategy"],
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "exit_date": exit_date.strftime("%Y-%m-%d"),
                "exit_reason": result["exit_reason"],
                "net_return_pct": result["net_ret_pct"],
                "win": result["win"],
                "chip_unverified": True,
            })
            open_positions.append({"stock_id": sid, "entry_date": entry_date, "exit_date": exit_date})

            # v3 shadow: 次要策略僅統計, 用同一套隔日進場確認與出場模擬
            for extra in sigs[1:]:
                e_stop = extra["hard_stop"]
                if n_open > extra["entry_price"] * 1.028 or n_open <= e_stop:
                    continue
                e_trigger = round(max(extra["entry_price"], n_open * 1.003), 2)
                if n_high < e_trigger or n_close < n_open * 0.998:
                    continue
                e_res = simulate_exit(df, i + 1, e_trigger, e_stop)
                if e_res["censored"]:
                    continue
                shadow_trades.append({
                    "strategy": extra["strategy"],
                    "net_return_pct": e_res["net_ret_pct"],
                    "win": e_res["win"],
                })

    df_t = pd.DataFrame(trades)
    if df_t.empty:
        summary = {
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": "v3_strategy_optimization", "total_trades": 0, "win_rate": 0.0,
            "rr_ratio": 0.0, "mdd_pct": 0.0, "cum_return_pct": 0.0, "by_strategy": {},
            "period_split": {"first_half": summarize_rows([]), "second_half": summarize_rows([])},
            "censored_trades_excluded": censored_count,
            "skipped_portfolio_full": skipped_portfolio_full,
            "skipped_stock_busy": skipped_stock_busy,
            "max_concurrent_positions": MAX_CONCURRENT,
            "slot_weight_pct": SLOT_WEIGHT * 100,
            "chip_filter_applied": False, "universe_point_in_time": False,
        }
        with open(BACKTEST_FILE, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        return summary

    df_t = df_t.sort_values("entry_date").reset_index(drop=True)
    df_t["port_impact"] = df_t["net_return_pct"] * SLOT_WEIGHT
    equity_curve = 100.0 * (1.0 + df_t["port_impact"] / 100.0).cumprod()
    peak = equity_curve.cummax()
    dd = ((equity_curve - peak) / peak) * 100.0

    wins = df_t[df_t["net_return_pct"] > 0]["net_return_pct"]
    losses = df_t[df_t["net_return_pct"] <= 0]["net_return_pct"]
    avg_win = float(wins.mean()) if not wins.empty else 0.0
    avg_loss = abs(float(losses.mean())) if not losses.empty else 1.0

    by_strat = {}
    for s_name in ALL_STRATEGIES:
        grp = df_t[df_t["strategy"] == s_name]
        by_strat[s_name] = summarize_rows(grp.to_dict("records"))

    mid = len(df_t) // 2
    period_split = {
        "first_half": summarize_rows(df_t.iloc[:mid].to_dict("records")),
        "second_half": summarize_rows(df_t.iloc[mid:].to_dict("records")),
    }

    summary = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "v3_strategy_optimization",
        "universe_scope": "台灣50 + 中型100 + 富櫃50 + 富櫃200 + MSCI成分股 (目前名單, 非 point-in-time)",
        "total_trades": int(len(df_t)),
        "win_rate": round(float(df_t["win"].mean() * 100), 1),
        "rr_ratio": round(avg_win / max(avg_loss, 0.1), 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(-avg_loss, 2),
        "cum_return_pct": round(float(equity_curve.iloc[-1] - 100.0), 2),
        "mdd_pct": round(float(dd.min()), 2),
        "by_strategy": by_strat,
        "shadow_by_strategy": {s: summarize_rows([t for t in shadow_trades if t["strategy"] == s]) for s in ALL_STRATEGIES},
        "period_split": period_split,
        "censored_trades_excluded": censored_count,
        "skipped_portfolio_full": skipped_portfolio_full,
        "skipped_stock_busy": skipped_stock_busy,
        "max_concurrent_positions": MAX_CONCURRENT,
        "slot_weight_pct": SLOT_WEIGHT * 100,
        "chip_filter_applied": False,
        "universe_point_in_time": False,
        "notes": "回測與實盤共用 strategy_core.evaluate_candidate; 法人濾網未套用 (無歷史資料); 名單為目前成分股, 仍有倖存者偏誤, 數字僅供相對比較。",
    }
    with open(BACKTEST_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary


def send_weekly_backtest_tg(summary: dict):
    lines = [
        f"📈【週六回測 v2 統一核心版｜{datetime.date.today().strftime('%Y-%m-%d')}】",
        f"• 總交易筆數：{summary['total_trades']} 筆 (另剔除未完交易 {summary.get('censored_trades_excluded', 0)} 筆)",
        f"• 整體勝率：{summary['win_rate']}%｜盈虧比 {summary['rr_ratio']} : 1",
        f"• 組合累計報酬 (每筆權重 {summary.get('slot_weight_pct', 20)}%, 最多同時 {summary.get('max_concurrent_positions', 5)} 檔)：{summary['cum_return_pct']:+.2f}%",
        f"• 最大回撤 (MDD)：{summary['mdd_pct']:.2f}%",
        "• ⚠️ 法人濾網未套用於回測 (無歷史資料)，且名單為目前成分股，數字僅供相對比較。",
        "\n📊【各策略細項】：",
    ]
    for s_name in ALL_STRATEGIES:
        v = summary.get("by_strategy", {}).get(s_name, {"trades": 0, "win_rate": 0.0, "avg_return_pct": 0.0})
        lines.append(f" - {s_name}：勝率 {v['win_rate']}% ({v['trades']}筆)｜單筆平均 {v['avg_return_pct']:+.2f}%")

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"請根據以下台股六大策略(S0~S5) v2 回測數據，用 3 句話給主人下週操作重點與資金配置建議：{json.dumps(summary, ensure_ascii=False)}"
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
