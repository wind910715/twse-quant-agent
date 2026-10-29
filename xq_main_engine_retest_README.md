# XQ 通過者主系統引擎重測 — 預註冊設計 (2026-10-29)

問題: XQ 夜班 40 項中通過初篩的 C21 烏龜買進、C02 股價領先大盤創新高、
S02 上半年賺贏去年全年,能否融入主系統 twse-quant-agent (S0/S1/S2)?

## 設計 (跑前凍結,跑完不改參數翻盤)

- 宇宙: `build_dynamic_index_universe()[:220]`,與 backtest.py 完全相同。
- 窗口: yfinance 2020-01-01 起。
- S0/S1/S2 訊號: 與 backtest.py 完全相同的迴圈呼叫 `evaluate_candidate`
  (require_chip=False,與週六回測同口徑)。
- C21/C02/S02 訊號: 夜班結果檔 `xq_signals/{C21,C02,S02}.csv` 的
  (ticker, signal_date),不重新實作 XQ 進場條件。
- 新策略閘門 (訊號日當天): MV20 ≥ 1000 張、收盤 ≥ 25 元、
  `market_regime_ok` (0050 vs MA20 + 個股 20 日漲幅)。
- 進場: 與 backtest.py 相同的隔日進場確認。
- 新策略停損 (假設,非 XQ 原規格): S2 式
  `max(訊號日低點 × 0.995, 訊號收盤 × 0.975)`。
- 出場: `simulate_exit` 全套 (三階停利、移動停利、3 日汰弱、MAX_HOLD 55 日、
  censored 剔除)。C02 原 60 日持有被引擎壓至 55 日,須揭露。
- 成本: simulate_exit 內含 (買 0.1425%、每段賣 0.4425%)。
- 組合: 上限 5 檔 (每筆 20%)、同股持倉不重進、同日同股取優先序第一個。
  優先序 S0 > S1 > S2 > C21 > C02 > S02 (既有優先,保守增量測試)。
- 組態: baseline、C21-only、C02-only、S02-only、combined (六策略併跑)。
- 統計: n、勝率、平均、RR、前後半、組合累計、MDD、訊號日重疊率、
  combined 實際執行的新策略筆數。

## 整合判定門檻 (事前宣告,僅供參考)

單策略通過主系統標準 (平均 >0、RR ≥ 1.8、前後半皆 >0、n ≥ 30),
且 combined 累計 > baseline 累計、且 combined MDD 不比 baseline 差超過 3 個百分點。

## 限制

- 宇宙為目前成分名單 (非 point-in-time),倖存者偏誤仍在,僅供相對比較。
- 法人濾網未套用 (無歷史法人資料)。
- 新策略訊號來自夜班 529 檔宇宙,限制在 220 檔樣本內筆數會縮水。
- S02 訊號僅自 2021Q1 起。
- 本實驗不寫 latest_backtest.json、不動 monitor/screener、不發 TG、不下單。
