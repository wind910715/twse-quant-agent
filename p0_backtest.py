"""
xq_main_engine_retest.py — XQ 夜班通過者 (C21/C02/S02) 主系統引擎重測 (實驗分支專用)

目的: 回答「XQ 三項通過者能否融入主系統 twse-quant-agent (S0/S1/S2)」。
方法: 取 XQ 夜班凍結規格的「進場訊號」,套上主系統引擎 (同一資金、出場、成本、
組合約束),與主系統基線 (S0/S1/S2) 在同一宇宙、同一資料、同一程式版本下比較,
並做併跑增量測試與訊號重疊測試。

預註冊設計 (2026-10-29,跑前凍結,跑完不改參數翻盤):
- 宇宙: screener.build_dynamic_index_universe() 取前 220 檔,與 backtest.py 完全相同。
- 窗口: yfinance 2020-01-01 起 (與 backtest.py 相同)。
- 訊號來源:
  - S0/S1/S2: 與 backtest.py 完全相同的迴圈呼叫 strategy_core.evaluate_candidate
    (require_chip=False,與週六回測同口徑;法人濾網未套用,須揭露)。
  - C21/C02/S02: XQ 夜班結果檔 xq_signals/{C21,C02,S02}.csv 的 (ticker, signal_date)
    (夜班已按凍結規格驗證過訊號;本重測不重新實作 XQ 進場條件,避免二度實作誤差)。
- 主系統閘門 (新策略訊號日當天必須通過,否則剔除):
  MV20 >= 1000 張、收盤 >= 25 元、market_regime_ok (0050 vs MA20 + 個股 20 日漲幅)。
  注意:此為「XQ 訊號 + 主系統閘門」兩段式篩選;XQ 自帶的大盤濾網/5 日均量門檻
  已包含在夜班訊號產生過程,不在本重測重複施加,也不反向放寬。
- 進場: 與 backtest.py 完全相同的隔日進場確認 (跳空 >2.8% 跳過、開盤破停損跳過、
  trigger = max(訊號收盤, 次日開盤 x1.003)、須高點觸及 trigger 且收盤不低於開盤 x0.998)。
- 停損 (新策略專用假設,因 XQ 規格本身為固定持有期、未定義結構停損):
  採用主系統 S2 式停損 = max(訊號日低點 x0.995, 訊號收盤 x0.975)。
  此為重測假設,非 XQ 原規格;若日後上線,停損須另行凍結規格驗證。
- 出場: strategy_core.simulate_exit 全套 (三階停利 TP1 +6%/TP2 +14%/TP3 +25%、
  保本/移動停利、3 日不漲汰弱、MAX_HOLD 55 日、未走完剔除 censored)。
  注意: C02 原規格為 60 日持有,主系統 MAX_HOLD 上限 55 日,故 C02 在本重測
  最長只持有 55 日,此為引擎限制造成的偏離,須揭露。
- 成本: simulate_exit 內含 (買入 0.1425%、每段賣出 0.4425%),與主系統相同。
  (夜班 XQ 成本為往返固定 0.585%,與本重測不同,數字不可直接對比。)
- 組合約束: 同時持倉上限 5 檔 (每筆權重 20%)、同股持倉期間不重進、
  同日同股只取優先序第一個。優先序: S0 > S1 > S2 > C21 > C02 > S02
  (既有策略優先,新策略只補既有未取的缺口,此為保守的增量測試)。
- 執行組態 (同一訊號集,五次組合模擬):
  baseline (S0/S1/S2)、C21-only、C02-only、S02-only、combined (六策略併跑)。
- 統計: 每組 n、勝率、平均報酬、RR (平均贏/|平均輸|)、前後半 (按進場日筆數切半)、
  組合累計報酬 (20% 權重複利)、MDD;另計訊號日重疊率 (新策略訊號與 S0/S1/S2
  同股同日) 與 combined 實際執行的新策略筆數。
- 整合判定門檻 (事前宣告,僅供參考,最終由使用者決定):
  單策略通過主系統標準 (平均 >0、RR >= 1.8、前後半皆 >0、n >= 30),
  且 combined 累計報酬 > baseline 累計報酬、且 combined MDD 不比 baseline 差超過 3 個百分點。

限制揭露 (必讀):
- 宇宙為目前成分名單 (非 point-in-time),仍有倖存者偏誤,數字僅供相對比較。
- 法人濾網未套用 (無歷史法人資料),與週六回測同口徑。
- 新策略訊號來自夜班 XQ 宇宙 (529 檔),本重測限制在 220 檔樣本內,筆數會縮水。
- S02 訊號僅自 2021Q1 起 (2020 各季缺 2019 年報基期,夜班已揭露)。
- 本程式不寫入 latest_backtest.json、不動 monitor/screener、不發 TG、不下單。
"""
import datetime
import json

import pandas as pd
import yfinance as yf

from screener import build_dynamic_index_universe
from strategy_core import (
    CONFIG, prepare_indicators, evaluate_candidate, market_regime_ok, simulate_exit,
    chip_is_positive,
)

SAMPLE_N = 220
SLOT_WEIGHT = 1.0 / CONFIG["MAX_CONCURRENT_POSITIONS"]  # 0.20
MAX_CONCURRENT = CONFIG["MAX_CONCURRENT_POSITIONS"]

# 優先序 (數字越小越優先;既有策略優先於新策略)
PRIORITY = {"S0_VCP收斂": 0, "S1_破底翻": 1, "S2_量增突破": 2, "C21": 3, "C02": 4, "S02": 5}
NEW_FILES = {"C21": "xq_signals/C21.csv", "C02": "xq_signals/C02.csv", "S02": "xq_signals/S02.csv"}

OUT_JSON = "p0_results.json"
OUT_MD = "p0_report.md"
CHIP_FILE = "xq_signals/chip_daily.csv"


def summarize_trades(trades):
    """與 backtest.py 同口徑的組合統計 (trades 已按 entry_date 排序)。"""
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "avg_return_pct": 0.0, "rr_ratio": 0.0,
                "avg_win_pct": 0.0, "avg_loss_pct": 0.0, "cum_return_pct": 0.0,
                "mdd_pct": 0.0, "first_half": {"trades": 0, "avg_return_pct": 0.0},
                "second_half": {"trades": 0, "avg_return_pct": 0.0}}
    df = pd.DataFrame(trades).sort_values("entry_date").reset_index(drop=True)
    df["port_impact"] = df["net_return_pct"] * SLOT_WEIGHT
    equity = 100.0 * (1.0 + df["port_impact"] / 100.0).cumprod()
    peak = equity.cummax()
    dd = ((equity - peak) / peak) * 100.0
    wins = df[df["net_return_pct"] > 0]["net_return_pct"]
    losses = df[df["net_return_pct"] <= 0]["net_return_pct"]
    avg_win = float(wins.mean()) if not wins.empty else 0.0
    avg_loss = abs(float(losses.mean())) if not losses.empty else 1.0
    mid = len(df) // 2

    def half_stats(sub):
        if sub.empty:
            return {"trades": 0, "avg_return_pct": 0.0}
        return {"trades": int(len(sub)),
                "avg_return_pct": round(float(sub["net_return_pct"].mean()), 4)}

    return {
        "trades": int(len(df)),
        "win_rate": round(float(df["win"].mean() * 100), 1),
        "avg_return_pct": round(float(df["net_return_pct"].mean()), 4),
        "rr_ratio": round(avg_win / max(avg_loss, 0.1), 4),
        "avg_win_pct": round(avg_win, 4),
        "avg_loss_pct": round(-avg_loss, 4),
        "cum_return_pct": round(float(equity.iloc[-1] - 100.0), 2),
        "mdd_pct": round(float(dd.min()), 2),
        "first_half": half_stats(df.iloc[:mid]),
        "second_half": half_stats(df.iloc[mid:]),
    }


def by_strategy_stats(trades):
    out = {}
    for s in sorted({t["strategy"] for t in trades}):
        grp = [t for t in trades if t["strategy"] == s]
        out[s] = {"trades": len(grp),
                  "win_rate": round(sum(t["win"] for t in grp) / len(grp) * 100, 1),
                  "avg_return_pct": round(sum(t["net_return_pct"] for t in grp) / len(grp), 4)}
    return out


def simulate_portfolio(signals_by_stock, sample_order, dfs, allowed):
    """組合模擬 (迴圈順序與 backtest.py 相同: 股票樣本順序 x 日期 ascending,
    全域 open_positions;同日同股已事先按優先序去重)。"""
    trades = []
    open_positions = []
    skipped_full = 0
    skipped_busy = 0
    censored = 0
    for sid in sample_order:
        df = dfs.get(sid)
        if df is None:
            continue
        for sig in signals_by_stock.get(sid, []):
            if sig["strategy"] not in allowed:
                continue
            i = sig["date_idx"]
            if i + 1 >= len(df):
                continue
            signal_close = sig["entry_price"]
            stop_p = sig["hard_stop"]
            next_bar = df.iloc[i + 1]
            n_open, n_high, n_close = float(next_bar["Open"]), float(next_bar["High"]), float(next_bar["Close"])
            if n_open > signal_close * 1.028 or n_open <= stop_p:
                continue
            trigger_buy = round(max(signal_close, n_open * 1.003), 2)
            if n_high < trigger_buy or n_close < n_open * 0.998:
                continue
            entry_date = df.index[i + 1]
            overlapping = [p for p in open_positions if p["entry_date"] <= entry_date < p["exit_date"]]
            if any(p["stock_id"] == sid for p in overlapping):
                skipped_busy += 1
                continue
            if len(overlapping) >= MAX_CONCURRENT:
                skipped_full += 1
                continue
            result = simulate_exit(df, i + 1, trigger_buy, stop_p)
            if result["censored"]:
                censored += 1
                continue
            exit_date = df.index[result["exit_idx"]]
            trades.append({
                "stock_id": sid, "strategy": sig["strategy"],
                "entry_date": entry_date.strftime("%Y-%m-%d"),
                "exit_date": exit_date.strftime("%Y-%m-%d"),
                "exit_reason": result["exit_reason"],
                "net_return_pct": result["net_ret_pct"], "win": result["win"],
            })
            open_positions.append({"stock_id": sid, "entry_date": entry_date, "exit_date": exit_date})
    return trades, {"skipped_portfolio_full": skipped_full, "skipped_stock_busy": skipped_busy,
                    "censored_excluded": censored}


def main():
    universe = build_dynamic_index_universe()
    sample_items = list(universe.items())[:SAMPLE_N]
    sample_order = [sid for sid, _ in sample_items]
    sample_tickers = {f"{sid}{sfx}" for sid, (_, sfx) in sample_items}
    tickers = ["0050.TW"] + [f"{sid}{sfx}" for sid, (_, sfx) in sample_items]
    print(f"universe total={len(universe)} sample={len(sample_items)}", flush=True)

    data = yf.download(tickers, start="2020-01-01", group_by="ticker", threads=True, progress=False)
    bench_df = None
    if "0050.TW" in data.columns.get_level_values(0):
        bench_df = data["0050.TW"].dropna(subset=["Close"]).copy()
        bench_df["B_MA20"] = bench_df["Close"].rolling(20).mean()
        bench_df["B_RET20"] = bench_df["Close"].pct_change(20)

    # P0: 載入 FinMind 三大法人日資料 (本地抓取,隨分支提交,Actions 離線讀檔)。
    # 欄位: stock_id,date,foreign_net,it_net,dealer_net,total_inst (張)。
    # foreign = Foreign_Investor + Foreign_Dealer_Self (對齊 T86 外資及陸資合計);
    # total = foreign + it + dealer (對齊 T86 三大法人合計)。
    chip = {}
    with open(CHIP_FILE, encoding="utf-8") as fh:
        header = fh.readline()
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) != 6:
                continue
            sid_c, date_c, f_c, it_c, dl_c, tot_c = parts
            chip[(sid_c, date_c)] = {"foreign_net": float(f_c), "it_net": float(it_c),
                                     "total_inst": float(tot_c), "has_data": True}
    print(f"chip rows loaded: {len(chip)}", flush=True)

    # signals[(sid, date_idx)] = best signal by priority (unfiltered)
    signals = {}
    # signals_chip: same, but only (sid, date) passing chip_is_positive (FinMind data).
    # 生產 evaluate_candidate 的法人閘門在策略匹配前整組擋下,與此手動過濾等價
    # (策略匹配本身不依賴 chip),故一次 evaluate 呼叫可同時產出兩組,無須兩倍運算。
    signals_chip = {}
    chip_stats = {"s_checked": 0, "s_passed": 0, "new_skipped_chip": 0}
    dfs = {}
    # --- S0/S1/S2: 與 backtest.py 完全相同的迴圈 ---
    for sid, (sname, sfx) in sample_items:
        t_sym = f"{sid}{sfx}"
        if t_sym not in data.columns.get_level_values(0):
            continue
        df_raw = data[t_sym].dropna(subset=["Close", "High", "Low", "Open", "Volume"]).copy()
        if len(df_raw) < 110:
            continue
        df = prepare_indicators(df_raw)
        dfs[sid] = df
        start_idx, end_idx = 60, len(df) - 2
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
            sigs = evaluate_candidate(df_raw.iloc[:i + 1], sid, sname, sfx,
                                      chip_info=None, bench_ret_20d=float(b_row["B_RET20"]),
                                      market_ok=m_ok, require_chip=False)
            if not sigs:
                continue
            sig = sigs[0]  # 同日同股取優先序第一個 (與 backtest.py 相同)
            key = (sid, i)
            if key not in signals or PRIORITY[sig["strategy"]] < PRIORITY[signals[key]["strategy"]]:
                signals[key] = {"date_idx": i, "strategy": sig["strategy"],
                                "entry_price": sig["entry_price"], "hard_stop": sig["hard_stop"]}
            # P0: 法人濾網平行組 (同訊號,僅多一道 chip 關卡)。
            chip_stats["s_checked"] += 1
            cinfo = chip.get((sid, dt_idx.strftime("%Y-%m-%d")))
            if cinfo is not None and chip_is_positive(cinfo):
                chip_stats["s_passed"] += 1
                if key not in signals_chip or PRIORITY[sig["strategy"]] < PRIORITY[signals_chip[key]["strategy"]]:
                    signals_chip[key] = {"date_idx": i, "strategy": sig["strategy"],
                                         "entry_price": sig["entry_price"], "hard_stop": sig["hard_stop"]}
        if len(dfs) % 50 == 0:
            print(f"base signals done: {len(dfs)} stocks", flush=True)
    print(f"S0/S1/S2 signals: {sum(1 for v in signals.values() if v['strategy'].startswith('S'))}", flush=True)

    # --- C21/C02/S02: 夜班 CSV 訊號 + 主系統閘門 ---
    new_stats = {}
    date_pos_cache = {}
    for strat, path in NEW_FILES.items():
        sig_df = pd.read_csv(path)
        raw_n = len(sig_df)
        gated = []
        skipped_no_data = 0
        skipped_gate = 0
        for row in sig_df.itertuples(index=False):
            ticker, sig_date = row.ticker, str(row.signal_date)
            if ticker not in sample_tickers:
                continue
            sid = next((s for s, (_, sfx) in sample_items if f"{s}{sfx}" == ticker), None)
            if sid is None or sid not in dfs:
                continue
            df = dfs[sid]
            if sid not in date_pos_cache:
                date_pos_cache[sid] = {d.strftime("%Y-%m-%d"): k for k, d in enumerate(df.index)}
            date_pos = date_pos_cache[sid]
            if sig_date not in date_pos:
                skipped_no_data += 1
                continue
            i = date_pos[sig_date]
            if i < 21 or i + 1 >= len(df):
                skipped_no_data += 1
                continue
            cur = df.iloc[i]
            if pd.isna(cur["MV20"]) or cur["MV20"] < CONFIG["MIN_MV20_LOTS"] or cur["Close"] < CONFIG["MIN_PRICE"]:
                skipped_gate += 1
                continue
            dt_idx = df.index[i]
            if bench_df is None or dt_idx not in bench_df.index:
                skipped_gate += 1
                continue
            b_row = bench_df.loc[dt_idx]
            if pd.isna(b_row["B_MA20"]) or pd.isna(b_row["B_RET20"]):
                skipped_gate += 1
                continue
            stock_ret20 = (float(cur["Close"]) - float(df["Close"].iloc[i - 20])) / float(df["Close"].iloc[i - 20])
            if not market_regime_ok(float(b_row["Close"]), float(b_row["B_MA20"]), stock_ret20):
                skipped_gate += 1
                continue
            entry_price = round(float(cur["Close"]), 2)
            hard_stop = round(float(max(float(cur["Low"]) * 0.995, entry_price * 0.975)), 2)
            key = (sid, i)
            cand = {"date_idx": i, "strategy": strat, "entry_price": entry_price, "hard_stop": hard_stop}
            if key not in signals or PRIORITY[strat] < PRIORITY[signals[key]["strategy"]]:
                signals[key] = cand
            # P0: 濾網組僅在 chip 通過時加入 (生產對全策略同此閘門)。
            cinfo_n = chip.get((sid, sig_date))
            if cinfo_n is not None and chip_is_positive(cinfo_n):
                if key not in signals_chip or PRIORITY[strat] < PRIORITY[signals_chip[key]["strategy"]]:
                    signals_chip[key] = cand
            else:
                chip_stats["new_skipped_chip"] += 1
            gated.append(cand)
        new_stats[strat] = {"csv_signals": int(raw_n), "in_sample_gated": len(gated),
                            "skipped_no_data": skipped_no_data, "skipped_gate": skipped_gate}
        print(f"{strat}: csv={raw_n} gated={len(gated)} no_data={skipped_no_data} gate={skipped_gate}", flush=True)

    # 整理成 per-stock 訊號表 (日期 ascending)
    signals_by_stock = {}
    for (sid, i), sig in signals.items():
        signals_by_stock.setdefault(sid, []).append(sig)
    for sid in signals_by_stock:
        signals_by_stock[sid].sort(key=lambda s: s["date_idx"])
    signals_by_stock_chip = {}
    for (sid, i), sig in signals_chip.items():
        signals_by_stock_chip.setdefault(sid, []).append(sig)
    for sid in signals_by_stock_chip:
        signals_by_stock_chip[sid].sort(key=lambda s: s["date_idx"])
    # 上市限定 (實盤 T86 僅覆蓋上市,故 P0 判定以此組為準)。
    tw_order = [sid for sid, (_, sfx) in sample_items if sfx == ".TW"]
    signals_by_stock_chip_tw = {sid: signals_by_stock_chip.get(sid, []) for sid in tw_order}

    # 訊號日重疊: 新策略訊號與 S0/S1/S2 同股同日
    base_keys = {(sid, s["date_idx"]) for sid, lst in signals_by_stock.items()
                 for s in lst if s["strategy"] in ("S0_VCP收斂", "S1_破底翻", "S2_量增突破")}
    overlap = {}
    for strat in ("C21", "C02", "S02"):
        keys = [(sid, s["date_idx"]) for sid, lst in signals_by_stock.items()
                for s in lst if s["strategy"] == strat]
        ov = sum(1 for k in keys if k in base_keys)
        overlap[strat] = {"signals": len(keys), "overlap_same_day": ov,
                          "overlap_pct": round(ov / max(len(keys), 1) * 100, 1)}

    # 六次組合模擬 (同一訊號集、同一資料,僅濾網與市場範圍不同)。
    s_all = {"S0_VCP收斂", "S1_破底翻", "S2_量增突破"}
    s_all6 = {"S0_VCP收斂", "S1_破底翻", "S2_量增突破", "C21", "C02", "S02"}
    runs = {
        "baseline_S0S1S2": (signals_by_stock, sample_order, s_all),
        "combined_all6": (signals_by_stock, sample_order, s_all6),
        "baseline_chip": (signals_by_stock_chip, sample_order, s_all),
        "combined_chip": (signals_by_stock_chip, sample_order, s_all6),
        "baseline_chip_tw": (signals_by_stock_chip_tw, tw_order, s_all),
        "combined_chip_tw": (signals_by_stock_chip_tw, tw_order, s_all6),
    }
    results = {"design": {
        "universe": f"build_dynamic_index_universe()[:{SAMPLE_N}] (same as backtest.py)",
        "window": "2020-01-01 ~ latest (yfinance)",
        "entry": "next-day confirmation (same as backtest.py)",
        "exit": "strategy_core.simulate_exit (TP1/2/3, trailing, 3d weak, MAX_HOLD 55)",
        "costs": "BUY 0.1425% + per-segment SELL 0.4425% (via simulate_exit)",
        "portfolio": "max 5 concurrent, 20% weight each, no re-entry while holding",
        "priority": "S0 > S1 > S2 > C21 > C02 > S02",
        "new_stop_rule": "max(signal-day low x0.995, signal close x0.975) [S2-style assumption]",
        "c02_note": "C02 XQ spec = 60d hold; capped at MAX_HOLD 55d by engine",
        "chip_filter": "FinMind TaiwanStockInstitutionalInvestorsBuySell (local fetch, committed as xq_signals/chip_daily.csv); chip_is_positive v2 (total>0 and foreign>0 or it>0); foreign = Foreign_Investor + Foreign_Dealer_Self (T86 aggregate); live T86 covers TW only, so verdict uses TW-only runs",
        "p0_verdict_rule": "TW-only: combined_chip_tw cum > baseline_chip_tw cum and MDD not worse by >3pp",
        "survivorship": "universe = current list, NOT point-in-time (same limitation as main backtest)",
    }, "new_signal_attrition": new_stats, "overlap": overlap, "chip_stats": chip_stats, "runs": {}}
    combined_chip_trades = []
    for name, (sbs, order, allowed) in runs.items():
        trades, meta = simulate_portfolio(sbs, order, dfs, allowed)
        summ = summarize_trades(trades)
        summ.update(meta)
        summ["by_strategy"] = by_strategy_stats(trades)
        results["runs"][name] = summ
        if name == "combined_chip":
            combined_chip_trades = trades
        print(f"RUN {name}: n={summ['trades']} win={summ['win_rate']}% avg={summ['avg_return_pct']} "
              f"RR={summ['rr_ratio']} cum={summ['cum_return_pct']}% MDD={summ['mdd_pct']}%", flush=True)

    # 濾網後 combined 的上市/上櫃分項 (以交易股票的市場別分)。
    sfx_map = {sid: sfx for sid, (_, sfx) in sample_items}
    breakdown = {}
    for mkt, sfx_want in (("TW", ".TW"), ("TWO", ".TWO")):
        grp = [t for t in combined_chip_trades if sfx_map.get(t["stock_id"]) == sfx_want]
        breakdown[mkt] = {"trades": len(grp),
                          "avg_return_pct": round(sum(t["net_return_pct"] for t in grp) / max(len(grp), 1), 4)}
    results["combined_chip_breakdown"] = breakdown

    # P0 判定 (事前宣告,僅供參考,最終由使用者決定;以 TW-only 為準)。
    base = results["runs"]["baseline_S0S1S2"]
    comb = results["runs"]["combined_all6"]
    base_c = results["runs"]["baseline_chip"]
    comb_c = results["runs"]["combined_chip"]
    base_tw = results["runs"]["baseline_chip_tw"]
    comb_tw = results["runs"]["combined_chip_tw"]
    assessment = {}
    assessment["combined_improves"] = bool(
        comb["cum_return_pct"] > base["cum_return_pct"]
        and comb["mdd_pct"] >= base["mdd_pct"] - 3.0)
    assessment["p0_full"] = {
        "filtered_combined_beats_filtered_baseline": bool(
            comb_c["cum_return_pct"] > base_c["cum_return_pct"]
            and comb_c["mdd_pct"] >= base_c["mdd_pct"] - 3.0),
        "filtered_comb_cum": comb_c["cum_return_pct"], "filtered_base_cum": base_c["cum_return_pct"],
        "filtered_comb_mdd": comb_c["mdd_pct"], "filtered_base_mdd": base_c["mdd_pct"],
    }
    assessment["p0_tw"] = {
        "pass": bool(
            comb_tw["cum_return_pct"] > base_tw["cum_return_pct"]
            and comb_tw["mdd_pct"] >= base_tw["mdd_pct"] - 3.0),
        "filtered_comb_cum": comb_tw["cum_return_pct"], "filtered_base_cum": base_tw["cum_return_pct"],
        "filtered_comb_mdd": comb_tw["mdd_pct"], "filtered_base_mdd": base_tw["mdd_pct"],
    }
    results["assessment"] = assessment
    results["generated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # Markdown 報告
    lines = [f"# P0 法人濾網回測報告 ({results['generated_at']})", "",
             "## 設計 (預註冊,跑前凍結)", ""]
    for k, v in results["design"].items():
        lines.append(f"- {k}: {v}")
    lines += ["", "## 法人濾網統計", "",
              f"- S 訊號檢查 {chip_stats['s_checked']} 筆、通過 {chip_stats['s_passed']} 筆",
              f"- 新策略訊號被濾網擋下 {chip_stats['new_skipped_chip']} 筆",
              "", "## 濾網後 combined 的上市/上櫃分項", ""]
    for mkt, b in breakdown.items():
        lines.append(f"- {mkt}: {b['trades']} 筆、平均 {b['avg_return_pct']}%")
    lines += ["", "## 訊號耗損 (夜班 CSV → 220 檔樣本 → 主系統閘門)", ""]
    for strat, st in new_stats.items():
        lines.append(f"- {strat}: CSV {st['csv_signals']} 筆 → 閘門後 {st['in_sample_gated']} 筆 "
                     f"(無資料跳過 {st['skipped_no_data']}、閘門剔除 {st['skipped_gate']})")
    lines += ["", "## 訊號日重疊 (新策略訊號與 S0/S1/S2 同股同日)", ""]
    for strat, ov in overlap.items():
        lines.append(f"- {strat}: {ov['signals']} 訊號中 {ov['overlap_same_day']} 筆重疊 ({ov['overlap_pct']}%)")
    lines += ["", "## 各組結果", "",
              "| 組別 | n | 勝率% | 平均% | RR | 前半平均% | 後半平均% | 累計% | MDD% |",
              "|---|---|---|---|---|---|---|---|---|"]
    for name, r in results["runs"].items():
        lines.append(f"| {name} | {r['trades']} | {r['win_rate']} | {r['avg_return_pct']} | {r['rr_ratio']} "
                     f"| {r['first_half']['avg_return_pct']} | {r['second_half']['avg_return_pct']} "
                     f"| {r['cum_return_pct']} | {r['mdd_pct']} |")
    lines += ["", "## 各組策略細項", ""]
    for name, r in results["runs"].items():
        lines.append(f"### {name}")
        for s, v in r["by_strategy"].items():
            lines.append(f"- {s}: {v['trades']} 筆、勝率 {v['win_rate']}%、平均 {v['avg_return_pct']}%")
    lines += ["", "## P0 判定 (事前門檻,僅供參考)", ""]
    for strat, a in assessment.items():
        lines.append(f"- {strat}: {a}")
    lines += ["",
              "## 限制",
              "- 宇宙為目前成分名單 (非 point-in-time),仍有倖存者偏誤,數字僅供相對比較。",
              "- 法人資料來自 FinMind (免費版日資料),與實盤 T86 來源不同,但濾網定義相同;上櫃在 FinMind 有資料、在 T86 無資料,故判定以 TW-only 為準。",
              "- 新策略停損為 S2 式假設,非 XQ 原規格;C02 持有上限由 60 日壓至 55 日。",
              "- 本報告不構成上線建議;最終是否融入由使用者決定。"]
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("WROTE", OUT_JSON, OUT_MD, flush=True)


if __name__ == "__main__":
    main()
