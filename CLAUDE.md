# Nathaniel's Trading Projects — Claude Memory

## Owner
- Name: Nathaniel
- Email: nate.adams.nh@gmail.com
- Platform: Windows (use `python`, not `python3`)

---

## Politician Copy Trader Bot

**Location:** `C:\Users\Nathaniel\Documents\Trading\politician-copy-trader\`

### Files
| File | Purpose |
|------|---------|
| `main.py` | Orchestrator — scrapes, queues, executes, notifies |
| `scraper.py` | Capitol Trades HTML scraper (BeautifulSoup) |
| `trader.py` | Alpaca API wrapper (buy, sell, price, market hours) |
| `notifier.py` | Windows balloon-tip notifications via PowerShell |
| `tracker.py` | P&L report generator |
| `config.json` | Politicians list, trade sizing, notification settings |
| `state.json` | Runtime state — seen keys, pending queue, executed history |

### How to Run
```
cd C:\Users\Nathaniel\Documents\Trading\politician-copy-trader
python main.py
```
Runs automatically via Windows Task Scheduler ("PoliticianCopyTrader") — hourly Mon-Fri 9:30 AM–6 PM.

### Alpaca Account
- Type: **Paper trading** (not live money)
- Credentials: `C:\Users\Nathaniel\Documents\Trading\config\alpaca_credentials.json`
- Endpoint: `https://paper-api.alpaca.markets/v2`
- API Key: PK47VMLQGKAOFSWZU3RPAMPL5M

### Configuration Highlights
- Trade amount: **$500/trade** (fixed, scale_by_size: false)
- 7 politicians tracked:
  - Warren Davidson (D000626) — +78.8% in 2025
  - Terri Sewell (S001185) — +67.9% in 2025
  - Bryan Steil (S001213) — +62.5% in 2025
  - Nick LaLota (L000598) — +61.6% in 2025
  - Nancy Pelosi (P000197)
  - Michael McCaul (M001157)
  - Ro Khanna (K000389)
- Skip keywords: treasury, t-bill, bond, note, bill, mutual fund, money market, etf, trust, index, cboe
- Gain milestone alerts: 5%, 10%, 25%, 50%, 100%
- Loss alert: -10%

### Key Technical Details
- Trade dedup key: `{politician_id}:{ticker}|{tx_date}|{tx_type}|{size_range}`
- Legacy Ro Khanna keys (without prefix) are still accepted
- Scraper tries `__NEXT_DATA__` JSON first, falls back to HTML table parsing
- Ticker regex: `([A-Z]{1,6}(?:[./][A-Z]{1,2})?):US`
- Alpaca symbol: ticker with `:US` removed, `/` replaced by `.` (e.g., BRK/B → BRK.B)
- Notional orders used; falls back to qty-based if 422 error

### Logs
`C:\Users\Nathaniel\Documents\Trading\logs\copy_trader.log`

---

## Momentum Strategy

**Location:** `C:\Users\Nathaniel\Documents\Trading\strategies\`
**Status: LIVE** — runs via Task Scheduler ("MomentumStrategy") every 30 min Mon-Fri 9:30 AM–6 PM

### Files
| File | Purpose |
|------|---------|
| `momentum_strategy.py` | Live trading loop — signals, orders, trail stop, kill-switch |
| `strategy_backtest.py` | Hardened Phase 2 backtest — walk-forward, slippage, regime detection |
| `strategy_state.json` | Runtime state (open positions, daily equity open) |

### Live Symbols (post walk-forward validation)
| Symbol | PF-Full | PF-OOS | Sharpe | Notes |
|--------|---------|--------|--------|-------|
| SOXL   | 2.44    | 1.71   | 1.42   | Leveraged semis ETF |
| INTC   | 2.20    | 2.52   | 1.02   | OOS better than IS — best signal |
| IONQ   | 1.66    | 1.94   | 0.88   | Quantum computing; 8 max consec losses |

**Removed:** F (in-sample PF 0.49 — lost money), NVDA (PF-OOS collapsed to 1.04)

### Parameters (final tuned)
- Entry: 9/21 EMA crossover + ≥5% dip from 30-day high
- Trail stop: lowest low of last 6 hourly bars
- Martingale: add 1.5× at -4% (lowered from 7% so it fires before trail stop)
- Kill-switch: -10% daily equity → close all
- Trade size: $500/trade

### Logs
`C:\Users\Nathaniel\Documents\Trading\logs\momentum_strategy.log`

---

## Email Config
- Location: `C:\Users\Nathaniel\Documents\Trading\config\email_config.json`
- SMTP: Gmail (smtp.gmail.com:587)
- **Status: FUNCTIONAL** — app_password set (tpvwyaxrngdekruz). Sends HTML email on copy-trader events.

---

## Options / Wheel Strategy

**Status: LIVE** — built 2026-05-26
**Script:** `C:\Users\Nathaniel\Documents\Trading\strategies\wheel_strategy.py`
**State:** `C:\Users\Nathaniel\Documents\Trading\config\wheel_state.json`
**Logs:** `C:\Users\Nathaniel\Documents\Trading\logs\wheel_strategy.log`

### How to Run
```
cd C:\Users\Nathaniel\Documents\Trading\strategies
python wheel_strategy.py [scan|execute|monitor] [SYMBOL ...]
```

### Modes
| Mode | Purpose |
|------|---------|
| `scan` | Scan default symbols for CSP/CC opportunities, ranked by annualized return |
| `execute` | Interactive — show scan, prompt to place limit order |
| `monitor` | Show live options positions, stock CC eligibility, expiry alerts, state P&L |

### Current Status (as of 2026-05-26)
- **QBTS** (100 shares) stopped out May 26 @ $27.68 — no longer held
- All 14 remaining positions are fractional → no covered-call candidates yet
- **Primary strategy: Cash-Secured Puts (CSPs)**
- Account cash available: **$42,881.57** | Options Level: **3**

### CSP Scan Parameters
- DTE window: 10–60 days (sweet spot ~30d for theta decay)
- OTM minimum: strike ≥3% below current price
- Spread filter: bid-ask spread ≤25% of mid (liquidity)
- Max contracts: 5 per position (capital limit)
- Default symbols: QBTS, IONQ, SOXL, INTC

### Wheel Strategy Flow
1. **Sell Cash-Secured Put** → collect premium; cash (strike × 100) reserved as collateral
2. **Not assigned** → keep full premium, repeat
3. **Assigned** → own 100 shares at effective cost = strike − premium collected
4. **Sell Covered Call** → collect premium on 100 shares held; repeat or get called away for profit

### Cheapest Path to 2nd Covered Call Position
- ACI (~$16/share) — need ~70 more shares ≈ $1,135 additional investment

### Alpaca Options API
- Snapshots: `https://data.alpaca.markets/v1beta1/options/snapshots/{SYMBOL}?feed=indicative&type=put`
- Order: `POST /v2/orders` — `{symbol, qty, side:"sell", type:"limit", time_in_force:"day", limit_price}`
- OCC parsing: `re.match(r'^([A-Z.]+)(\d{6})([CP])(\d{8})$', sym)` — strike = `int(strike_str) / 1000`

---

## Desktop Files
- `C:\Users\Nathaniel\Desktop\MCP_Trading_Servers.html`
- `C:\Users\Nathaniel\Desktop\Investment_Opportunities_2026.html`
- `C:\Users\Nathaniel\Desktop\TradingBackup_2026-05-23_15-00.zip`

---

## Auto-Memory Index
`C:\Users\Nathaniel\.claude\projects\C--Users-Nathaniel-Documents-Trading\memory\MEMORY.md`
