# v2 修正版變更說明 (branch: fix/unify-backtest-v2)

日期: 2026-10-23
目的: 先修 bug 並統一回測與實盤邏輯，不調策略參數 (參數調整必須建立在可信的回測上)。

## 新增
- `strategy_core.py` — 策略單一真相來源 (SSOT): 指標計算、S0~S5 評估、部位 sizing、
  出場計畫、大盤濾網、法人濾網、出場模擬，全專案只此一份實作。
- `tests/test_core.py` — 11 個單元測試 (全數通過)，含合成資料 S4 跳空型態的
  gate 開/關驗證、高價股部位、法人/大盤濾網、censored 交易、成本後停損/停利模擬。

## 修正的 bug
1. **部位 sizing 強制 1 張** (screener `calc_position_size` 的 `max(1, ...)`)
   - v1: 股價 2,000 元 → 動用 66.7% 資金、最大虧損 60,000 元 (預算 36,000)；3,000 元 → 100% 資金。
   - v2: 1 張超限就回傳 lots=0 + reason="one_lot_exceeds_limits"，訊號跳過，不硬買。
2. **停損被夾進 -1.8%~-2.6%**: v2 不再夾，adjusted_stop = 結構停損，部位以真實距離計算。
3. **法人濾網無資料自動通過** (預設 0 且條件含 `total >= 0`，上櫃股永遠通過):
   - v2: chip 資料需 `has_data=True`，且三大法人合計買超 > 0、外資或投信至少一方買超。
   - 實盤預設 `REQUIRE_CHIP_DATA=True`：上櫃股在接上 TPEx 法人資料前不會出訊號 (已知取捨，見待辦)。
4. **回測與實盤邏輯分叉**:
   - v1 回測有大盤濾網、實盤沒有；回測無法人濾網、實盤有；兩邊各複製一份策略條件。
   - v2: screener 與 backtest 共用 `evaluate_candidate()` 與 `market_regime_ok()`；
     回測以 `require_chip=False` 執行並在報表揭露 `chip_filter_applied: false`。
5. **回測組合報酬失真**: v1 每筆以 35% 資金獨立複利 (可同時無限筆)。
   - v2: 同時持倉上限 5 檔、每筆權重 20%、同股持倉期間不重進、同日同股多策略只取一筆，
     並統計因組合已滿被跳過的訊號數。
6. **交易成本低估**: v1 只扣固定 0.5%。v2 逐段計: 買入 0.1425%，每段賣出 0.4425%
   (手續費 0.1425% + 證交稅 0.3%)，TP1/TP2/TP3 分批賣出各自計成本。
7. **未完交易假結算**: v1 資料結尾未出場的交易用最後收盤當已實現。
   - v2: 標記 censored 並剔除，報表揭露剔除筆數。
8. **monitor TP3 賣法不一致**: v1 用「剩餘張數 50%」推測。
   - v2: 依出場計畫 `tp3_reduce_lots` 明確張數執行 (screener 已把該欄位寫入持倉檔)；
     舊持倉無欄位才退回舊算法。

## 回測報表新增揭露欄位
- `version`, `period_split` (前/後半段勝率與平均報酬，簡易樣本外檢查)
- `censored_trades_excluded`, `skipped_portfolio_full`, `skipped_stock_busy`
- `max_concurrent_positions`, `slot_weight_pct`
- `chip_filter_applied: false`, `universe_point_in_time: false` (限制揭露)

## 尚未處理 (下一階段)
- Point-in-time 成分股名單 (目前仍用現行名單回測過去，倖存者偏誤未消除，數字僅供相對比較)
- TPEx 上櫃法人資料源 (接上後上櫃股才會重新出訊號)
- 3~5 年長期間 + walk-forward 參數驗證 (樣本數需先達到每策略 100+ 筆再談調參)
- yfinance 價格為除權息調整後價格的影響評估 (台股除息旺季停損觸發失真)
- monitor 盤中即時價 vs backtest 收盤價的移動停利觸發差異 (已在程式註解標示)
