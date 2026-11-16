# V8c 事前註冊（凍結）— S4 訊號日單根真實波幅 TR_Ratio ≥ 2.8%

- 日期：2026-11-16
- Base：V5b（分支 `variant/v5b-s2-vol-open` @ `7e77fb3`；backtest.py MD5 `1fc7d9ec05fe9e15f48d81d39be5c600`，433 行）
- 變體分支：`variant/v8c-s4-tr`（自 V5b 分支建，僅此一組用）
- 對照分支：`variant/v8c-baseline`（plain backtest.py，自 `e70eaba` 建；backtest.py MD5 `b7e9dfa484118ed507891d48877b7b59`）
- 凍結 base commit（本批共用）：`7e77fb3`（variant）／`e70eaba`（baseline）

## 共用口徑（V 系列不變）

- harness：backtest.py v2（與實盤共用 strategy_core.evaluate_candidate）；期間 2016-01-01 起至最新；成本買 0.1425%／賣 0.4425%；universe＝build_dynamic_index_universe()（上限 220 檔）；權益 10 萬、單筆風險 1.2%、MAX_CONCURRENT=5、單檔上限 35,000 元；有倖存者偏誤，數字僅供相對比較；法人濾網無歷史資料（chip_filter_applied=False）。
- simulate_exit v2 出場凍結不動；strategy_core.py MD5 驗收後必須仍為 `327932c52ff927c220af35ecc60f9ad2`。
- 過關條件 (a)–(f)：(a) variant 總勝率 > 55%；(b) variant 總 RR ≥ 1.8；(c) variant 總勝率 > 同批 baseline；(d) 前後半段勝率皆 > baseline 對應半段；(e) 總筆數 ≥ 30；(f) 平均報酬 > 0。任一不滿足即 FAIL。
- **本輪採用門檻：總勝率 > 59.6% 且總 RR ≥ 1.88**（與證偽條件對齊；V8 證偽門 RR < 1.88 比舊採用門 1.87 嚴一格，1.87 在本輪視為未過）。
- (d) 以落地 latest_backtest.json 的 period_split 為準；JSON 未落地時 (d) 視為未判、總表數字一律標「過渡」。

## 唯一變因：S4 收集階段單根真實波幅下限

- 於 S4 訊號日 t 收盤時，計算當日單根真實波幅與前一日收盤價之比率 TR_Ratio_t = Max(High_t − Low_t, |High_t − Close_{t−1}|, |Low_t − Close_{t−1}|) / Close_{t−1}。
- 僅當 TR_Ratio_t ≥ 0.028（含 2.8% 邊界）時保留該筆 S4 訊號並於 t+1 進場；TR_Ratio_t < 0.028 則跳過不進場。**不設任何上檔上限**。
- 僅作用於 S4；其餘策略不動。與已 FAIL 的 V5d（ATR(14)/Close ≤ 4.5% 上限）方向相反——本變體是突破當根的「最低波幅地板」，非重測。
- 實作：新函數 `apply_s4_tr_filter(sigs, df_raw, i)`，掛在 S4 收集階段（參照 V7b 的 apply_s4_prerun_filter 掛點）；常數 `S4_TR_RATIO_MIN = 0.028`；`i < 1` 時擋下（收集迴圈 start_idx=60，實務不觸發）；計數 `skipped_s4_tr`（主迴圈計數，影子迴圈同步過濾但不計數）。
- 進場仍走 trigger_buy（t+1 進場價不變，不得改進場價）。

## 證偽條件（任一成立即證偽）

1. 總勝率 ≤ 59.6% 或總 RR < 1.88。
2. 被濾除之 TR_Ratio_t < 0.028 的 S4 交易事後平均報酬 ≥ +2.47%（V5b S4 獨立平均）。
   - 算式：(V5b S4 總報酬 − Variant S4 總報酬)／(V5b S4 筆數 − Variant S4 筆數)，濾網只刪不增時成立。

## 參照基線（V5b）

- S4 獨立：360 筆／勝率 59.4%／平均報酬 +2.47%（佔總筆數 50.3%）；總體 n=715／勝率 59.6%／RR 1.87／平均 +2.32%。
- 動機：V7b 證明 S4 的高 RR 仰賴強動能擴張標的；低波動牛皮股以 < 2.8% 狹幅 K 線觸發 S4 時 ATR 基數極小，停利毛利僅約 +3%～+4%，扣除 0.585% 固定成本後淨賺 W 被嚴重侵蝕；要求突破當日具備 ≥ 2.8% 實質波幅擴張，可剔除被手續費吃掉賠率的微突破。
- 若 V7c 日後採納為新 Base，本變體仍成立（正交：僅作用於 S4）。

## 分支 workflow 補丁（非策略邏輯，兩分支皆做）

- step 4 的 `git pull --rebase --autostash origin main` 改為對本分支 rebase：`git pull --rebase --autostash origin "$(git rev-parse --abbrev-ref HEAD)"`；只 push 到本分支。
- 新增 step 4b：以 actions/upload-artifact@v4 上傳 `latest_backtest.json`（artifact 名 backtest-result，if: always()，if-no-files-found: warn），與寫回同一步備份結果檔。
