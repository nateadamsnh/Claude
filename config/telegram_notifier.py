#!/usr/bin/env python3
"""
Shared Telegram notification helper for all trading bots.
Reads credentials from telegram_config.json in the same directory.
All functions fail silently so bots never crash on a notification error.
"""

import json
import logging
import requests
from pathlib import Path

log = logging.getLogger(__name__)

_CFG_FILE = Path(__file__).parent / "telegram_config.json"
_TOKEN    = None
_CHAT_ID  = None

def _load():
    global _TOKEN, _CHAT_ID
    try:
        with open(_CFG_FILE) as f:
            cfg = json.load(f)
        _TOKEN   = cfg.get("bot_token", "")
        _CHAT_ID = cfg.get("chat_id", "")
    except Exception as e:
        log.warning(f"[TELEGRAM] Could not load config: {e}")

_load()


def send(text: str):
    """Send a plain-text message. Fails silently."""
    if not _TOKEN or not _CHAT_ID:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
            json={"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if not r.ok:
            log.warning(f"[TELEGRAM] Send failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.warning(f"[TELEGRAM] Send error: {e}")
