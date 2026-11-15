# V6c 預註冊（凍結）— 同進場日候選訊號按策略優先序分配槽位

凍結日期：2026-11-16
來源：Gemini 第三輪提案（[TO-MUSE] id `1a1df7edf63c46c2`）＋第四輪補完規格（[TO-MUSE] id `1a1e16ac7843fce3`），經 Muse 驗證（`GEMINI_V7_REVIEW_VERIFICATION_2026-11-16.md`）可測。
Base：`variant/v5b-s2-vol-open`（Sync 7e77fb3）。Base backtest.py MD5 `1fc7d9ec05fe9e15f48d81d39be5c600`，433 行。
分支：`variant/v6c-strategy-priority`
strategy_core.py：不動（MD5 須維持 `327932c52ff927c220af35ecc60f9ad2`）。

## 唯一變因

僅改交易分配迴圈對所有候選進場訊號的排序鍵（屬 harness 改動，限分配迴圈）：

- 原排序鍵：`(entry_date 升冪, u_idx 升冪)`——外層 `for entry_date in sorted(cands_by_entry.keys())` 不變，同日內 `day_cands = sorted(cands_by_entry[entry_date], key=lambda c: c["u_idx"])`。
- 新排序鍵：`(entry_date 升冪, strategy_priority 升冪, u_idx 升冪)`——同日內改為 `sorted(cands_by_entry[entry_date], key=lambda c: (STRATEGY_PRIORITY[c["sig"]["strategy"]], c["u_idx"]))`。
- `STRATEGY_PRIORITY` 依 V5b 基線勝率與期望值梯隊事前固定定義，數值愈小優先權愈高：
  ```
  STRATEGY_PRIORITY = {
      "S0_VCP收斂": 0,
      "S2_量增突破": 1,
      "S4_強於大盤": 2,
      "S1_破底翻": 3,
      "S3_起漲股": 4,
  }
  ```
  （V5b 各策略勝率：S0 70.6%、S2 63.1%、S4 59.4%、S1 53.9%、S3 50.0%。）
- 同一進場日且同一策略內部仍維持按 u_idx 升冪排序；MAX_CONCURRENT=5 與單檔上限 35,000 元邊界規則完全不變。
- 無新增診斷計數器：此為重排序而非濾網，沒有被擋下的訊號可計數；效果以兩 run 的交易集合差異評估。

其餘全部不動：V5b 的 S2 量能／開盤框、V4 RS 掛載、收集階段、出場 simulate_exit v2、成本、期間、universe、單檔上限、stock_busy 與 portfolio_full 檢查、影子迴圈。

## 預期機制

避免低勝率 S1（53.9%／+1.73%）與 S3（50.0%／+1.58%）僅因 u_idx 靠前就搶走滿槽日原本屬於高勝率 S0（70.6%）、S2（63.1%）或 S4（59.4%）的槽位；槽位給期望值更高的策略，組合勝率與 RR 應同步上升。

## 採用條件（全滿足才 PASS）

(a) variant 總勝率 > 55%；(b) variant 總 RR ≥ 1.8；(c) variant 總勝率 > 同批 baseline；(d) 前後半段勝率皆 > baseline 對應半段；(e) 總筆數 ≥ 30；(f) 平均報酬 > 0。另加：總勝率 > 59.6% 且 RR ≥ 1.87（現行 Base V5b 門檻）。

## 證偽條件

- 採用門檻未過；或因排序改變而新遞補進場之交易平均報酬低於被排擠替換之原交易平均報酬（以 variant run 與同批 baseline run 的交易集合差異評估：新進集合 vs 被排擠集合的平均 net_return_pct 比較）。
- 註：Gemini 第三輪原文稱「S1 優先序搶槽位」前提錯誤——實情為分配按 u_idx 排序、無策略優先序可重排；本變體即為補完後的正確形式（改寫分配排序鍵），屬 harness 改動，限分配迴圈。

## 對照

- 同批重跑 `variant/v5-baseline`（plain 版，無 RS 掛載）供 (c)(d) 比較；採用門檻比 V5b 數字（n=715、59.6%、RR 1.87、MDD -7.4%）。
