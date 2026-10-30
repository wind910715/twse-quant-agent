# P0 法人濾網回測報告 (2026-10-30 13:59:01)

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
- chip_filter: FinMind TaiwanStockInstitutionalInvestorsBuySell (local fetch, committed as xq_signals/chip_daily.csv); chip_is_positive v2 (total>0 and foreign>0 or it>0); foreign = Foreign_Investor + Foreign_Dealer_Self (T86 aggregate); live T86 covers TW only, so verdict uses TW-only runs
- p0_verdict_rule: TW-only: combined_chip_tw cum > baseline_chip_tw cum and MDD not worse by >3pp
- survivorship: universe = current list, NOT point-in-time (same limitation as main backtest)

## 法人濾網統計

- S 訊號檢查 8139 筆、通過 6577 筆
- 新策略訊號被濾網擋下 370 筆

## 濾網後 combined 的上市/上櫃分項

- TW: 910 筆、平均 1.7944%
- TWO: 143 筆、平均 2.439%

## 訊號耗損 (夜班 CSV → 220 檔樣本 → 主系統閘門)

- C21: CSV 1702 筆 → 閘門後 463 筆 (無資料跳過 0、閘門剔除 148)
- C02: CSV 2758 筆 → 閘門後 729 筆 (無資料跳過 0、閘門剔除 193)
- S02: CSV 356 筆 → 閘門後 112 筆 (無資料跳過 0、閘門剔除 34)

## 訊號日重疊 (新策略訊號與 S0/S1/S2 同股同日)

- C21: 181 訊號中 0 筆重疊 (0.0%)
- C02: 446 訊號中 0 筆重疊 (0.0%)
- S02: 107 訊號中 0 筆重疊 (0.0%)

## 各組結果

| 組別 | n | 勝率% | 平均% | RR | 前半平均% | 後半平均% | 累計% | MDD% |
|---|---|---|---|---|---|---|---|---|
| baseline_S0S1S2 | 1146 | 53.9 | 1.8302 | 2.0052 | 1.386 | 2.2745 | 5963.02 | -12.86 |
| combined_all6 | 1139 | 53.3 | 1.8372 | 2.0678 | 1.4032 | 2.2704 | 5902.26 | -13.2 |
| baseline_chip | 1044 | 54.5 | 1.8927 | 2.0043 | 1.4593 | 2.3261 | 4692.57 | -11.8 |
| combined_chip | 1053 | 53.8 | 1.8819 | 2.0504 | 1.4835 | 2.2796 | 4738.61 | -12.42 |
| baseline_chip_tw | 962 | 54.1 | 1.8839 | 2.0155 | 1.3928 | 2.3749 | 3370.8 | -10.39 |
| combined_chip_tw | 973 | 52.8 | 1.816 | 2.0533 | 1.3405 | 2.2905 | 3067.06 | -11.56 |

## 各組策略細項

### baseline_S0S1S2
- S0_VCP收斂: 62 筆、勝率 50.0%、平均 2.101%
- S1_破底翻: 53 筆、勝率 66.0%、平均 3.1598%
- S2_量增突破: 1031 筆、勝率 53.5%、平均 1.7456%
### combined_all6
- C02: 47 筆、勝率 57.4%、平均 2.6651%
- C21: 29 筆、勝率 44.8%、平均 1.6614%
- S02: 21 筆、勝率 57.1%、平均 1.6319%
- S0_VCP收斂: 60 筆、勝率 51.7%、平均 2.2678%
- S1_破底翻: 51 筆、勝率 66.7%、平均 3.3029%
- S2_量增突破: 931 筆、勝率 52.6%、平均 1.6975%
### baseline_chip
- S0_VCP收斂: 66 筆、勝率 51.5%、平均 2.3588%
- S1_破底翻: 50 筆、勝率 64.0%、平均 2.7832%
- S2_量增突破: 928 筆、勝率 54.2%、平均 1.8116%
### combined_chip
- C02: 35 筆、勝率 57.1%、平均 3.0883%
- C21: 28 筆、勝率 50.0%、平均 1.8364%
- S02: 19 筆、勝率 47.4%、平均 0.1295%
- S0_VCP收斂: 62 筆、勝率 51.6%、平均 2.4231%
- S1_破底翻: 49 筆、勝率 63.3%、平均 2.7984%
- S2_量增突破: 860 筆、勝率 53.5%、平均 1.7818%
### baseline_chip_tw
- S0_VCP收斂: 57 筆、勝率 50.9%、平均 2.3426%
- S1_破底翻: 42 筆、勝率 66.7%、平均 3.4643%
- S2_量增突破: 863 筆、勝率 53.7%、平均 1.7767%
### combined_chip_tw
- C02: 34 筆、勝率 55.9%、平均 2.9159%
- C21: 28 筆、勝率 50.0%、平均 1.6139%
- S02: 19 筆、勝率 42.1%、平均 -0.1116%
- S0_VCP收斂: 56 筆、勝率 51.8%、平均 2.4307%
- S1_破底翻: 41 筆、勝率 65.9%、平均 3.499%
- S2_量增突破: 795 筆、勝率 52.5%、平均 1.692%

## P0 判定 (事前門檻,僅供參考)

- combined_improves: False
- p0_full: {'filtered_combined_beats_filtered_baseline': True, 'filtered_comb_cum': 4738.61, 'filtered_base_cum': 4692.57, 'filtered_comb_mdd': -12.42, 'filtered_base_mdd': -11.8}
- p0_tw: {'pass': False, 'filtered_comb_cum': 3067.06, 'filtered_base_cum': 3370.8, 'filtered_comb_mdd': -11.56, 'filtered_base_mdd': -10.39}

## 限制
- 宇宙為目前成分名單 (非 point-in-time),仍有倖存者偏誤,數字僅供相對比較。
- 法人資料來自 FinMind (免費版日資料),與實盤 T86 來源不同,但濾網定義相同;上櫃在 FinMind 有資料、在 T86 無資料,故判定以 TW-only 為準。
- 新策略停損為 S2 式假設,非 XQ 原規格;C02 持有上限由 60 日壓至 55 日。
- 本報告不構成上線建議;最終是否融入由使用者決定。
