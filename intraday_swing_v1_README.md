# 日內進場＋收盤濾網系統 v1-D（實驗分支，勿合併到 main）

- 凍結 SPEC：`~/workspace/your_files/日內進場波段留倉系統_SPEC_v1.md`（v1，2026-10-28）。
- 引擎：`intraday_swing_v1.py`（Yahoo 日線代理版；宇宙 8 檔；2020-01-01～2026-10-28；EMA 方向＋VCP 收斂＋量增突破＋MACD 確認；訊號隔日開盤進場；收盤濾網；ATR 硬停損＋EMA20 趨勢出場；成本 0.6%；另跑無濾網對照組）。
- 工作流程：`.github/workflows/intraday-swing-v1.yml`（push 觸發，僅限本實驗分支；結果 commit 回本分支 `results/intraday_swing_v1/`）。
- 本分支只新增檔案，不動 main、不動既有 workflow／監控／bot。
- 代理版限制：日線代理只能證明收盤濾網概念與組合邏輯，不能等同盤中進場績效；完整盤中版為 v2，須另寫 SPEC。
- 已知資料問題（見 results.json meta.errors）：Yahoo 無 6488.TW／8299.TW（該兩檔在 Yahoo 為 .TWO 後綴）；本版按 SPEC 字面規則（代號加 .TW）跳過，待 SPEC 修訂後再決定是否補 .TWO 後援。
