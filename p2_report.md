# P2 實際張數權重回測報告 (2026-10-30 13:13:42)

## 設計 (預註冊,跑前凍結)

- universe: build_dynamic_index_universe()[:220] (same as backtest.py)
- window: 2020-01-01 ~ latest (yfinance)
- flat: production: 20% weight each, 5-position count cap only
- real: calc_position_size capital_pct weight, 5-position cap + 100% capital cap
- verdict_rule: real MDD worse than flat MDD by >5pp => 35% cap too aggressive

## 兩組結果

| 組別 | n | 勝率% | RR | 累計% | MDD% | 最高同時名目% | 資金上限跳過 |
|---|---|---|---|---|---|---|---|
| flat | 1169 | 52.0 | 2.05 | 4728.82 | -18.12 | 442.7 | 0 |
| real | 918 | 50.3 | 2.06 | 8227.88 | -27.1 | 408.9 | 932 |

## P2 判定 (事前標準)

- {'real_mdd_worse_by_pp': 8.98, 'cap_too_aggressive': True, 'note': 'cap_too_aggressive True => 35% cap too aggressive, lower cap or reduce count first.'}
