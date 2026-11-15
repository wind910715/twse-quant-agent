# V7d 預註冊（凍結）— S3 訊號日攻擊量能確認濾網

凍結日期：2026-11-16
來源：Gemini 第四輪提案四（[TO-MUSE] id `1a1e16ac7843fce3`），經 Muse 驗證（`GEMINI_V7_REVIEW_VERIFICATION_2026-11-16.md`）可測。
Base：`variant/v5b-s2-vol-open`（Sync 7e77fb3）。Base backtest.py MD5 `1fc7d9ec05fe9e15f48d81d39be5c600`，433 行。
分支：`variant/v7d-s3-vol`
strategy_core.py：不動（MD5 須維持 `327932c52ff927c220af35ecc60f9ad2`）。

## 唯一變因

僅 S3（起漲股）在收集階段加一道訊號日量能濾網：

- 訊號日 t 收盤時，計算當日成交量 Vol_t 與含當日在內之過去 20 日均量 Vol_MA20 = Mean(Vol_{t-19}, ..., Vol_t) 之比值 VolRatio = Vol_t / Vol_MA20。
- 僅當 VolRatio ≥ 1.35（含 1.35 倍邊界）時保留該筆 S3 訊號；VolRatio < 1.35 時跳過不進場。
- 實作：新增函數 `apply_s3_vol_filter(sigs, df_raw, i)`，照抄既有 `apply_s2_vol_filter` 的寫法與回傳 pattern `(kept, skipped_count)`；只處理 `strategy == "S3_起漲股"`，其他策略原樣保留。
- 掛載點：收集階段 `apply_s2_vol_filter` 呼叫之後、`if not sigs:` 之前；回傳的 skipped 數累加進 `skipped_s3_vol`。
- 邊界：`i < 19`（交易日不足 20 日）視為不滿足，擋下（收集迴圈 start_idx=60，實務上不會觸發，僅作防護）。
- 計數器：`skipped_s3_vol`，初始化為 0，寫入兩個 summary dict，緊貼 `skipped_s2_open` 之後。

其餘全部不動：V5b 的 S2 量能／開盤框、V4 RS 掛載、出場 simulate_exit v2、成本、期間、universe、MAX_CONCURRENT=5、單檔上限 35,000 元；不卡均線扣抵斜率、不設乖離天花板；進場仍走既有 trigger_buy 邏輯。

## 預期機制

S3 勝率僅 50.0% 的核心病灶在於「無量虛晃的假轉強」；改以單一量能門檻 VolRatio ≥ 1.35 確認訊號日具備實質量能點火，剔除無量盤整敗單，同時完整保留高彈性大賺單（不設乖離上限）。

## 採用條件（全滿足才 PASS）

(a) variant 總勝率 > 55%；(b) variant 總 RR ≥ 1.8；(c) variant 總勝率 > 同批 baseline；(d) 前後半段勝率皆 > baseline 對應半段；(e) 總筆數 ≥ 30；(f) 平均報酬 > 0。另加：總勝率 > 59.6% 且 RR ≥ 1.87（現行 Base V5b 門檻）。

## 證偽條件

- S3 獨立勝率 ≤ V5b S3 的 50.0%（或 S3 獨立平均報酬 ≤ +1.58%），或總筆數 < 30。

## 對照

- 同批重跑 `variant/v5-baseline`（plain 版，無 RS 掛載）供 (c)(d) 比較；採用門檻比 V5b 數字（n=715、59.6%、RR 1.87、MDD -7.4%）。
- V5b 各策略基線：S0 68／70.6%／+3.25%、S1 115／53.9%／+1.73%、S2 122／63.1%／+2.23%、S3 50／50.0%／+1.58%、S4 360／59.4%／+2.47%。
