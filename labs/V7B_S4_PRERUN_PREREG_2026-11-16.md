# V7b 預註冊（凍結）— S4 突破前 5 日累計漲幅未過熱濾網

凍結日期：2026-11-16
來源：Gemini 第四輪提案二（[TO-MUSE] id `1a1e16ac7843fce3`），經 Muse 驗證（`GEMINI_V7_REVIEW_VERIFICATION_2026-11-16.md`）可測。
Base：`variant/v5b-s2-vol-open`（Sync 7e77fb3）。Base backtest.py MD5 `1fc7d9ec05fe9e15f48d81d39be5c600`，433 行。
分支：`variant/v7b-s4-prerun`
strategy_core.py：不動（MD5 須維持 `327932c52ff927c220af35ecc60f9ad2`）。

## 唯一變因

僅 S4（動能突破策略）在收集階段加一道訊號日前漲幅濾網：

- 訊號日 t 收盤時，計算突破前 5 個交易日累計漲幅 PreRun5 = (Close_{t-1} − Close_{t-6}) / Close_{t-6}（t-6 收盤至 t-1 收盤共 5 日漲幅，不含訊號日 t 當日漲幅）。
- 僅當 PreRun5 ≤ +0.085（含 +8.5% 邊界）時保留該筆 S4 訊號；PreRun5 > +0.085 時跳過不進場。
- 實作：新增函數 `apply_s4_prerun_filter(sigs, df_raw, i)`，照抄既有 `apply_s2_vol_filter` 的寫法與回傳 pattern `(kept, skipped_count)`；只處理 `strategy == "S4_強於大盤"`，其他策略原樣保留。
- 掛載點：收集階段 `apply_s2_vol_filter` 呼叫之後、`if not sigs:` 之前；回傳的 skipped 數累加進 `skipped_s4_prerun`。
- 邊界：`i < 6`（交易日不足）視為不滿足，擋下（收集迴圈 start_idx=60，實務上不會觸發，僅作防護）。
- 計數器：`skipped_s4_prerun`，初始化為 0，寫入兩個 summary dict，緊貼 `skipped_s2_open` 之後。

其餘全部不動：V5b 的 S2 量能／開盤框、V4 RS 掛載、出場 simulate_exit v2、成本、期間、universe、MAX_CONCURRENT=5、單檔上限 35,000 元；進場仍走既有 trigger_buy 邏輯。

## 預期機制

剔除在突破前 5 日已先急漲超過 +8.5%、短線波段動能提前耗盡的末端追高單；底部或平台整理蓄勢後首根突破的 S4 保留完整後續波段空間。不限制訊號日 t 當天突破漲幅與波動度，亦不限制 t+1 開盤跳空。

## 採用條件（全滿足才 PASS）

(a) variant 總勝率 > 55%；(b) variant 總 RR ≥ 1.8；(c) variant 總勝率 > 同批 baseline；(d) 前後半段勝率皆 > baseline 對應半段；(e) 總筆數 ≥ 30；(f) 平均報酬 > 0。另加：總勝率 > 59.6% 且 RR ≥ 1.87（現行 Base V5b 門檻）。

## 證偽條件

- S4 獨立勝率未高於 V5b S4（360 筆／59.4%／+2.47%），或總筆數 < 30。
- （註：Gemini 原證偽條款「被濾除交易事後勝率 ≥ 59.4% 且平均報酬 ≥ +2.47%」harness 無法直接觀測，改以 S4 獨立比較評估。）

## 對照

- 同批重跑 `variant/v5-baseline`（plain 版，無 RS 掛載）供 (c)(d) 比較；採用門檻比 V5b 數字（n=715、59.6%、RR 1.87、MDD -7.4%）。
- V5b 各策略基線：S0 68／70.6%／+3.25%、S1 115／53.9%／+1.73%、S2 122／63.1%／+2.23%、S3 50／50.0%／+1.58%、S4 360／59.4%／+2.47%。
