# twdata — 我們常用的台股公開資料，一個套件搞定

全部免費、免金鑰、免註冊。用法：

```bash
cd labs
python -m twdata.cli fetch daily                              # 上市+上櫃最新收盤
python -m twdata.cli fetch revenue --year 2026 --month 9       # 9 月營收（上市+上櫃，MOPS 歷史）
python -m twdata.cli fetch revenue-latest                      # 最新一個月營收（上市+上櫃，OpenAPI JSON）
python -m twdata.cli fetch margin                              # 融資融券餘額（上市+上櫃）
python -m twdata.cli fetch valuation                           # 本益比／殖利率／股價淨值比（上市）
python -m twdata.cli fetch institutional --date 20261030       # 三大法人（上市，外資+投信，可查歷史）
python -m twdata.cli fetch institutional-tpex                  # 三大法人（上櫃，最新交易日）
```

或在 Python 裡：

```python
from twdata import daily_close, monthly_revenue, margin_balance, valuation, institutional
df = daily_close()                 # 最新交易日，上市+上櫃
rev = monthly_revenue(2026, 9)     # 2026-09 營收（MOPS 歷史）
inst = institutional("20261030")   # 2026-10-30 上市外資+投信買賣超（股數）
```

## 各資料來源與限制（實測驗證，2026-11-07）

| 資料 | 來源 | 歷史查詢 | 備註 |
|---|---|---|---|
| 日價（上市） | TWSE OpenAPI `STOCK_DAY_ALL` | 無，只給最新交易日 | 原始收盤價（非除息調整），1389 檔含 ETF |
| 日價（上櫃） | TPEx OpenAPI `tpex_mainboard_daily_close_quotes` | 無，只給最新交易日 | 13618 列含債券／ETF，日期為民國年 |
| 月營收（歷史） | MOPS `t21sc03`（每月一頁／市場） | 有，2013-01 起 | 與 FinMind 交叉驗證：485 筆重疊全數差異 0.00% |
| 月營收（最新月） | TWSE `t187ap05_L`（上市）＋ TPEx `mopsfin_t187ap05_O`（上櫃） | 無，只給最新一個月 | JSON，比 MOPS HTML 乾淨；2330 與 MOPS／FinMind 完全一致，僅 6443／4739／4426 三檔差 1.8–9.2%（疑似更正） |
| 融資融券（上市） | TWSE OpenAPI `MI_MARGN` | 無，只給最新交易日 | 1306 檔；payload 無日期欄位 |
| 融資融券（上櫃） | TPEx OpenAPI `tpex_mainboard_margin_balance` | 無，只給最新交易日 | 919 檔；欄位為英文 |
| 本益比／殖利率／淨值比 | TWSE OpenAPI `BWIBBU_d` | 無，只給最新交易日 | 注意：`BWIBBU`（無後綴）會回錯誤頁，要用 `BWIBBU_d` |
| 外資／投信（上市） | 證交所網站 `fund/TWT38U`／`TWT44U` | 有，`?date=YYYYMMDD` | 1346 檔；投信只有當日有交易的檔（約 210 檔／日），缺席＝0；策略 D 要用 TWT44U |
| 三大法人（上櫃） | TPEx OpenAPI `tpex_3insti_daily_trading` | 無，只給最新交易日 | 923 檔；外資／投信／自營商買賣超皆為股數 |

## 重要限制

- **證交所與櫃買 OpenAPI 大多只給最新一期快照**，不能指定日期查歷史。回測要用的歷史日價仍走 yfinance，歷史營收走 MOPS，歷史法人（上市）走證交所網站 JSON。上櫃法人歷史目前無公開 JSON 來源（OpenAPI 只有最新日）。
- **TPEx 大檔案回傳不穩**：櫃買的回應常在傳輸中斷（Content-Length 不可靠），urllib 會拿到截斷的 JSON。本套件的 `get_json` 失敗時會自動改用 curl 重抓，實測 `tpex_3insti_daily_trading`（875KB）urllib 連續 4 次失敗、curl 一次成功。
- 證交所／櫃買中心的開放資料為**政府資料開放授權（須顯名標示原資料提供機關）、個人非商業使用**；請求間隔建議 ≥1 秒。ToAlpha 的 MCP 內容同樣限個人非商業使用。
- `fetch_revenue_mops.py` 的解析邏輯與 `twdata/revenue.py` 目前是兩份拷貝（前者寫在先、正在背景跑全量抓取），全量抓完後再合併成一份。

## 參考檔

- `twse_swagger.json`：證交所 OpenAPI 完整規格（143 個端點，2026-11-07 下載）。
- `tpex_swagger.json`：櫃買 OpenAPI 完整規格（225 個端點，2026-11-07 下載）。

## 即時報價 `realtime.py`（2026-11-07 實測）

免費免金鑰，兩支交易所網站內部 API（非官方開放平台，可能改版或限流；
券商 API 為較穩備援）。休市回最後一筆；盤中即時跳動待週一開盤再確認。

- `realtime_stocks(stock_ids)`：個股即時（證交所 MIS，上市／上櫃自動判別，
  現價／昨收／開高低／量／最佳買賣價）。
- `realtime_index()`：加權（t00）＋上櫃（o00）指數。
- `realtime_futures(kind_id="1")`：期交所 MIS getQuoteList（POST），KindID=1
  為台指期（TXF-S 現貨＋TXFK6 近月等），含現價／買賣盤／量／未平倉／結算／
  更新時間。注意：該站偶爾 302 轉址，模組內有 POST-preserving redirect
  handler；另有 3 次重試。
- `realtime_dashboard()`：儀表板三項（加權／上櫃／台指期近月）一次回傳 dict。

CLI：`cd labs && python -m twdata.cli fetch realtime`
