# V6e 預註冊（凍結）— 持倉 60 日左尾共跌頻率過濾

凍結日期：2026-11-16
來源：Gemini 第三輪提案（[TO-MUSE] id `1a1df7edf63c46c2`）＋第四輪補完規格（[TO-MUSE] id `1a1e16ac7843fce3`），經 Muse 驗證（`GEMINI_V7_REVIEW_VERIFICATION_2026-11-16.md`）可測。
Base：`variant/v5b-s2-vol-open`（Sync 7e77fb3）。Base backtest.py MD5 `1fc7d9ec05fe9e15f48d81d39be5c600`，433 行。
分支：`variant/v6e-left-tail-crash`
strategy_core.py：不動（MD5 須維持 `327932c52ff927c220af35ecc60f9ad2`）。

## 唯一變因

僅在分配階段加一道持倉左尾共跌過濾（選定單一「左尾共跌分支」，不依賴外部產業資料）：

- 候選標的 i 於訊號日 t 觸發、準備於 t+1 進場時，檢視於 t+1 開盤時仍持有中之帳上持倉標的集合 P——即分配迴圈已算出的 `overlapping`（`p["entry_date"] <= entry_date < p["exit_date"]`）。
- 若 |P| = 0，直接進場（不檢查）。
- 若 |P| >= 1，對集合 P 中每一檔現有持倉股 j：
  - 取收盤價面板 `close_m`（index＝交易日、columns＝`f"{sid}{sfx}"`，與 RS 面板同一份），分別取 i、j 截至訊號日 t 的最後 61 根收盤價（即 [t-60, t]），以 `pct_change()` 算出 60 個日收盤報酬率 R（R_k = Close_k / Close_{k-1} − 1）。
  - 共同有效日：i、j 報酬皆非 NaN 的交易日。共同有效日 < 60 時，視 N_crash(i,j) = 0。
  - N_crash(i,j) = 共同有效日中同時滿足 R_i ≤ −2.0% 且 R_j ≤ −2.0%（邊界含）的天數。
  - 任一 j 使 N_crash(i,j) ≥ 6（含）即跳過該候選；對所有 j 皆 N_crash ≤ 5 才正常進場。
- 常數：`CRASH_WINDOW = 60`（報酬天數；取 61 根收盤價）、`CRASH_RET = -0.02`、`CRASH_MIN = 6`。
- 計數器：`skipped_crash`，初始化為 0，寫入兩個 summary dict，緊貼 `skipped_s2_open` 之後。
- 檢查位置：分配迴圈內，stock_busy 與 portfolio_full 檢查之後、`simulate_exit` 之前。
- 影子迴圈不動：shadow 只統計已進場 primary 的次要策略，不參與左尾過濾。

其餘全部不動：V5b 的 S2 量能／開盤框、V4 RS 掛載、收集階段、出場 simulate_exit v2、成本、期間、universe、MAX_CONCURRENT=5、單檔上限 35,000 元、分配排序（entry_date＋u_idx）、stock_busy 與 portfolio_full 檢查。

## 預期機制

只單獨計算「單日雙雙重挫 ≤ −2.0%」的左尾極端共跌頻率，攔截高 Beta 同題材股在系統性殺盤日的連環停損；不看相關係數、不看產業分類，避免 V5f 相關性濾網幾乎不觸發的問題。

## 採用條件（全滿足才 PASS）

(a) variant 總勝率 > 55%；(b) variant 總 RR ≥ 1.8；(c) variant 總勝率 > 同批 baseline；(d) 前後半段勝率皆 > baseline 對應半段；(e) 總筆數 ≥ 30；(f) 平均報酬 > 0。另加：總勝率 > 59.6% 且 RR ≥ 1.87（現行 Base V5b 門檻）。

## 證偽條件

- 採用門檻未過；或 `skipped_crash < 30`（過濾幾乎沒觸發，機制無從驗證）。
- 註：Gemini 原證偽條款「被 N_crash ≥ 6 跳過之交易事後勝率 ≥ 59.6% 且平均報酬 ≥ +2.32%」harness 無法直接觀測（被跳過的交易不進場、無事後報酬），故改以總體／策略層變化與觸發筆數評估。

## 對照

- 同批重跑 `variant/v5-baseline`（plain 版，無 RS 掛載）供 (c)(d) 比較；採用門檻比 V5b 數字（n=715、59.6%、RR 1.87、MDD -7.4%）。
