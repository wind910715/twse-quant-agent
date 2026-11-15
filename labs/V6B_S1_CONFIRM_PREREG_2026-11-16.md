# V6b 預註冊：S1 量縮止跌＋隔日平開確認（S1 專屬）

- 狀態：已凍結（2026-11-16，使用者拍板「先測 V6a、V6b、V6d 就好」且 V6a 判讀完後即凍結，不得再改）
- 來源：Gemini 第三輪檢討（2026-11-15 21:56，Hydrogen）提案 V6b「S1 量縮止跌＋隔日平開」，
  Muse 已驗證可用（驗證檔 `GEMINI_V6_REVIEW_VERIFICATION_2026-11-15.md`）。
- Base：`variant/v5b-s2-vol-open`（V6a FAIL 不疊加，V5b 維持 Base：n=715、勝率 59.6%、RR 1.87、前半 57.7%／後半 61.5%、MDD -7.4%；
  S0 68 筆／70.6%／+3.25%、S1 115 筆／53.9%／+1.73%、S2 122 筆／63.1%／+2.23%、S3 50 筆／50.0%／+1.58%、S4 360 筆／59.4%／+2.47%）。
- 新分支：`variant/v6b-s1-confirm`，自 `origin/variant/v5b-s2-vol-open` 建。

## 唯一變因

僅針對 S1_破底翻 訊號，疊加兩道進場前置確認，兩道皆須通過才保留：

- 條件 A（量縮）：訊號日 T 的成交量 `Volume_T ≤ 2.0 × MA(Volume,20)_T`。
  MA20 為 T-19 至 T（含 T 日）20 個交易日成交量的簡單算術平均（與 V5b 條件 A 的 MA(Volume,5) 含 T 日口徑一致）。
  上市交易日不足 20 日（i < 19）視為不滿足（擋下）。
- 條件 B（隔日平開）：隔日 T+1 開盤價 `Open_{T+1} ≥ Close_T × 1.00`，邊界值含（剛好平開算滿足）。
  `Close_T` 即訊號日收盤（`signal_close`／`entry_price`，strategy_core.py L394／L474 已證實）。
- 未過任一道者該筆 S1 訊號直接放棄不進場，計入診斷計數器 `skipped_s1_confirm`
  （條件 A 擋下計 1 次；通過 A、條件 B 擋下計 1 次；兩道計數合併為同一計數器）。
- S0／S2／S3／S4 訊號邏輯、RS 掛載（表 A，S1 本就不掛）、S2 量能濾網（條件 A）與開盤位置條件（條件 B，Gap [-1%, +5%]）一字不動；
  `strategy_core.py` 一字不動；出場規則、停損、sizing、大盤閘門、universe、交易成本、回測期間（2016 起算）、MAX_CONCURRENT = 5、權益 10 萬、單筆風險 1.2%、單檔上限 35,000 全部凍結不動。

## 實作要點（已鎖定）

- 新增常數 `S1_VOL_MULT = 2.0`、`S1_VOL_MA_WINDOW = 20`、`S1_OPEN_MIN = 1.00`。
- 條件 A 在收集階段：新增函數 `apply_s1_confirm_filter(sigs, df_raw, i)`，只處理 S1，其他策略原樣保留；
  用 `df_raw`（原始日 K）的 Volume 計算；回傳 `(kept, skipped_count)`，沿用 `apply_rs_mount`／`apply_s2_vol_filter` 的回傳 pattern；
  掛載點在 `apply_s2_vol_filter` 呼叫之後、`if not sigs:` 之前。
- 條件 B 在主迴圈：`n_open` 讀出、既有 1.028 閘門之後、`trigger_buy` 之前（與 V5b 條件 B 同一位置）；
  僅當候選策略為 S1 時檢查，其他策略跳過；未過計 `skipped_s1_confirm += 1` 並 `continue`。
- 影子迴圈：S1 的影子 extra 同樣加條件 B 檢查（不計數），與 V5b 條件 B 在影子迴圈的處理一致；
  條件 A 已在收集階段套用於同一份 sigs 清單，影子不需另加。
- 診斷計數新增 `skipped_s1_confirm`，初始化為 0，寫入兩個 summary dict（空 trades 版與正常版），緊貼 `skipped_s2_open` 之後。
- **註記**：條件 B 的上限由既有 1.028 閘門把關（開高 >+2.8% 已被擋下），本條件真正起作用的是「隔日不可開低於昨收」的下限。

## 採用條件（事前宣告）

同批重跑 `variant/v5-baseline`（無程式改動的 plain baseline 分支）；(a)–(f) 全過 **且** 總勝率 > 59.6% **且** RR ≥ 1.87，才取代 V5b 成為新 Base；否則 FAIL、不採用，V5b 維持不動。

- (a) variant 總勝率 > 55%
- (b) variant 總 RR ≥ 1.8
- (c) variant 總勝率 > 同批 baseline
- (d) 前後半段勝率皆 > baseline 對應半段
- (e) 總筆數 ≥ 30
- (f) 平均報酬 > 0

## 證偽條件（任一即 FAIL）

- 未達上述採用門檻；
- S1 獨立勝率未高於 V5b S1（115 筆／53.9%／平均 +1.73%）；
- 過濾後總筆數 < 30。

## 診斷須回報

- S1 獨立勝率／筆數／平均報酬（V6b vs V5b）；
- skipped_s1_confirm、skipped_portfolio_full（資金利用率變化）、總筆數變化；
- 總勝率、RR、period_split 前後半、MDD；
- 其餘四策略勝率／筆數變化（確認無連帶誤傷）。

## 紀律

- 2.0 倍、MA20、1.00 為先驗凍結常數，不得網格掃描、不得事後調門檻、不得拼湊其他策略組合或改套 S0／S2／S3／S4。
- 不得用盤中預估收盤價；不得改動出場、停損、sizing、大盤閘門、universe、成本、期間、MAX 5。
- 出場凍結、進場須可執行；門檻凍結後不做網格掃描。

## V6a 判讀摘要（供對照）

- V6a（S4 開盤 Gap [-1%, +4%]）：FAIL。n=687、勝率 60.7%、RR 1.82（(a)–(f) 全過、勝率 > 59.6%，唯 RR 未達 1.87）；
  S4 獨立 331 筆／61.6%／+2.55%（vs V5b 360 筆／59.4%／+2.47%），機制有效但 RR 差 0.05；skipped_s4_gap 188、skipped_portfolio_full 54（V5b 66）。
