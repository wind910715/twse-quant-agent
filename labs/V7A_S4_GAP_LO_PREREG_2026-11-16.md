# V7a 預註冊（凍結）— S4 隔日開盤單邊下檔跳空保護濾網

凍結日期：2026-11-16
來源：Gemini 第四輪提案一（[TO-MUSE] id `1a1e16ac7843fce3`），經 Muse 驗證（`GEMINI_V7_REVIEW_VERIFICATION_2026-11-16.md`）後重述。
Base：`variant/v5b-s2-vol-open`（Sync 7e77fb3）。Base backtest.py MD5 `1fc7d9ec05fe9e15f48d81d39be5c600`，433 行。
分支：`variant/v7a-s4-gap-lo`
strategy_core.py：不動（MD5 須維持 `327932c52ff927c220af35ecc60f9ad2`）。

## 唯一變因

僅 S4（動能突破策略）在主迴圈與影子迴圈加一道開盤跳空下檔檢查：

- 訊號日 t、進場日 t+1。Gap = n_open / signal_close − 1（主迴圈）／e_gap = cand["n_open"] / extra["entry_price"] − 1（影子迴圈）。
- 僅當 Gap ≥ −0.005（含 −0.5% 邊界）時保留；Gap < −0.005 時跳過該筆訊號不進場。
- **不設任何上檔跳空上限**（V6a 的 +4.0% 上限移除；該上限因 1.028 閘門實務上從未觸發，見驗證檔第一節）。
- 檢查位置與 V6a 相同：主迴圈 1.028 閘門之後、trigger_buy 之前；影子迴圈同位置（不計數）。
- **進場價不變**：通過檢查者仍按既有 trigger_buy = round(max(signal_close, n_open × 1.003), 2) 進場。Gemini 原文「以 t+1 實際開盤價進場」在此重述為「於 t+1 進場」，不得改寫進場價邏輯。
- 常數：`S4_OPEN_GAP_LO = -0.005`；不定義上檔常數。
- 計數器：`skipped_s4_gap`（主迴圈計數，寫入兩個 summary；影子迴圈不計數）。

其餘全部不動：V5b 的 S2 量能／開盤框、V4 RS 掛載、出場 simulate_exit v2、成本、期間、universe、MAX_CONCURRENT=5、單檔上限 35,000 元。

## 預期機制（重述後）

V6a 已實證 S4 隔日開盤下檔濾網能把 S4 獨立勝率由 59.4% 推至 61.6%；V7a 測試把下檔下限由 −1% 收緊為 −0.5% 是否進一步濾掉開低轉弱單、同時守住 RR。Gemini 原稱「放開右尾」恢復右尾贏家的機制已證偽（+4% 上限從未觸發），不得作為本變體的機制宣稱。

## 採用條件（全滿足才 PASS）

(a) variant 總勝率 > 55%；(b) variant 總 RR ≥ 1.8；(c) variant 總勝率 > 同批 baseline；(d) 前後半段勝率皆 > baseline 對應半段；(e) 總筆數 ≥ 30；(f) 平均報酬 > 0。另加：總勝率 > 59.6% 且 RR ≥ 1.87（現行 Base V5b 門檻）。

## 證偽條件

- S4 獨立勝率未高於 V5b S4（360 筆／59.4%／+2.47%），或總筆數 < 30。
- （註：Gemini 原證偽條款「被濾除交易事後平均報酬 ≥ +2.47%」harness 無法直接觀測，改以 S4 獨立比較評估。）

## 對照

- 同批重跑 `variant/v5-baseline`（plain 版，無 RS 掛載）供 (c)(d) 比較；採用門檻比 V5b 數字（n=715、59.6%、RR 1.87、MDD -7.4%）。
- V5b 各策略基線：S0 68／70.6%／+3.25%、S1 115／53.9%／+1.73%、S2 122／63.1%／+2.23%、S3 50／50.0%／+1.58%、S4 360／59.4%／+2.47%。
