# XQ 通過者主系統引擎重測報告 (2026-10-30 00:59:53)

## 設計 (預註冊,跑前凍結)

- universe: build_dynamic_index_universe()[:220] (same as backtest.py)
- window: 2020-01-01 ~ latest (yfinance)
- entry: next-day confirmation (same as backtest.py)
- exit: strategy_core.simulate_exit (TP1/2/3, trailing, 3d weak, MAX_HOLD 55)
- costs: BUY 0.1425% + per-segment SELL 0.4425% (via simulate_exit)
- portfolio: max 5 concurrent, 20% weight each, no re-entry while holding
- priority: S0 > S1 > S2 > C21 > C02 > S02
- new_stop_rule: max(signal-day low x0.995, signal close x0.975) [S2-style assumption]
- c02_note: C02 XQ spec = 60d hold; capped at MAX_HOLD 55d by engine
- chip_filter: NOT applied (require_chip=False, same as Saturday backtest)
- survivorship: universe = current list, NOT point-in-time (same limitation as main backtest)

## 訊號耗損 (夜班 CSV → 220 檔樣本 → 主系統閘門)

- C21: CSV 1702 筆 → 閘門後 463 筆 (無資料跳過 0、閘門剔除 148)
- C02: CSV 2758 筆 → 閘門後 729 筆 (無資料跳過 0、閘門剔除 193)
- S02: CSV 356 筆 → 閘門後 112 筆 (無資料跳過 0、閘門剔除 34)

## 訊號日重疊 (新策略訊號與 S0/S1/S2 同股同日)

- C21: 181 訊號中 0 筆重疊 (0.0%)
- C02: 444 訊號中 0 筆重疊 (0.0%)
- S02: 107 訊號中 0 筆重疊 (0.0%)

## 各組結果

| 組別 | n | 勝率% | 平均% | RR | 前半平均% | 後半平均% | 累計% | MDD% |
|---|---|---|---|---|---|---|---|---|
| baseline_S0S1S2 | 1148 | 53.5 | 1.7922 | 2.0034 | 1.3164 | 2.268 | 5498.31 | -17.81 |
| C21_only | 81 | 43.2 | 1.4895 | 2.9554 | 1.0702 | 1.8985 | 26.49 | -7.26 |
| C02_only | 170 | 46.5 | 1.2246 | 2.028 | 1.8811 | 0.5681 | 49.79 | -7.9 |
| S02_only | 46 | 60.9 | 1.9207 | 1.7121 | 2.1904 | 1.6509 | 18.98 | -2.1 |
| combined_all6 | 1139 | 53.0 | 1.8211 | 2.0705 | 1.3541 | 2.2873 | 5686.47 | -14.58 |

## 各組策略細項

### baseline_S0S1S2
- S0_VCP收斂: 63 筆、勝率 50.8%、平均 2.1%
- S1_破底翻: 53 筆、勝率 66.0%、平均 3.1598%
- S2_量增突破: 1032 筆、勝率 53.0%、平均 1.7032%
### C21_only
- C21: 81 筆、勝率 43.2%、平均 1.4895%
### C02_only
- C02: 170 筆、勝率 46.5%、平均 1.2246%
### S02_only
- S02: 46 筆、勝率 60.9%、平均 1.9207%
### combined_all6
- C02: 47 筆、勝率 57.4%、平均 2.6651%
- C21: 29 筆、勝率 44.8%、平均 1.6614%
- S02: 21 筆、勝率 57.1%、平均 1.6319%
- S0_VCP收斂: 61 筆、勝率 52.5%、平均 2.2641%
- S1_破底翻: 51 筆、勝率 66.7%、平均 3.3029%
- S2_量增突破: 930 筆、勝率 52.3%、平均 1.6774%

## 整合判定 (事前門檻,僅供參考)

- C21: {'standalone_pass_main_standard': True}
- C02: {'standalone_pass_main_standard': True}
- S02: {'standalone_pass_main_standard': False}
- combined_improves: True

## 限制
- 宇宙為目前成分名單 (非 point-in-time),仍有倖存者偏誤,數字僅供相對比較。
- 法人濾網未套用 (無歷史法人資料),與週六回測同口徑。
- 新策略停損為 S2 式假設,非 XQ 原規格;C02 持有上限由 60 日壓至 55 日。
- 本報告不構成上線建議;最終是否融入由使用者決定。
