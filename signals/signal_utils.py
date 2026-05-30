#!/usr/bin/env python3
"""
Shared utilities for all signal monitors.
"""

import json
import logging
import smtplib
import sys
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

BASE_DIR    = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config" / "email_config.json"
STATES_DIR  = Path(__file__).parent / "states"
STATES_DIR.mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Managed stock symbols ─────────────────────────────────────────────────────
PORTFOLIO_SYMBOLS = [
    "ACI", "BAC", "BND", "BRK.B", "BWXT", "CARR", "CVX", "EMR",
    "GOOGL", "IAU", "IBM", "INTC", "IONQ", "LMT", "LOW", "MA",
    "MSFT", "NVDA", "O", "ORCL", "PLTR", "PM", "RKLB", "SOXX",
    "SYK", "V", "VNQ", "XLE", "XOM",
]

# Company names for fuzzy matching in SEC filings
COMPANY_NAMES = {
    "ACI":   ["Albertsons"],
    "BAC":   ["Bank of America"],
    "BND":   ["Vanguard Total Bond"],
    "BWXT":  ["BWX Technologies", "BWXT"],
    "CARR":  ["Carrier Global"],
    "CVX":   ["Chevron"],
    "EMR":   ["Emerson Electric"],
    "GOOGL": ["Alphabet"],
    "IAU":   ["iShares Gold"],
    "IBM":   ["International Business Machines", "IBM"],
    "INTC":  ["Intel"],
    "IONQ":  ["IonQ"],
    "LMT":   ["Lockheed Martin"],
    "LOW":   ["Lowe's"],
    "MA":    ["Mastercard"],
    "MSFT":  ["Microsoft"],
    "NVDA":  ["NVIDIA"],
    "O":     ["Realty Income"],
    "ORCL":  ["Oracle"],
    "PLTR":  ["Palantir"],
    "PM":    ["Philip Morris"],
    "RKLB":  ["Rocket Lab"],
    "SYK":   ["Stryker"],
    "V":     ["Visa"],
    "XOM":   ["Exxon Mobil"],
}

# Symbols worth monitoring for government contracts
GOVT_CONTRACT_SYMBOLS = ["LMT", "BWXT", "IONQ", "RKLB", "PLTR", "IBM", "MSFT", "NVDA"]


def get_logger(name: str, log_file: str) -> logging.Logger:
    log_path = BASE_DIR / "logs" / log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        sh = logging.StreamHandler(sys.stdout)
        fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
        fh.setFormatter(fmt)
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger


def load_state(filename: str) -> dict:
    p = STATES_DIR / filename
    if p.exists():
        return json.load(open(p))
    return {}


def save_state(filename: str, state: dict):
    with open(STATES_DIR / filename, "w") as f:
        json.dump(state, f, indent=2)


def send_signal_email(subject: str, body_text: str, body_html: str):
    """Send an alert email using the configured Gmail account."""
    if not CONFIG_FILE.exists():
        print("No email config found — skipping email.")
        return
    cfg = json.load(open(CONFIG_FILE))
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["sender_email"]
    msg["To"]      = cfg["recipient"]
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html,  "html"))
    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
            server.starttls()
            server.login(cfg["sender_email"], cfg["app_password"])
            server.sendmail(cfg["sender_email"], cfg["recipient"], msg.as_string())
        print(f"Email sent: {subject}")
    except Exception as e:
        print(f"Email failed: {e}")


def html_table(headers: list, rows: list, color: str = "#1a237e") -> str:
    th = "".join(f'<th style="padding:8px;text-align:left">{h}</th>' for h in headers)
    body = ""
    for i, row in enumerate(rows):
        bg = "#1a1d26" if i % 2 == 0 else "#16192a"
        td = "".join(f'<td style="padding:8px;border-bottom:1px solid #2a2d3a">{c}</td>' for c in row)
        body += f'<tr style="background:{bg}">{td}</tr>'
    return f"""
    <table style="border-collapse:collapse;width:100%;font-size:13px;margin-top:10px">
        <thead><tr style="background:{color};color:white">{th}</tr></thead>
        <tbody>{body}</tbody>
    </table>"""


def base_html(title: str, icon: str, content: str) -> str:
    return f"""<html><body style="font-family:Arial,sans-serif;max-width:800px;margin:auto;padding:20px;background:#0f1117;color:#e0e0e0">
    <h2 style="color:#fff">{icon} {title}</h2>
    <p style="color:#888;font-size:12px">{datetime.now().strftime('%Y-%m-%d %H:%M ET')}</p>
    <hr style="border-color:#2a2d3a">
    {content}
    <p style="color:#555;font-size:11px;margin-top:30px">— Nathaniel's Trading System | Auto-generated signal alert</p>
    </body></html>"""
