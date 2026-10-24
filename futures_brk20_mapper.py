#!/usr/bin/env python3
"""
futures_brk20_mapper.py — 小股期 BRK20 映射版訊號產生器（repo 根目錄，與 screener.py / backtest.py 同層）

定位
----
本程式是 twse-quant-agent 主系統「BRK20 突破量增」訊號的小股期（小現股期貨，100 股／口）
映射版：把主系統的日線訊號換算成期貨口數，只輸出乾淨的訊號 JSON，不做其他事。
驗證底本：2026-10-25 小股期主系統映射期貨版第一輪回測（現股代理版），
BRK20 映射版通過預註冊三標準（2,360 筆、勝率 51.1%、平均 +1.47%、RR 1.92、
前半超額 +0.10pp／後半 +0.61pp、映射劣化 −0.02pp）；S0／S3 映射版未通過，故不在本程式內。

限制（使用前必讀，不可省略）
----------------------------
(1) 本程式為現股還原價代理版：訊號與出場皆以現股 Adj Close 還原價計算，
    期貨基差未建模。正式實盤前，必須以期貨價格＋真實手續費＋價差／滑價假設重測通過，
    在那之前本程式的輸出只能當研究訊號，不能直接下單。
(2) BRK20 前半超額僅 +0.10pp，偏薄：期貨版重測若滑價稍大即可能翻掉，勿把通過解讀為穩固。
(3) 本程式只含 BRK20：S0／S3 映射版在同一驗證中未通過，絕對不要把它們加回來；
    S0／S3 在生產版現股宇宙的地位不受此影響，本程式只是不寫它們的期貨映射。

與 SSOT（strategy_core.py）的對照關係（為什麼不直接回傳 evaluate_candidate）
--------------------------------------------------------------------------
指標全部來自 strategy_core.prepare_indicators（SSOT），BRK20 三條件與
strategy_core.evaluate_candidate 內的 brk_hit 逐字一致：
    收盤 ＞ 前 20 日最高（不含訊號日當根）；
    量（張） ≥ 1.5 × MV20；
    收盤 ＞ MA60。
停損亦一致：stop = round(max(訊號日低點 × 0.995, 進場 × 0.975), 2)，
進場 = round(訊號日收盤, 2)。映射研究的 matcher 與 evaluate_candidate
在「現股部位通過（lots > 0）」的子集上抽樣 300 組比對，0 不一致。

不直接用 evaluate_candidate 回傳的原因有二，皆為映射研究本身的要求：
(a) evaluate_candidate 內含 calc_position_size（1000 股／張）並丟棄 lots ≤ 0 的訊號；
    映射研究的核心正是期貨 100 股／口救回這些高價訊號（現股版跳過 267 筆 BRK20，
    映射版僅跳過 4 筆）。直接用它，會把本程式要輸出的訊號藏起來。
(b) evaluate_candidate 含法人閘門（REQUIRE_CHIP_DATA=True 時無資料不給訊號）；
    映射驗證未納入法人濾網，故本程式不套用。若實盤要加法人防禦濾網，
    由使用者在 TG 串接層自行決定，不得默默改變已驗證的訊號集。
本程式也不套用 S 家族的 rs_strong／ud_vol_ratio 閘門：生產版 v3.4 口徑即為
BRK20 不經 S 家族閘門（優先序最低），僅受 market_ok（market_regime_ok）與
基本閘門（MA60 非空、MV20 ≥ 1000 張、收盤 ≥ 25 元）約束。

隔日進場確認（與生產版／映射研究同口徑，僅供歷史驗證與隔日觸發價參考）
--------------------------------------------------------------------------
對訊號日次一根 K（n_open / n_high / n_close，n = next bar）：
    若 n_open ＞ 訊號收盤 × 1.028，或 n_open ≤ 停損 → 跳過（隔日跳空過大或開盤即破停損）；
    trigger = round(max(訊號收盤, n_open × 1.003), 2)；
    若 n_high ＜ trigger，或 n_close ＜ n_open × 0.998 → 跳過（未觸發發價或收盤轉弱）；
    否則確認，entry_ref = trigger。
注意：實盤上 trigger 是隔日盤中觸發價，本程式用日線只能事後驗證；
當日訊號（尚未有次根）在輸出中標為 pending_confirmation，entry_ref 暫填訊號收盤。

期貨口數換算（凍結，映射研究指定公式）
--------------------------------------
總資金參數化（預設 3,000,000 元，與主系統 CONFIG["ACCOUNT_EQUITY"] 一致）；
單筆風險 1.2% → 風險金額 = 資金 × 0.012；單筆上限 35%；每槽 20%（最多 5 檔）。
    dist = entry − stop；
    lots_risk  = floor(風險金額 / (dist × 100))；
    lots_cap35 = floor(資金 × 0.35 / (100 × entry))；
    lots_slot20 = floor(資金 × 0.20 / (100 × entry))；
    lots = min(lots_risk, lots_cap35, lots_slot20)。
lots ＜ 1 → 跳過，原因以三者最小者歸因（risk／cap35／slot20），列入 skipped。
口數以訊號價計（生產版同口徑）；實際進場價為 trigger，差距 ≤ 2.8%，僅影響邊界口數。
名目 = lots × 100 × entry；max_loss_twd = lots × 100 × dist（未計手續費與滑價）。

禁止事項
--------
本程式不含任何 Telegram 程式碼、不接任何券商／期貨 API、不下單、
不寫任何會發真實通知或交易的路徑。本程式只產生訊號。
TG 串接點：見 emit_signals() 末尾註解 —— TG 由使用者後面在此接入。

用法
----
    python futures_brk20_mapper.py [--signal-date YYYY-MM-DD] [--capital 3000000]
                                   [--universe futures_universe.json] [--output signals.json]
預設 signal-date 為資料中最新交易日；輸出 JSON 同時印到 stdout（--output 時亦寫檔）。
"""

import argparse
import datetime
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import yfinance as yf

from strategy_core import CONFIG, market_regime_ok, prepare_indicators

STRATEGY = "BRK20"
SHARES_PER_LOT = 100
DEFAULT_CAPITAL = 3_000_000
RISK_PER_TRADE = 0.012
MAX_POS_RATIO = 0.35
SLOT_RATIO = 0.20  # 每槽 20%，最多 5 檔（主系統 MAX_CONCURRENT_POSITIONS）
UNIVERSE_FILE = "futures_universe.json"

# 隔日進場確認參數（生產版同口徑，見檔頭）
GAP_SKIP_PCT = 1.028
TRIGGER_MARKUP = 1.003
WEAK_CLOSE_PCT = 0.998


# ---------------------------------------------------------------- data
def load_universe(path: str) -> dict:
    """讀取期貨映射宇宙：{sid: {"name": ..., "suffix": ".TW"/".TWO"}}。

    產生方式：TAIFEX 股期標的 ∩ 主系統 universe_cache.json（2026-10-22 抓取，129 檔）。
    若需更新宇宙，請重跑該交集並覆寫此檔，不得手動增刪個股。
    """
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _extract_ticker_df(data, ticker: str):
    """從 yf.download 的回傳值抽出單一 ticker 的 OHLCV（處理新版 MultiIndex 欄位）。"""
    if ticker not in data.columns.get_level_values(0) and ticker not in data.columns.get_level_values(-1):
        return None
    try:
        df = data[ticker]
    except Exception:
        # 欄位層級為 (Price, Ticker) 時，ticker 在第二層
        df = data.xs(ticker, axis=1, level=-1)
    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if df.empty:
        return None
    return df


def fetch_data(universe: dict, period: str = "1y"):
    """下載宇宙＋0050 的日線（auto_adjust=True，還原價，與映射研究同口徑）。

    生產版 backtest 用未還原 Close，本程式用還原價是映射研究指定口徑，
    兩者 headline 數字只做背景對照；映射版 vs 現股執行版的對照在同一還原價上做。
    """
    tickers = ["0050.TW"] + [f"{sid}{info['suffix']}" for sid, info in universe.items()]
    data = yf.download(tickers, period=period, auto_adjust=True,
                       group_by="ticker", threads=True, progress=False)
    out, missing = {}, []
    for sid, info in universe.items():
        df = _extract_ticker_df(data, f"{sid}{info['suffix']}")
        if df is not None:
            out[sid] = df
        else:
            missing.append(sid)
    bench = _extract_ticker_df(data, "0050.TW")
    return out, bench, missing


# ---------------------------------------------------------------- signal
def brk20_signal(df_raw: pd.DataFrame, bench_close: float, bench_ma20: float):
    """對「截至訊號日為止」的 OHLCV 做 BRK20 判定（最後一列 = 訊號日）。

    回傳 None（無訊號）或 dict(entry, stop, close, prev_20_high, vol_lots, mv20)。
    公式與 strategy_core.evaluate_candidate 的 brk_hit 逐字一致（見檔頭對照說明）。
    """
    if df_raw is None or len(df_raw) < 65:
        return None
    df = prepare_indicators(df_raw.dropna(subset=["Close", "High", "Low", "Open", "Volume"]))
    if len(df) < 65:
        return None
    cur = df.iloc[-1]
    if pd.isna(cur["MA60"]) or cur["MV20"] < CONFIG["MIN_MV20_LOTS"] or cur["Close"] < CONFIG["MIN_PRICE"]:
        return None
    stock_ret_20d = (float(cur["Close"]) - float(df.iloc[-21]["Close"])) / max(float(df.iloc[-21]["Close"]), 1.0)
    if not market_regime_ok(float(bench_close), float(bench_ma20), stock_ret_20d):
        return None
    prev_20_high = df.iloc[-21:-1]["High"].max()
    brk_hit = (float(cur["Close"]) > float(prev_20_high)) and \
              (cur["vol_lots"] >= 1.5 * cur["MV20"]) and \
              (cur["Close"] > cur["MA60"])
    if not brk_hit:
        return None
    entry = round(float(cur["Close"]), 2)
    stop = round(float(max(cur["Low"] * 0.995, entry * 0.975)), 2)
    return {"entry": entry, "stop": stop, "close": round(float(cur["Close"]), 2),
            "prev_20_high": round(float(prev_20_high), 2),
            "vol_lots": round(float(cur["vol_lots"]), 1), "mv20": round(float(cur["MV20"]), 1)}


def confirm_with_next_bar(sig_close: float, stop: float, nxt: pd.Series):
    """隔日進場確認（生產版同口徑）。回傳 (status, trigger_or_None, reason_or_None)。"""
    n_open, n_high, n_close = float(nxt["Open"]), float(nxt["High"]), float(nxt["Close"])
    if n_open > sig_close * GAP_SKIP_PCT:
        return "skipped", None, "gap_over_2.8pct"
    if n_open <= stop:
        return "skipped", None, "open_below_stop"
    trig = round(max(sig_close, n_open * TRIGGER_MARKUP), 2)
    if n_high < trig:
        return "skipped", None, "trigger_not_touched"
    if n_close < n_open * WEAK_CLOSE_PCT:
        return "skipped", None, "close_weak"
    return "confirmed", trig, None


# ---------------------------------------------------------------- futures sizing
def futures_lots(entry: float, stop: float, capital: float):
    """期貨口數換算（凍結公式）。回傳 (lots, skip_reason_or_None)。"""
    dist = entry - stop
    if entry <= 0 or stop <= 0 or dist <= 0:
        return 0, "invalid_stop"
    risk_amt = capital * RISK_PER_TRADE
    lots_risk = math.floor(risk_amt / (dist * SHARES_PER_LOT))
    lots_cap35 = math.floor(capital * MAX_POS_RATIO / (SHARES_PER_LOT * entry))
    lots_slot20 = math.floor(capital * SLOT_RATIO / (SHARES_PER_LOT * entry))
    lots = min(lots_risk, lots_cap35, lots_slot20)
    if lots < 1:
        # 以三者最小者歸因（與映射研究跳過分類一致）
        reason = min((("risk", lots_risk), ("cap35", lots_cap35), ("slot20", lots_slot20)),
                     key=lambda kv: kv[1])[0]
        return 0, reason
    return lots, None


# ---------------------------------------------------------------- emit
def emit_signals(signal_date: str | None = None, capital: float = DEFAULT_CAPITAL,
                 universe_path: str = UNIVERSE_FILE, period: str = "1y") -> dict:
    """產生 BRK20 小股期映射訊號。

    回傳 JSON-ready dict：{"asof", "capital", "universe_count", "signals", "skipped"}。
    signals 每筆含 date（訊號日）、stock_id、stock_name、entry_ref、signal_close、
    trigger（確認後才有）、stop、lots、notional、max_loss_twd、strategy、status。
    status：confirmed（已隔日確認，entry_ref=trigger）／pending_confirmation
    （當日訊號，尚無次根，entry_ref 暫填訊號收盤）。
    skipped：訊號成立但被隔日確認或口數規則刷掉者，附 reason。

    TG 串接點：使用者後面在此接入 —— 目前只輸出 JSON，不發任何通知、不下單。
    """
    universe = load_universe(universe_path)
    data, bench, missing = fetch_data(universe, period=period)
    if bench is None or bench.empty:
        raise RuntimeError("無法取得 0050 基準資料，market_ok 無法判定")
    bench_ind = prepare_indicators(bench)

    # 決定訊號日：指定日或資料最新交易日
    all_dates = sorted({d for df in data.values() for d in df.index})
    if signal_date is None:
        sig_date = all_dates[-1]
    else:
        sig_date = pd.Timestamp(signal_date)
    sig_str = sig_date.strftime("%Y-%m-%d")

    # 基準日收盤與 MA20（取訊號日當根；若基準缺該日則取之前最近一根）
    bench_upto = bench_ind[bench_ind.index <= sig_date]
    if bench_upto.empty:
        raise RuntimeError(f"0050 在 {sig_str} 之前無資料")
    bench_close = float(bench_upto.iloc[-1]["Close"])
    bench_ma20 = float(bench_upto.iloc[-1]["MA20"]) if not pd.isna(bench_upto.iloc[-1]["MA20"]) else float("nan")

    signals, skipped = [], []
    for sid in sorted(data.keys()):
        info = universe[sid]
        df = data[sid]
        upto = df[df.index <= sig_date]
        if len(upto) < 65:
            continue
        sig = brk20_signal(upto, bench_close, bench_ma20)
        if sig is None:
            continue
        # 隔日確認：取訊號日之後的第一根
        nxt_rows = df[df.index > sig_date]
        status, trigger, reason = ("pending_confirmation", None, None)
        if not nxt_rows.empty:
            status, trigger, reason = confirm_with_next_bar(sig["close"], sig["stop"], nxt_rows.iloc[0])
        lots, lot_reason = futures_lots(sig["entry"], sig["stop"], capital)
        base = {"date": sig_str, "stock_id": sid, "stock_name": info["name"],
                "signal_close": sig["close"], "stop": sig["stop"],
                "strategy": STRATEGY, "prev_20_high": sig["prev_20_high"]}
        if status == "skipped":
            skipped.append({**base, "status": "skipped", "skip_stage": "entry_confirmation",
                            "reason": reason})
            continue
        if lots < 1:
            skipped.append({**base, "status": "skipped", "skip_stage": "futures_lots",
                            "reason": lot_reason})
            continue
        dist = sig["entry"] - sig["stop"]
        signals.append({**base,
                        "entry_ref": trigger if trigger is not None else sig["entry"],
                        "trigger": trigger,
                        "status": status,
                        "lots": lots,
                        "notional": round(lots * SHARES_PER_LOT * sig["entry"], 2),
                        "max_loss_twd": round(lots * SHARES_PER_LOT * dist, 2)})
    return {"asof": sig_str, "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "capital": capital, "universe_count": len(universe),
            "data_count": len(data), "missing_data": missing,
            "signals": signals, "skipped": skipped}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="BRK20 小股期映射版訊號產生器（只輸出 JSON，不下單、不發通知）")
    ap.add_argument("--signal-date", default=None, help="訊號日 YYYY-MM-DD，預設為資料最新交易日")
    ap.add_argument("--capital", type=float, default=DEFAULT_CAPITAL, help="總資金（預設 3000000）")
    ap.add_argument("--universe", default=UNIVERSE_FILE, help="期貨映射宇宙 JSON（預設 futures_universe.json）")
    ap.add_argument("--output", default=None, help="另存 JSON 檔路徑（未指定則只印 stdout）")
    ap.add_argument("--period", default="1y", help="yfinance 下載區間（預設 1y，需 ≥65 根 K）")
    args = ap.parse_args(argv)
    payload = emit_signals(signal_date=args.signal_date, capital=args.capital,
                            universe_path=args.universe, period=args.period)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
