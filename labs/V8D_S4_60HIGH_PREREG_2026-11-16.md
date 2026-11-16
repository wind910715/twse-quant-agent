# V8d 事前註冊（凍結）— S4 訊號日收盤創 60 日收盤新高

- 日期：2026-11-16
- Base：V5b（分支 `variant/v5b-s2-vol-open` @ `7e77fb3`；backtest.py MD5 `1fc7d9ec05fe9e15f48d81d39be5c600`，433 行）
- 變體分支：`variant/v8d-s4-60high`（自 V5b 分支建，僅此一組用）
- 對照分支：`variant/v8d-baseline`（plain backtest.py，自 `e70eaba` 建；backtest.py MD5 `b7e9dfa484118ed507891d48877b7b59`）
- 凍結 base commit（本批共用）：`7e77fb3`（variant）／`e70eaba`（baseline）

## 共用口徑（V 系列不變）

- harness：backtest.py v2（與實盤共用 strategy_core.evaluate_candidate）；期間 2016-01-01 起至最新；成本買 0.1425%／賣 0.4425%；universe＝build_dynamic_index_universe()（上限 220 檔）；權益 10 萬、單筆風險 1.2%、MAX_CONCURRENT=5、單檔上限 35,000 元；有倖存者偏誤，數字僅供相對比較；法人濾網無歷史資料（chip_filter_applied=False）。
- simulate_exit v2 出場凍結不動；strategy_core.py MD5 驗收後必須仍為 `327932c52ff927c220af35ecc60f9ad2`。
- 過關條件 (a)–(f)：(a) variant 總勝率 > 55%；(b) variant 總 RR ≥ 1.8；(c) variant 總勝率 > 同批 baseline；(d) 前後半段勝率皆 > baseline 對應半段；(e) 總筆數 ≥ 30；(f) 平均報酬 > 0。任一不滿足即 FAIL。
- **本輪採用門檻：總勝率 > 59.6% 且總 RR ≥ 1.88**（與證偽條件對齊；V8 證偽門 RR < 1.88 比舊採用門 1.87 嚴一格，1.87 在本輪視為未過）。
- (d) 以落地 latest_backtest.json 的 period_split 為準；JSON 未落地時 (d) 視為未判、總表數字一律標「過渡」。

## 唯一變因：S4 收集階段 60 日收盤新高確認

- 於 S4 訊號日 t 收盤時，取前 59 個交易日（t-59 至 t-1）之最高收盤價 HC59_t = Max(Close_{t−59}, ..., Close_{t−1})。
- 僅當訊號日收盤價滿足 Close_t ≥ HC59_t（含等號邊界，即訊號日收盤價創下包含當日在內之近 60 個交易日收盤新高）時保留該筆 S4 訊號並於 t+1 進場；Close_t < HC59_t 則跳過不進場。
- 僅作用於 S4；其餘策略不動。
- 實作：新函數 `apply_s4_60high_filter(sigs, df_raw, i)`，掛在 S4 收集階段（參照 V7b 的 apply_s4_prerun_filter 掛點）；窗口常數 `S4_60HIGH_WINDOW = 60`；`i < 59` 時擋下（收集迴圈 start_idx=60，實務不觸發）；計數 `skipped_s4_60high`（主迴圈計數，影子迴圈同步過濾但不計數）。
- 進場仍走 trigger_buy（t+1 進場價不變，不得改進場價）。

## 證偽條件（任一成立即證偽）

1. 總勝率 ≤ 59.6% 或總 RR < 1.88。
2. 被濾除之 Close_t < HC59_t 的 S4 交易事後平均報酬 ≥ +2.47%（V5b S4 獨立平均）。
   - 算式：(V5b S4 總報酬 − Variant S4 總報酬)／(V5b S4 筆數 − Variant S4 筆數)，濾網只刪不增時成立。

## 參照基線（V5b）

- S4 獨立：360 筆／勝率 59.4%／平均報酬 +2.47%（佔總筆數 50.3%）；總體 n=715／勝率 59.6%／RR 1.87／平均 +2.32%。
- 動機：在 simulate_exit v2 架構下，S4 獲利單能否從 +3% 小賺延展為右尾大賺的關鍵在於上方有無近距離歷史套牢賣壓；僅突破短週期高點、但 60 日內仍有前波收盤高點壓頂者，進場後上漲數個百分點即撞上解套賣壓而拉回觸發追蹤停利；強制要求收盤創 60 日新高（過去一季買盤全數解套），確保上方無密集套牢阻力。
- 若 V7c 日後採納為新 Base，本變體仍成立（正交：僅作用於 S4）。

## 分支 workflow 補丁（非策略邏輯，兩分支皆做）

- step 4 的 `git pull --rebase --autostash origin main` 改為對本分支 rebase：`git pull --rebase --autostash origin "$(git rev-parse --abbrev-ref HEAD)"`；只 push 到本分支。
- 新增 step 4b：以 actions/upload-artifact@v4 上傳 `latest_backtest.json`（artifact 名 backtest-result，if: always()，if-no-files-found: warn），與寫回同一步備份結果檔。
