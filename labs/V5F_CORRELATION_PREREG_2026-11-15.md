# V5f 預註冊：持倉 20 日相關性上限（分配層）

- 狀態：已凍結（2026-11-15，使用者 17:46 回「不要問我 你一路跑完」後即凍結，不得再改）
- 來源：Gemini 第二輪檢討（2026-11-15 01:19，Hydrogen medium）提案「新提案 3 持倉相關性上限」，Muse 已驗證無實質錯誤（門檻為先驗常數、非數據推導；實作成本最高，Gemini 自己也排最後）；優先順序經使用者 2026-11-15 01:27 拍板「照順序測」，V5a FAIL、V5b PASS、V5c FAIL、V5d FAIL、V5e FAIL 後，下一項即為本變體；使用者 2026-11-15 17:46 定「不要問我 你一路跑完」——本變體測完後不問，直接接 V5 佇列下一項（若有），直到整輪全 FAIL 且無近況可探才停。
- Base：`variant/v5b-s2-vol-open`（V5b 已採用成為新 Base：n=715、勝率 59.6%、RR 1.87、前半 57.7%／後半 61.5%、MDD -7.4%；S2 獨立 122 筆／63.1%／+2.23%、S3 50 筆／50.0%／+1.58%、S4 360 筆／59.4%／+2.47%）。**注意：V5c、V5d、V5e 皆 FAIL 不採用，本變體不疊加收盤強度、ATR、RS Top 15% 濾網，Base 退回 V5b。**
- 新分支：`variant/v5f-correlation`，自 `origin/variant/v5b-s2-vol-open` 建。

## 唯一變因

僅在**分配階段**（allocation loop）疊加一道持倉相關性檢查，S0–S4 全體一視同仁：

- 條件：分配候選股時，計算候選股與每一檔「當前已持倉標的」過去 20 個交易日日報酬率的 Pearson 相關係數，任一組 `> 0.70` 則跳過該候選股（不佔槽位，槽位留給同日後續候選股）。
- 「當前已持倉標的」定義（鎖定）：`open_positions` 中滿足 `entry_date <= 候選進場日 < exit_date` 者。分配迴圈是按進場日升序、同日按 u_idx 依序分配，且每分配一檔就即時 append 進 `open_positions`，因此此定義自動包含「同日已入選者」（其 entry_date 等於當日進場日、exit_date 在進場日之後）。Gemini 原文「同日高優先序已入選標的」在此修復後迴圈中即為「同日按 u_idx 已分配者」，特此凍結澄清。
- 資料與無前視（鎖定）：只用訊號日（含）之前 20 個交易日的收盤價。報酬面板 `ret_m = close_m.pct_change()` 在 `run_portfolio_backtest` 內、`compute_rs_panels` 之後只算一次；相關係數取 `ret_m` 截至訊號日 `dt_idx` 的最後 20 列（即 `ret_m.loc[:dt_idx].tail(20)`）。訊號日當下收盤價已知，無前視。
- 缺值處理（鎖定）：視窗不足 20 列，或候選股／對照股任一方在該 20 列視窗內含 NaN（例如掛牌未滿、停牌缺價），視為無法計算、**不跳過**（放行）。不得用 0 或均值填補。
- 邊界（鎖定）：相關係數剛好等於 0.70 時保留，只有嚴格 `> 0.70` 才跳過。
- 門檻定義（鎖定）：`CORR_MAX = 0.70`、`CORR_WINDOW = 20`，皆為先驗凍結常數。
- 訊號收集階段、影子迴圈一字不動：相關性檢查只在分配迴圈做，影子交易不佔槽位、不檢查。S0／S1／S2／S3／S4 的訊號邏輯與 RS 掛載（表 A）一字不動；S2 的量能濾網（條件 A，>2.5 倍 5 日均量）與開盤位置條件（條件 B，Gap [-1%, +5%]）一字不動；`strategy_core.py` 一字不動；出場規則、停損、sizing、大盤閘門、universe、交易成本、回測期間（2016 起算）、MAX_CONCURRENT = 5、權益 10 萬、單筆風險 1.2%、單檔上限 35,000 全部凍結不動。

## 實作要點（已鎖定）

- 新增常數 `CORR_MAX = 0.70`、`CORR_WINDOW = 20`（放在 S2_OPEN_GAP_HI 旁邊）。
- 新增函數 `corr_too_high(ret_m, sym_a, sym_b, signal_date)`，放在 `apply_s2_vol_filter` 之後：
  - 取 `window = ret_m.loc[:signal_date].tail(CORR_WINDOW)`；若 `len(window) < CORR_WINDOW` 回傳 False。
  - 取兩欄序列；任一欄含 NaN 回傳 False。
  - 回傳 `float(a.corr(b)) > CORR_MAX`（pandas Pearson，邊界 0.70 不跳過）。
- 在 `run_portfolio_backtest` 內，`rs_year, rs_quarter = compute_rs_panels(close_m)` 之後立刻加一行：`ret_m = close_m.pct_change()`（只算一次，不得在每次分配時重算報酬面板）。
- 掛載點在分配階段：`overlapping` 判定與 `skipped_stock_busy`／`skipped_portfolio_full` 檢查之後、`simulate_exit` 之前，插入：
  - 對每一檔 `overlapping` 部位，取對照符號 `held_sym = f"{p['stock_id']}{per_stock[p['stock_id']]['sfx']}"`，候選符號 `cand_sym = f"{sid}{per_stock[sid]['sfx']}"`（`per_stock` 在 `run_portfolio_backtest` 作用域內，鍵為 sid；所有已進場部位必在 `per_stock` 內）。
  - 任一 `corr_too_high(ret_m, cand_sym, held_sym, cand["signal_date"])` 為 True 即 `skipped_corr += 1` 並 `continue`（不佔槽位、同日後續候選股繼續分配）。
  - 並在迴圈外（`skipped_s2_open = 0` 旁邊）初始化 `skipped_corr = 0`。
- 診斷計數新增 `skipped_corr`，寫入兩個 summary dict（空 trades 版與正常版），緊貼 `skipped_s2_open` 之後。
- 效能：分配候選約數千筆，每筆至多對 5 檔持倉算 20 點相關係數，不得重算報酬面板、不得在收集階段做相關性檢查。

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

- 各策略獨立勝率／筆數／平均報酬（V5f vs V5b）；
- skipped_corr、skipped_portfolio_full（資金利用率變化：總筆數與滿倉跳過數對照 V5b）；
- 總勝率、RR、period_split 前後半、MDD。

## 紀律

- 0.70／20 皆為先驗凍結常數，不得網格掃描、不得事後調門檻、不得改套部分策略、不得改用產業分類表或改算其他窗口／其他相關係數定義。
- 不得用盤中預估收盤價；不得改動出場、停損、sizing、大盤閘門、universe、成本、期間、MAX 5。
- 出場凍結、進場須可執行；門檻凍結後不做網格掃描。

## V5a／V5b／V5c／V5d／V5e 判讀摘要（供對照）

- V5a（S1 RS 地板 ≥40）：FAIL。總勝率 58.3%／RR 1.87，但 (d) 後半 57.7% < baseline 後半 58.2%；地板只擋 12 筆，S1 獨立勝率反降（52.7% vs V4a 53.6%）。
- V5b（S2 量能 >2.5 倍＋開盤 Gap [-1%,+5%]）：PASS、採用。n=715、勝率 59.6%、RR 1.87、前半 57.7%／後半 61.5%；S2 獨立 122 筆／63.1%／+2.23%；skipped_s2_vol 3,766、skipped_s2_open 36；RR 僅高門檻 0.01，邊際薄。
- V5c（收盤強度 (C-L)/(H-L) ≥ 0.70，S2／S3／S4）：FAIL。n=484、勝率 59.5%、RR 1.82；(a)–(f) 全過但總勝率未 > 59.6%、RR 未 ≥ 1.87；S2＋S3＋S4 合併勝率 58.9% < V5b 合併 59.4% 證偽觸發；S2 單獨變好（65.0%／80 筆），S3、S4 反變差。
- V5d（ATR(14)/Close ≤ 4.5%，全策略）：FAIL。n=532、勝率 58.1%、RR 1.84；(a)–(f) 全過但總勝率未 > 59.6%、RR 未 ≥ 1.87；S2 獨立勝率升至 65.3% 但平均報酬降，S4 為主要拖累 59.4%→56.6%；同批 baseline RR 1.96 反高於 V5d 1.84。
- V5e（宇宙 RS 相對排名 Top 15%，全策略）：FAIL。n=617、勝率 59.2%、RR 1.87；(a)–(f) 全過但總勝率未 > 59.6%；skipped_rs_top15 455，總筆數僅 -98；S1 被砍最多（115→51 筆）勝率微升但貢獻大減，S2／S3／S4 勝率全降；同批 baseline RR 1.97 反高於 V5e 1.87。
