# V8b 事前註冊（凍結）— S2 隔日開盤禁止追高 Gap_{t+1} ≤ +1.0%

- 日期：2026-11-16
- Base：V5b（分支 `variant/v5b-s2-vol-open` @ `7e77fb3`；backtest.py MD5 `1fc7d9ec05fe9e15f48d81d39be5c600`，433 行）
- 變體分支：`variant/v8b-s2-gap1`（自 V5b 分支建，僅此一組用）
- 對照分支：`variant/v8b-baseline`（plain backtest.py，自 `e70eaba` 建；backtest.py MD5 `b7e9dfa484118ed507891d48877b7b59`）
- 凍結 base commit（本批共用）：`7e77fb3`（variant）／`e70eaba`（baseline）

## 共用口徑（V 系列不變）

- harness：backtest.py v2（與實盤共用 strategy_core.evaluate_candidate）；期間 2016-01-01 起至最新；成本買 0.1425%／賣 0.4425%；universe＝build_dynamic_index_universe()（上限 220 檔）；權益 10 萬、單筆風險 1.2%、MAX_CONCURRENT=5、單檔上限 35,000 元；有倖存者偏誤，數字僅供相對比較；法人濾網無歷史資料（chip_filter_applied=False）。
- simulate_exit v2 出場凍結不動；strategy_core.py MD5 驗收後必須仍為 `327932c52ff927c220af35ecc60f9ad2`。
- 過關條件 (a)–(f)：(a) variant 總勝率 > 55%；(b) variant 總 RR ≥ 1.8；(c) variant 總勝率 > 同批 baseline；(d) 前後半段勝率皆 > baseline 對應半段；(e) 總筆數 ≥ 30；(f) 平均報酬 > 0。任一不滿足即 FAIL。
- **本輪採用門檻：總勝率 > 59.6% 且總 RR ≥ 1.88**（與證偽條件對齊；V8 證偽門 RR < 1.88 比舊採用門 1.87 嚴一格，1.87 在本輪視為未過）。
- (d) 以落地 latest_backtest.json 的 period_split 為準；JSON 未落地時 (d) 視為未判、總表數字一律標「過渡」。
- **V8b 須補下檔 −1% 不動**：Gemini 原文只寫上檔 ≤ +1.0%，未提下檔；單一變因解讀為僅收緊上檔，V5b 的 S2 開盤守門下檔 −1% 保留（見唯一變因）。

## 唯一變因：S2 隔日開盤跳空上限收緊至 +1.0%

- V5b 的 S2 開盤守門為 Gap ∈ [−1%, +5%]（Gap = Open_{t+1}/Close_t − 1）；本變體改為 Gap ∈ **[−1%, +1.0%]**——下檔 −1% 不動，上檔由 +5% 收緊至 +1.0%（含邊界）。
- 僅作用於 S2；其餘策略不動；主迴圈的 1.028 閘門（n_open > signal_close × 1.028 即跳過）仍先執行、不動。
- 實作：在 V5b 的 S2 開盤守門檢查點改上檔常數（現行上檔常數值 0.05 改為 `S2_OPEN_GAP_HI = 0.01`）；計數沿用現有 `skipped_s2_open`（主迴圈計數，影子迴圈同步過濾但不計數）。
- 進場仍走 trigger_buy（t+1 進場價不變，不得改進場價）；Gap > +1.0% 時放棄追價、跳過該筆。

## 證偽條件（任一成立即證偽）

1. 總勝率 ≤ 59.6% 或總 RR < 1.88。
2. 被跳過之 Gap_{t+1} > +0.010 的 S2 交易事後平均報酬 ≥ +2.23%（V5b S2 獨立平均）。
   - 算式：(V5b S2 總報酬 − Variant S2 總報酬)／(V5b S2 筆數 − Variant S2 筆數)，濾網只刪不增時成立。

## 參照基線（V5b）

- S2 獨立：122 筆／勝率 63.1%／平均報酬 +2.23%（佔總筆數 17.1%）；總體 n=715／勝率 59.6%／RR 1.87／平均 +2.32%。
- 動機：S2 本質為拉回支撐低接策略，t+1 大幅跳空開高逾 +1% 時追價買在偏高價位，同時壓縮獲利空間 W 並放大停損距離 L；限制不追高直接改善進場價位階。
- 若 V7c 日後採納為新 Base，本變體仍成立（正交：僅作用於 S2）。

## 分支 workflow 補丁（非策略邏輯，兩分支皆做）

- step 4 的 `git pull --rebase --autostash origin main` 改為對本分支 rebase：`git pull --rebase --autostash origin "$(git rev-parse --abbrev-ref HEAD)"`；只 push 到本分支。
- 新增 step 4b：以 actions/upload-artifact@v4 上傳 `latest_backtest.json`（artifact 名 backtest-result，if: always()，if-no-files-found: warn），與寫回同一步備份結果檔。
