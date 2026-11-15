# V5e 預註冊：宇宙 RS 相對排名 Top 15% 濾網（全策略）

- 狀態：已凍結（2026-11-15，使用者回「不要問我 你一路跑完」後即凍結，不得再改）
- 來源：Gemini 第二輪檢討（2026-11-15 01:19，Hydrogen medium）提案「方向二 宇宙 RS 相對排名 Top 15%」，Muse 已驗證無實質錯誤（門檻為先驗常數、非數據推導）；優先順序經使用者 2026-11-15 01:27 拍板「照順序測」，V5a FAIL、V5b PASS、V5c FAIL、V5d FAIL 後，下一項即為本變體；使用者 2026-11-15 17:46 定「不要問我 你一路跑完」——本變體測完後不問，直接接著測相關性，直到整輪全 FAIL 且無近況可探才停。
- Base：`variant/v5b-s2-vol-open`（V5b 已採用成為新 Base：n=715、勝率 59.6%、RR 1.87、前半 57.7%／後半 61.5%、MDD -7.4%；S2 獨立 122 筆／63.1%／+2.23%、S3 50 筆／50.0%／+1.58%、S4 360 筆／59.4%／+2.47%）。**注意：V5c、V5d 皆 FAIL 不採用，本變體不疊加收盤強度、ATR 濾網，Base 退回 V5b。**
- 新分支：`variant/v5e-rs-top15`，自 `origin/variant/v5b-s2-vol-open` 建。

## 唯一變因

僅針對**全部五種策略**（S0_VCP收斂、S1_破底翻、S2_量增突破、S3_起漲股、S4_強於大盤），疊加一道訊號日宇宙 RS 相對排名濾網：

- 條件：訊號日 T，該檔的複合 RS ≥ 當日全宇宙複合 RS 的 85 百分位門檻（即只留宇宙前 15%），邊界值（含門檻）算滿足。
- 複合 RS 定義（與 `rs_composite` 逐字同一口徑）：年＋季皆有取平均；缺一取其一；皆缺取 50.0。
- 門檻定義（鎖定）：先算出全宇宙複合 RS 面板，再對訊號日 T 的截面（全部 universe 欄位，皆缺者以 50.0 計入）取 85 百分位，pandas `quantile(0.85, axis=1)`（預設線性內插，等同 numpy percentile 線性內插）。門檻只用訊號日 T 當下已知的收盤價算出，無前視。
- 訊號日 T 不在面板 index 內時視為不滿足（全部擋下）。
- S0／S1／S2／S3／S4 的訊號邏輯與 RS 掛載（表 A）一字不動；S2 的量能濾網（條件 A，>2.5 倍 5 日均量）與開盤位置條件（條件 B，Gap [-1%, +5%]）一字不動；`strategy_core.py` 一字不動；出場規則、停損、sizing、大盤閘門、universe、交易成本、回測期間（2016 起算）、MAX_CONCURRENT = 5、權益 10 萬、單筆風險 1.2%、單檔上限 35,000 全部凍結不動。
- 交互作用（已告知、非 bug）：已掛載策略須同時滿足複合 ≥ 80 與複合 ≥ 當日 P85 門檻；P85 門檻通常在 85–90 附近，多半比 80 更嚴。S1 雖未掛表 A，仍須過 Top 15% 門檻（宇宙層級濾網，全策略一視同仁）。

## 實作要點（已鎖定）

- 新增常數 `RS_TOP15_PERCENTILE = 85.0`。
- 新增函數 `compute_rs_top15_thresholds(rs_year, rs_quarter)`，在 `compute_rs_panels` 之後：
  - 複合面板向量化算法（與 `rs_composite` 逐值規則等價）：`comp = ((rs_year.fillna(rs_quarter) + rs_quarter.fillna(rs_year)) / 2).fillna(50.0)`。
  - 回傳 `thresholds = comp.quantile(0.85, axis=1)`（pandas Series，index 為日期），只算一次。
  - **不得在每次訊號呼叫時重算截面百分位**（迴圈約 44 萬次呼叫，逐次重算會拖垮回測）；門檻序列在 `run_portfolio_backtest` 內、`compute_rs_panels` 之後算一次，傳進濾網函數。
- 新增函數 `apply_rs_top15_filter(sigs, rs_year, rs_quarter, thresholds, signal_date, symbol)`，放在 `apply_s2_vol_filter` 之後：
  - 對每一個訊號（不分策略）都檢查；不要按策略區分。
  - 該檔複合分數用現成 `rs_composite(rs_year, rs_quarter, signal_date, symbol)` 取值（與複合面板同一口徑，保證一致）。
  - 若 `signal_date` 不在 `thresholds.index` 內，全部擋下（skipped += len(sigs)）。
  - 否則門檻 `thr = float(thresholds.at[signal_date])`，複合 ≥ thr 才保留（邊界含）。
  - 回傳 `(kept, skipped_count)`，沿用 `apply_s2_vol_filter` 的回傳 pattern。
- 掛載順序（診斷歸屬固定）：RS 掛載 → 量能濾網（S2）→ Top 15% 濾網（全策略）。三道皆須通過才保留；`skipped_rs_top15` 只計「通過前兩道、被 Top 15% 擋下」的筆數。
- 掛載點在收集階段，`apply_s2_vol_filter` 呼叫之後、`if not sigs:` 之前。
- 影子迴圈不需另加檢查：影子 extra 來自同一份已過濾的 sigs 清單，Top 15% 已在清單層套用（與條件 B 不同，條件 B 用隔日開盤價、須在影子迴圈另查）。
- 診斷計數新增 `skipped_rs_top15`，初始化為 0，寫入兩個 summary dict（空 trades 版與正常版），緊貼 `skipped_s2_open` 之後。

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
- 過濾後總筆數 < 30。

## 診斷須回報

- 各策略獨立勝率／筆數／平均報酬（V5e vs V5b）；
- skipped_rs_top15；
- 總勝率、RR、period_split 前後半、MDD。

## 紀律

- 85.0 為先驗凍結常數，不得網格掃描、不得事後調門檻、不得改套部分策略、不得把門檻改成排名位次切法（不用「前 N 檔」名次制，一律用百分位門檻制，含邊界平手）。
- 不得用盤中預估收盤價；不得改動出場、停損、sizing、大盤閘門、universe、成本、期間、MAX 5。
- 出場凍結、進場須可執行；門檻凍結後不做網格掃描。

## V5a／V5b／V5c／V5d 判讀摘要（供對照）

- V5a（S1 RS 地板 ≥40）：FAIL。總勝率 58.3%／RR 1.87，但 (d) 後半 57.7% < baseline 後半 58.2%；地板只擋 12 筆，S1 獨立勝率反降（52.7% vs V4a 53.6%）。
- V5b（S2 量能 >2.5 倍＋開盤 Gap [-1%,+5%]）：PASS、採用。n=715、勝率 59.6%、RR 1.87、前半 57.7%／後半 61.5%；S2 獨立 122 筆／63.1%／+2.23%；skipped_s2_vol 3,766、skipped_s2_open 36；RR 僅高門檻 0.01，邊際薄。
- V5c（收盤強度 (C-L)/(H-L) ≥ 0.70，S2／S3／S4）：FAIL。n=484、勝率 59.5%、RR 1.82；(a)–(f) 全過但總勝率未 > 59.6%、RR 未 ≥ 1.87；S2＋S3＋S4 合併勝率 58.9% < V5b 合併 59.4% 證偽觸發；S2 單獨變好（65.0%／80 筆），S3、S4 反變差。
- V5d（ATR(14)/Close ≤ 4.5%，全策略）：FAIL。n=532、勝率 58.1%、RR 1.84；(a)–(f) 全過但總勝率未 > 59.6%、RR 未 ≥ 1.87；S2 獨立勝率升至 65.3% 但平均報酬降，S4 為主要拖累 59.4%→56.6%；同批 baseline RR 1.96 反高於 V5d 1.84。
