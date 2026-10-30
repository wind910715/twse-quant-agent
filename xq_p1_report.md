# XQ P1 優先序翻轉重測報告 (2026-10-30 02:58:26)

## 設計 (預註冊,跑前凍結)

- universe: build_dynamic_index_universe()[:220] (same as backtest.py)
- window: 2020-01-01 ~ latest (yfinance)
- entry: next-day confirmation (same as backtest.py)
- exit: strategy_core.simulate_exit (TP1/2/3, trailing, 3d weak, MAX_HOLD 55)
- costs: BUY 0.1425% + per-segment SELL 0.4425% (via simulate_exit)
- portfolio: max 5 concurrent, 20% weight each, no re-entry while holding
- priority: S0 > S1 > S2 > C21 > C02 > S02
- p1_priority: S0 > S1 > C21 > C02 > S2 (flipped; S0/S1 stay first, S02 still last)
- p1_control: same-run original combined; no universe drift (same signals, universe, data)
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
| baseline_S0S1S2 | 1147 | 53.3 | 1.7573 | 1.9872 | 1.2896 | 2.2243 | 5052.08 | -17.39 |
| C21_only | 82 | 42.7 | 1.4435 | 2.9501 | 0.9885 | 1.8985 | 25.91 | -7.26 |
| C02_only | 171 | 46.2 | 1.2041 | 2.0333 | 1.8811 | 0.535 | 49.11 | -7.9 |
| S02_only | 46 | 60.9 | 1.9207 | 1.7121 | 2.1904 | 1.6509 | 18.98 | -2.1 |
| combined_all6 | 1141 | 52.7 | 1.7746 | 2.055 | 1.3136 | 2.2349 | 5144.5 | -15.12 |
| combined_flip | 1141 | 52.7 | 1.7746 | 2.055 | 1.3136 | 2.2349 | 5144.5 | -15.12 |

## 各組策略細項

### baseline_S0S1S2
- S0_VCP收斂: 63 筆、勝率 50.8%、平均 2.1%
- S1_破底翻: 53 筆、勝率 66.0%、平均 3.1598%
- S2_量增突破: 1031 筆、勝率 52.8%、平均 1.6643%
### C21_only
- C21: 82 筆、勝率 42.7%、平均 1.4435%
### C02_only
- C02: 171 筆、勝率 46.2%、平均 1.2041%
### S02_only
- S02: 46 筆、勝率 60.9%、平均 1.9207%
### combined_all6
- C02: 47 筆、勝率 57.4%、平均 2.6651%
- C21: 30 筆、勝率 43.3%、平均 1.53%
- S02: 21 筆、勝率 57.1%、平均 1.6319%
- S0_VCP收斂: 62 筆、勝率 51.6%、平均 2.1858%
- S1_破底翻: 51 筆、勝率 66.7%、平均 3.3029%
- S2_量增突破: 930 筆、勝率 51.9%、平均 1.6295%
### combined_flip
- C02: 59 筆、勝率 52.5%、平均 1.9285%
- C21: 55 筆、勝率 50.9%、平均 1.5169%
- S02: 21 筆、勝率 57.1%、平均 1.6319%
- S0_VCP收斂: 62 筆、勝率 51.6%、平均 2.1858%
- S1_破底翻: 51 筆、勝率 66.7%、平均 3.3029%
- S2_量增突破: 893 筆、勝率 52.0%、平均 1.6679%

## 整合判定 (事前門檻,僅供參考)

- C21: {'standalone_pass_main_standard': True}
- C02: {'standalone_pass_main_standard': True}
- S02: {'standalone_pass_main_standard': False}
- combined_improves: True
- p1: {'flipped_beats_original': False, 'flipped_cum': 5144.5, 'original_cum': 5144.5, 'flipped_mdd': -15.12, 'original_mdd': -15.12, 'flipped_rr': 2.055, 'original_rr': 2.055, 'flipped_first': 1.3136, 'flipped_second': 2.2349}

## 限制
- 宇宙為目前成分名單 (非 point-in-time),仍有倖存者偏誤,數字僅供相對比較。
- 法人濾網未套用 (無歷史法人資料),與週六回測同口徑。
- 新策略停損為 S2 式假設,非 XQ 原規格;C02 持有上限由 60 日壓至 55 日。
- 本報告不構成上線建議;最終是否融入由使用者決定。
