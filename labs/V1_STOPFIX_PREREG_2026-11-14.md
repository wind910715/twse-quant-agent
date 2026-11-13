# V1 變體：解除停損 -2.5% 硬夾＋結構停損距離上限閘門 — 事前註冊（2026-11-14）

註冊人：Muse（Goldie，總指揮）。變體未跑回測前凍結，跑完才可判定，不得事後改條件。

## 假設

`strategy_core.py` 的 `evaluate_candidate()` 把各策略停損寫死為 `max(結構低點×0.995/0.996, 進場×0.975)`，
等於硬夾在 -2.5% 內，與 `calc_position_size()`「停損不做假、adjusted_stop 永遠等於結構停損」的註解自相矛盾。
高波動強勢股次日回測突破點常在 -2.8%～-3.5% 震盪後再拉升，-2.5% 硬夾造成大量「剛好洗盤跌破即掃損、隨後噴出」的冤死單，
同時壓低勝率與 RR。解除硬夾、改純結構停損，應能減少冤死單、提升勝率；
配套距離上限閘門防止放寬後納入先天風險報酬比不良的追高單，守住 RR。

## 變動（唯一變因，其餘全部不動）

檔案：`strategy_core.py` 的 `evaluate_candidate()`，五處停損計算（S0/S1/S2/S3/S4）：

1. 移除 `entry * 0.975` 下限，改為純結構停損：
   - S0：`stop = round(float(last5["Low"].min() * 0.996), 2)`
   - S1：`stop = round(float(cur["Low"] * 0.996), 2)`
   - S2/S3/S4：`stop = round(float(cur["Low"] * 0.995), 2)`
2. 新增距離上限閘門：計算出停損後，若 `(entry - stop) / entry > 0.07`，該訊號不發（不 append）。
   閘門對五個策略一律適用。

不動的部分：進場邏輯、優先序、sizing（含單筆風險 1.2%）、出場（C2／三階停利引用）、
大盤閘門 `market_regime_ok()`、universe、成本、期間、回測 harness。

## 對照設計

- Harness：`backtest.py` v2（與實盤共用 `strategy_core.evaluate_candidate`）。
- Universe：`build_dynamic_index_universe()`（上限 220 檔）；期間 2016-01-01 起至最新資料（2026 年）；成本買 0.1425%／賣 0.4425%。
  （2026-11-14 01:36 使用者更正：回測期間一律 2016-2026 年；`backtest.py` 原寫死 2020-01-01 已同步改為 2016-01-01，兩個分支皆改，改動發生在任何 run 啟動之前。）
- Baseline：`variant/v1-baseline` 分支＝當日 live working tree 原樣（未改版），同 harness 跑一次。
- Variant：`variant/v1-stopfix` 分支＝baseline＋上述唯一變因，同 harness 跑一次。
- 2026-10-31 的 `latest_backtest.json`（總勝率 51.5%、RR 2.07）因策略名單已改版（舊 C21/C02），不得當本次 baseline。

## 過關條件（事前宣告，全部滿足才 PASS）

- (a) variant 總勝率 > 55%
- (b) variant 總 RR ≥ 1.8
- (c) variant 總勝率 > baseline 總勝率
- (d) 前半段與後半段勝率皆 > baseline 對應半段勝率
- (e) 總交易筆數 ≥ 30
- (f) 平均報酬 > 0

任一不滿足即 FAIL，不採用、不微調重測、不換口徑再跑。
通過僅代表「候選採用」，是否併入主系統由使用者拍板。

## 揭露限制

- Universe 非 point-in-time（倖存者偏誤仍在，數字僅供相對比較）。
- 法人濾網無歷史資料（chip_filter_applied=False，逐筆標 chip_unverified）。
- 停損邏輯屬紅線範圍：本次變體實作由 Muse 逐行驗證，Antigravity／Gemini 僅可提案與執行已驗證的 diff，不得自行改紅線程式。
