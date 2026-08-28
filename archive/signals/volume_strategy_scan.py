#!/usr/bin/env python3
"""
Volume Strategy Daily Scanner
==============================
Scans ~150 liquid stocks for volume-based trading setups each market close.

Signal types ranked by composite score:
  🚀 Volume Breakout  — RVOL > 2x AND close > 5-day high
  📈 Momentum Surge   — RVOL > 1.5x AND price up > 2%
  💪 Accumulation     — RVOL > 1.5x AND close in top 25% of day range
  ⚠️  Distribution    — RVOL > 1.5x AND price down > 2% (bearish warning)
  📊 Elevated Volume  — RVOL 1.3-1.5x with notable price action

Saves HTML to logs/volume_scan_{date}.html and emails to nate.adams.nh@gmail.com
Runs Mon-Fri at 4:15 PM ET via Task Scheduler.
"""

import json
import sys
import requests
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from signal_utils import get_logger, send_signal_email

BASE_DIR = Path(__file__).parent.parent

with open(BASE_DIR / "config" / "alpaca_credentials.json") as f:
    creds = json.load(f)
DATA_URL = "https://data.alpaca.markets/v2"
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}

log = get_logger("volume_scan", "volume_scan.log")

# ── Universe of ~150 liquid stocks to scan ────────────────────────────────────
UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "ORCL", "ASML",
    # Financials
    "JPM", "BAC", "GS", "MS", "WFC", "BLK", "V", "MA", "AXP", "COF",
    # Healthcare
    "UNH", "LLY", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "SYK", "BSX",
    # Industrials & Defense
    "LMT", "RTX", "NOC", "GD", "BA", "CAT", "DE", "HON", "EMR", "CARR",
    # Energy
    "XOM", "CVX", "COP", "EOG", "SLB", "MPC", "VLO", "PSX", "OXY", "HAL",
    # Consumer
    "AMZN", "WMT", "COST", "TGT", "HD", "LOW", "NKE", "SBUX", "MCD", "YUM",
    # Communication
    "GOOGL", "META", "NFLX", "DIS", "T", "VZ", "CMCSA", "CHTR", "SPOT", "SNAP",
    # Semiconductors
    "NVDA", "AMD", "INTC", "QCOM", "AVGO", "MU", "AMAT", "LRCX", "KLAC", "MRVL",
    # High-momentum / speculative
    "PLTR", "IONQ", "RKLB", "QBTS", "SOXL", "MSTR", "COIN", "MARA", "HOOD", "SOFI",
    "RIVN", "LCID", "SMCI", "OKTA", "CRWD", "PANW", "ZS", "DDOG", "SNOW", "NET",
    # REITs & Utilities
    "O", "VNQ", "AMT", "PLD", "EQIX", "DLR", "NEE", "DUK", "SO", "AEP",
    # Portfolio holdings
    "IBM", "BRK.B", "BWXT", "IAU", "ACI", "PM", "SOXX", "XLE",
    # ETFs with high volume (good for regime context)
    "SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLI", "XLU", "XLP",
    "ARKK", "ITA", "SMH", "GLD", "TLT",
]
# Deduplicate while preserving order
UNIVERSE = list(dict.fromkeys(UNIVERSE))

LOOKBACK_DAYS   = 75   # fetch enough bars for 50d avg + today
RVOL_MIN        = 1.3  # minimum 20d RVOL to appear in report
AVG_VOL_MIN     = 200_000  # filter out illiquid names


def get_bars(symbol: str) -> list:
    start = (date.today() - timedelta(days=LOOKBACK_DAYS + 20)).isoformat()
    try:
        r = requests.get(
            f"{DATA_URL}/stocks/{symbol}/bars",
            headers=HEADERS,
            params={"timeframe": "1Day", "start": start, "limit": LOOKBACK_DAYS + 20, "feed": "sip"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("bars", [])
    except Exception:
        pass
    return []


def classify_signal(rvol, pct_change, close, high, low, prev_5d_high,
                    above_avwap, prev_above_avwap):
    """Return (signal_label, emoji, score_bonus)."""
    in_top_25   = (close - low) / (high - low) >= 0.75 if high > low else False
    breakout    = close > prev_5d_high
    vwap_reclaim = above_avwap and not prev_above_avwap   # crossed above anchored VWAP today

    if rvol >= 1.5 and vwap_reclaim:
        return "VWAP Reclaim", "🎯", 3.5   # highest conviction — institutions re-entering
    if rvol >= 2.0 and breakout:
        return "Volume Breakout", "🚀", 3.0
    if rvol >= 1.5 and pct_change >= 2.0:
        return "Momentum Surge", "📈", 2.0
    if rvol >= 1.5 and in_top_25 and pct_change >= 0:
        return "Accumulation", "💪", 1.5
    if rvol >= 1.5 and pct_change <= -2.0:
        return "Distribution", "⚠️", 1.0   # bearish — still worth knowing
    return "Elevated Volume", "📊", 0.5


def scan_symbol(symbol: str) -> dict | None:
    bars = get_bars(symbol)
    if len(bars) < 22:
        return None

    today_bar  = bars[-1]
    prev_bars  = bars[:-1]

    today_vol   = float(today_bar.get("v", 0))
    today_open  = float(today_bar.get("o", 0))
    today_close = float(today_bar.get("c", 0))
    today_high  = float(today_bar.get("h", 0))
    today_low   = float(today_bar.get("l", 0))

    avg_vol_20 = sum(float(b.get("v", 0)) for b in prev_bars[-20:]) / 20
    if avg_vol_20 < AVG_VOL_MIN:
        return None

    rvol_20 = today_vol / avg_vol_20 if avg_vol_20 > 0 else 0
    if rvol_20 < RVOL_MIN:
        return None

    # 50-day average — only compute if we have enough bars
    bars_50 = prev_bars[-50:]
    avg_vol_50 = sum(float(b.get("v", 0)) for b in bars_50) / len(bars_50) if len(bars_50) >= 40 else None
    rvol_50 = today_vol / avg_vol_50 if avg_vol_50 else None

    # Dual-timeframe confirmation: elevated on BOTH 20d and 50d
    dual_confirmed = rvol_50 is not None and rvol_20 >= 1.5 and rvol_50 >= 1.5

    pct_change = (today_close - today_open) / today_open * 100 if today_open > 0 else 0
    prev_close = float(prev_bars[-1].get("c", today_open))
    pct_vs_prev = (today_close - prev_close) / prev_close * 100 if prev_close > 0 else 0

    # SMA20 trend
    sma20 = sum(float(b.get("c", 0)) for b in prev_bars[-20:]) / 20
    above_sma20 = today_close > sma20
    sma20_pct   = (today_close - sma20) / sma20 * 100

    # 5-day high for breakout detection
    prev_5d_high = max(float(b.get("h", 0)) for b in prev_bars[-5:])

    # Day range position (where close sits within today's range)
    range_pct = (today_close - today_low) / (today_high - today_low) * 100 if today_high > today_low else 50

    # ── VWAP calculations ────────────────────────────────────────────────────
    # 1. Intraday VWAP proxy: typical price = (H+L+C)/3
    #    If close > typical price, buyers dominated late in the day (above intraday VWAP)
    intraday_vwap_proxy = (today_high + today_low + today_close) / 3
    above_intraday_vwap = today_close >= intraday_vwap_proxy
    intraday_vwap_pct   = (today_close - intraday_vwap_proxy) / intraday_vwap_proxy * 100

    # 2. 20-day Anchored VWAP: sum(typical_price × volume) / sum(volume) over past 20 sessions
    #    Represents where the average buyer from the last month stands
    avwap_bars = prev_bars[-20:]
    avwap_num  = sum(
        ((float(b.get("h",0)) + float(b.get("l",0)) + float(b.get("c",0))) / 3) * float(b.get("v",0))
        for b in avwap_bars
    )
    avwap_den  = sum(float(b.get("v", 0)) for b in avwap_bars)
    anchored_vwap     = avwap_num / avwap_den if avwap_den > 0 else sma20
    above_avwap       = today_close > anchored_vwap
    avwap_pct         = (today_close - anchored_vwap) / anchored_vwap * 100

    # Was price above/below anchored VWAP yesterday? (for VWAP Reclaim detection)
    if len(prev_bars) >= 21:
        yest = prev_bars[-1]
        yest_close = float(yest.get("c", 0))
        prev_above_avwap = yest_close > anchored_vwap
    else:
        prev_above_avwap = above_avwap  # can't determine — no reclaim

    signal_label, emoji, score_bonus = classify_signal(
        rvol_20, pct_change, today_close, today_high, today_low, prev_5d_high,
        above_avwap, prev_above_avwap
    )

    # Composite score:
    #   base      = RVOL_20 × trend multiplier + signal bonus
    #   +1.5      if dual-confirmed (elevated on both 20d AND 50d)
    #   +0.5      if rvol_50 >= 2x only
    #   +0.8      if price above anchored VWAP (institutional support)
    #   +0.4      if price above intraday VWAP proxy (buyers dominated the day)
    trend_mult = 1.2 if above_sma20 else 0.8
    score = (rvol_20 * trend_mult) + score_bonus
    if dual_confirmed:
        score += 1.5
    elif rvol_50 is not None and rvol_50 >= 2.0:
        score += 0.5
    if above_avwap:
        score += 0.8
    if above_intraday_vwap:
        score += 0.4

    return {
        "symbol":             symbol,
        "close":              today_close,
        "pct_change":         pct_change,
        "pct_vs_prev":        pct_vs_prev,
        "today_vol":          today_vol,
        "avg_vol_20":         avg_vol_20,
        "avg_vol_50":         avg_vol_50,
        "rvol_20":            rvol_20,
        "rvol_50":            rvol_50,
        "dual_confirmed":     dual_confirmed,
        "sma20":              sma20,
        "above_sma20":        above_sma20,
        "sma20_pct":          sma20_pct,
        "range_pct":          range_pct,
        "anchored_vwap":      anchored_vwap,
        "above_avwap":        above_avwap,
        "avwap_pct":          avwap_pct,
        "intraday_vwap_proxy": intraday_vwap_proxy,
        "above_intraday_vwap": above_intraday_vwap,
        "intraday_vwap_pct":  intraday_vwap_pct,
        "signal":             signal_label,
        "emoji":              emoji,
        "score":              score,
        "breakout":           today_close > prev_5d_high,
    }


# ── HTML generation ───────────────────────────────────────────────────────────

def signal_badge(signal: str) -> str:
    styles = {
        "VWAP Reclaim":    ("background:#6a1b9a;color:#fff",       "🎯"),
        "Volume Breakout": ("background:#1b5e20;color:#fff",       "🚀"),
        "Momentum Surge":  ("background:#2e7d32;color:#fff",       "📈"),
        "Accumulation":    ("background:#01579b;color:#fff",       "💪"),
        "Distribution":    ("background:#b71c1c;color:#fff",       "⚠️"),
        "Elevated Volume": ("background:#e65100;color:#fff",       "📊"),
    }
    style, emoji = styles.get(signal, ("background:#555;color:#fff", ""))
    return f'<span style="{style};padding:3px 8px;border-radius:4px;font-size:12px;font-weight:bold;white-space:nowrap">{emoji} {signal}</span>'


def pct_cell(pct: float) -> str:
    if pct >= 3:    color = "#1b5e20"; bg = "#e8f5e9"
    elif pct >= 1:  color = "#2e7d32"; bg = "#f1f8e9"
    elif pct >= 0:  color = "#388e3c"; bg = "#f9fbe7"
    elif pct >= -1: color = "#e65100"; bg = "#fff3e0"
    elif pct >= -3: color = "#bf360c"; bg = "#fbe9e7"
    else:           color = "#b71c1c"; bg = "#ffebee"
    return f'<span style="color:{color};background:{bg};padding:2px 7px;border-radius:3px;font-weight:bold;font-size:12px">{pct:+.2f}%</span>'


def rvol_bar(rvol: float) -> str:
    pct = min(rvol / 5 * 100, 100)
    if rvol >= 2:     color = "#1b5e20"
    elif rvol >= 1.5: color = "#e65100"
    else:             color = "#555"
    return (
        f'<div style="display:flex;align-items:center;gap:6px">'
        f'<div style="background:#e0e0e0;border-radius:3px;width:70px;height:9px;overflow:hidden">'
        f'<div style="background:{color};width:{pct:.0f}%;height:100%;border-radius:3px"></div></div>'
        f'<span style="color:{color};font-weight:bold;font-size:12px">{rvol:.1f}x</span></div>'
    )


def rvol_50_cell(rvol_50) -> str:
    """Render the 50d RVOL cell — n/a if not enough history."""
    if rvol_50 is None:
        return '<span style="color:#bbb;font-size:12px">n/a</span>'
    if rvol_50 >= 2:     color = "#1b5e20"; bg = "#e8f5e9"
    elif rvol_50 >= 1.5: color = "#e65100"; bg = "#fff3e0"
    else:                color = "#555";    bg = "#f5f5f5"
    return f'<span style="color:{color};background:{bg};padding:2px 7px;border-radius:3px;font-weight:bold;font-size:12px">{rvol_50:.1f}x</span>'


def dual_badge(dual: bool, rvol_50) -> str:
    """Show a confirmation badge when both baselines are elevated."""
    if dual:
        return '<span style="background:#4a148c;color:#fff;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:bold">🔥 DUAL</span>'
    if rvol_50 is not None and rvol_50 >= 1.3:
        return '<span style="background:#e8f5e9;color:#2e7d32;padding:2px 7px;border-radius:4px;font-size:11px">partial</span>'
    return '<span style="color:#ccc;font-size:11px">—</span>'


def avwap_cell(avwap: float, close: float, avwap_pct: float, above: bool) -> str:
    """Anchored VWAP (20d) — price level and % distance."""
    if above:
        color = "#1b5e20"; bg = "#e8f5e9"; arrow = "▲"
    else:
        color = "#b71c1c"; bg = "#ffebee"; arrow = "▼"
    return (
        f'<div style="line-height:1.4">'
        f'<span style="color:{color};background:{bg};padding:2px 6px;border-radius:3px;'
        f'font-weight:bold;font-size:12px">{arrow} {avwap_pct:+.1f}%</span>'
        f'<br><span style="color:#888;font-size:11px">${avwap:.2f}</span>'
        f'</div>'
    )


def intraday_vwap_cell(above: bool, pct: float) -> str:
    """Intraday VWAP proxy — did price close above (H+L+C)/3?"""
    if above:
        return f'<span style="color:#1b5e20;font-size:12px;font-weight:bold">▲ {pct:+.2f}%</span>'
    else:
        return f'<span style="color:#b71c1c;font-size:12px;font-weight:bold">▼ {pct:+.2f}%</span>'


def build_html(results: list, scan_date: str, elapsed: float) -> str:
    bullish = [r for r in results if r["signal"] != "Distribution"]
    bearish = [r for r in results if r["signal"] == "Distribution"]

    def rows_html(items):
        out = ""
        for i, r in enumerate(items):
            bg    = "#ffffff" if i % 2 == 0 else "#f8f9fa"
            trend = "▲ Above" if r["above_sma20"] else "▼ Below"
            trend_col = "#2e7d32" if r["above_sma20"] else "#c62828"
            out += f"""
            <tr style="background:{bg}">
              <td style="padding:9px 12px;font-weight:bold;font-size:14px;color:#111;white-space:nowrap">{r['emoji']} {r['symbol']}</td>
              <td style="padding:9px 12px">{signal_badge(r['signal'])}</td>
              <td style="padding:9px 12px;font-weight:bold;color:#111">${r['close']:.2f}</td>
              <td style="padding:9px 12px">{pct_cell(r['pct_change'])}</td>
              <td style="padding:9px 12px">{pct_cell(r['pct_vs_prev'])}</td>
              <td style="padding:9px 12px">{rvol_bar(r['rvol_20'])}</td>
              <td style="padding:9px 12px">{rvol_50_cell(r['rvol_50'])}</td>
              <td style="padding:9px 12px">{dual_badge(r['dual_confirmed'], r['rvol_50'])}</td>
              <td style="padding:9px 12px;color:#444">{r['today_vol']/1e6:.1f}M</td>
              <td style="padding:9px 12px;color:#444">{r['avg_vol_20']/1e6:.1f}M</td>
              <td style="padding:9px 12px">{avwap_cell(r['anchored_vwap'], r['close'], r['avwap_pct'], r['above_avwap'])}</td>
              <td style="padding:9px 12px">{intraday_vwap_cell(r['above_intraday_vwap'], r['intraday_vwap_pct'])}</td>
              <td style="padding:9px 12px;color:{trend_col};font-weight:bold;font-size:12px">{trend} SMA20</td>
              <td style="padding:9px 12px;color:#444">{r['range_pct']:.0f}%</td>
              <td style="padding:9px 12px;color:#999;font-size:12px">{r['score']:.2f}</td>
            </tr>"""
        return out

    header_row = """
        <tr style="background:#1a237e;color:white">
          <th style="padding:10px 12px;text-align:left">Symbol</th>
          <th style="padding:10px 12px;text-align:left">Signal</th>
          <th style="padding:10px 12px;text-align:left">Close</th>
          <th style="padding:10px 12px;text-align:left">Day Chg</th>
          <th style="padding:10px 12px;text-align:left">vs Prev Close</th>
          <th style="padding:10px 12px;text-align:left">RVOL (20d)</th>
          <th style="padding:10px 12px;text-align:left">RVOL (50d)</th>
          <th style="padding:10px 12px;text-align:left">Confirm</th>
          <th style="padding:10px 12px;text-align:left">Vol Today</th>
          <th style="padding:10px 12px;text-align:left">Avg Vol (20d)</th>
          <th style="padding:10px 12px;text-align:left">AVWAP (20d)</th>
          <th style="padding:10px 12px;text-align:left">Intraday VWAP</th>
          <th style="padding:10px 12px;text-align:left">Trend</th>
          <th style="padding:10px 12px;text-align:left">Day Range %</th>
          <th style="padding:10px 12px;text-align:left">Score</th>
        </tr>"""

    bullish_table = f"""
        <table style="border-collapse:collapse;width:100%;font-size:13px;margin-top:10px;box-shadow:0 1px 4px rgba(0,0,0,.12)">
          <thead>{header_row}</thead>
          <tbody>{rows_html(bullish)}</tbody>
        </table>""" if bullish else "<p style='color:#666'>No bullish setups today.</p>"

    bearish_table = f"""
        <table style="border-collapse:collapse;width:100%;font-size:13px;margin-top:10px;box-shadow:0 1px 4px rgba(0,0,0,.12)">
          <thead>{header_row}</thead>
          <tbody>{rows_html(bearish)}</tbody>
        </table>""" if bearish else "<p style='color:#666'>No distribution signals today.</p>"

    nb, nd = len(bullish), len(bearish)
    breakouts    = sum(1 for r in results if r["breakout"])
    vwap_reclaims = sum(1 for r in results if r["signal"] == "VWAP Reclaim")
    above_avwap_ct = sum(1 for r in results if r["above_avwap"])

    stats_html = f"""
    <div style="display:flex;gap:16px;margin:16px 0;flex-wrap:wrap">
      <div style="background:#e8f5e9;border:1px solid #a5d6a7;border-radius:8px;padding:12px 24px;text-align:center">
        <div style="font-size:26px;color:#1b5e20;font-weight:bold">{nb}</div>
        <div style="color:#388e3c;font-size:12px">Bullish Signals</div>
      </div>
      <div style="background:#ffebee;border:1px solid #ef9a9a;border-radius:8px;padding:12px 24px;text-align:center">
        <div style="font-size:26px;color:#b71c1c;font-weight:bold">{nd}</div>
        <div style="color:#c62828;font-size:12px">Distribution Signals</div>
      </div>
      <div style="background:#f3e5f5;border:1px solid #ce93d8;border-radius:8px;padding:12px 24px;text-align:center">
        <div style="font-size:26px;color:#6a1b9a;font-weight:bold">{vwap_reclaims}</div>
        <div style="color:#7b1fa2;font-size:12px">VWAP Reclaims 🎯</div>
      </div>
      <div style="background:#fff3e0;border:1px solid #ffcc80;border-radius:8px;padding:12px 24px;text-align:center">
        <div style="font-size:26px;color:#e65100;font-weight:bold">{breakouts}</div>
        <div style="color:#bf360c;font-size:12px">Breakouts (5d high)</div>
      </div>
      <div style="background:#e8f5e9;border:1px solid #a5d6a7;border-radius:8px;padding:12px 24px;text-align:center">
        <div style="font-size:26px;color:#2e7d32;font-weight:bold">{above_avwap_ct}</div>
        <div style="color:#388e3c;font-size:12px">Above AVWAP (20d)</div>
      </div>
      <div style="background:#e3f2fd;border:1px solid #90caf9;border-radius:8px;padding:12px 24px;text-align:center">
        <div style="font-size:26px;color:#0d47a1;font-weight:bold">{len(UNIVERSE)}</div>
        <div style="color:#1565c0;font-size:12px">Symbols Scanned</div>
      </div>
    </div>"""

    legend = """
    <div style="margin-top:24px;padding:14px 16px;background:#f5f5f5;border:1px solid #e0e0e0;border-radius:8px;font-size:12px;color:#555;line-height:1.9">
      <strong style="color:#111">Signal Guide:</strong>&nbsp;
      🚀 <strong>Volume Breakout</strong> = RVOL(20d) ≥ 2x &amp; close above 5-day high &nbsp;|&nbsp;
      📈 <strong>Momentum Surge</strong> = RVOL(20d) ≥ 1.5x &amp; price up ≥ 2% &nbsp;|&nbsp;
      💪 <strong>Accumulation</strong> = RVOL(20d) ≥ 1.5x &amp; close in top 25% of day range &nbsp;|&nbsp;
      ⚠️ <strong>Distribution</strong> = RVOL(20d) ≥ 1.5x &amp; price down ≥ 2% &nbsp;|&nbsp;
      📊 <strong>Elevated Volume</strong> = RVOL(20d) 1.3–1.5x notable action<br>
      🎯 <strong>VWAP Reclaim</strong> = RVOL ≥ 1.5x &amp; price crossed back above 20d Anchored VWAP today — highest-conviction signal, institutions re-entering<br>
      <strong>RVOL (50d)</strong> = today's volume vs 50-day average — second baseline for conviction &nbsp;|&nbsp;
      <span style="background:#4a148c;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px;font-weight:bold">🔥 DUAL</span>
      = elevated on <em>both</em> 20d and 50d simultaneously &nbsp;|&nbsp;
      <span style="background:#e8f5e9;color:#2e7d32;padding:1px 6px;border-radius:3px;font-size:11px">partial</span>
      = RVOL(50d) ≥ 1.3x but not dual-confirmed<br>
      <strong>AVWAP (20d)</strong> = 20-day Anchored VWAP — volume-weighted average price over past 20 sessions.
      ▲ green = price above (longs profitable, institutional support level). ▼ red = price below (acts as overhead resistance).<br>
      <strong>Intraday VWAP</strong> = close vs (H+L+C)/3 typical price proxy.
      ▲ green = buyers dominated into the close. ▼ red = sellers had control at end of day.<br>
      <strong>Day Range %</strong> = where close sits in today's high-low range (100% = at the high, 0% = at the low) &nbsp;|&nbsp;
      <strong>Score</strong> = RVOL × trend multiplier + signal bonus + dual bonus + AVWAP/VWAP bonuses
    </div>"""

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Volume Strategy Scan — {scan_date}</title>
<style>
  body {{ font-family: Arial, sans-serif; max-width: 1200px; margin: auto;
         padding: 24px; background: #ffffff; color: #111; }}
  h2   {{ color: #1a237e; margin-bottom: 4px; }}
  h3   {{ color: #333; margin: 28px 0 8px; border-bottom: 2px solid #e0e0e0; padding-bottom: 6px; }}
  th   {{ text-align: left; white-space: nowrap; }}
  td   {{ white-space: nowrap; }}
  hr   {{ border: none; border-top: 1px solid #e0e0e0; }}
</style>
</head><body>
  <h2>📊 Volume Strategy Daily Scan</h2>
  <p style="color:#666;font-size:13px;margin-top:4px">
    {scan_date} &nbsp;|&nbsp; Generated {datetime.now().strftime('%H:%M ET')}
    &nbsp;|&nbsp; Scan completed in {elapsed:.1f}s
  </p>
  <hr>
  {stats_html}

  <h3>🟢 Bullish Setups ({nb})</h3>
  {bullish_table}

  <h3>🔴 Distribution / Bearish Signals ({nd})</h3>
  {bearish_table}

  {legend}
  <p style="color:#aaa;font-size:11px;margin-top:30px">
    — Nathaniel's Trading System | Volume Strategy Scanner | Paper trading account
  </p>
</body></html>"""


def run():
    t0 = datetime.now()
    today_str = date.today().isoformat()
    log.info("=" * 65)
    log.info(f"VOLUME STRATEGY SCAN  |  {today_str}")
    log.info(f"Universe: {len(UNIVERSE)} symbols")
    log.info("=" * 65)

    results = []
    for sym in UNIVERSE:
        try:
            r = scan_symbol(sym)
            if r:
                results.append(r)
                dual_tag = " 🔥DUAL" if r["dual_confirmed"] else ""
                r50 = f"{r['rvol_50']:.2f}x" if r["rvol_50"] else "  n/a"
                log.info(
                    f"  {sym:8s} | close=${r['close']:>8.2f} | chg={r['pct_change']:+5.2f}% "
                    f"| rvol20={r['rvol_20']:.2f}x | rvol50={r50} | {r['emoji']} {r['signal']}{dual_tag}"
                )
        except Exception as e:
            log.error(f"  {sym}: error — {e}")

    results.sort(key=lambda x: -x["score"])
    elapsed = (datetime.now() - t0).total_seconds()
    log.info(f"Scan complete: {len(results)} candidates found in {elapsed:.1f}s")

    # Save HTML report
    html_dir = BASE_DIR / "logs" / "volume_scans"
    html_dir.mkdir(parents=True, exist_ok=True)
    html_path = html_dir / f"volume_scan_{today_str}.html"
    html_body = build_html(results, today_str, elapsed)
    html_path.write_text(html_body, encoding="utf-8")
    log.info(f"HTML saved: {html_path}")

    # Build email
    nb = len([r for r in results if r["signal"] != "Distribution"])
    nd = len([r for r in results if r["signal"] == "Distribution"])
    breakouts = sum(1 for r in results if r["breakout"])

    top5 = [r for r in results if r["signal"] != "Distribution"][:5]
    top5_text = "\n".join(
        f"  {r['emoji']} {r['symbol']:8s}  {r['signal']:20s}  "
        f"RVOL20={r['rvol_20']:.1f}x  RVOL50={r['rvol_50']:.1f}x  {r['pct_change']:+.2f}%"
        for r in top5
    )

    plain = (
        f"Volume Strategy Scan — {today_str}\n"
        f"{'='*50}\n"
        f"Bullish signals: {nb}  |  Distribution: {nd}  |  Breakouts: {breakouts}\n\n"
        f"Top Bullish Candidates:\n{top5_text}\n\n"
        f"Full HTML report attached in body."
    )

    subject = (
        f"📊 Volume Scan {today_str} — "
        f"{nb} bullish, {nd} distribution, {breakouts} breakout{'s' if breakouts != 1 else ''}"
    )

    send_signal_email(subject, plain, html_body)
    log.info(f"Email sent: {subject}")
    log.info("Run complete.\n")


if __name__ == "__main__":
    run()
