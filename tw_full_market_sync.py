#!/usr/bin/env python3
"""
台股全市場（上市 TWSE + 上櫃 TPEx）每日行情同步與本地看盤伺服器 v5.1
====================================================================
- 零第三方套件相依（僅使用 Python 3 內建標準函式庫）
- 自動排除 ETF (00xx)、ETN (02xx)、權證、特別股與興櫃，嚴格保留 4 碼上市/上櫃普通股
- 支援增量更新本地 SQLite 資料庫 (tw_full_market.db)，每日盤後執行約 3~5 秒完成
- 支援 --seed-csv 將基線 CSV 歷史日 K 匯入 SQLite，首次啟動即具完整 RS 均線
- 支援 --backfill 同時回補上市 (TWSE) 與上櫃 (TPEx) 歷史日 K（內建抗限流延遲）
- 匯出 tw_full_market_db.json（供網頁看盤選股系統自動載入）與 16 欄對齊之 tw_full_market_db.csv
- 支援 --serve 啟動本機伺服器 (http://127.0.0.1:8787/index.html)

使用方式：
  1. 首次建置（先灌入內建 43 檔基線，再同步今日全市場）：
     python3 tw_full_market_sync.py --seed-csv tw_stocks_database_43_20261124.csv

  2. 每日更新全市場收盤行情並啟動本機看盤系統：
     python3 tw_full_market_sync.py --serve

  3. 針對指定標的（含上市與上櫃）安全回補近 N 個月歷史日 K：
     python3 tw_full_market_sync.py --backfill 2330,2454,3661,3711,3491,3305 --months 3
"""

import argparse
import csv
import datetime
import functools
import http.server
import json
import os
import random
import re
import socketserver
import sqlite3
import ssl
import time
import urllib.error
import urllib.request

DB_PATH = "tw_full_market.db"
OUT_JSON = "tw_full_market_db.json"
OUT_CSV = "tw_full_market_db.csv"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

# 嚴格比對台灣上市/上櫃一般普通股（4 碼數字、首位 1-9，排除 00xx ETF、02xx ETN、權證、特別股）
ORDINARY_STOCK_RE = re.compile(r"^[1-9]\d{3}$")


def parse_float(val):
    if val is None:
        return None
    s = str(val).strip().replace(",", "").replace("+", "")
    if not s or s in ("--", "-", "N/A", "X0.00", "除權", "除息"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_int(val):
    f = parse_float(val)
    return int(round(f)) if f is not None else 0


def roc_to_iso(roc_str):
    """將民國日期 (115/11/24 或 1151124) 轉為 YYYY-MM-DD"""
    s = str(roc_str).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    m = re.match(r"^(\d{2,3})[/\-](\d{1,2})[/\-](\d{1,2})$", s)
    if m:
        y = int(m.group(1)) + 1911
        return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m2 = re.match(r"^(\d{3})(\d{2})(\d{2})$", s)
    if m2:
        y = int(m2.group(1)) + 1911
        return f"{y:04d}-{int(m2.group(2)):02d}-{int(m2.group(3)):02d}"
    return datetime.date.today().isoformat()


def fetch_json(url, timeout=20, retries=2):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
        except Exception as exc:
            if attempt < retries:
                wait = 3.0 + random.uniform(1.0, 2.5)
                print(f"  [重試 {attempt+1}/{retries}] {url} 失敗 ({exc})，等待 {wait:.1f}s...")
                time.sleep(wait)
            else:
                raise


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS stocks (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            market TEXT NOT NULL,
            sector TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS daily_bars (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume INTEGER NOT NULL,
            PRIMARY KEY (code, date)
        );
        CREATE TABLE IF NOT EXISTS taiex_bars (
            date TEXT PRIMARY KEY,
            close REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inst_flows (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            foreign_net INTEGER DEFAULT 0,
            trust_net INTEGER DEFAULT 0,
            dealer_net INTEGER DEFAULT 0,
            total_net INTEGER DEFAULT 0,
            PRIMARY KEY (code, date)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_code_date ON daily_bars(code, date);
        CREATE INDEX IF NOT EXISTS idx_inst_code_date ON inst_flows(code, date);
        """
    )
    conn.commit()


def seed_from_csv(conn, csv_path):
    """將基線 CSV（如 tw_stocks_database_43_20261124.csv）匯入 SQLite，不覆蓋已存在之較完整紀錄"""
    if not os.path.exists(csv_path):
        print(f"[警告] 找不到基線 CSV：{csv_path}，略過匯入。")
        return 0
    inserted_bars = 0
    inserted_taiex = 0
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            code = str(r.get("code", "")).strip()
            if not ORDINARY_STOCK_RE.match(code):
                continue
            name = str(r.get("name", "")).strip() or code
            market = str(r.get("market", "上市")).strip() or "上市"
            sector = str(r.get("sector", "")).strip()
            dt = str(r.get("date", "")).strip()
            c = parse_float(r.get("close"))
            if not dt or c is None or c <= 0:
                continue
            o = parse_float(r.get("open")) or c
            h = parse_float(r.get("high")) or max(o, c)
            l = parse_float(r.get("low")) or min(o, c)
            vol = parse_int(r.get("volume_shares"))
            conn.execute(
                "INSERT OR IGNORE INTO stocks(code, name, market, sector) VALUES (?, ?, ?, ?)",
                (code, name, market, sector),
            )
            cur = conn.execute(
                "INSERT OR IGNORE INTO daily_bars(code, date, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (code, dt, o, h, l, c, vol),
            )
            if cur.rowcount > 0:
                inserted_bars += 1
            tx = parse_float(r.get("taiex_close"))
            if tx and tx > 0:
                tcur = conn.execute(
                    "INSERT OR IGNORE INTO taiex_bars(date, close) VALUES (?, ?)",
                    (dt, tx),
                )
                if tcur.rowcount > 0:
                    inserted_taiex += 1
    conn.commit()
    print(f"[基線匯入] 自 {csv_path} 匯入 {inserted_bars} 筆日 K、{inserted_taiex} 筆加權指數。")
    return inserted_bars


def sync_taiex(conn):
    """抓取加權指數當月與前兩個月收盤，供 S02 RS 計算基準"""
    today = datetime.date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(3):
        months.append(f"{y:04d}{m:02d}01")
        m -= 1
        if m == 0:
            m = 12
            y -= 1

    inserted = 0
    for ymd in reversed(months):
        url = f"https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?response=json&date={ymd}"
        try:
            data = fetch_json(url)
            if data.get("stat") != "OK":
                continue
            for row in data.get("data", []):
                dt = roc_to_iso(row[0])
                close = parse_float(row[4])
                if close:
                    conn.execute(
                        "INSERT OR REPLACE INTO taiex_bars(date, close) VALUES (?, ?)",
                        (dt, close),
                    )
                    inserted += 1
            time.sleep(random.uniform(1.2, 2.0))
        except Exception as exc:
            print(f"[警告] 加權指數 {ymd} 抓取失敗: {exc}")
    conn.commit()
    print(f"[TAIEX] 已同步 {inserted} 筆加權指數日收盤。")


def sync_twse_daily(conn):
    """抓取 TWSE 上市全市場當日收盤行情（單次請求取得全部上市個股）"""
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json"
    print("[TWSE] 正在抓取上市全市場日行情...")
    data = fetch_json(url)
    raw_date = data.get("date", "")
    if re.match(r"^\d{8}$", raw_date):
        trade_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    else:
        trade_date = datetime.date.today().isoformat()

    rows = data.get("data", [])
    count = 0
    for r in rows:
        # 欄位：[0]代號 [1]名稱 [2]成交股數 [3]成交金額 [4]開盤 [5]最高 [6]最低 [7]收盤 [8]漲跌價差 [9]成交筆數
        code = str(r[0]).strip()
        if not ORDINARY_STOCK_RE.match(code):
            continue
        name = str(r[1]).strip()
        vol = parse_int(r[2])
        o, h, l, c = parse_float(r[4]), parse_float(r[5]), parse_float(r[6]), parse_float(r[7])
        if c is None or c <= 0:
            continue
        o = o if o and o > 0 else c
        h = h if h and h > 0 else max(o, c)
        l = l if l and l > 0 else min(o, c)
        conn.execute(
            "INSERT OR REPLACE INTO stocks(code, name, market, sector) VALUES (?, ?, '上市', COALESCE((SELECT sector FROM stocks WHERE code=?), '上市普通股'))",
            (code, name, code),
        )
        conn.execute(
            "INSERT OR REPLACE INTO daily_bars(code, date, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (code, trade_date, o, h, l, c, vol),
        )
        count += 1
    conn.commit()
    print(f"[TWSE] 已寫入 {trade_date} 共 {count} 檔上市普通股。")
    return trade_date


def sync_tpex_daily(conn):
    """抓取 TPEx 上櫃全市場當日收盤行情（單次請求取得全部上櫃個股）"""
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
    print("[TPEx] 正在抓取上櫃全市場日行情...")
    data = fetch_json(url)
    if not isinstance(data, list):
        print("[警告] TPEx OpenAPI 回傳格式非清單，略過。")
        return
    count = 0
    latest_date = ""
    for item in data:
        code = str(item.get("SecuritiesCompanyCode", "")).strip()
        if not ORDINARY_STOCK_RE.match(code):
            continue
        name = str(item.get("CompanyName", "")).strip()
        trade_date = roc_to_iso(item.get("Date", ""))
        latest_date = trade_date
        c = parse_float(item.get("Close"))
        if c is None or c <= 0:
            continue
        o = parse_float(item.get("Open")) or c
        h = parse_float(item.get("High")) or max(o, c)
        l = parse_float(item.get("Low")) or min(o, c)
        vol = parse_int(item.get("TradingShares"))
        conn.execute(
            "INSERT OR REPLACE INTO stocks(code, name, market, sector) VALUES (?, ?, '上櫃', COALESCE((SELECT sector FROM stocks WHERE code=?), '上櫃普通股'))",
            (code, name, code),
        )
        conn.execute(
            "INSERT OR REPLACE INTO daily_bars(code, date, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (code, trade_date, o, h, l, c, vol),
        )
        count += 1
    conn.commit()
    print(f"[TPEx] 已寫入 {latest_date} 共 {count} 檔上櫃普通股。")


def sync_twse_inst(conn):
    """抓取 TWSE 上市全市場當日三大法人買賣超（單位換算為張）"""
    url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json&selectType=ALLBUT0999"
    print("[TWSE 籌碼] 正在抓取上市三大法人買賣超...")
    try:
        data = fetch_json(url)
        if data.get("stat") != "OK":
            print("[警告] TWSE T86 非 OK 狀態（可能尚未公布），略過。")
            return
        raw_date = data.get("date", "")
        trade_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if re.match(r"^\d{8}$", raw_date) else datetime.date.today().isoformat()
        count = 0
        for r in data.get("data", []):
            code = str(r[0]).strip()
            if not ORDINARY_STOCK_RE.match(code):
                continue
            # T86 欄位：[4]外陸資買賣超股數 [10]投信買賣超股數 [11]自營商買賣超股數 [18]三大法人買賣超股數
            f_lots = round(parse_int(r[4]) / 1000.0) if len(r) > 4 else 0
            t_lots = round(parse_int(r[10]) / 1000.0) if len(r) > 10 else 0
            d_lots = round(parse_int(r[11]) / 1000.0) if len(r) > 11 else 0
            tot_lots = round(parse_int(r[18]) / 1000.0) if len(r) > 18 else (f_lots + t_lots + d_lots)
            conn.execute(
                "INSERT OR REPLACE INTO inst_flows(code, date, foreign_net, trust_net, dealer_net, total_net) VALUES (?, ?, ?, ?, ?, ?)",
                (code, trade_date, f_lots, t_lots, d_lots, tot_lots),
            )
            count += 1
        conn.commit()
        print(f"[TWSE 籌碼] 已寫入 {trade_date} 共 {count} 檔上市三大法人買賣超。")
    except Exception as exc:
        print(f"[警告] TWSE 三大法人抓取失敗: {exc}")


def sync_tpex_inst(conn):
    """抓取 TPEx 上櫃全市場當日三大法人買賣超（單位換算為張）"""
    url = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&o=json&se=EW&t=D"
    print("[TPEx 籌碼] 正在抓取上櫃三大法人買賣超...")
    try:
        data = fetch_json(url)
        tables = data.get("tables", [])
        rows = tables[0].get("data", []) if tables else data.get("aaData", [])
        raw_date = data.get("date", "") or data.get("reportDate", "")
        trade_date = roc_to_iso(raw_date) if raw_date else datetime.date.today().isoformat()
        count = 0
        for r in rows:
            code = str(r[0]).strip()
            if not ORDINARY_STOCK_RE.match(code):
                continue
            # TPEx 3itrade 欄位：[10]外資及陸資買賣超股數 [13]投信買賣超股數 [22]自營商買賣超股數 [23]三大法人買賣超股數
            f_lots = round(parse_int(r[10]) / 1000.0) if len(r) > 10 else 0
            t_lots = round(parse_int(r[13]) / 1000.0) if len(r) > 13 else 0
            d_lots = round(parse_int(r[22]) / 1000.0) if len(r) > 22 else 0
            tot_lots = round(parse_int(r[23]) / 1000.0) if len(r) > 23 else (f_lots + t_lots + d_lots)
            conn.execute(
                "INSERT OR REPLACE INTO inst_flows(code, date, foreign_net, trust_net, dealer_net, total_net) VALUES (?, ?, ?, ?, ?, ?)",
                (code, trade_date, f_lots, t_lots, d_lots, tot_lots),
            )
            count += 1
        conn.commit()
        print(f"[TPEx 籌碼] 已寫入 {trade_date} 共 {count} 檔上櫃三大法人買賣超。")
    except Exception as exc:
        print(f"[警告] TPEx 三大法人抓取失敗: {exc}")


def backfill_twse_stock(conn, code, months=3):
    """安全回補單檔上市個股近 N 個月日 K（內建抗限流延遲 2.8~4.2 秒）"""
    today = datetime.date.today()
    y, m = today.year, today.month
    total = 0
    for _ in range(months):
        ymd = f"{y:04d}{m:02d}01"
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?response=json&date={ymd}&stockNo={code}"
        try:
            data = fetch_json(url)
            if data.get("stat") == "OK":
                for row in data.get("data", []):
                    dt = roc_to_iso(row[0])
                    vol = parse_int(row[1])
                    o, h, l, c = parse_float(row[3]), parse_float(row[4]), parse_float(row[5]), parse_float(row[6])
                    if c and c > 0:
                        conn.execute(
                            "INSERT OR REPLACE INTO daily_bars(code, date, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (code, dt, o or c, h or c, l or c, c, vol),
                        )
                        total += 1
                conn.commit()
        except Exception as exc:
            print(f"  [上市回補略過] {code} {ymd}: {exc}")
        time.sleep(random.uniform(2.8, 4.2))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    print(f"  [回補完成] 上市 {code}：共 {total} 根日 K")
    return total


def backfill_tpex_stock(conn, code, months=3):
    """安全回補單檔上櫃個股近 N 個月日 K（TPEx 官方個股日成交資訊）"""
    today = datetime.date.today()
    y, m = today.year, today.month
    total = 0
    for _ in range(months):
        ymd = f"{y:04d}/{m:02d}/01"
        url = f"https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?date={ymd}&code={code}&response=json"
        try:
            data = fetch_json(url)
            tables = data.get("tables", [])
            rows = tables[0].get("data", []) if tables else data.get("aaData", [])
            for row in rows:
                # TPEx 個股日成交欄位：[0]日期 [1]成交仟股 [2]成交仟元 [3]開盤 [4]最高 [5]最低 [6]收盤
                dt = roc_to_iso(row[0])
                vol_shares = parse_int(row[1]) * 1000
                o, h, l, c = parse_float(row[3]), parse_float(row[4]), parse_float(row[5]), parse_float(row[6])
                if c and c > 0:
                    conn.execute(
                        "INSERT OR REPLACE INTO daily_bars(code, date, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (code, dt, o or c, h or c, l or c, c, vol_shares),
                    )
                    total += 1
            conn.commit()
        except Exception as exc:
            print(f"  [上櫃回補略過] {code} {ymd}: {exc}")
        time.sleep(random.uniform(2.8, 4.2))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    print(f"  [回補完成] 上櫃 {code}：共 {total} 根日 K")
    return total


def backfill_stock_auto(conn, code, months=3):
    """依資料庫市場別自動選擇 TWSE 或 TPEx 回補；若尚無市場別則先嘗試 TWSE 再回退 TPEx"""
    row = conn.execute("SELECT market FROM stocks WHERE code=?", (code,)).fetchone()
    if row and row[0] == "上櫃":
        return backfill_tpex_stock(conn, code, months=months)
    if row and row[0] == "上市":
        return backfill_twse_stock(conn, code, months=months)
    total = backfill_twse_stock(conn, code, months=months)
    if total == 0:
        total = backfill_tpex_stock(conn, code, months=months)
    return total


def export_outputs(conn):
    """匯出全市場 JSON（給網頁系統秒讀）與 16 欄對齊之 CSV 資料庫"""
    taiex_rows = conn.execute("SELECT date, close FROM taiex_bars ORDER BY date ASC").fetchall()
    taiex_map = {d: c for d, c in taiex_rows}

    inst_rows = conn.execute(
        "SELECT code, date, foreign_net, trust_net, dealer_net, total_net FROM inst_flows"
    ).fetchall()
    inst_map = {(c, d): (fn, tn, dn, tot) for c, d, fn, tn, dn, tot in inst_rows}

    stock_rows = conn.execute("SELECT code, name, market, sector FROM stocks ORDER BY code ASC").fetchall()
    stocks_payload = {}
    csv_count = 0

    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f_csv:
        w = csv.writer(f_csv)
        w.writerow([
            "code", "name", "market", "sector", "date",
            "open", "high", "low", "close", "volume_shares", "volume_lots",
            "taiex_close", "inst_foreign_lots", "inst_trust_lots", "inst_dealer_lots", "inst_total_lots",
        ])
        for code, name, market, sector in stock_rows:
            bars = conn.execute(
                "SELECT date, open, high, low, close, volume FROM daily_bars WHERE code=? ORDER BY date ASC",
                (code,),
            ).fetchall()
            if not bars:
                continue
            ohlcv = []
            h52 = 0.0
            for dt, o, h, l, c, vol in bars[-260:]:
                if h > h52:
                    h52 = h
                ohlcv.append([dt, o, h, l, c, vol])
                inst_vals = inst_map.get((code, dt), ("", "", "", ""))
                w.writerow([
                    code, name, market, sector, dt,
                    o, h, l, c, vol, round(vol / 1000.0),
                    taiex_map.get(dt, ""),
                    inst_vals[0], inst_vals[1], inst_vals[2], inst_vals[3],
                ])
                csv_count += 1
            inst_row = conn.execute(
                "SELECT date, foreign_net, trust_net, dealer_net, total_net FROM inst_flows WHERE code=? ORDER BY date DESC LIMIT 1",
                (code,),
            ).fetchone()
            inst_obj = {
                "date": inst_row[0],
                "foreign": inst_row[1],
                "trust": inst_row[2],
                "dealer": inst_row[3],
                "total": inst_row[4],
            } if inst_row else None
            last = ohlcv[-1]
            stocks_payload[f"REAL_{code}"] = {
                "title": f"{code} {name}（{market}・真實 {last[0]}）",
                "short": f"{code} {name}",
                "market": market,
                "sector": sector,
                "h52": h52,
                "today": {"open": last[1], "high": last[2], "low": last[3], "close": last[4], "volume": last[5]},
                "ohlcv": ohlcv,
                **({"inst": inst_obj} if inst_obj else {}),
            }

    payload = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "stock_count": len(stocks_payload),
        "taiex": taiex_map,
        "stocks": stocks_payload,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f_json:
        json.dump(payload, f_json, ensure_ascii=False, separators=(",", ":"))

    print(f"[匯出完成] {OUT_JSON} ({len(stocks_payload)} 檔個股) 與 {OUT_CSV} ({csv_count} 筆日 K，16 欄完整對齊)。")


def serve_local(port=8787):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=os.getcwd())
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}/index.html"
        print(f"\n🚀 本機全市場看盤伺服器已啟動：{url}")
        print("   （請確認 index.html 與本腳本位於同一資料夾，開啟即自動載入最新全市場資料庫）")
        print("   按 Ctrl+C 結束伺服器。")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止本機伺服器。")


def main():
    parser = argparse.ArgumentParser(description="台股全市場（上市＋上櫃）行情同步與本機看盤伺服器 v5.1")
    parser.add_argument("--seed-csv", type=str, default="", help="先從指定基線 CSV 匯入歷史日 K 至 SQLite（不覆蓋既有紀錄）")
    parser.add_argument("--skip-sync", action="store_true", help="略過線上抓取，直接重新匯出或啟動伺服器")
    parser.add_argument("--backfill", type=str, default="", help="指定要回補歷史日 K 的上市/上櫃代號（逗號分隔，如 2330,2454,3491）")
    parser.add_argument("--months", type=int, default=3, help="回補月數（預設 3 個月，約 60 根日 K）")
    parser.add_argument("--serve", action="store_true", help="同步完成後啟動本機伺服器 (http://127.0.0.1:8787/index.html)")
    parser.add_argument("--port", type=int, default=8787, help="本機伺服器埠號（預設 8787）")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.seed_csv:
        seed_from_csv(conn, args.seed_csv)

    if not args.skip_sync:
        sync_taiex(conn)
        sync_twse_daily(conn)
        time.sleep(random.uniform(1.0, 1.8))
        sync_tpex_daily(conn)
        time.sleep(random.uniform(1.0, 1.8))
        sync_twse_inst(conn)
        time.sleep(random.uniform(1.0, 1.8))
        sync_tpex_inst(conn)

    if args.backfill:
        codes = [c.strip() for c in args.backfill.split(",") if ORDINARY_STOCK_RE.match(c.strip())]
        print(f"[歷史回補] 開始安全回補 {len(codes)} 檔（自動辨識上市/上櫃）近 {args.months} 個月日 K...")
        for code in codes:
            backfill_stock_auto(conn, code, months=args.months)

    export_outputs(conn)
    conn.close()

    if args.serve:
        serve_local(port=args.port)


if __name__ == "__main__":
    main()
