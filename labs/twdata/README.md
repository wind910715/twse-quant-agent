# twdata — 我們常用的台股公開資料，一個套件搞定

全部免費、免金鑰、免註冊。用法：

```bash
cd labs
python -m twdata.cli fetch daily                              # 上市+上櫃最新收盤
python -m twdata.cli fetch revenue --year 2026 --month 9       # 9 月營收（上市+上櫃）
python -m twdata.cli fetch margin                              # 融資融券餘額（上市）
python -m twdata.cli fetch valuation                           # 本益比／殖利率／股價淨值比（上市）
python -m twdata.cli fetch institutional --date 20261030       # 三大法人（外資+投信）
```

或在 Python 裡：

```python
from twdata import daily_close, monthly_revenue, margin_balance, valuation, institutional
df = daily_close()                 # 最新交易日，上市+上櫃
rev = monthly_revenue(2026, 9)     # 2026-09 營收
inst = institutional("20261030")   # 2026-10-30 外資+投信買賣超（股數）
```

## 各資料來源與限制（實測驗證，2026-11-07）

| 資料 | 來源 | 歷史查詢 | 備註 |
|---|---|---|---|
| 日價（上市） | TWSE OpenAPI `STOCK_DAY_ALL` | 無，只給最新交易日 | 原始收盤價（非除息調整），1389 檔含 ETF |
| 日價（上櫃） | TPEx OpenAPI `tpex_mainboard_daily_close_quotes` | 無，只給最新交易日 | 13618 列含債券／ETF，日期為民國年 |
| 月營收 | MOPS `t21sc03`（每月一頁／市場） | 有，2013-01 起 | 與 FinMind 交叉驗證：485 筆重疊全數差異 0.00% |
| 融資融券 | TWSE OpenAPI `MI_MARGN` | 無，只給最新交易日 | 1306 檔；payload 無日期欄位 |
| 本益比／殖利率／淨值比 | TWSE OpenAPI `BWIBBU_d` | 無，只給最新交易日 | 注意：`BWIBBU`（無後綴）會回錯誤頁，要用 `BWIBBU_d` |
| 外資買賣超 | 證交所網站 `fund/TWT38U` | 有，`?date=YYYYMMDD` | 1346 檔；第 6-8 欄恆為 0，第 9-11 欄重複第 1 組，皆忽略 |
| 投信買賣超 | 證交所網站 `fund/TWT44U` | 有，`?date=YYYYMMDD` | 只有投信有交易的檔（約 210 檔／日），缺席＝0；策略 D 要用這個 |
| 自營商買賣超 | 證交所網站 `fund/TWT43U` | 有 | 每券商每檔一列（約 18k 列），未包進套件 |

## 重要限制

- **證交所 OpenAPI 大多只給最新一期快照**，不能指定日期查歷史。回測要用的歷史日價仍走 yfinance（已驗證），歷史營收走 MOPS，歷史法人走證交所網站 JSON。
- **上櫃的融資融券／三大法人端點尚未確認**：TPEx swagger.json 從這台 VM 下載會中斷（IncompleteRead），端點名稱待從 https://www.tpex.org.tw/openapi/ 再查。目前上櫃只有日價。
- 證交所／櫃買中心的開放資料為**非商業使用**授權；請求間隔建議 ≥1 秒。
- `fetch_revenue_mops.py` 的解析邏輯與 `twdata/revenue.py` 目前是兩份拷貝（前者寫在先、正在背景跑全量抓取），全量抓完後再合併成一份。

## 參考檔

- `twse_swagger.json`：證交所 OpenAPI 完整規格（143 個端點，2026-11-07 下載）。
- `tpex_swagger.json`：待補（下載失敗，見上）。
