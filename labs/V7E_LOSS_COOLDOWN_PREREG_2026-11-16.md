# V7e 預註冊（凍結）— 全系統同標的虧損出場後 15 交易日冷卻鎖

凍結日期：2026-11-16
來源：Gemini 第四輪提案五（[TO-MUSE] id `1a1e16ac7843fce3`），經 Muse 驗證（`GEMINI_V7_REVIEW_VERIFICATION_2026-11-16.md`）可測。
Base：`variant/v5b-s2-vol-open`（Sync 7e77fb3）。Base backtest.py MD5 `1fc7d9ec05fe9e15f48d81d39be5c600`，433 行。
分支：`variant/v7e-loss-cooldown`
strategy_core.py：不動（MD5 須維持 `327932c52ff927c220af35ecc60f9ad2`）。

## 唯一變因

全系統（S0–S4 共用）同標的虧損出場後冷卻鎖，實作在分配階段：

- 追蹤每檔標的最近一筆已出場的真實交易（primary `trades`，不含 shadow）：若該筆扣除成本後單筆淨報酬 `net_return_pct < 0.0`（不含 0.0，剛好打平不算虧損），該標的進入冷卻鎖定；若最近一筆淨報酬 `>= 0.0`（獲利或打平），冷卻天數為 0（即解除鎖定）。
- 冷卻窗口：以該標的自身日 K 交易日序號計，`[t_exit+1, t_exit+15]` 共 15 個交易日。候選進場日序號 `entry_idx` 與最近一筆虧損交易出場日序號 `exit_idx` 之差 `1 <= entry_idx - exit_idx <= 15` 時，該候選訊號一律跳過；差值 `>= 16` 時解除，可正常進場。
- 同股持倉不重疊（既有 stock_busy 規則），故同一標的的真實交易按進場／出場先後嚴格排序，最後一筆 append 進 `trades` 的交易即為該標的最近一筆已出場交易——以 append 順序追蹤即無前視。
- 實作：分配階段維護 `last_loss_exit_idx: dict[sid, exit_idx]`（虧損出場日序號表）。候選進入分配迴圈時，在 stock_busy 與 portfolio_full 檢查之後、`simulate_exit` 之前檢查：若 `sid` 在表中且 `1 <= entry_idx - last_loss_exit_idx[sid] <= 15`，計數 `skipped_cooldown += 1` 並跳過。真實交易 append 進 `trades` 後立即更新表：`net_ret_pct < 0.0` 時設 `last_loss_exit_idx[sid] = result["exit_idx"]`；`net_ret_pct >= 0.0` 時清除該標的表項（若有）。
- 常數：`LOSS_COOLDOWN_DAYS = 15`。
- 計數器：`skipped_cooldown`，初始化為 0，寫入兩個 summary dict，緊貼 `skipped_s2_open` 之後。
- 影子迴圈不動：shadow 只統計已進場 primary 的次要策略，不參與冷卻判定、不更新冷卻表。

其餘全部不動：V5b 的 S2 量能／開盤框、V4 RS 掛載、收集階段濾網（無）、出場 simulate_exit v2、成本、期間、universe、MAX_CONCURRENT=5、單檔上限 35,000 元、分配排序（entry_date＋u_idx）。

## 預期機制

攔截「觸發訊號 → 停損出場 → 數日內於下跌途中再度觸發 → 連續二次停損」的連環鞭打效應：剛虧損出場的標的短期內動能與籌碼皆未修復，再度進場的期望值為負，冷卻 15 日讓它走完修復期再說。

## 採用條件（全滿足才 PASS）

(a) variant 總勝率 > 55%；(b) variant 總 RR ≥ 1.8；(c) variant 總勝率 > 同批 baseline；(d) 前後半段勝率皆 > baseline 對應半段；(e) 總筆數 ≥ 30；(f) 平均報酬 > 0。另加：總勝率 > 59.6% 且 RR ≥ 1.87（現行 Base V5b 門檻）。

## 證偽條件

- 採用門檻未過；或 `skipped_cooldown < 30`（冷卻幾乎沒觸發，機制無從驗證）。
- 註：Gemini 原證偽條款「被 15 日冷卻鎖跳過之交易事後勝率 ≥ 55.0% 且平均報酬 ≥ +1.50%」harness 無法直接觀測（被跳過的交易不進場、無事後報酬），故改以總體／策略層變化與觸發筆數評估。

## 對照

- 同批重跑 `variant/v5-baseline`（plain 版，無 RS 掛載）供 (c)(d) 比較；採用門檻比 V5b 數字（n=715、59.6%、RR 1.87、MDD -7.4%）。
- 注意：精神類似法人系統 O-3「停損後 20 日冷卻」（該系統 FAIL），但系統／範圍／天數皆不同（法人 S1 單策略 20 日 vs 本系統全策略 15 日），不構成重提已 FAIL 方向。
