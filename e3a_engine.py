"""
e3a_engine.py — E3a 選股邏輯的單一真相來源 (SSOT)，與 strategy_core 並列

定位 (2026-11-11，使用者拍板：quant 跟 Space 選股頁要是同一套程式)：
  * 訊號本身仍由 strategy_core.evaluate_candidate() 產生 (S0-S4，SSOT 不動)。
    本模組絕不重寫 S0-S4 的任何條件，只在訊號之上加「進場／出場／空檔資金」這一層。
  * 本模組是 C 進場 (§12.1)、C2 出場、E3a 空檔買 0050、E3c v3 0050 技術停損優先
    的唯一 Python 實作。Space 選股頁、auto_detect 後台、回測程式都應讀這裡，
    不得再各寫一份 (先前漂移實例：Space 與引擎進場價差 0.20；
    auto_detect 漲停門檻用 1.10 而非凍結的 1.095、reason 名稱與凍結碼不同)。

凍結來源 (數字以回測結果檔為準，本模組不跑回測、不改回測)：
  * C 進場：new_rules_spec_20261110.md §12.1。
  * C2 出場：同上 §12.2 (無 TP 封頂、無時限、收盤破 MA20＋回吐 20% 移動停損＋技術停損)。
  * E3a：spec_E3_20261110.md — 空檔現金買 0050，無停損停利；結果
    results/new_rules_E3a_backtest_20261110.json：n=2,546、勝率 61.55%、平均 +3.38%、
    RR 2.82、總報酬 +2,415.16%、年化 63.31%、夏普 2.26、MDD -23.82%、
    H1 +333.45%、H2 +480.26% vs 0050 +260.04%，PASS (全部裡面唯一 PASS)。
  * E3c v3：spec_E3c_v3_20261110.md — E3a ＋ 0050 技術停損優先
    (日K前一日最低價 → 30 日最低收盤 ×0.99 → 固定進場 ×0.95)；
    結果 n=2,609、勝率 61.67%、RR 2.80、總報酬 +1,186.07%、年化 47.47%、
    夏普 2.88、MDD -10.35%、H1 +326.53%、H2 +201.52%，FAIL (僅 H2)。
    技術停損只佔 0050 出場 6/40 次，出場仍由回吐 20% 主導。
  * 使用者決策 (2026-11-11)：策略就用目前最強的 E3a；0050 停損照 C 項原則
    (技術面優先)，所以實盤 live 邏輯 = E3a ＋ E3c v3 的 0050 停損。
    本模組把兩者分開標示：E3a (無 0050 停損，凍結 PASS 版) 與 E3a+stop (E3c v3 版)，
    live 預設走 E3a+stop，不得把 v3 的數字說成 E3a 的 PASS。

回測 vs 實盤口徑差異 (寫死，不得混用)：
  * 回測只有日 K：Close >= trigger 只是「收盤站上」的代理，不是真的 15 分鐘站穩；
    0050 賣出價以進場當日收盤價代理開盤價 (本地快取無 0050 開盤價)；
    0050 價為 yfinance 還原價 (含息)，報酬略高於現股價差。
  * 實盤 live：C 確認的收盤站住以 13:30 後最後一筆現價當收盤代理；
    0050 停損檢查用收盤價判定 (盤中跌破收回不觸發，只會晚、不會早)。
    c2_check_live() 是明確標示的 live 變體 (用即時價檢查，觸發比收盤判定早)，
    僅供 auto_detect 這類盤中監控用，不得把它的觸發筆數拿去對回測數字。

本模組頂層只用 stdlib (不 import pandas/numpy)，純函數可被 auto_detect
(stdlib-only daemon) 直接 import；需要 strategy_core.evaluate_candidate()
的 reproduce/emit 函數才在函數內 lazy import。
"""

import json
import math
from dataclasses import dataclass, field

E3A_VERSION = "E3a+stop (E3c v3)"
E3A_FROZEN = {
    "strategy": "E3a",
    "entry": "C (spec §12.1)",
    "exit": "C2 (no TP cap, no time limit, MA20 break + 20% giveback trail + hard stop)",
    "idle_cash": "buy 0050 when no stock holdings (E3a, no stop on ETF leg)",
    "etf_stop_overlay": "E3c v3 tech-stop priority (prev-day low -> low30 close x0.99 -> fixed x0.95)",
    "backtest_E3a": {"n": 2546, "win_rate_pct": 61.55, "avg_pct": 3.38, "rr": 2.82,
                     "total_return_pct": 2415.16, "annualized_pct": 63.31, "sharpe": 2.26,
                     "mdd_pct": -23.82, "half1_pct": 333.45, "half2_pct": 480.26, "verdict": "PASS"},
    "backtest_E3c_v3": {"n": 2609, "win_rate_pct": 61.67, "avg_pct": 3.36, "rr": 2.80,
                        "total_return_pct": 1186.07, "annualized_pct": 47.47, "sharpe": 2.88,
                        "mdd_pct": -10.35, "half1_pct": 326.53, "half2_pct": 201.52, "verdict": "FAIL (H2 only)"},
    "bench_0050_half2_pct": 260.04,
}

# ---- 凍結常數 (C §12.1 / C2 §12.2 / E3c v3 §1.1) ----
C_OPEN_UPPER = 1.05        # 開盤 <= 訊號 x 1.05
C_LIMIT_UP = 1.095         # 開盤 >= 訊號 x 1.095 判定漲停開盤跳過 (凍結值；auto_detect 舊版用 1.10 是漂移)
C_TRIGGER_MARKUP = 1.003   # trigger = max(訊號, 開盤 x 1.003)
TRAIL_PCT = 0.20           # 回吐 20% 移動停損
ETF_FIXED_FALLBACK = 0.95  # 0050 固定金額備胎 (最後順位才用)
ETF_LOW30_FACTOR = 0.99    # 0050 30 日最低收盤 x 0.99
BUY_COST = 0.001425
SELL_COST = 0.004425
E3A_LOT_SHARES = 1000      # 一張
E3A_START_CAPITAL = 1_000_000


def _r2(x):
    return round(float(x), 2)


# ---------------------------------------------------------------- C 進場 (§12.1)
def c_entry_check(signal, stop, open_, high, close):
    """C 進場確認 (凍結 §12.1，唯一版本)。

    signal = 訊號價 S (訊號日收盤價)，stop = 停損價 P。
    進場窗口僅限次一交易日；當日未觸發訊號作廢。
    回傳 dict {passed, trigger, reason}，reason 為凍結碼：
      pass / no_price / open_le_stop / open_gt_105 / limit_open_skip /
      high_below_trigger / close_below_trigger。
    檢查順序即凍結程式順序 (new_rules_E3_backtest.py，註解寫明：漲停先於 1.05 上限檢查，
    否則漲停開盤永遠先被計為 >1.05)：開盤未破停損 → 非漲停開盤 (>=1.095x 跳過) →
    開盤不超過 1.05x → High 碰到 trigger → Close 站住 trigger。進場價 = trigger。
    注意：Close >= trigger 只是日 K 代理，不是真的 15 分鐘站穩 (見模組 docstring)。
    另注意：trigger = round(max(S, Open×1.003), 2) 是程式唯一公式；規格文字
    「Open <= S 時 trigger = S」是簡化寫法，Open 略低於 S 時 Open×1.003 仍可能 > S
    (例如 S=351.0、Open=350.0 → trigger=351.05)，以 max() 公式為準。
    """
    if open_ is None or high is None or close is None:
        return {"passed": False, "trigger": None, "reason": "no_price"}
    if open_ <= stop:
        return {"passed": False, "trigger": None, "reason": "open_le_stop"}
    if open_ >= signal * C_LIMIT_UP:
        return {"passed": False, "trigger": None, "reason": "limit_open_skip"}
    if open_ > signal * C_OPEN_UPPER:
        return {"passed": False, "trigger": None, "reason": "open_gt_105"}
    trigger = _r2(max(signal, open_ * C_TRIGGER_MARKUP))
    if high < trigger:
        return {"passed": False, "trigger": trigger, "reason": "high_below_trigger"}
    if close < trigger:
        return {"passed": False, "trigger": trigger, "reason": "close_below_trigger"}
    return {"passed": True, "trigger": trigger, "reason": "pass"}


# ---------------------------------------------------------------- C2 出場 (§12.2，收盤判定)
def c2_exit_lines(entry, hard_stop, peak_high):
    """C2 當前出場線 (收盤判定用)。

    trail = max(停損, peak - 0.20*(peak-entry))；peak == entry 時 trail = 停損
    (trail 未啟動，E3c v2 邊界裁決)。peak 只計進場後新高，不得用進場前的高點
    (Space 曾因此把 11-10 0050 的 peak 誤種為當日最高 100.50，已修為 99.70)。
    回傳 {hard_stop, trail_price, trail_active, peak_high}。
    """
    if peak_high is None or peak_high <= entry:
        trail = hard_stop
        active = False
    else:
        trail = max(hard_stop, peak_high - TRAIL_PCT * (peak_high - entry))
        active = True
    return {"hard_stop": _r2(hard_stop), "trail_price": _r2(trail),
            "trail_active": active, "peak_high": _r2(peak_high) if peak_high is not None else None}


def c2_check_bar(day_num, bar, entry, hard_stop, peak_high_prev):
    """C2 逐根檢查 (凍結順序，與 simulate_exit_trailing20 完全一致)。

    bar = {open, high, low, close, ma20 (可為 None)}，day_num 自進場日起算 1, 2, ...
    順序：技術停損優先 (day1 收盤 <= 停損 → hard_stop_day1；day>=2 開盤 <= 停損 →
    gap_stop，否則最低 <= 停損 → hard_stop) → 回吐 20% (day>=2 收盤 <= trail →
    trail_close) → 破 MA20 (day>=2，ma20 非空且收盤 < ma20 → ma20_exit)。
    peak 以當日 High 在檢查完後更新 (不拿當日 High 當日觸發 trail)。
    回傳 {exited, reason, exit_price, peak_high}，reason 為
    hard_stop_day1 / gap_stop / hard_stop / trail_close / ma20_exit / None。
    """
    o, h, lo, c = (float(bar["open"]), float(bar["high"]),
                   float(bar["low"]), float(bar["close"]))
    ma20 = bar.get("ma20")
    ma20 = None if ma20 is None or (isinstance(ma20, float) and math.isnan(ma20)) else float(ma20)
    peak = peak_high_prev
    lines = c2_exit_lines(entry, hard_stop, peak)
    trail = lines["trail_price"]

    def out(reason, price):
        return {"exited": True, "reason": reason, "exit_price": _r2(price),
                "peak_high": _r2(max(peak, h))}

    if day_num == 1:
        if c <= hard_stop:
            return out("hard_stop_day1", hard_stop)
    else:
        if o <= hard_stop:
            return out("gap_stop", o)
        if lo <= hard_stop:
            return out("hard_stop", hard_stop)
    if day_num >= 2:
        if lines["trail_active"] and c <= trail:
            return out("trail_close", c)
        if ma20 is not None and c < ma20:
            return out("ma20_exit", c)
    new_peak = max(peak, h)
    return {"exited": False, "reason": None, "exit_price": None, "peak_high": _r2(new_peak)}


def c2_check_live(entry, tech_stop, peak, price, high):
    """C2 0050 停損的 live 變體 (僅供盤中監控，如 auto_detect)。

    與收盤判定的 c2_check_bar 不同：用即時價檢查，觸發比收盤判定早；
    peak 用盤中 High 更新。回傳 (triggered, reason, new_peak, trail)，
    reason 為 tech_stop / trail_stop / None。tech_stop 在此即技術停損價
    (對股票部位傳 hard_stop；對 0050 傳 resolve_etf_tech_stop 的結果)。
    不得把本函數的觸發筆數拿去對回測數字。
    """
    new_peak = peak
    if high is not None:
        new_peak = max(peak, high)
    if price is not None:
        new_peak = max(new_peak, price)
    lines = c2_exit_lines(entry, tech_stop, new_peak)
    trail = lines["trail_price"] if lines["trail_active"] else None
    if price is None:
        return False, None, _r2(new_peak), trail
    if price <= tech_stop:
        return True, "tech_stop", _r2(new_peak), trail
    if trail is not None and price <= trail:
        return True, "trail_stop", _r2(new_peak), trail
    return False, None, _r2(new_peak), trail


# ---------------------------------------------------------------- 0050 技術停損優先 (E3c v3 §1.1)
def resolve_etf_tech_stop(entry, prev_day_low=None, low30_close=None):
    """0050 技術停損優先順序 (每次買進時決定，寫入部位狀態後不再每天重算)。

    1. 日K：前一日最低價，若存在且 < 進場價 → 用它，來源 prev_day_low。
    2. 30 日最低收盤 x0.99，若 < 進場價 → 用它，來源 low30_close_x099。
    3. 固定金額備胎：進場價 x0.95，來源 fixed_095。
    回傳 (price, source)。實例：11-10 進場 99.70、前一日 11-09 最低 94.50
    → (94.50, 'prev_day_low')。
    """
    if prev_day_low is not None and float(prev_day_low) < entry:
        return _r2(prev_day_low), "prev_day_low"
    if low30_close is not None:
        p = _r2(float(low30_close) * ETF_LOW30_FACTOR)
        if p < entry:
            return p, "low30_close_x099"
    return _r2(entry * ETF_FIXED_FALLBACK), "fixed_095"


def ma20_of(closes):
    """收盤序列的 MA20 (最後一筆收盤日為 as-of 日；呼叫端須標 as-of 日期)。

    注意：Space 存的 0050 MA20 105.73 是截至 11-09 收盤的 MA20；
    截至 11-10 收盤 (99.70) 的 MA20 是 104.97。兩者皆小於 99.70 嗎？
    不：99.70 < 105.73 且 < 104.97，任一口徑下 11-10 收盤都在 MA20 之下，
    所以「11-10 那張按 v3 根本不該買」的結論不變，但數值須標 as-of 日，不得混用。
    """
    if closes is None or len(closes) < 20:
        return None
    return _r2(sum(float(c) for c in closes[-20:]) / 20.0)


# ---------------------------------------------------------------- 0050 空檔部位狀態機 (E3a + E3c v3)
@dataclass
class EtfIdleLeg:
    """0050 空檔部位 (收盤判定口徑，與 E3c v3 回測一致)。

    E3a 原版：空檔現金買 0050，不設停損停利。
    Live 版 (E3a+stop)：加 E3c v3 技術停損優先＋回吐 20%，MA20 只當買進／買回條件。
    買進只在收盤站上 MA20 才執行；停損出清當日不買回 (冷卻)，之後收盤重新站回
    MA20 且無股票部位才再買回。賣出價：回測以進場當日收盤價代理開盤價 (無開盤價
    資料)；實盤用實際成交價，呼叫端傳入。
    """
    shares: int = 0
    entry: float | None = None          # 平均成本進場價
    peak_close: float | None = None     # 進場後最高收盤價 (只計進場後新高)
    tech_stop: float | None = None
    tech_stop_source: str | None = None
    entry_date: str | None = None
    cooldown_date: str | None = None    # 停損出清日，當日不買回
    realized_pnl: float = 0.0
    basis: float = 0.0                  # 剩餘成本基礎 (含買成本)

    @property
    def holding(self):
        return self.shares > 0

    def market_value(self, close):
        return _r2(self.shares * close) if self.holding else 0.0

    def can_buy(self, close, ma20, has_stock_holdings, today):
        if has_stock_holdings or close is None or ma20 is None:
            return False
        if close < ma20:
            return False
        if self.cooldown_date is not None and self.cooldown_date == today:
            return False
        return True

    def buy(self, cash, close, ma20, today, prev_day_low=None, low30_close=None):
        """收盤後用全部閒置現金買入／加碼 (E3a §1.1 規則 1＋E3c v3 §1.3)。

        股數 = floor(現金 / (收盤價 x 1.001425))；<1 股不買。
        加碼時平均成本改變，技術停損依當日資料重新依優先順序決定；
        peak = max(舊 peak, 當日收盤價)。回傳買進股數。
        """
        if cash <= 0 or close is None or close <= 0:
            return 0
        n = int(cash // (close * (1 + BUY_COST)))
        if n < 1:
            return 0
        cost = n * close * (1 + BUY_COST)
        if self.holding:
            new_entry = (self.basis + cost) / (self.shares + n)
        else:
            new_entry = close
        self.entry = _r2(new_entry)
        self.shares += n
        self.basis = _r2(self.basis + cost)
        self.peak_close = close if self.peak_close is None else max(self.peak_close, close)
        self.tech_stop, self.tech_stop_source = resolve_etf_tech_stop(
            self.entry, prev_day_low, low30_close)
        if self.entry_date is None:
            self.entry_date = today
        return n

    def sell_for_stock(self, shortfall, close):
        """為股票進場補缺口，只賣所需份額 (E3a §1.1 規則 2)，非整筆出清。

        賣出股數 = min(持有, ceil(缺口 / (收盤價 x (1-0.004425))))；
        賣出所得 = 股數 x 收盤價 x (1-0.004425)。回傳 (賣出股數, 所得)。
        剩餘股數沿用同一組 entry / peak / tech_stop / 開倉日，不重算。
        """
        if shortfall <= 0 or not self.holding or close is None or close <= 0:
            return 0, 0.0
        n = min(self.shares, int(math.ceil(shortfall / (close * (1 - SELL_COST)))))
        if n < 1:
            return 0, 0.0
        proceeds = _r2(n * close * (1 - SELL_COST))
        avg_basis = self.basis / self.shares
        self.realized_pnl = _r2(self.realized_pnl + (proceeds - n * avg_basis))
        self.basis = _r2(self.basis - n * avg_basis)
        self.shares -= n
        if self.shares == 0:
            self._reset_position()
        return n, proceeds

    def check_close(self, close, today):
        """收盤停損檢查 (買進當日不檢查，從買進隔日起)。

        技術停損與回吐 20% 先觸先出，整筆以當日收盤價賣出 (賣成本 0.4425%)。
        回傳 None 或 {reason, exit_price, shares, realized_pnl}。
        """
        if not self.holding or self.entry_date == today:
            return None
        lines = c2_exit_lines(self.entry, self.tech_stop, self.peak_close)
        trail = lines["trail_price"] if lines["trail_active"] else None
        reason = None
        if close <= self.tech_stop:
            reason = "tech_stop"
        elif trail is not None and close <= trail:
            reason = "trail_stop"
        if reason is None:
            if close > (self.peak_close or 0):
                self.peak_close = close
            return None
        proceeds = _r2(self.shares * close * (1 - SELL_COST))
        pnl = _r2(self.realized_pnl + (proceeds - self.basis))
        evt = {"reason": reason, "exit_price": _r2(close), "shares": self.shares,
               "tech_stop": self.tech_stop, "tech_stop_source": self.tech_stop_source,
               "trail_price": trail, "realized_pnl": pnl, "date": today}
        self.realized_pnl = pnl
        self.cooldown_date = today
        self._reset_position()
        return evt

    def _reset_position(self):
        self.shares = 0
        self.entry = None
        self.peak_close = None
        self.tech_stop = None
        self.tech_stop_source = None
        self.entry_date = None
        self.basis = 0.0


# ---------------------------------------------------------------- Space / auto_detect 共用的輸出契約
def emit_e3a_picks(signals, etf_leg, signal_date, confirm_date=None, ma20_asof=None):
    """產出 e3a_picks.json：quant 是唯一生產者，Space 只讀此檔、不得重算。

    signals：daily_candidate_pool 的 five_signals (每筆含 stock_id/strategy/
    entry_price/hard_stop/sources/rs_year/rs_quarter)，原值照抄，不得改寫。
    etf_leg：dict 或 EtfIdleLeg，含 entry/shares/tech_stop/tech_stop_source/
    tech_stop_detail/ma20/status；另須標 ma20_asof 日期 (見 ma20_of 註記)。
    回傳 dict；Space 的 importPicks 應以此檔為唯一輸入，進場價／停損價／
    trigger／技術停損／MA20 皆以此檔為準，不得在 TypeScript 端重算 C/C2。
    """
    out_signals = []
    for s in signals:
        out_signals.append({
            "stock_id": str(s["stock_id"]),
            "strategy": s["strategy"],
            "entry_price": _r2(s["entry_price"]),
            "hard_stop": _r2(s["hard_stop"]),
            "sources": list(s.get("sources", [])),
            "rs_year": s.get("rs_year"),
            "rs_quarter": s.get("rs_quarter"),
            "signal_date": s.get("signal_date", signal_date),
            "shares": E3A_LOT_SHARES,
        })
    if isinstance(etf_leg, EtfIdleLeg):
        lines = (c2_exit_lines(etf_leg.entry, etf_leg.tech_stop, etf_leg.peak_close)
                 if etf_leg.holding else None)
        etf = {
            "status": "holding" if etf_leg.holding else "cash",
            "entry_price": etf_leg.entry,
            "shares": etf_leg.shares,
            "tech_stop": etf_leg.tech_stop,
            "tech_stop_source": etf_leg.tech_stop_source,
            "tech_stop_detail": None,
            "trail_price": lines["trail_price"] if lines and lines["trail_active"] else None,
            "peak_close": etf_leg.peak_close,
            "realized_pnl": etf_leg.realized_pnl,
        }
    else:
        etf = dict(etf_leg) if etf_leg else {"status": "cash"}
    etf["ma20"] = etf.get("ma20")
    etf["ma20_asof"] = ma20_asof
    return {
        "version": E3A_VERSION,
        "strategy": "E3a",
        "entry_rule": "C (spec §12.1)",
        "exit_rule": "C2 (no TP cap, no time limit, MA20 break + 20% giveback trail + hard stop)",
        "etf_rule": "E3a idle cash into 0050 + E3c v3 tech-stop priority",
        "signal_date": signal_date,
        "confirm_date": confirm_date,
        "generated_at": None,  # 呼叫端填寫台北時間
        "signals": out_signals,
        "etf_leg": etf,
        "frozen_backtest": E3A_FROZEN,
        "method_notes": [
            "Signals are strategy_core.evaluate_candidate S0-S4 (SSOT); this module adds only entry/exit/idle-cash layers.",
            "Close>=trigger is a daily-K proxy for holding above trigger, NOT verified 15-min behaviour.",
            "0050 sell price in backtest proxies the open with the entry-day close (no 0050 open in local cache); 0050 prices are adjusted (incl. dividends).",
            "E3a PASS (n=2546, RR 2.82, H2 +480.26%) is the no-stop ETF-leg version; live E3a+stop is E3c v3 (FAIL, H2 only, +201.52%), MDD -10.35% vs E3a -23.82%.",
        ],
    }


def reproduce_signal_via_core(sid, sname, sfx, df_raw, bench_ret_20d, market_ok,
                               bench_series=None):
    """用 strategy_core.evaluate_candidate() 重現單一訊號 (驗證用，非實盤路徑)。

    df_raw 為截至訊號日的日 K (最後一列 = 訊號日)。回測口徑 require_chip=False。
    回傳 evaluate_candidate 的原始結果 list。
    """
    from strategy_core import evaluate_candidate  # lazy：頂層保持 stdlib-only
    return evaluate_candidate(df_raw, sid, sname, sfx, None, bench_ret_20d,
                              market_ok=market_ok, require_chip=False,
                              bench_series=bench_series)


if __name__ == "__main__":
    import sys
    print(json.dumps({"version": E3A_VERSION, "frozen": E3A_FROZEN},
                     ensure_ascii=False, indent=1))
    print("c_entry_check(351.0, 342.22, open=352.0, high=356.0, close=355.0) =",
          c_entry_check(351.0, 342.22, 352.0, 356.0, 355.0))
    print("resolve_etf_tech_stop(99.70, prev_day_low=94.50, low30_close=92.50) =",
          resolve_etf_tech_stop(99.70, prev_day_low=94.50, low30_close=92.50))
