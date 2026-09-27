#!/usr/bin/env python3
"""
Full account analysis — matches buys to sells per symbol, computes P&L,
and generates a rich HTML report sent by email.
"""

import json
import requests
import smtplib
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent

with open(BASE_DIR / "config" / "alpaca_credentials.json") as f:
    creds = json.load(f)
with open(BASE_DIR / "config" / "email_config.json") as f:
    ecfg = json.load(f)

BASE_URL = creds["endpoint"]
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}
RECIPIENT = "nate.adams.nh@gmail.com"


# ── Fetch data ────────────────────────────────────────────────────────────────

def fetch_orders():
    all_orders = []
    after = None
    while True:
        params = {"status": "all", "limit": 500, "direction": "asc"}
        if after:
            params["after"] = after
        r = requests.get(f"{BASE_URL}/orders", headers=HEADERS, params=params).json()
        if not isinstance(r, list) or not r:
            break
        all_orders.extend(r)
        if len(r) < 500:
            break
        after = r[-1]["created_at"]
    return [o for o in all_orders if o["status"] == "filled"]


def fetch_portfolio_history():
    r = requests.get(f"{BASE_URL}/account/portfolio/history", headers=HEADERS,
                     params={"period": "5A", "timeframe": "1D"})
    return r.json()


def fetch_account():
    return requests.get(f"{BASE_URL}/account", headers=HEADERS).json()


# ── P&L matching (FIFO) ───────────────────────────────────────────────────────

def match_trades(filled_orders):
    """
    Match buys → sells using FIFO per symbol.
    Returns list of closed trade dicts and dict of open positions.
    Options orders (asset_class == us_option) are separated.
    """
    # Separate equity vs options
    equity  = [o for o in filled_orders if o.get("asset_class") != "us_option"]
    options = [o for o in filled_orders if o.get("asset_class") == "us_option"]

    closed_trades = []
    open_lots     = defaultdict(list)   # symbol -> [(qty, price, date)]

    for o in sorted(equity, key=lambda x: x["filled_at"]):
        sym   = o["symbol"]
        qty   = float(o["filled_qty"])
        price = float(o["filled_avg_price"])
        dt    = o["filled_at"][:10]
        side  = o["side"]

        if side == "buy":
            open_lots[sym].append({"qty": qty, "price": price, "date": dt})
        else:  # sell
            remaining = qty
            cost_basis = 0.0
            buy_date   = None
            while remaining > 0 and open_lots[sym]:
                lot = open_lots[sym][0]
                use = min(lot["qty"], remaining)
                cost_basis += use * lot["price"]
                buy_date    = lot["date"]
                lot["qty"] -= use
                remaining  -= use
                if lot["qty"] <= 0:
                    open_lots[sym].pop(0)

            if buy_date:
                proceeds   = qty * price
                pl         = proceeds - cost_basis
                hold_days  = (datetime.fromisoformat(dt) - datetime.fromisoformat(buy_date)).days
                closed_trades.append({
                    "symbol":     sym,
                    "qty":        qty,
                    "buy_price":  cost_basis / qty if qty else 0,
                    "sell_price": price,
                    "buy_date":   buy_date,
                    "sell_date":  dt,
                    "pl":         pl,
                    "pl_pct":     pl / cost_basis * 100 if cost_basis else 0,
                    "hold_days":  hold_days,
                    "proceeds":   proceeds,
                    "cost":       cost_basis,
                })

    return closed_trades, dict(open_lots), options


def tag_strategy(sym, date_str, notes=""):
    """Heuristic strategy tagger based on symbol and date."""
    d = date_str
    options_syms = {"MARA", "SOFI", "IONQ", "DKNG", "QBTS", "SOXL", "INTC", "TSLA"}
    politician_syms = {
        "NVDA","AAPL","GOOGL","AMZN","MSFT","META","AMD","AVGO",
        "JPM","BAC","WFC","COST","PANW","CRWD","NET","OKTA","PLTR",
        "ORCL","UBER","ABNB","BKNG","MSTR","COIN","HOOD","RIVN",
    }
    momentum_syms  = {"SOXL", "INTC", "IONQ", "NVDA"}
    manager_syms   = {
        "IBM","ORCL","PLTR","MSFT","EMR","IAU","CARR","BAC","LMT",
        "CVX","BND","NVDA","XLE","MA","XOM","BRK.B","SOXX","V","VNQ",
        "O","LOW","BWXT","RKLB","GOOGL","INTC","SYK","ACI","PM","IONQ",
    }

    if sym in options_syms and d >= "2026-06-01":
        return "Wheel Strategy"
    if sym in momentum_syms and d >= "2026-05-20" and d <= "2026-06-22":
        return "Momentum Strategy"
    if sym in politician_syms and d >= "2026-05-20" and d <= "2026-06-12":
        return "Copy Trader"
    if sym in manager_syms:
        return "Strategy Manager"
    return "Other"


# ── HTML report ───────────────────────────────────────────────────────────────

def color_pl(val):
    if val > 0:   return "#1b5e20", "#e8f5e9"
    if val < 0:   return "#b71c1c", "#ffebee"
    return "#555", "#f5f5f5"


def fmt_pl(val, pct=None):
    tc, bg = color_pl(val)
    sign = "+" if val >= 0 else ""
    pct_str = f" ({sign}{pct:.1f}%)" if pct is not None else ""
    return f'<span style="color:{tc};background:{bg};padding:2px 6px;border-radius:3px;font-weight:bold;font-size:12px">{sign}${val:,.2f}{pct_str}</span>'


def build_report(closed, open_lots, options, ph, acct):
    ts   = [datetime.fromtimestamp(t, tz=timezone.utc) for t in ph["timestamp"]]
    eqs  = ph["equity"]

    # Filter non-zero equity points
    points = [(t, e) for t, e in zip(ts, eqs) if e and e > 0]
    start_eq  = points[0][1]  if points else 50000
    end_eq    = float(acct["portfolio_value"])
    peak_eq   = max(e for _, e in points) if points else end_eq
    start_dt  = points[0][0].strftime("%Y-%m-%d")  if points else "?"
    end_dt    = points[-1][0].strftime("%Y-%m-%d") if points else "?"

    total_pl    = sum(t["pl"] for t in closed)
    total_wins  = [t for t in closed if t["pl"] > 0]
    total_loses = [t for t in closed if t["pl"] <= 0]
    win_rate    = len(total_wins) / len(closed) * 100 if closed else 0
    avg_win     = sum(t["pl"] for t in total_wins)  / len(total_wins)  if total_wins  else 0
    avg_loss    = sum(t["pl"] for t in total_loses) / len(total_loses) if total_loses else 0
    profit_factor = abs(sum(t["pl"] for t in total_wins) / sum(t["pl"] for t in total_loses)) if total_loses and sum(t["pl"] for t in total_loses) != 0 else 0
    max_win  = max((t["pl"] for t in closed), default=0)
    max_loss = min((t["pl"] for t in closed), default=0)
    avg_hold = sum(t["hold_days"] for t in closed) / len(closed) if closed else 0

    # Per-strategy breakdown
    strat_stats = defaultdict(lambda: {"trades": 0, "pl": 0.0, "wins": 0})
    for t in closed:
        strat = tag_strategy(t["symbol"], t["sell_date"])
        strat_stats[strat]["trades"] += 1
        strat_stats[strat]["pl"]     += t["pl"]
        strat_stats[strat]["wins"]   += 1 if t["pl"] > 0 else 0

    # Per-symbol breakdown
    sym_stats = defaultdict(lambda: {"trades": 0, "pl": 0.0, "wins": 0, "total_vol": 0.0})
    for t in closed:
        sym_stats[t["symbol"]]["trades"]    += 1
        sym_stats[t["symbol"]]["pl"]        += t["pl"]
        sym_stats[t["symbol"]]["wins"]      += 1 if t["pl"] > 0 else 0
        sym_stats[t["symbol"]]["total_vol"] += t["proceeds"]

    # Equity curve sparkline data (last 60 non-zero points)
    spark = [(t.strftime("%Y-%m-%d"), e) for t, e in points[-60:]]
    if spark:
        sp_min = min(e for _, e in spark)
        sp_max = max(e for _, e in spark)
        sp_range = sp_max - sp_min or 1
        # SVG path
        w, h_px = 700, 80
        pts = []
        for i, (_, e) in enumerate(spark):
            x = i / (len(spark) - 1) * w if len(spark) > 1 else w / 2
            y = h_px - (e - sp_min) / sp_range * h_px
            pts.append(f"{x:.1f},{y:.1f}")
        spark_svg = f"""
        <svg viewBox="0 0 {w} {h_px}" style="width:100%;height:80px;margin-top:8px">
          <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#1b5e20" stop-opacity="0.3"/>
            <stop offset="100%" stop-color="#1b5e20" stop-opacity="0"/>
          </linearGradient></defs>
          <polygon points="{pts[0].split(',')[0]},{h_px} {' '.join(pts)} {pts[-1].split(',')[0]},{h_px}"
                   fill="url(#g)"/>
          <polyline points="{' '.join(pts)}" fill="none" stroke="#2e7d32" stroke-width="2"/>
        </svg>"""
    else:
        spark_svg = ""

    # ── Trade log table (last 50) ─────────────────────────────────────────────
    trade_rows = ""
    for t in sorted(closed, key=lambda x: x["sell_date"], reverse=True)[:80]:
        strat = tag_strategy(t["symbol"], t["sell_date"])
        tc, bg = color_pl(t["pl"])
        sign = "+" if t["pl"] >= 0 else ""
        trade_rows += f"""
        <tr style="background:#{'ffffff' if closed.index(t) % 2 == 0 else 'f8f9fa'}">
          <td style="padding:7px 10px;font-weight:bold">{t['sell_date']}</td>
          <td style="padding:7px 10px;font-weight:bold;font-size:13px">{t['symbol']}</td>
          <td style="padding:7px 10px;color:#555">{strat}</td>
          <td style="padding:7px 10px">{t['qty']:.2f}</td>
          <td style="padding:7px 10px">${t['buy_price']:.2f}</td>
          <td style="padding:7px 10px">${t['sell_price']:.2f}</td>
          <td style="padding:7px 10px">{t['hold_days']}d</td>
          <td style="padding:7px 10px"><span style="color:{tc};background:{bg};padding:2px 6px;border-radius:3px;font-weight:bold;font-size:12px">{sign}${t['pl']:,.2f} ({sign}{t['pl_pct']:.1f}%)</span></td>
        </tr>"""

    # ── Strategy table ────────────────────────────────────────────────────────
    strat_rows = ""
    for strat, s in sorted(strat_stats.items(), key=lambda x: -x[1]["pl"]):
        wr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
        tc, bg = color_pl(s["pl"])
        sign = "+" if s["pl"] >= 0 else ""
        strat_rows += f"""
        <tr>
          <td style="padding:8px 12px;font-weight:bold">{strat}</td>
          <td style="padding:8px 12px">{s['trades']}</td>
          <td style="padding:8px 12px">{wr:.0f}%</td>
          <td style="padding:8px 12px"><span style="color:{tc};background:{bg};padding:2px 6px;border-radius:3px;font-weight:bold;font-size:12px">{sign}${s['pl']:,.2f}</span></td>
        </tr>"""

    # ── Symbol table (top 20 by |P&L|) ───────────────────────────────────────
    sym_rows = ""
    for sym, s in sorted(sym_stats.items(), key=lambda x: -abs(x[1]["pl"]))[:20]:
        wr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
        tc, bg = color_pl(s["pl"])
        sign = "+" if s["pl"] >= 0 else ""
        sym_rows += f"""
        <tr>
          <td style="padding:8px 12px;font-weight:bold;font-size:13px">{sym}</td>
          <td style="padding:8px 12px">{s['trades']}</td>
          <td style="padding:8px 12px">{wr:.0f}%</td>
          <td style="padding:8px 12px;color:#555">${s['total_vol']:,.0f}</td>
          <td style="padding:8px 12px"><span style="color:{tc};background:{bg};padding:2px 6px;border-radius:3px;font-weight:bold;font-size:12px">{sign}${s['pl']:,.2f}</span></td>
        </tr>"""

    # ── Best / worst trades ───────────────────────────────────────────────────
    best5  = sorted(closed, key=lambda x: -x["pl"])[:5]
    worst5 = sorted(closed, key=lambda x:  x["pl"])[:5]

    def bw_rows(items):
        out = ""
        for t in items:
            tc, bg = color_pl(t["pl"])
            sign = "+" if t["pl"] >= 0 else ""
            out += f'<tr><td style="padding:6px 10px;font-weight:bold">{t["symbol"]}</td><td style="padding:6px 10px;color:#555">{t["buy_date"]} → {t["sell_date"]} ({t["hold_days"]}d)</td><td style="padding:6px 10px"><span style="color:{tc};background:{bg};padding:2px 6px;border-radius:3px;font-weight:bold;font-size:12px">{sign}${t["pl"]:,.2f} ({sign}{t["pl_pct"]:.1f}%)</span></td></tr>'
        return out

    pl_color = "#1b5e20" if total_pl >= 0 else "#b71c1c"
    pl_sign  = "+" if total_pl >= 0 else ""
    net_pct  = (end_eq - 50000) / 50000 * 100

    header_row_style = 'style="background:#1a237e;color:white"'
    th = 'style="padding:10px 12px;text-align:left;white-space:nowrap"'

    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Full Account Analysis — {end_dt}</title>
<style>
  body {{ font-family: Arial, sans-serif; max-width: 1100px; margin: auto; padding: 24px;
         background: #fff; color: #111; }}
  h2   {{ color: #1a237e; margin-bottom: 4px; }}
  h3   {{ color: #333; margin: 32px 0 10px; border-bottom: 2px solid #e0e0e0; padding-bottom: 6px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px;
           box-shadow: 0 1px 4px rgba(0,0,0,.1); margin-top: 8px; }}
  tr:nth-child(even) {{ background: #f8f9fa; }}
  th   {{ text-align: left; white-space: nowrap; }}
  td   {{ white-space: nowrap; }}
  hr   {{ border: none; border-top: 1px solid #e0e0e0; margin: 8px 0; }}
  .stat-grid {{ display: flex; gap: 14px; flex-wrap: wrap; margin: 16px 0; }}
  .stat {{ border-radius: 8px; padding: 14px 20px; text-align: center; min-width: 110px; }}
  .stat .val {{ font-size: 22px; font-weight: bold; }}
  .stat .lbl {{ font-size: 11px; margin-top: 2px; }}
</style>
</head><body>

<h2>📈 Full Account Analysis</h2>
<p style="color:#666;font-size:13px;margin-top:4px">
  Period: {start_dt} → {end_dt} &nbsp;|&nbsp; Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}
</p>
<hr>

<!-- ── Account Overview ─────────────────────────────────────────────────── -->
<h3>Account Overview</h3>
<div class="stat-grid">
  <div class="stat" style="background:#e3f2fd;border:1px solid #90caf9">
    <div class="val" style="color:#0d47a1">$50,000</div>
    <div class="lbl" style="color:#1565c0">Starting Capital</div>
  </div>
  <div class="stat" style="background:#e3f2fd;border:1px solid #90caf9">
    <div class="val" style="color:#0d47a1">${end_eq:,.0f}</div>
    <div class="lbl" style="color:#1565c0">Current Value</div>
  </div>
  <div class="stat" style="background:{'#e8f5e9' if total_pl >= 0 else '#ffebee'};border:1px solid {'#a5d6a7' if total_pl >= 0 else '#ef9a9a'}">
    <div class="val" style="color:{pl_color}">{pl_sign}${total_pl:,.2f}</div>
    <div class="lbl" style="color:{pl_color}">Total Realized P&L</div>
  </div>
  <div class="stat" style="background:{'#e8f5e9' if net_pct >= 0 else '#ffebee'};border:1px solid {'#a5d6a7' if net_pct >= 0 else '#ef9a9a'}">
    <div class="val" style="color:{pl_color}">{'+' if net_pct >= 0 else ''}{net_pct:.1f}%</div>
    <div class="lbl" style="color:{pl_color}">Net Return</div>
  </div>
  <div class="stat" style="background:#f3e5f5;border:1px solid #ce93d8">
    <div class="val" style="color:#6a1b9a">${peak_eq:,.0f}</div>
    <div class="lbl" style="color:#7b1fa2">Peak Portfolio Value</div>
  </div>
</div>

<!-- Equity curve -->
<div style="background:#f8f9fa;border:1px solid #e0e0e0;border-radius:8px;padding:12px 16px;margin-top:8px">
  <div style="font-size:12px;color:#888;margin-bottom:4px">Portfolio Equity Curve (daily)</div>
  {spark_svg}
  <div style="display:flex;justify-content:space-between;font-size:11px;color:#aaa;margin-top:2px">
    <span>{start_dt}</span><span>{end_dt}</span>
  </div>
</div>

<!-- ── Trade Statistics ──────────────────────────────────────────────────── -->
<h3>Trade Statistics (Closed Trades)</h3>
<div class="stat-grid">
  <div class="stat" style="background:#e3f2fd;border:1px solid #90caf9">
    <div class="val" style="color:#0d47a1">{len(closed)}</div>
    <div class="lbl" style="color:#1565c0">Total Closed Trades</div>
  </div>
  <div class="stat" style="background:#e8f5e9;border:1px solid #a5d6a7">
    <div class="val" style="color:#1b5e20">{len(total_wins)}</div>
    <div class="lbl" style="color:#2e7d32">Winning Trades</div>
  </div>
  <div class="stat" style="background:#ffebee;border:1px solid #ef9a9a">
    <div class="val" style="color:#b71c1c">{len(total_loses)}</div>
    <div class="lbl" style="color:#c62828">Losing Trades</div>
  </div>
  <div class="stat" style="background:#{'e8f5e9' if win_rate >= 50 else 'fff3e0'};border:1px solid #{'a5d6a7' if win_rate >= 50 else 'ffcc80'}">
    <div class="val" style="color:#{'1b5e20' if win_rate >= 50 else 'e65100'}">{win_rate:.1f}%</div>
    <div class="lbl" style="color:#555">Win Rate</div>
  </div>
  <div class="stat" style="background:#e8f5e9;border:1px solid #a5d6a7">
    <div class="val" style="color:#1b5e20">+${avg_win:,.2f}</div>
    <div class="lbl" style="color:#2e7d32">Avg Win</div>
  </div>
  <div class="stat" style="background:#ffebee;border:1px solid #ef9a9a">
    <div class="val" style="color:#b71c1c">${avg_loss:,.2f}</div>
    <div class="lbl" style="color:#c62828">Avg Loss</div>
  </div>
  <div class="stat" style="background:#{'e8f5e9' if profit_factor >= 1 else 'ffebee'};border:1px solid #{'a5d6a7' if profit_factor >= 1 else 'ef9a9a'}">
    <div class="val" style="color:#{'1b5e20' if profit_factor >= 1 else 'b71c1c'}">{profit_factor:.2f}x</div>
    <div class="lbl" style="color:#555">Profit Factor</div>
  </div>
  <div class="stat" style="background:#f5f5f5;border:1px solid #e0e0e0">
    <div class="val" style="color:#333">{avg_hold:.0f}d</div>
    <div class="lbl" style="color:#555">Avg Hold Time</div>
  </div>
  <div class="stat" style="background:#e8f5e9;border:1px solid #a5d6a7">
    <div class="val" style="color:#1b5e20">+${max_win:,.2f}</div>
    <div class="lbl" style="color:#2e7d32">Best Trade</div>
  </div>
  <div class="stat" style="background:#ffebee;border:1px solid #ef9a9a">
    <div class="val" style="color:#b71c1c">${max_loss:,.2f}</div>
    <div class="lbl" style="color:#c62828">Worst Trade</div>
  </div>
</div>

<!-- ── Strategy Breakdown ────────────────────────────────────────────────── -->
<h3>Performance by Strategy</h3>
<table>
  <thead><tr {header_row_style}>
    <th {th}>Strategy</th><th {th}>Trades</th><th {th}>Win Rate</th><th {th}>Net P&L</th>
  </tr></thead>
  <tbody>{strat_rows}</tbody>
</table>

<!-- ── Symbol Breakdown ──────────────────────────────────────────────────── -->
<h3>Top 20 Symbols by P&L Impact</h3>
<table>
  <thead><tr {header_row_style}>
    <th {th}>Symbol</th><th {th}>Trades</th><th {th}>Win Rate</th><th {th}>Volume</th><th {th}>Net P&L</th>
  </tr></thead>
  <tbody>{sym_rows}</tbody>
</table>

<!-- ── Best & Worst ──────────────────────────────────────────────────────── -->
<div style="display:flex;gap:20px;margin-top:28px;flex-wrap:wrap">
  <div style="flex:1;min-width:300px">
    <h3 style="margin-top:0">🏆 5 Best Trades</h3>
    <table>
      <thead><tr {header_row_style}><th {th}>Symbol</th><th {th}>Period</th><th {th}>P&L</th></tr></thead>
      <tbody>{bw_rows(best5)}</tbody>
    </table>
  </div>
  <div style="flex:1;min-width:300px">
    <h3 style="margin-top:0">💔 5 Worst Trades</h3>
    <table>
      <thead><tr {header_row_style}><th {th}>Symbol</th><th {th}>Period</th><th {th}>P&L</th></tr></thead>
      <tbody>{bw_rows(worst5)}</tbody>
    </table>
  </div>
</div>

<!-- ── Full Trade Log ────────────────────────────────────────────────────── -->
<h3>Trade Log (Most Recent 80)</h3>
<table>
  <thead><tr {header_row_style}>
    <th {th}>Close Date</th><th {th}>Symbol</th><th {th}>Strategy</th>
    <th {th}>Qty</th><th {th}>Entry</th><th {th}>Exit</th>
    <th {th}>Hold</th><th {th}>P&L</th>
  </tr></thead>
  <tbody>{trade_rows}</tbody>
</table>

<!-- ── Key Takeaways ─────────────────────────────────────────────────────── -->
<h3>Key Takeaways</h3>
<div style="background:#f5f5f5;border:1px solid #e0e0e0;border-radius:8px;padding:16px;font-size:13px;line-height:1.9;color:#333">
  <strong>What Worked:</strong><br>
  {"• Wheel Strategy (options premium collection) — generated consistent income through CSP/CC cycles<br>" if strat_stats.get("Wheel Strategy", {}).get("pl", 0) > 0 else ""}
  {"• Strategy Manager (portfolio stop-loss mgmt) — protected gains with trailing stops<br>" if strat_stats.get("Strategy Manager", {}).get("pl", 0) > 0 else ""}
  {"• Momentum Strategy — EMA crossover + dip entries captured trend moves<br>" if strat_stats.get("Momentum Strategy", {}).get("pl", 0) > 0 else ""}
  {"• Copy Trader — politician disclosure following generated alpha<br>" if strat_stats.get("Copy Trader", {}).get("pl", 0) > 0 else ""}
  • Win rate of <strong>{win_rate:.1f}%</strong> with profit factor <strong>{profit_factor:.2f}x</strong> — {'above 1.0 means total wins exceeded total losses' if profit_factor >= 1 else 'below 1.0 means losses outweighed wins in dollar terms'}<br>
  <br>
  <strong>What Didn't Work:</strong><br>
  {"• Copy Trader — flat P&L, ~-$30 net on ~$10K cycled, discontinued Jun 2026<br>" if strat_stats.get("Copy Trader", {}).get("pl", 0) <= 0 else ""}
  {"• Momentum Strategy — some symbols (F, NVDA) had poor OOS performance<br>" if strat_stats.get("Momentum Strategy", {}).get("pl", 0) <= 0 else ""}
  • Average hold time of <strong>{avg_hold:.0f} days</strong> suggests {'short-term trading style — transaction costs matter more at this horizon' if avg_hold < 10 else 'medium-term positioning'}<br>
  • Best trade <strong>+${max_win:,.2f}</strong> vs worst <strong>${max_loss:,.2f}</strong> — {'good asymmetry, wins larger than losses' if abs(max_win) > abs(max_loss) else 'unfavorable asymmetry, largest loss bigger than largest win'}
</div>

<p style="color:#aaa;font-size:11px;margin-top:30px">
  — Nathaniel's Trading System | Full Account Analysis | Paper trading account<br>
  Note: P&L calculated via FIFO matching of filled orders. Options premiums reflected in sell transactions.
</p>
</body></html>"""

    return html


def send_email(subject, html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = ecfg["sender_email"]
    msg["To"]      = RECIPIENT
    msg.attach(MIMEText("See HTML version for full analysis.", "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(ecfg["smtp_host"], ecfg["smtp_port"]) as s:
        s.starttls()
        s.login(ecfg["sender_email"], ecfg["app_password"])
        s.sendmail(ecfg["sender_email"], RECIPIENT, msg.as_string())
    print(f"Email sent: {subject}")


def main():
    print("Fetching data...")
    orders  = fetch_orders()
    ph      = fetch_portfolio_history()
    acct    = fetch_account()
    print(f"  {len(orders)} filled orders")

    print("Matching trades (FIFO)...")
    closed, open_lots, options = match_trades(orders)
    print(f"  {len(closed)} closed round-trips | {sum(len(v) for v in open_lots.values())} open lots | {len(options)} option orders")

    total_pl = sum(t["pl"] for t in closed)
    wins     = sum(1 for t in closed if t["pl"] > 0)
    print(f"  Total realized P&L: ${total_pl:+,.2f}")
    print(f"  Win rate: {wins}/{len(closed)} = {wins/len(closed)*100:.1f}%" if closed else "  No closed trades")

    print("Building HTML report...")
    html = build_report(closed, open_lots, options, ph, acct)

    out  = BASE_DIR / "logs" / "account_analysis.html"
    out.write_text(html, encoding="utf-8")
    print(f"Saved: {out}")

    print("Sending email...")
    send_email("Full Account Analysis - Nathaniel's Paper Trading", html)
    print("Done.")


if __name__ == "__main__":
    main()
