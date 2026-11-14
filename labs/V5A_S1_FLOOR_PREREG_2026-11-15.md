# V5a 預註冊：S1 破底翻 RS 地板 ≥40（2026-11-15 凍結）

## 來源
Gemini 第二輪檢討（2026-11-15 01:19，Hydrogen medium）新提案第 1 順位，Muse 已驗證可用（`GEMINI_REVIEW_VERIFICATION_2026-11-15.md`）。機制：V4a 表 A 讓 S1 完全不掛 RS，會放行結構性長空的「接刀股」，且 S1 優先序高於 S2–S4，可能佔槽位排擠高 RS 訊號；地板 40 只剃最弱尾端。

## 唯一變因（單一變因，其餘全部凍結）
- Base：`variant/v4a-fixed-loop`（修復後日曆迴圈＋V4a 表 A＋step4 修復，commit dbedd43＋Sync 9e1fa59）。
- 新分支：`variant/v5a-s1-floor-40`。
- 改動：僅 `backtest.py` 的 RS 掛載區——S1_破底翻訊號須複合 RS ≥ 40.0 才保留（新增常數 `RS_S1_FLOOR = 40.0`，掛載邏輯在 `apply_rs_mount` 內對 S1 單獨設地板）；S0／S2／S3／S4 維持複合 RS ≥ 80.0；`strategy_core.py` 一字不動；進場、確認、出場、停損、sizing、大盤閘門、universe、成本、期間（2016 起算）、MAX_CONCURRENT=5、權益 10 萬、單筆風險 1.2%、單檔上限 35,000 全部不動。
- 診斷：新增 `skipped_s1_floor` 計數（與 `skipped_rs_mount` 同 pattern，寫進 summary，不影響交易行為）。
- RS 口徑與 V4a 相同：複合＝(rs_year＋rs_quarter)/2，缺一取其一，皆缺取 50.0（<40 擋下）；訊號日收盤計算、截面百分位×99、無前視。

## 對照組
- 同批 plain baseline：分支 `variant/v5-baseline`（自 `fix/step4-autostash-20261114` 的 e047cf5 建，無程式改動，修復後迴圈、無掛載），同批派 run。Baseline run 43621275051 已派發（2026-11-15 01:28，in_progress）。
- 採用比較基準：V4a 修復後迴圈重測 run 43611162569（n=1,133、勝率 58.1%、RR 1.86、前半 58.7%／後半 57.5%）。

## 過關條件（事前宣告）
(a)–(f) 凍結模板（同 V1–V4）：(a) 總勝率 >55%；(b) 總 RR ≥1.8；(c) 總勝率 > 同批 baseline；(d) 前後半段勝率皆 > baseline 對應半段；(e) 總筆數 ≥30；(f) 平均報酬 >0。全滿足才 PASS。
採用條件（取代 V4a 表 A 上線）：(a)–(f) 全過 **且** 總勝率 > 58.1%（V4a 修復後迴圈）**且** RR ≥ 1.86。若 (a)–(f) 過但勝率 ≤58.1% 或 RR <1.86，判定 FAIL、不採用，表 A 維持不動。
診斷須回報：S1 獨立勝率／筆數（V5a vs V4a）、被地板擋下的 S1 筆數（skipped_s1_floor）、各策略勝率。

## 紀律
- 門檻 40.0 為先驗常數，不得事後調門檻（40 vs 50 不另測）、不得網格掃描、不得事後拼湊其他掛載表。
- 若 FAIL，此路（S1 地板）不追，依 Gemini 順序下一位是 V5 方向一 S2 量能濾網。
- 程式撰寫交 AGY（Opus 4.6 優先），Muse 驗收後才派 GitHub run；每輪同批重跑 baseline。
