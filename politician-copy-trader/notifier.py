#!/usr/bin/env python3
"""
Notifications for the copy trader.
- Windows desktop balloon-tip notifications via PowerShell
- Email notifications via Gmail SMTP
"""

import json
import logging
import smtplib
import subprocess
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
EMAIL_FILE  = BASE_DIR.parent / "config" / "email_config.json"

# Telegram
sys.path.insert(0, str(BASE_DIR.parent / "config"))
try:
    from telegram_notifier import send as _tg_send
except Exception:
    def _tg_send(text: str): pass

# Load email config once at import time
_EMAIL_CFG = None
_EMAIL_ENABLED = False

def _load_email_config():
    global _EMAIL_CFG, _EMAIL_ENABLED
    try:
        if EMAIL_FILE.exists():
            with open(EMAIL_FILE) as f:
                _EMAIL_CFG = json.load(f)
            pw = _EMAIL_CFG.get("app_password", "")
            _EMAIL_ENABLED = bool(pw and "PASTE" not in pw and len(pw) >= 16)
    except Exception as e:
        log.warning(f"Could not load email config: {e}")

_load_email_config()

# Load notification config
_NOTIF_CFG = {}
try:
    with open(CONFIG_FILE) as f:
        _cfg = json.load(f)
    _NOTIF_CFG = _cfg.get("notifications", {})
except Exception:
    pass

_EMAIL_NOTIF_ENABLED = _NOTIF_CFG.get("email_notifications", True)


# ── Email ──────────────────────────────────────────────────────────────────────

def _send_email(subject: str, body_text: str, body_html: str = None):
    """Send an email notification. Fails silently on error."""
    if not _EMAIL_ENABLED or not _EMAIL_NOTIF_ENABLED:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["From"]    = _EMAIL_CFG["sender_email"]
        msg["To"]      = _EMAIL_CFG["recipient"]
        msg["Subject"] = subject

        msg.attach(MIMEText(body_text, "plain"))
        if body_html:
            msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(_EMAIL_CFG["smtp_host"], _EMAIL_CFG["smtp_port"]) as server:
            server.starttls()
            server.login(_EMAIL_CFG["sender_email"], _EMAIL_CFG["app_password"])
            server.send_message(msg)

        log.info(f"[EMAIL] Sent: {subject}")
    except Exception as e:
        log.warning(f"Email failed: {e}")


# ── Windows balloon-tip ────────────────────────────────────────────────────────

def _ps_notify(title: str, message: str, timeout_ms: int = 8000):
    """Fire a Windows system-tray balloon-tip notification via PowerShell."""
    title   = title.replace("'", "`'")
    message = message.replace("'", "`'")

    ps = f"""
Add-Type -AssemblyName System.Windows.Forms
$icon   = [System.Drawing.SystemIcons]::Information
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon               = $icon
$notify.BalloonTipTitle    = '{title}'
$notify.BalloonTipText     = '{message}'
$notify.BalloonTipIcon     = 'Info'
$notify.Visible            = $true
$notify.ShowBalloonTip({timeout_ms})
Start-Sleep -Milliseconds {timeout_ms + 500}
$notify.Dispose()
"""
    try:
        subprocess.Popen(
            ["powershell", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info(f"[NOTIFY] {title} -- {message}")
    except Exception as e:
        log.warning(f"Notification failed: {e}")


# ── Public notification functions ──────────────────────────────────────────────

def new_trade(politician: str, symbol: str, tx_type: str, company: str, size_range: str):
    """Notify when a new trade is detected from a followed politician."""
    action  = "BUY" if tx_type == "buy" else "SELL"
    title   = f"Capitol Trades -- New {action}: {symbol}"
    message = (
        f"{politician} filed a {action} on {company} ({size_range})\n"
        f"Copying trade now on Alpaca."
    )
    _ps_notify(title, message)
    _tg_send(
        f"<b>Capitol Trades — {action}: {symbol}</b>\n"
        f"Politician: {politician}\n"
        f"Company: {company}  |  Size: {size_range}\n"
        f"Copying trade on Alpaca now."
    )

    # Email
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = (
        f"New Congressional Trade Detected\n"
        f"{'=' * 40}\n\n"
        f"Politician : {politician}\n"
        f"Action     : {action}\n"
        f"Symbol     : {symbol}\n"
        f"Company    : {company}\n"
        f"Size       : {size_range}\n"
        f"Detected   : {now}\n\n"
        f"Trade is being copied on your Alpaca paper account.\n"
    )
    html = f"""
<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;">
  <h2 style="color:#1a73e8;">Capitol Trades &mdash; New {action}: {symbol}</h2>
  <table style="border-collapse:collapse;width:100%;">
    <tr><td style="padding:8px;color:#555;width:130px;"><b>Politician</b></td><td style="padding:8px;">{politician}</td></tr>
    <tr style="background:#f5f5f5;"><td style="padding:8px;color:#555;"><b>Action</b></td><td style="padding:8px;color:{'#0b8a00' if action=='BUY' else '#c0392b'};font-weight:bold;">{action}</td></tr>
    <tr><td style="padding:8px;color:#555;"><b>Symbol</b></td><td style="padding:8px;font-weight:bold;">{symbol}</td></tr>
    <tr style="background:#f5f5f5;"><td style="padding:8px;color:#555;"><b>Company</b></td><td style="padding:8px;">{company}</td></tr>
    <tr><td style="padding:8px;color:#555;"><b>Size</b></td><td style="padding:8px;">{size_range}</td></tr>
    <tr style="background:#f5f5f5;"><td style="padding:8px;color:#555;"><b>Detected</b></td><td style="padding:8px;">{now}</td></tr>
  </table>
  <p style="color:#555;margin-top:16px;">Trade is being copied on your Alpaca paper account.</p>
  <p style="color:#aaa;font-size:12px;">Politician Copy Trader Bot</p>
</body></html>
"""
    _send_email(f"[Copy Trader] New {action}: {symbol} ({politician})", text, html)


def gain_milestone(symbol: str, politician: str, pct: float, pnl_usd: float):
    """Notify when a copied position crosses a gain milestone."""
    sign  = "+" if pct >= 0 else ""
    title = f"[UP] {symbol} {sign}{pct:.1f}% -- Copy Trade Alert"
    message = (
        f"Position copied from {politician} is up {sign}{pct:.1f}% "
        f"(${pnl_usd:+.2f}). Milestone hit!"
    )
    _ps_notify(title, message, timeout_ms=10000)
    _tg_send(
        f"<b>Gain Milestone: {symbol} {sign}{pct:.1f}%</b>\n"
        f"Politician: {politician}\n"
        f"P&amp;L: ${pnl_usd:+.2f}"
    )

    # Email
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = (
        f"Gain Milestone Hit!\n"
        f"{'=' * 40}\n\n"
        f"Symbol     : {symbol}\n"
        f"Politician : {politician}\n"
        f"Gain       : {sign}{pct:.1f}%\n"
        f"P&L        : ${pnl_usd:+.2f}\n"
        f"Time       : {now}\n"
    )
    html = f"""
<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;">
  <h2 style="color:#0b8a00;">&#x1F4C8; {symbol} {sign}{pct:.1f}% &mdash; Milestone Hit!</h2>
  <table style="border-collapse:collapse;width:100%;">
    <tr><td style="padding:8px;color:#555;width:130px;"><b>Symbol</b></td><td style="padding:8px;font-weight:bold;">{symbol}</td></tr>
    <tr style="background:#f5f5f5;"><td style="padding:8px;color:#555;"><b>Politician</b></td><td style="padding:8px;">{politician}</td></tr>
    <tr><td style="padding:8px;color:#555;"><b>Gain</b></td><td style="padding:8px;color:#0b8a00;font-weight:bold;">{sign}{pct:.1f}%</td></tr>
    <tr style="background:#f5f5f5;"><td style="padding:8px;color:#555;"><b>P&amp;L</b></td><td style="padding:8px;color:#0b8a00;font-weight:bold;">${pnl_usd:+.2f}</td></tr>
    <tr><td style="padding:8px;color:#555;"><b>Time</b></td><td style="padding:8px;">{now}</td></tr>
  </table>
  <p style="color:#aaa;font-size:12px;">Politician Copy Trader Bot</p>
</body></html>
"""
    _send_email(f"[Copy Trader] {symbol} {sign}{pct:.1f}% Milestone!", text, html)


def loss_alert(symbol: str, politician: str, pct: float, pnl_usd: float):
    """Notify when a copied position drops 10%+ below entry."""
    title   = f"[WARN] {symbol} {pct:.1f}% -- Copy Trade Down"
    message = (
        f"Position copied from {politician} is down {pct:.1f}% "
        f"(${pnl_usd:+.2f}). Consider reviewing."
    )
    _ps_notify(title, message, timeout_ms=10000)
    _tg_send(
        f"<b>Loss Alert: {symbol} {pct:.1f}%</b>\n"
        f"Politician: {politician}\n"
        f"P&amp;L: ${pnl_usd:+.2f}\n"
        f"Consider reviewing this position."
    )

    # Email
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = (
        f"Loss Alert!\n"
        f"{'=' * 40}\n\n"
        f"Symbol     : {symbol}\n"
        f"Politician : {politician}\n"
        f"Loss       : {pct:.1f}%\n"
        f"P&L        : ${pnl_usd:+.2f}\n"
        f"Time       : {now}\n\n"
        f"Consider reviewing this position.\n"
    )
    html = f"""
<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;">
  <h2 style="color:#c0392b;">&#x26A0;&#xFE0F; {symbol} {pct:.1f}% &mdash; Loss Alert</h2>
  <table style="border-collapse:collapse;width:100%;">
    <tr><td style="padding:8px;color:#555;width:130px;"><b>Symbol</b></td><td style="padding:8px;font-weight:bold;">{symbol}</td></tr>
    <tr style="background:#f5f5f5;"><td style="padding:8px;color:#555;"><b>Politician</b></td><td style="padding:8px;">{politician}</td></tr>
    <tr><td style="padding:8px;color:#555;"><b>Loss</b></td><td style="padding:8px;color:#c0392b;font-weight:bold;">{pct:.1f}%</td></tr>
    <tr style="background:#f5f5f5;"><td style="padding:8px;color:#555;"><b>P&amp;L</b></td><td style="padding:8px;color:#c0392b;font-weight:bold;">${pnl_usd:+.2f}</td></tr>
    <tr><td style="padding:8px;color:#555;"><b>Time</b></td><td style="padding:8px;">{now}</td></tr>
  </table>
  <p style="color:#555;margin-top:16px;">Consider reviewing this position.</p>
  <p style="color:#aaa;font-size:12px;">Politician Copy Trader Bot</p>
</body></html>
"""
    _send_email(f"[Copy Trader] LOSS ALERT: {symbol} {pct:.1f}%", text, html)
