"""
Alpaca Paper Trading Daily Summary
Fetches account data from Alpaca paper API and emails a summary.

Config files expected:
  C:/Users/Nathaniel/Documents/Trading/config/alpaca_credentials.json
  C:/Users/Nathaniel/Documents/Trading/config/email_credentials.json

email_credentials.json format:
{
    "smtp_user": "your_gmail@gmail.com",
    "smtp_password": "your_app_password_here",
    "to_address": "nate.adams.nh@gmail.com"
}
"""

import json
import os
import smtplib
import sys
import traceback
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR = r"C:\Users\Nathaniel\Documents\Trading\config"
ALPACA_CREDS = os.path.join(BASE_DIR, "alpaca_credentials.json")
EMAIL_CREDS  = os.path.join(BASE_DIR, "email_credentials.json")
API_BASE     = "https://paper-api.alpaca.markets/v2"


def load_json(path):
    with open(path) as f:
        return json.load(f)


def alpaca_get(endpoint, params=None, headers=None):
    r = requests.get(f"{API_BASE}{endpoint}", headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def fmt_usd(val):
    try:
        v = float(val)
        sign = "+" if v > 0 else ""
        return f"{sign}${v:,.2f}"
    except Exception:
        return str(val)


def fmt_pct(val):
    try:
        v = float(val) * 100
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.2f}%"
    except Exception:
        return str(val)


def color_val(val, positive_good=True):
    try:
        v = float(val)
        if v > 0:
            c = "#16a34a" if positive_good else "#dc2626"
        elif v < 0:
            c = "#dc2626" if positive_good else "#16a34a"
        else:
            c = "#6b7280"
        return c
    except Exception:
        return "#6b7280"


def build_html(acct, positions, orders, today_str):
    portfolio_val = float(acct.get("portfolio_value", 0))
    cash          = float(acct.get("cash", 0))
    buying_power  = float(acct.get("buying_power", 0))
    last_equity   = float(acct.get("last_equity", portfolio_val))
    today_pl      = portfolio_val - last_equity
    today_pl_pct  = (today_pl / last_equity * 100) if last_equity else 0

    # Sort positions by unrealized P&L %
    def upnl_pct(p):
        try:
            return float(p.get("unrealized_plpc", 0))
        except Exception:
            return 0.0

    sorted_pos = sorted(positions, key=upnl_pct, reverse=True)
    winners    = sorted_pos[:5]
    losers     = sorted_pos[-5:][::-1]

    # Notable: biggest intraday movers
    def intraday_chg(p):
        try:
            return abs(float(p.get("change_today", 0)))
        except Exception:
            return 0.0

    notable = sorted(positions, key=intraday_chg, reverse=True)[:5]

    pl_color = color_val(today_pl)

    # Helper: position row
    def pos_row(p, highlight=False):
        sym     = p.get("symbol", "")
        qty     = p.get("qty", "")
        entry   = f"${float(p.get('avg_entry_price', 0)):,.2f}"
        cur     = f"${float(p.get('current_price', 0)):,.2f}"
        mkt_val = f"${float(p.get('market_value', 0)):,.2f}"
        upl     = float(p.get("unrealized_pl", 0))
        uplp    = float(p.get("unrealized_plpc", 0)) * 100
        tod     = float(p.get("change_today", 0)) * 100
        upl_c   = color_val(upl)
        tod_c   = color_val(tod)
        bg      = "#f0fdf4" if highlight else ""
        style   = f' style="background:{bg}"' if bg else ""
        return (
            f"<tr{style}>"
            f"<td style='padding:6px 10px;font-weight:600'>{sym}</td>"
            f"<td style='padding:6px 10px;text-align:right'>{qty}</td>"
            f"<td style='padding:6px 10px;text-align:right'>{entry}</td>"
            f"<td style='padding:6px 10px;text-align:right'>{cur}</td>"
            f"<td style='padding:6px 10px;text-align:right'>{mkt_val}</td>"
            f"<td style='padding:6px 10px;text-align:right;color:{upl_c}'>${upl:+,.2f}</td>"
            f"<td style='padding:6px 10px;text-align:right;color:{upl_c}'>{uplp:+.2f}%</td>"
            f"<td style='padding:6px 10px;text-align:right;color:{tod_c}'>{tod:+.2f}%</td>"
            "</tr>"
        )

    # Top 5 mini-table helper
    def mini_table(title, rows, color):
        if not rows:
            return f"<p style='color:#6b7280;font-size:13px'>No positions.</p>"
        cells = ""
        for p in rows:
            sym  = p.get("symbol", "")
            uplp = float(p.get("unrealized_plpc", 0)) * 100
            upl  = float(p.get("unrealized_pl", 0))
            cells += (
                f"<tr>"
                f"<td style='padding:4px 10px;font-weight:600'>{sym}</td>"
                f"<td style='padding:4px 10px;text-align:right;color:{color}'>{uplp:+.2f}%</td>"
                f"<td style='padding:4px 10px;text-align:right;color:{color}'>${upl:+,.2f}</td>"
                "</tr>"
            )
        return (
            f"<table style='border-collapse:collapse;width:100%'>"
            f"<tr style='background:#f9fafb'>"
            f"<th style='padding:4px 10px;text-align:left;font-size:12px;color:#6b7280'>Symbol</th>"
            f"<th style='padding:4px 10px;text-align:right;font-size:12px;color:#6b7280'>Unr. P&L %</th>"
            f"<th style='padding:4px 10px;text-align:right;font-size:12px;color:#6b7280'>Unr. P&L $</th>"
            "</tr>"
            + cells +
            "</table>"
        )

    # Notable movers
    notable_rows = ""
    for p in notable:
        sym = p.get("symbol", "")
        tod = float(p.get("change_today", 0)) * 100
        c   = color_val(tod)
        notable_rows += f"<li><strong>{sym}</strong>: <span style='color:{c}'>{tod:+.2f}%</span> today</li>"
    notable_html = f"<ul style='margin:4px 0;padding-left:20px'>{notable_rows}</ul>" if notable_rows else "<p style='color:#6b7280'>No notable movers.</p>"

    # All positions table
    all_rows = "".join(pos_row(p) for p in sorted_pos) if positions else (
        "<tr><td colspan='8' style='padding:12px;text-align:center;color:#6b7280'>No open positions.</td></tr>"
    )

    # Open orders
    order_rows = ""
    for o in orders:
        sym    = o.get("symbol", "")
        side   = o.get("side", "").upper()
        qty    = o.get("qty") or o.get("notional", "")
        otype  = o.get("type", "")
        lim    = f"${float(o['limit_price']):,.2f}" if o.get("limit_price") else "—"
        stop   = f"${float(o['stop_price']):,.2f}" if o.get("stop_price") else "—"
        tif    = o.get("time_in_force", "").upper()
        status = o.get("status", "")
        side_c = "#16a34a" if side == "BUY" else "#dc2626"
        order_rows += (
            f"<tr>"
            f"<td style='padding:6px 10px;font-weight:600'>{sym}</td>"
            f"<td style='padding:6px 10px;color:{side_c};font-weight:600'>{side}</td>"
            f"<td style='padding:6px 10px;text-align:right'>{qty}</td>"
            f"<td style='padding:6px 10px'>{otype}</td>"
            f"<td style='padding:6px 10px;text-align:right'>{lim}</td>"
            f"<td style='padding:6px 10px;text-align:right'>{stop}</td>"
            f"<td style='padding:6px 10px'>{tif}</td>"
            f"<td style='padding:6px 10px;color:#6b7280'>{status}</td>"
            "</tr>"
        )
    if not order_rows:
        order_rows = "<tr><td colspan='8' style='padding:12px;text-align:center;color:#6b7280'>No open orders.</td></tr>"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f3f4f6;margin:0;padding:20px">
<div style="max-width:860px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)">

  <!-- Header -->
  <div style="background:#1e293b;color:#fff;padding:20px 28px">
    <h1 style="margin:0;font-size:20px;font-weight:700">📊 Alpaca Paper Account Summary</h1>
    <p style="margin:4px 0 0;color:#94a3b8;font-size:13px">{today_str}</p>
  </div>

  <!-- Stats row -->
  <div style="display:flex;gap:0;border-bottom:1px solid #e5e7eb">
    {"".join([
        f"<div style='flex:1;padding:18px 24px;border-right:1px solid #e5e7eb'>"
        f"<div style='font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#6b7280'>{label}</div>"
        f"<div style='font-size:22px;font-weight:700;color:{vc};margin-top:4px'>{val}</div>"
        "</div>"
        for label, val, vc in [
            ("Portfolio Value", f"${portfolio_val:,.2f}", "#1e293b"),
            ("Cash", f"${cash:,.2f}", "#1e293b"),
            ("Buying Power", f"${buying_power:,.2f}", "#1e293b"),
            ("Today's P&L", f"${today_pl:+,.2f} ({today_pl_pct:+.2f}%)", pl_color),
        ]
    ])}
  </div>

  <!-- Winners / Losers -->
  <div style="display:flex;gap:0;border-bottom:1px solid #e5e7eb">
    <div style="flex:1;padding:20px 24px;border-right:1px solid #e5e7eb">
      <h3 style="margin:0 0 12px;font-size:14px;color:#16a34a">🏆 Top 5 Winners</h3>
      {mini_table("Winners", winners, "#16a34a")}
    </div>
    <div style="flex:1;padding:20px 24px">
      <h3 style="margin:0 0 12px;font-size:14px;color:#dc2626">📉 Top 5 Losers</h3>
      {mini_table("Losers", losers, "#dc2626")}
    </div>
  </div>

  <!-- Notable -->
  <div style="padding:20px 24px;border-bottom:1px solid #e5e7eb">
    <h3 style="margin:0 0 10px;font-size:14px;color:#1e293b">⚡ Notable Today</h3>
    {notable_html}
  </div>

  <!-- All Positions -->
  <div style="padding:20px 24px;border-bottom:1px solid #e5e7eb">
    <h3 style="margin:0 0 12px;font-size:14px;color:#1e293b">All Positions ({len(positions)})</h3>
    <div style="overflow-x:auto">
      <table style="border-collapse:collapse;width:100%;font-size:13px">
        <tr style="background:#f9fafb;border-bottom:2px solid #e5e7eb">
          <th style="padding:8px 10px;text-align:left;color:#6b7280;font-weight:600">Symbol</th>
          <th style="padding:8px 10px;text-align:right;color:#6b7280;font-weight:600">Qty</th>
          <th style="padding:8px 10px;text-align:right;color:#6b7280;font-weight:600">Avg Entry</th>
          <th style="padding:8px 10px;text-align:right;color:#6b7280;font-weight:600">Cur Price</th>
          <th style="padding:8px 10px;text-align:right;color:#6b7280;font-weight:600">Mkt Value</th>
          <th style="padding:8px 10px;text-align:right;color:#6b7280;font-weight:600">Unr. P&L</th>
          <th style="padding:8px 10px;text-align:right;color:#6b7280;font-weight:600">Unr. P&L %</th>
          <th style="padding:8px 10px;text-align:right;color:#6b7280;font-weight:600">Today %</th>
        </tr>
        {all_rows}
      </table>
    </div>
  </div>

  <!-- Open Orders -->
  <div style="padding:20px 24px">
    <h3 style="margin:0 0 12px;font-size:14px;color:#1e293b">Open Orders ({len(orders)})</h3>
    <div style="overflow-x:auto">
      <table style="border-collapse:collapse;width:100%;font-size:13px">
        <tr style="background:#f9fafb;border-bottom:2px solid #e5e7eb">
          <th style="padding:8px 10px;text-align:left;color:#6b7280;font-weight:600">Symbol</th>
          <th style="padding:8px 10px;text-align:left;color:#6b7280;font-weight:600">Side</th>
          <th style="padding:8px 10px;text-align:right;color:#6b7280;font-weight:600">Qty</th>
          <th style="padding:8px 10px;text-align:left;color:#6b7280;font-weight:600">Type</th>
          <th style="padding:8px 10px;text-align:right;color:#6b7280;font-weight:600">Limit</th>
          <th style="padding:8px 10px;text-align:right;color:#6b7280;font-weight:600">Stop</th>
          <th style="padding:8px 10px;text-align:left;color:#6b7280;font-weight:600">TIF</th>
          <th style="padding:8px 10px;text-align:left;color:#6b7280;font-weight:600">Status</th>
        </tr>
        {order_rows}
      </table>
    </div>
  </div>

  <div style="padding:14px 24px;background:#f9fafb;border-top:1px solid #e5e7eb;font-size:11px;color:#9ca3af;text-align:center">
    Paper trading account — not real money · Generated by alpaca_daily_summary.py
  </div>
</div>
</body>
</html>"""
    return html


def main():
    today_str = date.today().strftime("%B %d, %Y")
    print(f"[{today_str}] Starting Alpaca daily summary...")

    # Load credentials
    alpaca = load_json(ALPACA_CREDS)
    email  = load_json(EMAIL_CREDS)

    api_key    = alpaca.get("api_key") or alpaca.get("APCA-API-KEY-ID")
    api_secret = alpaca.get("api_secret") or alpaca.get("APCA-API-SECRET-KEY")
    headers    = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}

    # Fetch data
    print("  Fetching account...")
    acct = alpaca_get("/account", headers=headers)

    print("  Fetching positions...")
    positions = alpaca_get("/positions", headers=headers)
    if not isinstance(positions, list):
        positions = []

    print("  Fetching open orders...")
    orders = alpaca_get("/orders", params={"status": "open", "limit": 50}, headers=headers)
    if not isinstance(orders, list):
        orders = []

    print(f"  Got {len(positions)} positions, {len(orders)} open orders.")

    # Build HTML
    html = build_html(acct, positions, orders, today_str)

    # Send email
    smtp_user = email["smtp_user"]
    smtp_pass = email["smtp_password"]
    to_addr   = email["to_address"]
    subject   = f"📊 Alpaca Paper Account Summary — {today_str}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = to_addr
    msg.attach(MIMEText(html, "html"))

    print(f"  Sending email to {to_addr}...")
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, to_addr, msg.as_string())

    print("  Done! Email sent.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
