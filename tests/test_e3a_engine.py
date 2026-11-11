"""tests/test_e3a_engine.py — E3a SSOT 單元測試 (stdlib unittest)。

凍結對照：
  * C 進場 §12.1：開盤>停損、開盤<=訊號x1.05、漲停開盤 >=訊號x1.095 跳過、
    trigger=max(訊號, 開盤x1.003)、High>=trigger 且 Close>=trigger、進場價=trigger。
  * C2 出場：技術停損優先 → 回吐 20% → 破 MA20；peak 只計進場後新高。
  * E3c v3 §1.1：0050 技術停損 前一日最低價 → 30 日最低收盤 x0.99 → 固定 x0.95。
執行：python3 -m unittest tests.test_e3a_engine -v (repo 根目錄)。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from e3a_engine import (  # noqa: E402
    c_entry_check, c2_check_bar, c2_check_live, c2_exit_lines,
    ma20_of, resolve_etf_tech_stop, EtfIdleLeg,
)


class TestCEntry(unittest.TestCase):
    def test_pass(self):
        r = c_entry_check(351.0, 342.22, 352.0, 356.0, 355.0)
        self.assertEqual(r, {"passed": True, "trigger": 353.06, "reason": "pass"})

    def test_trigger_is_signal_when_open_below_signal(self):
        # max() 公式為準：max(351.0, 350.0x1.003=351.05) = 351.05，不是 351.0。
        r = c_entry_check(351.0, 342.22, 350.0, 352.0, 351.5)
        self.assertEqual((r["passed"], r["trigger"]), (True, 351.05))

    def test_open_le_stop(self):
        self.assertEqual(c_entry_check(351.0, 342.22, 342.22, 345.0, 345.0)["reason"], "open_le_stop")

    def test_open_gt_105(self):
        self.assertEqual(c_entry_check(351.0, 342.22, 370.0, 372.0, 372.0)["reason"], "open_gt_105")

    def test_limit_up_skip_at_1095(self):
        # 凍結門檻 1.095 (351x1.095=384.345)：開盤 384.50 跳過；
        # 384.30 低於門檻不跳過，但 >1.05x 上限 (368.55)，理由是 open_gt_105。
        # 順序是凍結程式順序：漲停先於 1.05 上限檢查。
        self.assertEqual(c_entry_check(351.0, 342.22, 384.50, 384.50, 384.50)["reason"], "limit_open_skip")
        self.assertEqual(c_entry_check(351.0, 342.22, 384.30, 384.30, 384.30)["reason"], "open_gt_105")

    def test_high_below_trigger(self):
        r = c_entry_check(351.0, 342.22, 352.0, 352.5, 352.5)
        self.assertEqual((r["passed"], r["reason"]), (False, "high_below_trigger"))

    def test_close_below_trigger(self):
        r = c_entry_check(351.0, 342.22, 352.0, 356.0, 352.0)
        self.assertEqual((r["passed"], r["reason"]), (False, "close_below_trigger"))

    def test_no_price(self):
        self.assertEqual(c_entry_check(351.0, 342.22, None, 356.0, 355.0)["reason"], "no_price")


class TestC2Exit(unittest.TestCase):
    def test_trail_formula(self):
        lines = c2_exit_lines(100.0, 97.5, 110.0)
        self.assertEqual(lines["trail_price"], 108.0)  # 110 - 0.2*(110-100)
        self.assertTrue(lines["trail_active"])

    def test_peak_eq_entry_trail_inactive(self):
        lines = c2_exit_lines(99.70, 94.50, 99.70)
        self.assertEqual((lines["trail_price"], lines["trail_active"]), (94.50, False))

    def test_hard_stop_day1(self):
        r = c2_check_bar(1, {"open": 100, "high": 101, "low": 96, "close": 96, "ma20": 99},
                         100.0, 97.5, 100.0)
        self.assertEqual((r["exited"], r["reason"], r["exit_price"]), (True, "hard_stop_day1", 97.5))

    def test_gap_stop_day2(self):
        r = c2_check_bar(2, {"open": 97.0, "high": 99, "low": 96, "close": 98, "ma20": 99},
                         100.0, 97.5, 102.0)
        self.assertEqual((r["exited"], r["reason"], r["exit_price"]), (True, "gap_stop", 97.0))

    def test_trail_before_ma20(self):
        # 收盤同時破 trail (101.6) 與 MA20 (102)：回吐先觸發，理由是 trail_close。
        r = c2_check_bar(2, {"open": 103, "high": 104, "low": 101, "close": 101.0, "ma20": 102.0},
                         100.0, 97.5, 110.0)
        self.assertEqual((r["exited"], r["reason"]), (True, "trail_close"))

    def test_ma20_exit(self):
        # trail 97.5 (peak==entry)，收盤 99 < MA20 100 → ma20_exit。
        r = c2_check_bar(3, {"open": 101, "high": 101, "low": 99, "close": 99.0, "ma20": 100.0},
                         100.0, 97.5, 100.0)
        self.assertEqual((r["exited"], r["reason"]), (True, "ma20_exit"))

    def test_peak_updates_after_checks(self):
        r = c2_check_bar(2, {"open": 101, "high": 106, "low": 100, "close": 105, "ma20": 99},
                         100.0, 97.5, 102.0)
        self.assertEqual((r["exited"], r["peak_high"]), (False, 106.0))

    def test_live_variant_tech_before_trail(self):
        trig, reason, peak, trail = c2_check_live(99.70, 94.50, 99.70, 94.40, 94.40)
        self.assertEqual((trig, reason), (True, "tech_stop"))
        # trail = 102.0 - 0.2*(102.0-99.70) = 102.0 - 0.46 = 101.54；101.20 <= 101.54 觸發。
        trig, reason, peak, trail = c2_check_live(99.70, 94.50, 102.0, 101.20, 102.0)
        self.assertEqual((trig, reason, trail), (True, "trail_stop", 101.54))


class TestEtfTechStop(unittest.TestCase):
    def test_11_10_lot(self):
        # 11-10 實例：進場 99.70，前一日 11-09 最低 94.50 → (94.50, prev_day_low)。
        self.assertEqual(resolve_etf_tech_stop(99.70, prev_day_low=94.50, low30_close=92.50),
                         (94.50, "prev_day_low"))

    def test_low30_fallback(self):
        self.assertEqual(resolve_etf_tech_stop(100.0, prev_day_low=101.0, low30_close=92.50),
                         (91.58, "low30_close_x099"))

    def test_fixed_fallback(self):
        self.assertEqual(resolve_etf_tech_stop(100.0, prev_day_low=None, low30_close=None),
                         (95.0, "fixed_095"))

    def test_ma20_asof(self):
        # 真實收盤序列 (0050_ohlc_adj.csv)：Space 存的 105.73 是截至 11-09 收盤的 MA20；
        # 截至 11-10 收盤 (99.70) 是 104.97。兩者皆 > 99.70，買進閘門結論不變。
        closes_1109 = [114.8, 112.4, 113.0, 108.9, 109.0, 108.15, 107.25, 108.15,
                       106.9, 106.9, 106.6, 108.65, 107.25, 104.95, 104.1, 100.6,
                       99.85, 97.15, 92.5, 97.45]
        self.assertEqual(ma20_of(closes_1109), 105.73)
        self.assertEqual(ma20_of(closes_1109[1:] + [99.70]), 104.97)


class TestEtfIdleLeg(unittest.TestCase):
    def test_buy_gate_below_ma20(self):
        # 11-10 實例：收盤 99.70 < MA20 105.73 → 按 v3 根本不該買 (已知 Space 衝突)。
        leg = EtfIdleLeg()
        self.assertFalse(leg.can_buy(99.70, 105.73, False, "2026-11-10"))
        self.assertTrue(leg.can_buy(106.0, 105.73, False, "2026-11-10"))

    def test_buy_sets_tech_stop_and_peak(self):
        leg = EtfIdleLeg()
        n = leg.buy(100000, 99.70, 105.73, "2026-11-10", prev_day_low=94.50, low30_close=92.50)
        self.assertEqual(n, 1001)  # floor(100000 / (99.70*1.001425))
        self.assertEqual((leg.tech_stop, leg.tech_stop_source), (94.50, "prev_day_low"))
        self.assertEqual(leg.peak_close, 99.70)

    def test_stop_not_checked_on_buy_day(self):
        leg = EtfIdleLeg()
        leg.buy(100000, 99.70, 90.0, "2026-11-10", prev_day_low=94.50, low30_close=92.50)
        self.assertIsNone(leg.check_close(90.0, "2026-11-10"))

    def test_tech_stop_next_day(self):
        leg = EtfIdleLeg()
        leg.buy(100000, 99.70, 90.0, "2026-11-10", prev_day_low=94.50, low30_close=92.50)
        evt = leg.check_close(94.40, "2026-11-11")
        self.assertEqual(evt["reason"], "tech_stop")
        self.assertFalse(leg.holding)
        self.assertEqual(leg.cooldown_date, "2026-11-11")


if __name__ == "__main__":
    unittest.main()
