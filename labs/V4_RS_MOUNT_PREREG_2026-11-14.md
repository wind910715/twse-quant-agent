# V4 事前註冊：雙 RS≥80 分策略掛載 A/B 測試

- 日期：2026-11-14
- 狀態：凍結（設計＋程式草稿＋本地驗證已完成；未推分支、未派 GitHub run）
- 驗證順序：第 4 項（V1 停損硬夾 FAIL → V2 S3 體檢＋槽位排序跑分中 → V3 market_regime_ok 動態過濾設計已驗證待派發 → 本次 → 之後有想法繼續開新變體，直到某一整輪全部 FAIL 且無近況可探才停）
- 工作區：`~/workspace/stock-strategy/v4-work/`（自 GitHub main 重新 clone，HEAD f376f32）

## 0. 共用口徑（V4a／V4b 共用，與 V1／V2 相同）

- harness：`backtest.py` v2（與實盤共用 `strategy_core.evaluate_candidate`）
- 期間：2016-01-01 起至最新資料（main 原為 2020-01-01；V1 已凍結 2016 口徑，V4 沿用）
- 成本：買 0.1425%／賣 0.4425%
- universe：`build_dynamic_index_universe()`（上限 220 檔）
- 權益 10 萬、單筆風險 1.2%、MAX_CONCURRENT_POSITIONS = 5
- 有倖存者偏誤，數字僅供相對比較；法人濾網無歷史資料（chip_filter_applied=False）
- **baseline（共用對照）＝V2／V3 判讀後的現行 live 系統＋起算日 2016-01-01**。草稿以現行 main 為基準（僅 1 行起算日差異，見 `diffs/BASELINE_2016.diff`）；派發前須 rebase 到當時的 live 系統（見 §5 揭露）。

## 1. 假設

- H1：RS 濾網在五大策略上已獨立驗證有效（TSM labs，181 檔宇宙，2020 起，進場＝隔日 trigger 限價單，出場＝simulate_exit v2）：R0 無濾網 n=1,262、勝率 53.17%、平均 +1.8608%、RR 2.0427；R1 年 RS≥80：n=871、勝率 61.77%、平均 +2.52%、RR 1.85；R2 季 RS≥80：n=1,057、勝率 59.79%、平均 +2.44%、RR 1.92；R3 年＋季複合≥80：n=782、勝率 62.66%、平均 +2.70%、RR 1.89。A3（複合≥80 去 S3）：n=775、63.10%、平均 +2.74%、RR 1.888；B3（複合≥90）：n=516、63.18%、平均 +2.67%、RR 1.824。**但那是 TSM labs 口徑（181 檔、每日候選池式回測），不是主系統 harness 口徑**——主系統 harness（backtest.py v2、2016–2026、動態 universe 220 檔、MAX 5 槽位、逐檔迴圈）下，RS 掛載的效果尚未驗證，本次即驗此點。
- H2：雙 RS≥80 不可全面前置——會殺死 S1 破底翻這種反轉策略（S1 買的是打下去的股，訊號日常常 RS 不高）。共識（主聊天室 2026-11-14 01:29 定）：改分策略掛載。
- H3：掛載範圍是開放問題。A 測「動能全掛、S1 除外」（最廣的合理掛載）；B 測「只掛最具鑑別力的兩處」（最窄的合理掛載）。兩者對照可回答：掛載是越廣越好，還是 S0／S3 那兩處的掛載反而是拖累。

## 2. RS 計算口徑（凍結，沿用 R 家族，不得自創）

- rs_year（年 RS）＝O'Neil 加權四季報酬（0.4×ret63＋0.2×ret126＋0.2×ret189＋0.2×ret252）的全宇宙截面百分位 ×99（1–99）；rs_quarter（季 RS）＝63 日報酬的截面百分位 ×99；ret{q}＝close／close.shift(q)－1。
- 複合＝(rs_year＋rs_quarter)／2；缺其一取其一；皆缺取 50.0。
- **門檻：複合 ≥ 80 才放行**（與 R3／A3 同定義）。
- RS 以**訊號日收盤**計算，排名為當日截面排名，不含未來資料（無前視，本地驗證 C1 證實）。
- 皆缺（50.0）時複合 50.0＜80，掛載策略的該訊號被擋下（保守原則：無 RS 證據＝不進）。實務上極少發生：harness 要求 len(df_raw)≥110 且訊號日 i≥60，季 RS 只在訊號日前 3 天（i=60–62）缺值。

## 3. A／B 掛載表（凍結全文）

策略名以 `STRATEGY_NAMES` 為準：S0_VCP收斂、S1_破底翻、S2_量增突破、S3_起漲股、S4_強於大盤。「掛」＝該策略訊號須複合 RS≥80 才保留，否則丟棄；「不掛」＝該策略訊號不受 RS 影響，原樣保留。

### 變體 A — 動能全掛，S1 除外

| 策略 | 掛載 |
|---|---|
| S0_VCP收斂 | 掛（複合≥80） |
| S1_破底翻 | 不掛 |
| S2_量增突破 | 掛（複合≥80） |
| S3_起漲股 | 掛（複合≥80） |
| S4_強於大盤 | 掛（複合≥80） |

- 為什麼這樣分：所有動能／突破型策略都加上品質濾網，只豁免唯一的反轉策略 S1。這是最廣的合理掛載，測「RS 濾網對動能家族整體有沒有用」。
- 預期解決的問題：動能策略在弱勢股上的假突破（R1／R2／R3 證據：掛載後勝率 60–63%，高於無濾網 53%）。

### 變體 B — 只掛最具鑑別力的兩處

| 策略 | 掛載 |
|---|---|
| S0_VCP收斂 | 不掛 |
| S1_破底翻 | 不掛 |
| S2_量增突破 | 掛（複合≥80） |
| S3_起漲股 | 不掛 |
| S4_強於大盤 | 掛（複合≥80） |

- 為什麼這樣分（只掛 S2／S4）：
  - S2 量增突破：量增可以發生在弱勢股身上（出貨量、死貓跳），RS≥80 才能分辨「強勢股的量增突破」與「弱勢股的量增」。且 S2 佔 baseline 成交約 75%（V1 baseline 1,367／1,814 筆），掛載效果最能量化。
  - S4 強於大盤：其訊號本身是指數相對強度（個股／加權的 20 日布林突破），與宇宙截面百分位 RS 是不同維度；掛載等於疊加第二層 RS，測「雙重 RS」是否加分。
  - S0 VCP 收斂不掛：VCP 突破來自整理基部，突破當下 RS 往往還沒拉起來——突破本身就是 RS 事件；掛 RS≥80 會殺掉 VCP 設計要抓的基部突破。
  - S3 起漲股不掛：100 日新高本身已隱含高 RS，掛載近乎無作用（絕大多數 S3 訊號本就通過 ≥80），只會修剪最弱的新高；不掛可測掛載在「策略已隱含強度」之處是否多餘。
  - S1 破底翻不掛：反轉策略，同 A。
- 預期解決的問題：只在濾網有鑑別力的地方花成本（少擋好單），避免 S0／S3 的掛載誤殺。

### A／B 對照的判讀意義

- A PASS、B FAIL → 廣掛載有效，S0／S3 的掛載是加分。
- B PASS、A FAIL → S0／S3 的掛載是拖累，窄掛載才是正確範圍。
- 兩者皆 PASS → 選總指標較優者（比較順序：總勝率 → 總 RR → 累計報酬）。
- 兩者皆 FAIL → 分策略掛載此路不通，結論即「RS 掛載在主系統 harness 下無效」，**不得事後拼湊第三種掛載表重測**（要新想法須另開新輪、重新註冊）。

## 4. 落地方式（行級）

- 程式：**只改 `backtest.py`，`strategy_core.py` 一字不動**（evaluate_candidate 不動；live screener 不受影響，見 §6 揭露）。
- RS helpers（`compute_rs_panels`、`rs_composite`）**複製 V2b 已驗證的版本**（V2b `diffs/V2b_slots.diff`，其本地驗證 B1 已證實面板與手算一致、無前視）；其中 `compute_rs_panels` 與 V2b 一字相同，`rs_composite` 除 docstring 一行說明文字（註明掛載時 50.0＜80 會被擋下）外一字相同，執行碼一字相同。V4 本地驗證 C1 重新驗證一次複製無誤。註：V2b 若先被採用進 live，派發時 rebase 改為共用 live 已有的 helpers（機械性重構，不改行為）；若 V2b 未被採用，V4 diff 自帶這兩支 helpers。
- 新增 `apply_rs_mount(sigs, rs_year, rs_quarter, signal_date, symbol)`（約 10 行）＋掛載表 `RS_MOUNT`（dict，策略名→bool）＋門檻常數 `RS_MOUNT_THRESHOLD = 80.0`。A、B 的唯一差異就是 `RS_MOUNT` 的內容。
- 插入點（backtest.py，`run_portfolio_backtest` 內）：
  1. 迴圈前（twii_series 建立後、`trades = []` 之前）：由 `data.xs("Close", level=1, axis=1)` 建收盤價面板 → `compute_rs_panels` 得 `rs_year`、`rs_quarter`。
  2. `evaluate_candidate` 回傳後、`sig = sigs[0]` 之前：`sigs = apply_rs_mount(sigs, rs_year, rs_quarter, dt_idx, t_sym)`；被濾掉的訊號計入診斷計數 `skipped_rs_mount`（不影響交易行為，僅供判讀：掛載擋了多少訊號）；若過濾後為空則 `continue`。
- **不動的部分**：`evaluate_candidate` 訊號評估、`market_regime_ok` 大盤閘門、`simulate_exit` 出場、隔日進場確認規則（開盤 ≤ 訊號價×1.028、開盤 ＞ 停損價、trigger＝max(訊號價, 開盤×1.003)、當日最高 ≥ trigger、收盤 ≥ 開盤×0.998）、sizing、停損、法人閘門、universe、成本、期間、槽位邏輯（MAX 5、逐檔迴圈原樣）。主策略／shadow 策略的區分與統計不變——掛載對 primary 與 shadow 一視同仁（過濾發生在 `sig = sigs[0]` 之前，同一 (股, 日) 的整串訊號一起過濾）。
- Diff 規模：見 `diffs/V4a_rs_a.diff`、`diffs/V4b_rs_b.diff`（A、B 各對同一 baseline；baseline 僅 1 行起算日差異，見 `diffs/BASELINE_2016.diff`）。

## 5. 對照設計與過關條件

- 三組各自對同一 baseline：baseline（V2／V3 判讀後的 live＋2016）、V4a（baseline＋A 掛載表）、V4b（baseline＋B 掛載表）。
- 建議 GitHub run 安排（由主 Muse 決定）：baseline＋V4a＋V4b 三 run（baseline 共用一次即可）。
- **過關條件（沿用 V1 模板，A、B 各自判讀，全數滿足才 PASS）：**
  (a) variant 總勝率 > 55%；(b) variant 總 RR ≥ 1.8；(c) variant 總勝率 > baseline；(d) 前後半段勝率皆 > baseline 對應半段；(e) 總筆數 ≥ 30；(f) 平均報酬 > 0。
- **條件 (d) 的限制（沿用 V1 前例）：** V1 時因 workflow 同步步驟故障（`git pull --rebase` 碰 unstaged changes 失敗 exit 128），`latest_backtest.json` 未寫回分支，前後半段勝率無法判定。同步修法目前暫停（2026-11-14 09:40 使用者拍板）。若 V4 run 仍遇同一故障，(d) 標為「**無法判定**」，其餘 (a)(b)(c)(e)(f) 仍須全滿足；(d) 無法判定時不得標為完整 PASS，須註明「條件性通過，(d) 待同步修復後補跑再定」。
- **A／B 判讀方式：** 各自按 (a)–(f) 判讀；只採用 PASS 的掛載表；兩者皆 PASS 選總指標較優者（勝率 → RR → 累計）；兩者皆 FAIL 即「分策略掛載此路不通」，不得事後拼湊第三種掛載表重測。

## 6. 與 R 家族 A3／B3 的區別（必須寫清）

- R 家族（A3 主池＋B3 金選）是**每日訊號線**的 RS 掛載：181 檔宇宙、每日候選池口徑、訊號隔日 trigger 限價進場、在線上跑實盤訊號。
- 本項是**主系統回測 harness**下的掛載測試：backtest.py v2、2016–2026、動態 universe 220 檔、MAX 5 槽位、逐檔迴圈、原版進出場。不是把 TSM labs 的驗證再跑一次。
- RS 定義相同（R 家族口徑，§2），但掛載表不同：R 家族是「全策略掛複合≥80（A3）／≥90（B3）＋去 S3」；本項是「分策略掛複合≥80，S1 一律不掛」，且在主系統 harness 下驗證。
- **採用路徑不同：** 本項若 PASS，採用的是「掛載表」本身；但落地到 live 還須另一步——把同一掛載表＋RS 計算移植到 live screener 的訊號路徑（screener.py 呼叫 evaluate_candidate 之處），因本次變體只改 backtest.py、live screener 完全不受影響。該移植不在本項變體範圍內，須由主 Muse 另行驗證後執行。

## 7. 揭露限制

- 倖存者偏誤：universe 為現存成分邏輯，數字偏樂觀，僅供相對比較。
- 法人濾網無歷史資料；回測期內 chip_filter_applied=False。
- 若 V2a 判讀為「剔除 S3」，則 live 將無 S3 訊號，掛載表中 S3 那列即無作用（不影響 A／B 判讀的有效性；判讀仍以組合 Δ 為準）。
- 若 V2b 判讀為 PASS 並採用，則 live backtest.py 已含 RS helpers，V4 派發時 rebase 改為共用（§4）；若 V2b 未採用，V4 自帶 helpers。
- V4a／V4b 各自的 Δ 包含「掛載擋單」與「擋單後槽位釋出給其他訊號」兩部分，無法完全分離；判讀以組合 Δ 與 `skipped_rs_mount` 計數共同為準。
- RS 面板以 yf 收盤價計算；yfinance 事後修訂會使數字輕微漂移（V1／RS60 前例），引用須註明口徑與日期。
- 成本、期間、universe 上限 220 檔皆與 V1／V2 相同；結果只在同一口徑下可比。

## 8. 本地驗證結果（假資料，2026-11-14）

- **C1：RS 面板（複製驗證）**（`verify_v4.py`，venv pandas 3.1.0，21 項全通過）：v4a／v4b 的 `compute_rs_panels`、`rs_composite`、`apply_rs_mount` 原始碼一字相同；8 檔合成收盤面板，rs_year／rs_quarter 共 16 值與 plain-python 手算逐值一致（誤差 <1e-6）；改 201 日後收盤價，200 日 rs_quarter 不變（無前視）；200 日 rs_year 為 NaN（252 日歷史不足）。結論：複製自 V2b 的 helpers 無誤。
- **C2：掛載表單元測試**（`verify_v4.py`，A 22 項、B 18 項，共 40 項全通過）：合成 RS 面板（高 RS 股複合 92.5、80.0 邊界、低 RS 股複合 35.0／50.0 皆缺）× 5 策略 × 高／低 RS——
  - A：S0／S2／S3／S4 高 RS 放行、低 RS 擋下（各 1 放行例＋1 擋下例）；S1 高／低皆放行；複合恰 80.0 放行（≥80 含邊界）。
  - B：S2／S4 高 RS 放行、低 RS 擋下；S0／S1／S3 高／低皆放行。
  - 不掛載策略的訊號 list 與輸入一字不差（同一物件內容、順序不變）。
- **C3：端到端迴圈對照**（`verify_v4.py`，猴補 yf／universe／訊號／出場，同一份合成資料，summarize_rows 攔截逐筆，11 項全通過）——
  - 場景（同訊號日 2025-02-25、MAX=20 排除槽位干擾；3 檔固定漂移股，訊號日複合 RS：1001=99.0、1002=33.0、1003=66.0；訊號：1001 五策略全觸、1002 五策略全觸、1003 僅 S2）：
  - baseline 成交：A 股 primary S0、G 股 primary S0、H 股 S2（共 3 筆 primary；shadow 另計）。
  - V4a 成交：A 股 primary S0、G 股 primary S1（S0／S2／S3／S4 被擋，S1 遞補為 primary）、H 股無成交；skipped_rs_mount＝5（G 股 4＋H 股 1）。
  - V4b 成交：A 股 primary S0、G 股 primary S0（S2／S4 被擋，S0 仍為 primary）、H 股無成交；skipped_rs_mount＝3（G 股 2＋H 股 1）。
  - 結論：掛載表在迴圈內確實按表放行／擋下；S1 在低 RS 下仍進場；被擋策略的遞補行為（G 股 V4a 由 S1 遞補）符合設計。

## 9. 檔案清單（全部在 `~/workspace/stock-strategy/v4-work/`）

| 檔案 | 內容 |
|---|---|
| `labs/V4_RS_MOUNT_PREREG_2026-11-14.md` | 本事前註冊（凍結） |
| `code/backtest_baseline.py` | 共用 baseline 草稿（main backtest.py 僅起算日 2016） |
| `code/backtest_v4a.py` | V4a 草稿（baseline＋A 掛載表） |
| `code/backtest_v4b.py` | V4b 草稿（baseline＋B 掛載表） |
| `diffs/V4a_rs_a.diff` | baseline backtest.py → V4a |
| `diffs/V4b_rs_b.diff` | baseline backtest.py → V4b |
| `diffs/BASELINE_2016.diff` | main backtest.py → baseline（1 行：start 2020→2016） |
| `build_v4.py` | 草稿產生器（由 main backtest.py 機械產生 baseline／v4a／v4b 三檔） |
| `verify_v4.py` | 本地驗證腳本（C1＋C2＋C3） |
| `twse-quant-agent/` | 自 GitHub main 重新 clone（HEAD f376f32，乾淨，未動） |

## 10. 建議分支命名

- V4a：`variant/v4-rs-a`
- V4b：`variant/v4-rs-b`
- baseline 共用：`variant/v4-baseline`（＝V2／V3 判讀後的 live＋2016 起算日；若 live 已含 V2b 的 RS helpers，派發前 rebase 共用）
