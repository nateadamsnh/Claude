# Nathaniel's Trading Projects — Claude Memory

## Owner
- Name: Nathaniel
- Email: nate.adams.nh@gmail.com
- Platform: Windows (use `python`, not `python3`)
- Last updated: 2026-05-30

---

## Git Repository
- URL: https://github.com/nateadamsnh/Claude
- Local: `C:\Users\Nathaniel\Documents\Trading`
- Credentials and runtime state files are gitignored (never committed)

---

## Alpaca Account
- Type: **Paper trading** (not live money)
- Credentials: `C:\Users\Nathaniel\Documents\Trading\config\alpaca_credentials.json`
- Broker endpoint: `https://paper-api.alpaca.markets/v2`
- Data endpoint: `https://data.alpaca.markets`
- API Key: PK47VMLQGKAOFSWZU3RPAMPL5M
- **Portfolio value: $51,588.76** | **Cash: $7,276.24** (as of 2026-05-30)
- Options Level: 3

---

## Politician Copy Trader Bot

**Status: DISCONTINUED 2026-06-11** — removed at Nathaniel's request after flat performance (~-$30 net on ~$10K cycled through 24 trades). All 16 copy-trade positions liquidated at the 2026-06-12 open, "PoliticianCopyTrader" Task Scheduler task deleted. Code and state.json kept for records. The separate `signals/senate_disclosures.py` senator copy monitor was ALSO discontinued the same day (12 positions ~$3,893 liquidated, "SenateDisclosures" task deleted) — all congressional copy-trading is now shut down.

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

### Politicians Tracked (current — as of 2026-05-30)
| Name | ID | Party | Notes |
|------|----|-------|-------|
| Nancy Pelosi | P000197 | Democrat | 44 trades, $97.81M volume. NVDA, AAPL, GOOGL, AMZN focus. $3M+ documented gains, 133% on Broadcom options. |
| Cleo Fields | F000110 | Democrat | 222 trades, $22.74M volume. Pure Magnificent 7 — NVDA (44), GOOGL (26), AAPL (20), MSFT (16). IT sector 61%. Bought ORCL before Trump TikTok exec order. |
| Ro Khanna | K000389 | Democrat | 12,548 trades, $211.18M volume. Very active — JPM, AMZN, Meta, Micron, GOOGL. High signal volume, watch for noise. |

**Removed (inactive/left Congress):** Warren Davidson, Terri Sewell, Bryan Steil, Nick LaLota, Michael McCaul, Marjorie Taylor Greene (resigned Jan 5, 2026)

### Configuration Highlights
- Trade amount: **$500/trade** (fixed, scale_by_size: false)
- Skip keywords: treasury, t-bill, bond, note, bill, mutual fund, money market, etf, trust, index, xsp, mini spx, cboe
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

## Strategy Manager (Portfolio Stop-Loss)

**Script:** `C:\Users\Nathaniel\Documents\Trading\strategies\strategy_manager.py`
**Status: LIVE** — runs via Task Scheduler ("StrategyManager") every minute Mon-Fri 9:30 AM–6 PM
**State:** `C:\Users\Nathaniel\Documents\Trading\strategies\states\{SYMBOL}_state.json` (per symbol)

### What It Does
- Monitors 29 holdings with GTC stop-loss orders
- Trailing stop: raises floor once position gains ≥10% (trails 5% below highest price)
- Ladder buy: adds 2× position at -20% from entry price
- Kill-switch: -10% daily equity → close all positions

### Current Portfolio (as of 2026-05-30)
| Symbol | Qty | Entry | P&L |
|--------|-----|-------|-----|
| IBM | 2.00 | $249.58 | +19.3% |
| ORCL | 2.64 | $189.52 | +19.1% |
| PLTR | 14 | $132.03 | +18.6% |
| IONQ | 31 | $62.27 | +15.7% |
| MSFT | 4 | $411.90 | +9.3% |
| EMR | 3.71 | $134.87 | +6.6% |
| IAU | 100 | $83.21 | +2.7% |
| CARR | 7.99 | $62.58 | +2.1% |
| BAC | 9.75 | $51.26 | +0.7% |
| LMT | 3 | $527.53 | +0.6% |
| CVX | 10 | $182.01 | +0.2% |
| BND | 27 | $73.29 | +0.2% |
| NVDA | 9 | $211.98 | -0.4% |
| XLE | 34 | $56.65 | -0.6% |
| MA | 1.00 | $498.94 | -1.0% |
| XOM | 13 | $146.84 | -1.1% |
| BRK.B | 2.08 | $479.69 | -1.1% |
| SOXX | 3 | $576.53 | -1.3% |
| V | 1.51 | $331.16 | -1.5% |
| VNQ | 20 | $97.27 | -1.6% |
| O | 32 | $62.31 | -1.7% |
| LOW | 2.29 | $218.01 | -1.7% |
| BWXT | 10 | $199.58 | -1.9% |
| RKLB | 13 | $146.20 | -1.9% |
| GOOGL | 5 | $388.44 | -2.1% |
| INTC | 4.23 | $118.10 | -2.9% |
| SYK | 1.59 | $314.52 | -3.0% |
| ACI | 30.58 | $16.35 | -4.5% |
| PM | 2.66 | $188.10 | -5.7% |

**Open orders:** 29 GTC stop-loss orders active | 1 limit buy: URA @ $48

### Logs
`C:\Users\Nathaniel\Documents\Trading\logs\strategy_manager.log` (via portfolio_monitor.py)

---

## Options / Wheel Strategy (MULTI-SYMBOL: MARA, SOFI, IONQ, DKNG)

**Status: LIVE — THE ONLY ACTIVE TRADING STRATEGY** (2026-06-22 Nathaniel liquidated everything and kept only the wheel; all other trading strategies disabled).
**Script:** `strategies\options_wheel.py` — basket configured via `SYMBOLS = ["MARA","SOFI","IONQ","DKNG"]`. Each symbol runs an independent CSP→CC cycle with its own state file.
**Task:** `\Alpaca\WheelStrategy` (every 30 min Mon-Fri 9:30 AM–6 PM)
**State:** `strategies\{symbol}_wheel_state.json` (one per symbol)
**Logs:** `logs\wheel.log` (shared)

**History:** QBTS → MARA (2026-06-11), then opened to a 4-symbol basket (2026-06-22). QBTS final record +$371, 1 cycle, archived in `qbts_wheel_state.json`.

### Key implementation details (all fixed 2026-06-22)
- **Options-snapshot API quirk:** the per-contract `?symbols=<occ>` query returns empty quotes on this data tier, and the chain is empty without `feed=indicative`. ALL quote lookups go through `get_chain()` with `feed=indicative`. **This was the real cause of the chronic "no real bid" skips** — not just QBTS illiquidity.
- **Capital split:** shared options buying power is divided fair-share across symbols each run (`obp / symbols_remaining`), recomputed per symbol from LIVE buying power.
- **Buying-power reservation:** Alpaca reserves ~2× nominal collateral (strike×100) in options_buying_power per short put — sized via `SHORT_PUT_BP_FACTOR = 2.0`.
- **Contract qty tracked in state** (`active_qty`) so covered calls never exceed shares held (no naked calls) and premium accounting is accurate.
- **Capital reality:** ~$25–35K effective options BP across 4 names; IONQ (~$53 strike) is capital-heavy, so not all 4 always fill the same run — they rotate as contracts cycle.

**Discontinued/disabled:** TSLA wheel, QBTS wheel (underlying switched), Momentum, GovtContracts, StrategyManager, both copy traders — all disabled 2026-06-22 per "only keep the wheel."

### Wheel Strategy Flow
1. **Sell Cash-Secured Put** → collect premium; cash (strike × 100) reserved as collateral
2. **Not assigned** → keep full premium, repeat
3. **Assigned** → own 100 shares at effective cost = strike − premium collected
4. **Sell Covered Call** → collect premium on 100 shares held; repeat or get called away for profit

### CSP Scan Parameters
- DTE window: 10–60 days (sweet spot ~30d for theta decay)
- OTM minimum: strike ≥3% below current price
- Spread filter: bid-ask spread ≤25% of mid (liquidity)
- Max contracts: 5 per position (capital limit) — scaled by Markov regime
- Default symbols: QBTS, IONQ, SOXL, INTC

### Wheel Candidate Scan
**Script:** `C:\Users\Nathaniel\Documents\Trading\strategies\wheel_candidate_scan.py`
**Email:** `C:\Users\Nathaniel\Documents\Trading\strategies\wheel_scan_email.py`
Scans UBER, F, SMCI, DKNG, SOFI, RIVN, MARA — runs Monday 9:35 AM, emails at 9:50 AM

### Alpaca Options API
- Snapshots: `https://data.alpaca.markets/v1beta1/options/snapshots/{SYMBOL}?feed=indicative&type=put`
- Contracts: `GET https://paper-api.alpaca.markets/v2/options/contracts`
- Order: `POST /v2/orders` — `{symbol, qty, side:"sell", type:"limit", time_in_force:"day", limit_price}`
- OCC parsing: `re.match(r'^([A-Z.]+)(\d{6})([CP])(\d{8})$', sym)` — strike = `int(strike_str) / 1000`
- IV field in snapshots: `snapshot["impliedVolatility"]` (NOT `greeks["iv"]`)

### Logs
`C:\Users\Nathaniel\Documents\Trading\logs\qbts_wheel.log`

---

## Markov Regime Detector

**Script:** `C:\Users\Nathaniel\Documents\Trading\strategies\regime_detector.py`
**State:** `C:\Users\Nathaniel\Documents\Trading\strategies\regime_state.json`

### How It Works
- Fetches 220 days of SPY bars via Alpaca SIP feed
- Classifies market into 3 states based on SPY vs 200-day SMA:
  - **BULL** (SPY > SMA200): Full wheel contracts (5 max)
  - **CAUTION** (0–2% below SMA200): Half contracts (2 max)
  - **BEAR** (>2% below SMA200): No new puts — wheel paused
- Used by qbts_wheel_strategy.py before selling new CSPs

### Current Reading (as of 2026-05-29)
- **Regime: BULL** | SPY $750.46 | SMA200 $680.01 | +10.4% above

---

## Signal Monitoring System

**Location:** `C:\Users\Nathaniel\Documents\Trading\signals\`
**Shared utilities:** `signal_utils.py` — logging, state load/save (atomic), email sending, shared constants
**State files:** `signals/states/{monitor}.json` (gitignored — runtime data)

### All 10 Signal Monitors
| Script | Schedule | Source | What It Watches |
|--------|----------|--------|-----------------|
| `signal_digest.py` | 8:30 AM Mon-Fri | All | Morning summary email — portfolio + all signals |
| `earnings_calendar.py` | 8:00 AM Mon-Fri | Alpaca corporate actions | Warns 2d and 1d before any holding reports earnings |
| `contract_awards.py` | 6:00 PM Mon-Fri | USASpending.gov | Federal contracts ≥$1M for LMT, BWXT, RKLB, PLTR, IBM, MSFT, NVDA, IONQ |
| `insider_trades.py` | Every 2h market hours | SEC Form 4 RSS | CEO/director buying in portfolio holdings (≥$10K) |
| `hedge_fund_13f.py` | Monday 7:00 AM | SEC EDGAR | New 13F filings from Buffett, Ackman, Burry, Citadel, etc. |
| `activist_monitor.py` | Every 4h market hours | SEC EDGAR 13D/13G | Activists taking 5%+ stakes in portfolio holdings |
| `etf_flows.py` | 9:45 AM Mon-Fri | Alpaca Data | Unusual volume (>2x 20d avg) in SOXX, XLE, XLK, VNQ, ITA, ARKK |
| `short_interest.py` | Mon & Wed 7:30 AM | FINRA | Squeeze setups (SI drops >20%) or bear warnings (SI rises >30%) |
| `unusual_options.py` | Every 30min market hours | Alpaca Options | IV spikes (>1.8x hist vol) or put/call skew >1.6 in holdings |
| ~~`senate_disclosures.py`~~ | REMOVED 2026-06-11 | Capitol Trades | Discontinued with politician copy trader — positions liquidated, task deleted |

### Hedge Funds Tracked (13F)
Berkshire (0001067983), Pershing Square (0001336528), Scion (0001649339),
Appaloosa (0001006438), Third Point (0001040273), Duquesne (0001536411),
Tiger Global (0001167483), Citadel (0001423298)

### Task Scheduler Folder
All signal tasks live under `\Alpaca\Signals\` in Windows Task Scheduler.

---

## Email Config
- Location: `C:\Users\Nathaniel\Documents\Trading\config\email_config.json`
- SMTP: Gmail (smtp.gmail.com:587)
- **Status: FUNCTIONAL** — app_password configured. Sends HTML email on all signal events.

---

## Key File Locations
| Purpose | Path |
|---------|------|
| Alpaca credentials | `config\alpaca_credentials.json` |
| Email config | `config\email_config.json` |
| Politician config | `politician-copy-trader\config.json` |
| Politician state | `politician-copy-trader\state.json` |
| QBTS wheel state | `strategies\qbts_wheel_state.json` |
| Regime state | `strategies\regime_state.json` |
| Per-symbol stop states | `strategies\states\{SYMBOL}_state.json` |
| Signal states | `signals\states\{monitor}.json` |
| Copy trader log | `logs\copy_trader.log` |
| Strategy manager log | `logs\portfolio_monitor.log` |
| QBTS wheel log | `logs\qbts_wheel.log` |
| Signal logs | `logs\{monitor}.log` |

---

## Desktop Files
- `C:\Users\Nathaniel\Desktop\MCP_Trading_Servers.html`
- `C:\Users\Nathaniel\Desktop\Investment_Opportunities_2026.html`
- `C:\Users\Nathaniel\Desktop\TradingBackup_2026-05-23_15-00.zip`
- `C:\Users\Nathaniel\Documents\Trading\TRADING_SOURCES.html` — full signal source reference

---

## Auto-Memory Index
`C:\Users\Nathaniel\.claude\projects\C--Users-Nathaniel-Documents-Trading\memory\MEMORY.md`
