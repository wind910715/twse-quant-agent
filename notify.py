"""TG 發送共用 sender（2026-10-31 新版早晚推播用）。

現有 monitor.py / screener.py / labs 各自的 sender 本批不動（推播文字精簡尚未定案），
新腳本一律用這一支，不再新增第 N 套實作。無 token 時只印出訊息（供本機乾跑驗證）。
"""
import os

import requests


def send_telegram(text: str) -> None:
    print(text)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i in range(0, len(text), 3800):
        try:
            requests.post(url, json={"chat_id": chat_id, "text": text[i:i + 3800]}, timeout=10)
        except Exception:
            pass
