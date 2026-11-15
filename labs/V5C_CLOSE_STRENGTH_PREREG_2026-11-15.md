# V5c 預註冊：收盤強度濾網（S2／S3／S4）

- 狀態：已凍結（2026-11-15，使用者拍板開始測收盤強度後即凍結，不得再改）
- 來源：Gemini 第二輪檢討（2026-11-15 01:19，Hydrogen medium）提案①「訊號日收盤強度 (C-L)/(H-L) ≥ 0.70」，Muse 已驗證無實質錯誤；優先順序經使用者 2026-11-15 01:27 拍板「照順序測」，V5a（S1 地板）FAIL、V5b（S2 量能）PASS 後，下一項即為本變體。
- Base：`variant/v5b-s2-vol-open`（V5b 已採用成為新 Base：n=715、勝率 59.6%、RR 1.87、前半 57.7%／後半 61.5%、MDD -7.4%；S2 獨立 122 筆／63.1%／+2.23%、S3 50 筆／50.0%／+1.58%、S4 360 筆／59.4%／+2.47%）。
- 新分支：`variant/v5c-close-strength`，自 `origin/variant/v5b-s2-vol-open` 建。

## 唯一變因

僅針對 S2_量增突破、S3_起漲股、S4_強於大盤 三種訊號，疊加一道訊號日收盤強度濾網：

- 條件：訊號日 T 的收盤強度 `(Close_T - Low_T) / (High_T - Low_T) >= 0.70`，邊界值 0.70 算滿足。
- High_T == Low_T（當日零振幅）時視為不滿足（擋下），避免除以零。
- 強度只用訊號日 T 的日 K OHLC（收盤已知），無前視。
- S0_VCP收斂、S1_破底翻 訊號邏輯與 RS 掛載（表 A）一字不動；S2 的量能濾網（條件 A，>2.5 倍 5 日均量）與開盤位置條件（條件 B，Gap [-1%, +5%]）一字不動；`strategy_core.py` 一字不動；出場規則、停損、sizing、大盤閘門、universe、交易成本、回測期間（2016 起算）、MAX_CONCURRENT = 5、權益 10 萬、單筆風險 1.2%、單檔上限 35,000 全部凍結不動。

## 實作要點（已鎖定）

- 新增常數 `CLOSE_STRENGTH_MIN = 0.70`。
- 新增函數 `apply_close_strength_filter(sigs, df_raw, i)`：只處理 S2／S3／S4，其他策略原樣保留；用 `df_raw`（原始日 K）的 High／Low／Close 計算；回傳 `(kept, skipped_count)`，沿用 `apply_rs_mount`／`apply_s2_vol_filter` 的回傳 pattern。
- 掛載順序（診斷歸屬固定）：RS 掛載 → 量能濾網（S2）→ 收盤強度濾網（S2／S3／S4）。三道皆須通過才保留；`skipped_close_strength` 只計「通過前兩道、被收盤強度擋下」的筆數。
- 掛載點在收集階段，`apply_s2_vol_filter` 呼叫之後、`if not sigs:` 之前。
- 影子迴圈不需另加檢查：影子 extra 來自同一份已過濾的 sigs 清單，收盤強度已在清單層套用（與條件 B 不同，條件 B 用隔日開盤價、須在影子迴圈另查）。
- 診斷計數新增 `skipped_close_strength`，初始化為 0，寫入兩個 summary dict（空 trades 版與正常版），緊貼 `skipped_s2_open` 之後。

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
- S2＋S3＋S4 合併獨立勝率未高於 V5b 同三策略合併勝率 59.4%（V5b：S2 122 筆×63.1%＋S3 50 筆×50.0%＋S4 360 筆×59.4% ≈ 316 勝／532 筆）；
- 過濾後 S2＋S3＋S4 剩餘總筆數 < 30。

## 診斷須回報

- S2／S3／S4 各自獨立勝率／筆數／平均報酬（V5c vs V5b）、三者合併勝率；
- skipped_close_strength、各策略勝率；
- 總勝率、RR、period_split 前後半、MDD。

## 紀律

- 0.70 為先驗凍結常數，不得網格掃描、不得事後調門檻、不得拼湊其他策略組合或改套 S0／S1。
- 不得用盤中預估收盤價；不得改動出場、停損、sizing、大盤閘門、universe、成本、期間、MAX 5。
- 出場凍結、進場須可執行；門檻凍結後不做網格掃描。

## V5a／V5b 判讀摘要（供對照）

- V5a（S1 RS 地板 ≥40）：FAIL。總勝率 58.3%／RR 1.87，但 (d) 後半 57.7% < baseline 後半 58.2%；地板只擋 12 筆，S1 獨立勝率反降（52.7% vs V4a 53.6%）。
- V5b（S2 量能 >2.5 倍＋開盤 Gap [-1%,+5%]）：PASS、採用。n=715、勝率 59.6%、RR 1.87、前半 57.7%／後半 61.5%；S2 獨立 122 筆／63.1%／+2.23%；skipped_s2_vol 3,766、skipped_s2_open 36；RR 僅高門檻 0.01，邊際薄。
