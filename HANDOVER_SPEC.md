# 台股全市場技術分析選股與看盤系統｜交接規格書（v5.1 優化版）

* **更新日期**：2026-11-25 21:10 (UTC+8)
* **系統版本**：v5.1（修正 v5 實跑健檢 4 項 Bug ＋ 3 項實戰優化）
* **原始 Pine Script 依據**：`S01-S02-S03-S04-S06.pine`（Drive ID: `1WA1bk44-ZZD2s4kT2NCKEKl-IU5OB12A`）

---

## 一、v5.1 修正與優化摘要

### 已修正 Bug（4 項）
1. **匯出 CSV 16 欄對齊修復**（`tw_full_market_sync.py`）：補齊 `export_outputs()` 寫入三大法人 4 欄（`inst_foreign_lots`、`inst_trust_lots`、`inst_dealer_lots`、`inst_total_lots`），已驗證 3,270 筆日 K 皆為 16 欄零錯位。
2. **本機伺服器網址 404 修復**（`tw_full_market_sync.py`）：`--serve` 開啟路徑修正為 `http://127.0.0.1:8787/index.html`。
3. **GitHub Actions 排程調整**（`.github/workflows/daily-sync.yml`）：由台灣時間 `14:40` 延後至 `15:40`（`cron: '40 7 * * 1-5'`），避開 15:00–15:30 三大法人籌碼公布空窗；並新增首次執行自動以 `--seed-csv` 灌入 43 檔基線歷史。
4. **`--backfill` 支援上櫃標的**（`tw_full_market_sync.py`）：新增 `backfill_tpex_stock()` 與 `backfill_stock_auto()`，自動依市場別回補上市（TWSE）或上櫃（TPEx，如 `3491`）。

### 已完成優化（3 項）
1. **台股跳動檔位（Tick Size）1-to-1 對齊**（`screener_engine.py` 與 `index.html` 同步）：`SL` 向下取檔、`TP` 向上取檔，貼合實戰掛單價位並依對齊後點位重算 `R:R`。
2. **引擎邊界防呆與法人欄位擴充**（`screener_engine.py`）：加入序列長度檢查、大盤指數缺值／零值前值填補（避免除零錯誤），並支援選用 `inst_total_lots` 標註籌碼輔助。
3. **基線 CSV 種子匯入**（`tw_full_market_sync.py --seed-csv`）：首次建庫可直接匯入 43 檔歷史日 K（3,270 筆）與 77 日加權指數，避免剛部署時歷史天數不足無法算 RS 均線。

---

## 二、交接檔案清冊

| 檔案路徑 | 用途 | 驗證狀態 |
| :--- | :--- | :--- |
| `index.html` | 單一檔前端看盤與全市場選股系統（v5.1 同步台股跳動檔位對齊） | `node --check` 通過 |
| `tw_full_market_sync.py` | 零第三方相依 Python 3 同步腳本（TWSE/TPEx 日行情＋三大法人＋`--seed-csv`＋上市/上櫃 `--backfill`＋`--serve`） | `py_compile` 與 `--seed-csv --skip-sync` 實測通過；線上官方 API 屬 `NOT TESTED` |
| `screener_engine.py` | 與前端邏輯 1-to-1 對應的獨立 Python 量化評分模組（v5.1） | `py_compile`、單元測試與 43 檔全庫掃描通過 |
| `.github/workflows/daily-sync.yml` | GitHub Actions 排程（週一至週五台灣時間 15:40 自動同步） | YAML 結構檢查通過；GitHub Actions 實機屬 `NOT TESTED` |
| `tw_stocks_database_43_20261125.csv` |  內建 43 檔真實台股基線資料庫（42 上市＋1 上櫃 3491，3,354 筆日 K） |  欄位與筆數已驗證（已回補 4 個月真實量價，零成交量 0 筆） |

---

## 三、待實機驗證項目（`NOT TESTED`）

* `[NOT TESTED]` **TWSE / TPEx 官方 API 實機連線與欄位解析**（含新增的 `backfill_tpex_stock` TPEx 個股月成交端點）。
* `[NOT TESTED]` **GitHub Actions 15:40 排程自動推檔**（請確認 `Settings → Actions → General` 已開啟 `Read and write permissions`）。
* `[NOT TESTED]` **手機瀏覽器（iOS Safari / Android Chrome）實機觸控與 PWA 版面**。

---

## 四、快速啟動指令

```bash
# 首次建庫（匯入內建 43 檔基線＋抓取今日全市場）並啟動本機看盤
python3 tw_full_market_sync.py --seed-csv tw_stocks_database_43_20261125.csv --serve
# 瀏覽器開啟 http://127.0.0.1:8787/index.html

# 針對核心 6 檔（含上櫃 3491）回補近 3 個月真實量價日 K
python3 tw_full_market_sync.py --backfill 2330,2454,3661,3711,3491,3305 --months 3
```
