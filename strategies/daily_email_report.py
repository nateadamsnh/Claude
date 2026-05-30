#!/usr/bin/env python3
"""
Daily Portfolio P&L Email Report
==================================
Sends a formatted daily summary email at 4:00 PM ET (after market close).
Covers all open positions, today's P&L, overall account value, and
the current status of each active trading strategy.

Config:  Trading/config/email_config.json
         --> Set app_password to your Gmail App Password to activate.
"""

import json
import smtplib
import sys
import requests
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent.parent
CONFIG_FILE  = BASE_DIR / "config" / "alpaca_credentials.json"
EMAIL_FILE   = BASE_DIR / "config" / "email_config.json"
LOG_FILE     = BASE_DIR / "logs" / "email_report.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
log = logging.getLogger(__name__)

# ── Load configs ──────────────────────────────────────────────────────────────
with open(CONFIG_FILE) as f:
    creds = json.load(f)
with open(EMAIL_FILE) as f:
    ecfg = json.load(f)

BASE_URL = creds["endpoint"]
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}

STRATEGIES = [
    {"symbol": "TSLA", "state_file": BASE_DIR / "strategies" / "tsla_strategy_state.json"},
    {"symbol": "QBTS", "state_file": BASE_DIR / "strategies" / "qbts_strategy_state.json"},
]


# ── Alpaca helpers ────────────────────────────────────────────────────────────
def get_account():
    r = requests.get(f"{BASE_URL}/account", headers=HEADERS, timeout=10)
    return r.json()

def get_positions():
    r = requests.get(f"{BASE_URL}/positions", headers=HEADERS, timeout=10)
    return r.json() if r.status_code == 200 else []

def get_open_orders():
    r = requests.get(f"{BASE_URL}/orders", headers=HEADERS,
                     params={"status": "open"}, timeout=10)
    return r.json() if r.status_code == 200 else []

def load_state(state_file):
    p = Path(state_file)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {}


# ── Build HTML email ──────────────────────────────────────────────────────────
def build_email(account, positions, open_orders):
    today     = datetime.now().strftime("%A, %B %d, %Y")
    timestamp = datetime.now().strftime("%I:%M %p ET")

    port_val   = float(account["portfolio_value"])
    cash       = float(account["cash"])
    equity     = float(account["equity"])
    last_eq    = float(account["last_equity"])
    day_pl     = equity - last_eq
    day_pl_pct = (day_pl / last_eq * 100) if last_eq else 0
    pl_color   = "#2e7d32" if day_pl >= 0 else "#c62828"
    pl_arrow   = "+" if day_pl >= 0 else ""

    # ── Positions table rows ──────────────────────────────────────────────────
    pos_rows = ""
    total_unreal = 0.0
    for pos in positions:
        sym       = pos["symbol"]
        qty       = float(pos["qty"])
        entry     = float(pos["avg_entry_price"])
        price     = float(pos["current_price"])
        mkt_val   = float(pos["market_value"])
        upl       = float(pos["unrealized_pl"])
        uplpct    = float(pos["unrealized_plpc"]) * 100
        chg_today = float(pos["change_today"]) * 100
        total_unreal += upl

        upl_color = "#2e7d32" if upl >= 0 else "#c62828"
        chg_color = "#2e7d32" if chg_today >= 0 else "#c62828"

        # Load strategy state for stop info
        state = {}
        for s in STRATEGIES:
            if s["symbol"] == sym:
                state = load_state(s["state_file"])
                break
        stop_px = state.get("stop_price", "N/A")
        stop_str = f"${stop_px:.2f}" if isinstance(stop_px, float) else stop_px

        # Is trailing active?
        trailing = "Active" if state.get("trailing_active") else "Waiting"
        trail_color = "#2e7d32" if state.get("trailing_active") else "#888"

        pos_rows += f"""
        <tr>
          <td style="padding:10px;font-weight:bold;font-size:15px;">{sym}</td>
          <td style="padding:10px;text-align:right;">{qty:.0f}</td>
          <td style="padding:10px;text-align:right;">${entry:.2f}</td>
          <td style="padding:10px;text-align:right;">${price:.2f}</td>
          <td style="padding:10px;text-align:right;color:{chg_color};">{chg_today:+.2f}%</td>
          <td style="padding:10px;text-align:right;">${mkt_val:,.2f}</td>
          <td style="padding:10px;text-align:right;color:{upl_color};font-weight:bold;">
            ${upl:+,.2f}<br><small>({uplpct:+.2f}%)</small>
          </td>
          <td style="padding:10px;text-align:right;">{stop_str}</td>
          <td style="padding:10px;text-align:center;color:{trail_color};">{trailing}</td>
        </tr>"""

    # ── Open orders table rows ────────────────────────────────────────────────
    order_rows = ""
    for o in open_orders:
        o_sym    = o.get("symbol", "")
        o_side   = o.get("side", "").upper()
        o_type   = o.get("type", "").upper()
        o_qty    = o.get("qty", "")
        o_stop   = o.get("stop_price") or o.get("limit_price") or "MKT"
        o_tif    = o.get("time_in_force", "").upper()
        side_color = "#c62828" if o_side == "SELL" else "#1565c0"
        order_rows += f"""
        <tr>
          <td style="padding:8px;font-weight:bold;">{o_sym}</td>
          <td style="padding:8px;color:{side_color};font-weight:bold;">{o_side}</td>
          <td style="padding:8px;">{o_type}</td>
          <td style="padding:8px;text-align:right;">{o_qty}</td>
          <td style="padding:8px;text-align:right;">${o_stop}</td>
          <td style="padding:8px;">{o_tif}</td>
        </tr>"""

    if not order_rows:
        order_rows = '<tr><td colspan="6" style="padding:10px;color:#888;text-align:center;">No open orders</td></tr>'

    total_upl_color = "#2e7d32" if total_unreal >= 0 else "#c62828"

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:0;">
  <div style="max-width:700px;margin:30px auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 8px rgba(0,0,0,0.1);overflow:hidden;">

    <!-- Header -->
    <div style="background:#1a237e;color:#fff;padding:24px 28px;">
      <h1 style="margin:0;font-size:22px;">Daily Portfolio Report</h1>
      <p style="margin:6px 0 0;opacity:0.85;">{today} &nbsp;|&nbsp; {timestamp}</p>
    </div>

    <!-- Account Summary -->
    <div style="padding:24px 28px;border-bottom:1px solid #eee;">
      <h2 style="margin:0 0 16px;font-size:16px;color:#333;text-transform:uppercase;
                 letter-spacing:1px;">Account Summary</h2>
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="padding:8px 0;">
            <span style="color:#666;font-size:13px;">Portfolio Value</span><br>
            <span style="font-size:26px;font-weight:bold;color:#1a237e;">${port_val:,.2f}</span>
          </td>
          <td style="padding:8px 0;text-align:center;">
            <span style="color:#666;font-size:13px;">Day P&amp;L</span><br>
            <span style="font-size:26px;font-weight:bold;color:{pl_color};">
              {pl_arrow}${abs(day_pl):,.2f}
            </span><br>
            <span style="font-size:13px;color:{pl_color};">({pl_arrow}{day_pl_pct:.2f}%)</span>
          </td>
          <td style="padding:8px 0;text-align:right;">
            <span style="color:#666;font-size:13px;">Cash Available</span><br>
            <span style="font-size:22px;font-weight:bold;color:#333;">${cash:,.2f}</span>
          </td>
        </tr>
      </table>
    </div>

    <!-- Positions -->
    <div style="padding:24px 28px;border-bottom:1px solid #eee;">
      <h2 style="margin:0 0 16px;font-size:16px;color:#333;text-transform:uppercase;
                 letter-spacing:1px;">Open Positions</h2>
      <table width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;font-size:13px;">
        <thead>
          <tr style="background:#f0f0f0;color:#555;">
            <th style="padding:10px;text-align:left;">Symbol</th>
            <th style="padding:10px;text-align:right;">Shares</th>
            <th style="padding:10px;text-align:right;">Entry</th>
            <th style="padding:10px;text-align:right;">Price</th>
            <th style="padding:10px;text-align:right;">Today</th>
            <th style="padding:10px;text-align:right;">Mkt Value</th>
            <th style="padding:10px;text-align:right;">Unrealized P&amp;L</th>
            <th style="padding:10px;text-align:right;">Stop</th>
            <th style="padding:10px;text-align:center;">Trailing</th>
          </tr>
        </thead>
        <tbody>{pos_rows}</tbody>
        <tfoot>
          <tr style="background:#fafafa;font-weight:bold;">
            <td colspan="6" style="padding:10px;">Total Unrealized P&amp;L</td>
            <td style="padding:10px;text-align:right;color:{total_upl_color};">
              ${total_unreal:+,.2f}
            </td>
            <td colspan="2"></td>
          </tr>
        </tfoot>
      </table>
    </div>

    <!-- Open Orders -->
    <div style="padding:24px 28px;border-bottom:1px solid #eee;">
      <h2 style="margin:0 0 16px;font-size:16px;color:#333;text-transform:uppercase;
                 letter-spacing:1px;">Active Orders</h2>
      <table width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;font-size:13px;">
        <thead>
          <tr style="background:#f0f0f0;color:#555;">
            <th style="padding:8px;text-align:left;">Symbol</th>
            <th style="padding:8px;text-align:left;">Side</th>
            <th style="padding:8px;text-align:left;">Type</th>
            <th style="padding:8px;text-align:right;">Qty</th>
            <th style="padding:8px;text-align:right;">Price</th>
            <th style="padding:8px;text-align:left;">TIF</th>
          </tr>
        </thead>
        <tbody>{order_rows}</tbody>
      </table>
    </div>

    <!-- Footer -->
    <div style="padding:16px 28px;background:#fafafa;color:#999;font-size:12px;">
      Automated report from your Alpaca Paper Trading account.
      Strategies monitored: TSLA trailing stop | QBTS trailing stop
    </div>
  </div>
</body>
</html>"""
    return html


# ── Send email ────────────────────────────────────────────────────────────────
def send_email(html_body):
    sender    = ecfg["sender_email"]
    password  = ecfg["app_password"]
    recipient = ecfg["recipient"]
    today_str = datetime.now().strftime("%b %d, %Y")

    if "PASTE_YOUR" in password:
        log.error("Email not sent — app_password not set in config/email_config.json")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Portfolio Report — {today_str}"
    msg["From"]    = f"Trading Bot <{sender}>"
    msg["To"]      = recipient
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(ecfg["smtp_host"], ecfg["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        log.info(f"Email sent to {recipient}")
        return True
    except Exception as e:
        log.error(f"Failed to send email: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    log.info("=" * 65)
    log.info(f"Daily Email Report  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 65)

    account     = get_account()
    positions   = get_positions()
    open_orders = get_open_orders()

    html = build_email(account, positions, open_orders)
    send_email(html)


if __name__ == "__main__":
    run()
