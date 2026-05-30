#!/usr/bin/env python3
"""
Wheel Candidate Scan - Email Report
====================================
Reads the latest wheel scan results and emails them to Nathaniel.
Scheduled to run Monday morning after the scan completes.
"""

import json
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from datetime import date
from regime_detector import get_regime

BASE_DIR    = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config" / "email_config.json"
SCAN_FILE   = BASE_DIR / "logs" / "wheel_scan_latest.json"

with open(CONFIG_FILE) as f:
    cfg = json.load(f)

def build_email(results, scan_date):
    # ── Plain text version ────────────────────────────────────────────────────
    text_lines = [
        f"Wheel Candidate Scan — {scan_date}",
        "=" * 55,
        "",
        "RANKED BY ANNUALIZED YIELD:",
        "",
    ]
    for r in sorted(results, key=lambda x: -x["annual_yield"]):
        text_lines.append(
            f"  {r['symbol']:6s}  {r['annual_yield']:>5.1f}% ann  "
            f"${r['mid']:.2f}/sh credit  "
            f"${r['collateral']:,.0f} collateral  "
            f"{r['dte']}d to exp  "
            f"spread {r['spread_pct']:.0f}%"
        )
    text_lines += ["", "Full detail:", ""]
    for r in sorted(results, key=lambda x: -x["annual_yield"]):
        text_lines.append(
            f"  {r['symbol']}  stock=${r['stock_price']:.2f}  "
            f"strike=${r['strike']:.2f} ({r['otm_pct']:+.1f}%)  "
            f"exp {r['expiration']} ({r['dte']}d)  "
            f"bid=${r['bid']:.2f} ask=${r['ask']:.2f} mid=${r['mid']:.2f}"
        )
    text_lines += ["", "-- Nathaniel's Trading System"]

    # ── HTML version ──────────────────────────────────────────────────────────
    ranked = sorted(results, key=lambda x: -x["annual_yield"])

    rows = ""
    for r in ranked:
        color = "#e6f4ea" if r["annual_yield"] >= 30 else "#fff8e1" if r["annual_yield"] >= 20 else "#ffffff"
        rows += f"""
        <tr style="background:{color}">
            <td><strong>{r['symbol']}</strong></td>
            <td>${r['stock_price']:.2f}</td>
            <td>${r['strike']:.2f} ({r['otm_pct']:+.1f}%)</td>
            <td>{r['expiration']} ({r['dte']}d)</td>
            <td><strong>${r['mid']:.2f}</strong></td>
            <td>{r['spread_pct']:.0f}%</td>
            <td>${r['collateral']:,.0f}</td>
            <td><strong>{r['annual_yield']:.1f}%</strong></td>
        </tr>"""

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:800px;margin:auto;padding:20px">
    <h2 style="color:#1a237e">🎯 Wheel Strategy Candidate Scan</h2>
    <p style="color:#555">{scan_date} &nbsp;|&nbsp; 10% OTM Cash-Secured Puts &nbsp;|&nbsp; 14–35 DTE</p>
    <hr>

    <table style="border-collapse:collapse;width:100%;font-size:14px">
        <thead>
        <tr style="background:#1a237e;color:white;text-align:left">
            <th style="padding:8px">Symbol</th>
            <th style="padding:8px">Stock</th>
            <th style="padding:8px">Strike (OTM%)</th>
            <th style="padding:8px">Expiry (DTE)</th>
            <th style="padding:8px">Premium</th>
            <th style="padding:8px">Spread</th>
            <th style="padding:8px">Collateral</th>
            <th style="padding:8px">Ann. Yield</th>
        </tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>

    <br>
    <h3 style="color:#1a237e">Quick Guide</h3>
    <ul style="color:#333;font-size:14px">
        <li><strong>Ann. Yield</strong> — annualized return if the put expires worthless (higher = better)</li>
        <li><strong>Spread %</strong> — bid/ask spread as % of mid (lower = more liquid, target &lt;25%)</li>
        <li><strong>Collateral</strong> — cash reserved per contract (strike × 100)</li>
        <li><span style="background:#e6f4ea;padding:2px 6px">Green</span> = Ann. Yield ≥30% &nbsp;
            <span style="background:#fff8e1;padding:2px 6px">Yellow</span> = ≥20%</li>
    </ul>

    <p style="color:#888;font-size:12px;margin-top:30px">
        — Nathaniel's Trading System &nbsp;|&nbsp; Paper Account &nbsp;|&nbsp; Auto-generated at market open
    </p>
    </body></html>
    """
    return "\n".join(text_lines), html


def send():
    if not SCAN_FILE.exists():
        print("No scan results file found — scan may not have run yet.")
        sys.exit(1)

    data = json.load(open(SCAN_FILE))
    results   = data.get("results", [])
    scan_date = data.get("date", str(date.today()))

    # Get current market regime
    regime_data = get_regime(verbose=False)
    regime      = regime_data["regime"]

    regime_icon  = {"BULL": "🟢", "CAUTION": "🟡", "BEAR": "🔴"}.get(regime, "⚪")
    regime_note  = (f"Market Regime: {regime_icon} {regime}  |  "
                    f"SPY ${regime_data['spy_price']:.2f} vs SMA200 ${regime_data['sma200']:.2f} "
                    f"({regime_data['pct_vs_200']:+.1f}%)")

    if regime == "BEAR":
        regime_action = "⛔ BEAR market — wheel is PAUSED. Do not sell new puts until SPY recovers above its 200-day SMA."
    elif regime == "CAUTION":
        regime_action = "⚠️ CAUTION zone — sell at HALF contracts until SPY clears the 200-day SMA."
    else:
        regime_action = "✅ BULL market — proceed normally with full contracts."

    if not results:
        print("Scan file exists but no results — options may have been illiquid.")
        subject = f"Wheel Scan {scan_date} — No Liquid Quotes Found"
        body    = f"{regime_note}\n{regime_action}\n\nNo liquid options quotes found. Markets may still be opening — try checking manually after 10 AM."
        html    = f"<html><body><p>{regime_note}</p><p>{regime_action}</p><p>No liquid options quotes found. Markets may still be opening.</p></body></html>"
    else:
        subject = f"🎯 Wheel Scan {scan_date} — {len(results)} Candidates  |  {regime_icon} {regime}"
        body, html = build_email(results, scan_date)
        # Inject regime banner into HTML
        regime_banner = f"""
        <div style="padding:12px;border-radius:6px;margin-bottom:20px;
             background:{'#e8f5e9' if regime=='BULL' else '#fff8e1' if regime=='CAUTION' else '#ffebee'}">
            <strong>{regime_note}</strong><br>{regime_action}
        </div>"""
        html = html.replace("<h2", regime_banner + "<h2", 1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["sender_email"]
    msg["To"]      = cfg["recipient"]
    msg.attach(MIMEText(body, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
        server.starttls()
        server.login(cfg["sender_email"], cfg["app_password"])
        server.sendmail(cfg["sender_email"], cfg["recipient"], msg.as_string())

    print(f"Email sent to {cfg['recipient']} — {len(results)} candidates")


if __name__ == "__main__":
    send()
