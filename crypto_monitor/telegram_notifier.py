#!/usr/bin/env python3
"""Send messages through the Telegram Bot API.

Usage:
    from crypto_monitor.telegram_notifier import send_telegram
    ok = send_telegram(bot_token, chat_id, text)
"""
import requests


def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    """Send *text* (HTML) to *chat_id* using *bot_token*.

    Returns True when the message was delivered, False otherwise.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json().get("ok", False)
    except Exception:
        return False
