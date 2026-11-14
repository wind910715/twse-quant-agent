# V2 事前註冊：S3 主系統口徑體檢（V2a）＋槽位排序（V2b）

- 日期：2026-11-14
- 狀態：凍結（設計＋程式草稿＋本地驗證已完成；未推分支、未派 GitHub run）
- 驗證順序：第 2 項（V1 停損硬夾已完 FAIL → 本次 → 第 3 項強化 market_regime_ok → 第 4 項雙 RS≥80 分策略掛載 A/B）
- 工作區：`~/workspace/stock-strategy/v2-work/`（自 GitHub main 重新 clone，HEAD f376f32）

## 0. 共用口徑（V2a／V2b 共用，與 V1 相同）

- harness：`backtest.py` v2（與實盤共用 `strategy_core.evaluate_candidate`）
- 期間：2016-01-01 起至最新資料（main 原為 2020-01-01；V1 已凍結 2016 口徑，V2 沿用）
- 成本：買 0.1425%／賣 0.4425%
- universe：`build_dynamic_index_universe()`（上限 220 檔）
- 權益 10 萬、單筆風險 1.2%、MAX_CONCURRENT_POSITIONS = 5
- 有倖存者偏誤，數字僅供相對比較；法人濾網無歷史資料（chip_filter_applied=False）
- **baseline（共用對照）＝現行 live 程式原樣（＝V1 baseline）＋起算日 2016-01-01**，僅 1 行差異（見 `diffs/BASELINE_2016.diff`）

## 1. 假設

- H1（V2a）：S3 在主系統口徑的**組合邊際貢獻未知**。V1 log 驗得 S3 單獨數字：baseline S3 勝率 35.6%（59 筆，單筆平均 +0.66%）；stopfix S3 勝率 41.7%（60 筆，單筆平均 +1.30%）。S3 單獨勝率低於整體（51.7%），但不能直接斷定拖累——S3 佔用槽位與否、與其他策略的時間分佈都會影響組合。必須以「含 S3 vs 剔 S3 兩個投資組合」的總指標比較為準。
- H2（V2b）：槽位競爭嚴重，分配順序可能影響組合。舊 run 曾有 skipped_portfolio_full 1,809 vs 成交 1,168 筆，約六成訊號因槽位滿被擋。R 家族驗證顯示 RS 高者勝率較高（R1 年 RS≥80 勝率 61.77%、R2 季 RS≥80 勝率 59.79%、R3 年＋季複合勝率 62.66%，皆高於整體 51.5%），但那是 R 家族口徑下的 RS **濾網**，不是槽位排序——把 RS 用在「同日多訊號搶槽」的分配上，效果未驗證，本次即驗此點。
- H3（V2b 附帶發現）：現行逐檔外圈迴圈有日曆併發漏洞——較晚進場日的交易在較早進場日的 overlapping 判定中不被計入，組合可能超過 MAX_CONCURRENT=5 同時持有。本地驗證 B3 場景 2 已證實（MAX=1 時 baseline 仍同時持有 2 檔）。逐日化同時修正此漏洞（見 §5 揭露）。

## 2. V2a「剔除 S3」——唯一變因（行級）

- **唯一變因：S3 訊號不再發出。**
- 程式：`strategy_core.py` 第 417–437 行 S3 區塊（100 日新高＋間隔 >100 根）整段移除，改以 4 行註解標示（見 `diffs/V2a_nos3.diff`，+4/−21 行；草稿檔 `code/strategy_core_v2a.py`）。
- 同日同股若僅有 S3 訊號，`evaluate_candidate` 回傳 `[]`。
- **不動的部分**：S0／S1／S2／S4、停損（含 entry×0.975 硬夾，V1 變體未採用）、sizing、出場（`simulate_exit` v2 三階停利＋硬停損＋3 日汰弱＋55 天）、大盤閘門、法人閘門、universe、成本、期間、槽位邏輯。`backtest.py` 不動（除共用 baseline 的 1 行起算日）。

## 3. V2b「槽位排序」——唯一變因（行級）

### 3.1 現行槽位填法（已讀 `backtest.py`＋`strategy_core.py` 確認）

- 無顯式排序，實效為 **universe 插入順序先到先得**（`build_dynamic_index_universe()`：FALLBACK_UNIVERSE → 上市成交金額前 150 降冪 → 上櫃前 120 降冪，截前 220 檔；快取命中時為快取順序）。
- 迴圈為逐檔外圈、逐日內圈；槽位檢查在進場確認後，以 `entry_date` 的 overlapping 持倉數判定（`len(overlapping) >= MAX_CONCURRENT`），`skipped_portfolio_full` 計數。
- 同股多策略：`matched[0]` 為主策略進場，其餘策略僅 shadow 統計（不佔槽位）。
- 另有日曆併發漏洞（見 H3）：逐檔外圈使較晚進場日的交易可不計入較早進場日的 overlapping 判定。

### 3.2 凍結排序規則（全文，只用訊號當下已知的資料，不得前視）

> 同一進場日多候選同時觸發、槽位不足時，按以下順序分配，先到先得：
> 1. **年＋季 RS 複合高→低**。複合＝(rs_year ＋ rs_quarter)／2；缺其一取其一；皆缺取 50.0 中性。
> 2. **同分 → 策略優先序 S0 > S1 > S2 > S3 > S4。**
> 3. **再同分 → 代號升冪。**
> - rs_year（年 RS）＝O'Neil 加權四季報酬（0.4×ret63＋0.2×ret126＋0.2×ret189＋0.2×ret252）的全宇宙截面百分位 ×99（1–99）；rs_quarter（季 RS）＝63 日報酬的截面百分位 ×99；ret{q}＝close／close.shift(q)－1。與已驗證的 R1／R2／R3 同口徑（TSM labs `build_indicators`）。
> - RS 以**訊號日收盤**計算，排名為當日截面排名，不含未來資料（無前視，本地驗證 B1 證實）。
> - `RANK_MODE = "rs"` 為預設；設為 `"universe"` 時回到原 universe 順序，僅本地驗證對照用，不上 GitHub。

### 3.3 落地方式（行級）

- 程式：`backtest.py` 新增 helpers（`compute_rs_panels`、`rs_composite`、`rank_candidates`、`STRATEGY_PRIORITY`、`RANK_MODE`，約 32 行）＋ `run_portfolio_backtest` 迴圈改為**逐日外圈**（按進場日升序；收集階段只做訊號評估＋隔日進場確認，分配階段排序後依序做忙碌／槽位判定與出場模擬）。見 `diffs/V2b_slots.diff`（+111/−18 行，含註解與空行；草稿檔 `code/backtest_v2b.py`；對照檔 `code/backtest_baseline.py`）。
- **不動的部分**：`evaluate_candidate` 呼叫方式與訊號評估、`simulate_exit` 出場、隔日進場確認規則（開盤 ≤ 訊號價×1.028、開盤 ＞ 停損價、trigger＝max(訊號價, 開盤×1.003)、當日最高 ≥ trigger、收盤 ≥ 開盤×0.998）、sizing、停損、大盤閘門、法人閘門、universe、成本、期間。主策略／shadow 策略的區分與統計不變。
- **綑綁揭露**：此變體綑綁 (i) 時序化處理（機械性重構）與 (ii) RS 排序——現行逐檔迴圈無法表達同日排序，RS 排序只能落在逐日迴圈上；且逐日化同時修正 H3 的日曆併發漏洞。判讀見 §4.2。

## 4. 對照設計與過關條件

### 4.1 對照

- 三組各自對同一 baseline：baseline（main＋2016）、V2a（baseline − S3）、V2b（baseline ＋逐日迴圈＋RS 排序，RANK_MODE="rs"）。
- 建議 GitHub run 安排（由主 Muse 決定）：baseline＋V2a 一組雙 run；baseline＋V2b 一組雙 run。`v2b-universe` 模式僅本地驗證對照，不上 GitHub。

### 4.2 過關條件

**V2b（沿用 V1 模板，全數滿足才 PASS）：**
(a) variant 總勝率 > 55%；(b) variant 總 RR ≥ 1.8；(c) variant 總勝率 > baseline；(d) 前後半段勝率皆 > baseline 對應半段；(e) 總筆數 ≥ 30；(f) 平均報酬 > 0。
- 判讀附帶：若 V2b-rs 與 baseline 差異顯著，其中一部分可能來自迴圈重構本身（併發漏洞修正），而非 RS 排序；本地 B3 已證實重構改變分配行為的機制，但真實資料上的量級未知。若 V2b-rs 未 PASS 且需歸因，須另跑 `v2b-universe` 模式（僅重構）作分離對照，再下結論；不得將重構效應直接歸給 RS 排序。

**V2a（體檢，V1 模板不直接適用，改用雙向判讀）：**
- V1 模板是為「變體應優於 baseline」設計的；V2a 的目的是**量 S3 的邊際貢獻**，不是預設剔除就該贏，因此改用以下判讀：
  - (i) 若剔 S3 版全滿足 (a)–(f) → **PASS（剔除 S3）**：S3 為淨拖累。
  - (ii) 若剔 S3 版任一核心指標（總勝率、總 RR、累計報酬）低於 baseline → **保留 S3**：S3 邊際貢獻為正。體檢結論明確，此結果不是 variant FAIL，而是「S3 有用」的測量結果。
  - (iii) 若介於 (i)(ii) 之間（部分滿足、Δ 接近零）→ 視 Δ 大小與前後半段一致性由主 Muse 判讀，**預設保留**（不動現行程式）。
- 關鍵判讀量：Δ（variant − baseline）的總勝率／總 RR／累計／MDD／筆數／前後半段勝率。**不以 S3 單獨勝率（35.6%）為判據**，只以組合 Δ 為準。

**條件 (d) 的限制（沿用 V1 前例）：**
- V1 時因 workflow 同步步驟故障（`git pull --rebase` 碰 unstaged changes 失敗 exit 128），`latest_backtest.json` 未寫回分支，前後半段勝率無法判定。同步修法目前暫停（2026-11-14 09:40 使用者拍板）。
- 若 V2 run 仍遇同一故障，(d) 標為「**無法判定**」，其餘 (a)(b)(c)(e)(f) 仍須全滿足；(d) 無法判定時不得標為完整 PASS，須註明「條件性通過，(d) 待同步修復後補跑再定」。

## 5. 揭露限制

- 倖存者偏誤：universe 為現存成分邏輯，數字偏樂觀，僅供相對比較。
- 法人濾網無歷史資料；回測期內 chip_filter_applied=False。
- V2a 剔 S3 同時釋出槽位給其他策略：Δ 包含「S3 消失」與「其他策略多進」兩部分，無法完全分離；判讀以組合 Δ 為準。
- V2b 綑綁重構＋排序（見 §3.3）；重構修正的日曆併發漏洞在真實資料上的量級未量測。
- V2b 的 RS 排序只影響「同日多訊號搶槽」的分配；非競爭日（訊號數 ≤ 空槽）的交易完全不受影響。
- 成本、期間、universe 上限 220 檔皆與 V1 相同；結果只在同一口徑下可比。

## 6. 本地驗證結果（假資料，2026-11-14）

- **V2a**（`verify_v2a.py`，venv pandas 3.1.0）：合成 150 根 OHLCV，最後一根跳空創新高。
  - 案例 1（爆量，S2＋S3 同觸）：baseline 回傳 [S2, S3]，V2a 回傳 [S2]。
  - 案例 2（量不足，僅 S3 觸發）：baseline 回傳 [S3]，V2a 回傳 []。
  - 結論：S3 確實停發，其他策略逐項一致。2/2 通過。
- **V2b**（`verify_v2b.py`，30 項檢查全通過）：
  - B1：RS 面板——8 檔合成收盤面板，rs_year／rs_quarter 共 16 值與 plain-python 手算逐值一致（誤差 <1e-6）；改 201 日後收盤價，200 日 rs_quarter 不變（無前視）；200 日 rs_year 為 NaN（252 日歷史不足）。
  - B2：排序——6 候選複合 92.5/82.5/75/60/35/15 按高→低排序，5 槽取前 5、擠掉最低（1006）；平手決勝 S0 優先、再按代號升冪；universe 模式按 u_idx；缺值 fallback（缺年取季、缺季取年、皆缺取 50.0）。
  - B3：端到端迴圈對照（猴補 yf／universe／訊號／出場，同一份合成資料，summarize_rows 攔截逐筆）——
    - 場景 1（同日搶槽，6 檔同進場日、5 槽、universe 順序反向）：baseline 成交 [1002..1006]（擠掉 1001）；v2b-rs 成交 [1001..1005]（擠掉 1006）；v2b-universe 與 baseline 成交集合一致（機械性重構對照通過）。
    - 場景 2（跨日搶槽，MAX=1；1001 早序晚進場、1006 晚序早進場，窗口重疊）：baseline 同時持有 2 檔（MAX=1 仍雙進，證實日曆併發漏洞）；v2b 只進早進場日那檔 [1006]（MAX=1 確實守住）。

## 7. 檔案清單（全部在 `~/workspace/stock-strategy/v2-work/`）

| 檔案 | 內容 |
|---|---|
| `labs/V2_SLOTS_PREREG_2026-11-14.md` | 本事前註冊（凍結） |
| `code/strategy_core_v2a.py` | V2a 草稿（main strategy_core.py 僅移除 S3 區塊） |
| `code/backtest_baseline.py` | 共用 baseline 草稿（main backtest.py 僅起算日 2016） |
| `code/backtest_v2b.py` | V2b 草稿（baseline＋逐日迴圈＋RS 排序，RANK_MODE="rs"） |
| `diffs/V2a_nos3.diff` | main strategy_core.py → V2a（+4/−21 行） |
| `diffs/BASELINE_2016.diff` | main backtest.py → baseline（1 行：start 2020→2016） |
| `diffs/V2b_slots.diff` | baseline backtest.py → V2b（+111/−18 行） |
| `build_v2b.py` | V2b 草稿產生器（由 main backtest.py 機械產生 baseline／v2b 兩檔） |
| `verify_v2a.py` | V2a 本地驗證腳本 |
| `verify_v2b.py` | V2b 本地驗證腳本（B1＋B2＋B3） |
| `twse-quant-agent/` | 自 GitHub main 重新 clone（HEAD f376f32，乾淨，未動） |

## 8. 建議分支命名

- V2a：`variant/v2-nos3`
- V2b：`variant/v2-slots`
- baseline 共用：`variant/v2-baseline`（＝V1 baseline 口徑＋2016 起算日；若 V1 baseline 分支已含 2016 口徑，可直接沿用該分支作對照）
