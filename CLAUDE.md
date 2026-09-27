# Nathaniel's Trading Projects — Claude Memory

## Owner
- Name: Nathaniel
- Email: nate.adams.nh@gmail.com
- Platform: Windows (use `python`, not `python3`)
- Last updated: 2026-08-27

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
- API Key: PKVC3QNMNEPECL7LOBOPN5FFIE (**new account, created 2026-09-26** — Nathaniel deleted the old paper account and made a fresh one; the old key `PK47VMLQ…` is dead)
- **Portfolio value: $500,000** | **Cash: $500,000** | Buying power $2,000,000 | Options buying power $500,000 (Nathaniel funded the account up from $50,000 on 2026-09-27; 0 positions, confirmed live against `/v2/account`)
- Holdings turn over daily under the current live strategy (see Buy Consistent-History Losers below) — verify current value/holdings directly against Alpaca (`/v2/account`, `/v2/positions`) rather than trusting any static table in this file.
- Options Level: 3

---

## Politician Copy Trader Bot

**Status: RE-ENABLED 2026-09-26** at Nathaniel's request (paper account). `\Alpaca\PoliticianCopyTrader` (hourly Mon-Fri 9:30-18:00) and `\Alpaca\SenateDisclosures` (every 2h, same window) recreated via `scripts\register_copy_trader_tasks.ps1`; first runs 2026-09-28 9:30 AM. `signals\senate_disclosures.py` moved back out of `archive\`. Both are in the Heartbeat `JOBS`. Senate script fixes made on re-enable: the scraper now parses real trade dates/sizes (before, blank dates collapsed dedup keys); sells only close a held position (before, a sell with no position could open a short); trades seen while the market is closed are deferred, not dropped; non-200 responses now log an error; baseline seeded 2026-09-26 (82 existing disclosures marked seen, so no backlog is traded). Rounds and Hoeven legitimately show 0 trades on Capitol Trades. History below is from the first run.

**Protective stop-loss added 2026-09-27** (Nathaniel asked directly: "is there a stop loss on this?" — there wasn't). Before this, nothing managed downside between a copied buy and whatever eventual sell disclosure told the strategy to exit — the "-10% loss alert" below only ever sent a notification, never closed anything. Both `politician-copy-trader\trader.py` (new `attach_protective_stops()`, called as Step 4b of `main.py`'s `run()`) and `signals\senate_disclosures.py` (duplicate local functions, same pattern, called from its own `run()`) now place a **10% Alpaca-managed GTC trailing stop** on any held whole shares not already covered by one — checked fresh against the broker's *open orders*, not local state, every run, so it self-heals across runs and across whichever of the two scripts actually bought a given symbol. Only whole shares are covered: Alpaca's `trailing_stop` order type rejects fractional quantities, and a $500 notional buy is almost always fractional, so a small remainder (typically well under $20) stays unprotected — same accepted tradeoff the retired Buy-Consistent-Losers strategy used. Both scripts' sell path now cancels any resting stop first (`cancel_stops()`), so a copied politician/senator sell isn't blocked by shares the stop already reserves. **No changes needed to Heartbeat** — its existing `check_broker()` already flags any symbol account-wide with whole shares held but no resting sell order covering them (`uncovered:{sym}`), so a silent failure in this new code would already surface there. Verified 2026-09-27: both scripts' new functions run clean against the live (0-position) account; a real test buy+cancel round-trip confirmed order placement/cancellation works, but the market was closed so a real fill-to-stop cycle wasn't observable — worth a glance at `logs\copy_trader.log` / `logs\senate_disclosures.log` after Monday's first live buy to confirm a `[STOP]` line actually appears.

**Previously DISCONTINUED 2026-06-11** — removed at Nathaniel's request after flat performance (~-$30 net on ~$10K cycled through 24 trades). All 16 copy-trade positions liquidated at the 2026-06-12 open, "PoliticianCopyTrader" Task Scheduler task deleted. Code and state.json kept for records. The separate `signals/senate_disclosures.py` senator copy monitor was ALSO discontinued the same day (12 positions ~$3,893 liquidated, "SenateDisclosures" task deleted) — all congressional copy-trading is now shut down.

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
- Loss alert: -10% (notification only) **+ actual 10% trailing stop-loss as of 2026-09-27** — see below

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
**Status: DISABLED** (2026-06-22, per "only keep the wheel" — see Wheel Strategy section; confirmed disabled in Task Scheduler as of 2026-08-27). Section below is historical.

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
**Status: DISABLED** (2026-06-22, per "only keep the wheel"; confirmed disabled in Task Scheduler as of 2026-08-27). Section below, including the portfolio table, is a historical snapshot — none of these holdings are current (see Buy Consistent-History Losers section for what's actually live).
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

## Options / Wheel Strategy (MULTI-SYMBOL: MARA, SOFI, IONQ, DKNG, SMCI, INTC, CVNA, HOOD)

**Status: RE-ENABLED 2026-09-27** at Nathaniel's request (paper account). `\Alpaca\WheelStrategy` was confirmed Disabled as of 2026-08-27 (had been since some undocumented date after 2026-07-21 — see History below) and was flipped back to Ready 2026-09-27 with its existing schedule/action untouched (every 30 min, 9:30 AM–6 PM Mon-Fri, running `strategies\options_wheel.py`). First run 2026-09-28 9:30 AM ET. Was live from 2026-06-22 until superseded by the Buy Consistent-History Losers strategy (documented in the next section, now also retired) — the two strategies were never meant to run simultaneously; re-enabling the wheel does not touch Buy-Consistent-Losers/DynamicStopManager, which stay Disabled.
**Script:** `strategies\options_wheel.py` — basket configured via `SYMBOLS = ["MARA","SOFI","IONQ","DKNG","SMCI","INTC","CVNA","HOOD"]`. Each symbol runs an independent CSP→CC cycle with its own state file.

**Basket widened 2026-09-27** at Nathaniel's request — SMCI, INTC, CVNA, HOOD added to the existing 4 (MARA/SOFI/IONQ/DKNG kept, not replaced). Picked from a live options screen that day (~5% OTM put, 10–60 DTE, spread ≤25%, ranked by annualized premium yield after filtering out ≤15%-spread names and one sub-$5 name): MARA 120%, SMCI 99%, INTC 86%, CVNA 77%, HOOD 70% annualized. New symbols need no manual state setup — `load_state()` returns fresh CSP-stage defaults for any symbol without an existing `{symbol}_wheel_state.json`. `max_notional_per_symbol` in `config\wheel_limits.json` ($15,000) already caps any one symbol's exposure regardless of basket size.

**Capital-fit concern from 2026-09-27 (found via a `WHEEL_DRY_RUN=1` smoke test) is now resolved.** At the account's original $50,000 balance, splitting `options_buying_power` evenly across 8 symbols (`obp / remaining`) left only ~$6,250/symbol on average, well under what INTC (~$22,140/contract with `SHORT_PUT_BP_FACTOR = 2.0`), HOOD (~$21,490), and CVNA (~$11,710) need for even 1 contract — a dry run confirmed all three logged "Insufficient budget ... Skipping" on a flat $10,000 test budget. **Nathaniel funded the account up to $500,000 the same day**, confirmed live against `/v2/account` (`options_buying_power: 500000`). An even eighth of that is ~$62,500/symbol — comfortably above every symbol's per-contract requirement — so this should no longer bind in practice. Not re-tested end to end at the new balance (market was closed); worth a glance at `logs\wheel.log` after Monday's first live run to confirm all 8 symbols are actually getting sized normally rather than something else unexpected capping them.

**`strategies\wheel_candidate_scan.py` fixed 2026-09-27** — it had the same per-contract-quote bug as the original `options_wheel.py` (returns empty bid/ask on this data tier without `feed=indicative`; see Key implementation details below), just never patched when that was fixed elsewhere on 2026-06-22. So every scan silently reported "no liquid puts found" for names that actually had one. Now uses the same bulk `get_chain()` pattern, applies the same spread ≤25% filter, and its candidate universe is widened from the original 7 (UBER, F, SMCI, DKNG, SOFI, RIVN, MARA) to 25 similarly-liquid, moderately-priced, high-IV names — this is what surfaced SMCI/INTC/CVNA/HOOD above. Still not scheduled in Task Scheduler (`\Alpaca\WheelCandidateScan` / `\Alpaca\WheelCandidateEmail` both Disabled) — this was a manual, one-off screen, not a recurring job.
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

## Buy Consistent-History Losers Strategy

**Status: RETIRED 2026-09-22 (evening)** — `\Alpaca\BuyConsistentLosers` and `\Alpaca\DynamicStopManager` Disabled at Nathaniel's direction after the news-split backtest (below) killed the last plausible repair. Nothing trades. The `ConsistentLosers` signal email and the `Heartbeat` monitor still run; expect the heartbeat to report the two disabled tasks indefinitely (that is correct — silence it by disabling `\Alpaca\Heartbeat` too, or drop those jobs from `JOBS`). **Don't re-enable without a new premise and a fresh backtest** — the code is safe now, the strategy isn't profitable.

**Why it was retired.** The 2026-09-22 run (1,265 trades, signals 2024-09-03..2026-09-18) was worse than September's: mean **−0.36%/trade** (t=−3.3), PF 0.77, −0.77%/trade vs SPY (t=−6.9) while SPY returned +40.6%, and it **beat 0 of 20 random any-stock runs** (random ≥$5 averaged −0.01%). Second half worse than first (−0.46% vs −0.21%). Live parity 119/132, so the backtest models what actually traded.

**The news-split test (the hypothesis that failed).** Chan-style premise: extreme moves *without* news revert, moves *with* news drift, so mixing them cancels any edge. Added `NewsChecker`/`split_by_news()`/`welch_t()` to the backtest (no look-ahead: only articles published by the signal day's close; failed lookups cached as −1 and excluded from both buckets rather than counted as "no news"). Result: **no news −0.32%/trade (t=−2.6, 921 trades) vs news −0.49% (t=−2.1, 344 trades), difference +0.17%, Welch t=+0.7.** Right direction, statistically nothing, and the no-news half is still clearly negative on its own — there is no profitable subset hiding inside the screen. Flags: `--no-news-split`, `--news-days-before N`; `news_count` is a column in `trades.csv`. Results: `backtests\results\2026-09-22_205431\`.

**(Moot as of 2026-09-26: the paper account was deleted and recreated, so everything below no longer exists at the broker.)** **Left at the broker when it was turned off:** LQD 4 shares with a resting stop at $103.74 (it no longer trails — nothing is managing it), ~29 fractional sub-1-share leftovers with no stops (~$1,800), and queued market buys covering the KBR −13 / MGM −12 shorts (placed by Nathaniel 2026-09-22 evening, filling at the 09-23 open).

**History: re-enabled earlier the same day** ("fix the problems and trade automatically") after the state-drift fix below; one clean scheduled manager run at 11:42 (exit 0, 1 lot, no orders), then retired that evening once the news-split result came in. Paused 2026-09-16 → 2026-09-22.

**Fix (2026-09-22): the broker's holdings are the source of truth, not the state file.** `dynamic_stop_manager.py`: every sell (stop replacement, "already passed" market close, new-lot absorption) is capped by `sellable_qty()` = whole shares held − shares reserved by other open sells, read fresh right before the order; step 1b `allocate_lots_to_holdings()` retires/shrinks lots each run so their total never exceeds held whole shares; `reconcile_uncovered_positions()` counts all open sells as coverage and never adopts a short. `buy_consistent_losers.py`: skips any symbol the account is short, and sizes its safety-net trailing stop by sellable shares. `FUND_NAME_RE` now also matches issuer names (ProShares, Direxion, GraniteShares, iShares, SPDR, MiniShares, Invesco QQQ) — 110 more funds excluded, 0 operating companies (checked against the live universe; bare "Ultra"/"Vanguard"/"WisdomTree" were rejected because they hit Ultra Clean, American Vanguard, WisdomTree Inc). `dynamic_stops.json` rebuilt from live positions (27 lots → 1, LQD); pre-rebuild copy at `signals\states\dynamic_stops.2026-09-22.pre-rebuild.json`. Tests: `python -m unittest scripts.tests.test_dynamic_stop_manager` (replays the MGM and ghost-lot failures).

**Stop manager opened SHORT positions, found 2026-09-16.** From 2026-09-03 the manager logged ~450 `URGENT ... leaving unprotected` errors/day while every run still ended "Run complete." Root cause: `dynamic_stops.json` kept lots whose shares were already sold (at pause time: FHB tracked 75 shares vs 0 held, M 42 vs 0, HTGC 118 vs 59, ...), and no sell path checks actual holdings before selling. Alpaca rejected most of those sells (the 403s) but not all: KBR got two 13-share stops filled 90 min apart on 2026-09-09 → short 13; MGM got a 12-share market sell with nothing held on 2026-09-14 → short 12. Also: QID (a leveraged *inverse* QQQ ETF) was bought 2026-09-14 — `FUND_NAME_RE` misses fund names without "ETF"/"Fund" in them (e.g. "ProShares UltraShort QQQ").

**Historical note:** the section below was written 2026-08-27 from a Task Scheduler audit — this strategy was built and turned on without ever being added to this file.

**Signal script:** `signals\consistent_losers.py` — every trading day after the close, scans the full non-OTC US equity universe (~13,000 symbols, not just Alpaca's top-50-losers screener) for today's biggest price losers that were STABLE over the prior 20 trading days (no single day's close move >±5%, no single day's volume change >±50%). The point: a stock only becomes one of the day's most extreme losers because something was already brewing, so pre-filtering to big losers first would almost always fail the "was quiet before" check — checking the whole universe instead surfaces genuine "boring stock hit by a surprise drop" events. Ranks survivors by today's % loss, keeps the top 5, emails them. Runs via Task Scheduler `\Alpaca\Signals\ConsistentLosers`, ~4:10 PM ET Mon-Fri (after close, once EOD bars settle).

**Earnings filter added 2026-08-27:** the 20-day stability window is blind to a stock that dropped BECAUSE it just reported earnings (the report day itself falls outside the lookback window it checks), so a genuine earnings-driven repricing sails through looking identical to random noise. HRL and TPR (see P&L investigation above) both qualified this way and kept falling instead of reverting. Fix keyword-matches Alpaca news headlines for "earnings"/quarter-mentions in a ±3/+5 day window around the qualifying date and excludes any match. Verified against real data: reliably excludes "just reported" cases (same-day earnings coverage is heavy) and would have fully excluded all 3 of TPR's actual buy dates thanks to a lucky pre-earnings preview article; only best-effort for "about to report" on quieter names — 5 of HRL's 7 actual buys predated any earnings press coverage and would NOT have been caught. **Do not trust Alpaca's `/v1beta1/corporate-actions` endpoint for earnings data** — it doesn't support an "earnings" type at all (confirmed via a live HTTP 400); this is why `earnings_calendar.py` below has been silently non-functional the whole time it was ever enabled.

**Buy script:** `scripts\buy_consistent_losers.py` — reads the prior day's qualifier list, buys the largest **whole-share** quantity worth ~$500 of each symbol, waits for fill, then attaches a **3% Alpaca-managed trailing stop** (`type: trailing_stop`, GTC) as an immediate safety net — see below, this gets superseded within minutes by the staged system.

**Staged per-lot stop-loss (replaced the flat trail 2026-08-29):** Nathaniel specified a 3-stage stop instead of one fixed percent: (1) **tight** — trail 3% below the highest price since that lot's own entry; (2) once that 3% trail would reach or exceed the lot's entry price, switch to **breakeven_hold** — freeze the stop at exactly the entry price and leave it there, no further tightening; (3) once price reaches or exceeds entry price × 1.05 (+5%), switch to **wide_trail** — resume trailing, now 5% below the highest price, for the rest of the lot's life. Alpaca's native trailing_stop order type can't express freeze/resume, so this is hand-rolled in `scripts\dynamic_stop_manager.py`, which computes the target itself and replaces the resting order (cancel + resubmit a plain `stop` order) whenever the stage or target price changes. Tracked **per lot, not per symbol** — several symbols (HRL, FHB, ...) have multiple $500 buys on different days at different prices, each with its own independent stage. Runs via Task Scheduler `\Alpaca\DynamicStopManager`, every 15 min during market hours (9:35 AM–4:05 PM ET) Mon-Fri. State: `signals\states\dynamic_stops.json` (list of active lots: symbol, qty, entry_price, highest_price, stage, stop_order_id). Logs: `logs\dynamic_stop_manager.log`.

**Silent three-day outage, found and fixed 2026-09-02.** Every scheduled run from 2026-08-31 through 2026-09-02 crashed partway and threw away its work, leaving 9 symbols (~$6K, including HRL's whole 141-share position) with no stop at all. Three compounding causes, all now fixed:
1. **`datetime.fromisoformat` on Python 3.10** cannot parse Alpaca's variable-precision fractional seconds — a fill stamped `...:01.41339+00:00` (5 digits) raises `ValueError`, since 3.10 accepts only exactly 3 or 6 (3.11+ handles full ISO 8601). Failure was intermittent, depending purely on the digits in a timestamp. Now normalized via `parse_iso()`.
2. **State was saved only at the very end of `run()`**, so any exception discarded every reconciliation and stop replacement from that run. The tell was the log re-reporting the *same* lots as CLOSED every cycle with identical prices, and never printing "Run complete." Now saved in a `finally` block.
3. **A lot whose stop replacement had failed stayed naked forever** — with `stop_order_id: None` but an unchanged target price, the "has anything changed?" check said no and skipped it. Missing order now forces a re-place.

Also added `reconcile_uncovered_positions()`, which runs last and adopts any held whole shares that no resting stop covers, anchoring them at the current price. Coverage is measured from **open stop orders at the broker, not the state file** — shares reserved by a not-yet-absorbed native trailing stop are already protected, and trying to double-stop them returns HTTP 403. This makes the system self-healing against exactly the state-vs-broker drift that caused the outage; running it now reports "GAPS: none."

**Important edge case this script handles:** a stage transition computed off a stale "highest price since entry" can produce a target that's already at or above the *current* price (e.g. a stock peaked weeks ago and has since pulled back past where the new stage's trail would sit). Alpaca rejects a resting stop order priced that way (`stop price must be less than current price` — it would trigger instantly). The manager treats this as "the stop has effectively already fired" and closes the lot immediately via market order rather than leaving it silently unprotected — this is what actually happened to a MOS lot during setup (closed for a ~$65 combined gain, correctly, since it had peaked and pulled back). Migrating the 44 pre-existing lots to this system on 2026-08-29 surfaced 19 symbols (HRL, TPR, and 17 others, ~44 individual lots) where the same condition held from their *original* purchase dates — per Nathaniel's direction, those were restarted with today's price as a fresh entry anchor rather than closed, since the old flat-3% stop had already been holding them without issue. Skips if the signal is stale (>4 days old, to avoid trading on outdated data over a weekend/holiday gap) or already ran today (idempotent via `last_run` in its state file). Runs via Task Scheduler `\Alpaca\BuyConsistentLosers`, weekdays 9:31 AM ET (just after the open). This task's `StartBoundary` is **2026-08-03** — the strategy has been live since then.

**Bug found and fixed 2026-08-27:** from 2026-08-03 through 2026-08-27, buys were sized by `notional` (dollar amount) instead of whole shares, which produces fractional quantities — and Alpaca's `trailing_stop` order type rejects fractional quantities (HTTP 422). Combined with the task originally running at 9:25 AM (5 min before the 9:30 open, so `wait_for_fill`'s poll window elapsed before the order could fill and the script gave up thinking the buy failed), the net effect was **70 real buy fills / $34,999 invested / 29 open positions with zero stop-loss protection ever placed**, while the script's own logs and state file (`last_bought: []`) showed "Bought 0/N" every single day. Found via a routine portfolio check, not by the strategy itself. Fixed same day: buys now use whole-share qty (so trailing stops succeed), task moved to 9:31 AM (post-open), and the 29 pre-existing naked positions were retroactively protected with 5% trailing stops on their floor whole-share quantity (a small fractional remainder per position, typically <1 share, is left uncovered — not worth a partial-share stop). Realized P&L from the bug period: $0 (nothing had ever sold); unrealized P&L at time of fix: -$673.82 (-1.93%) on $34,999.30 invested.

**Volume-spike section added to the email 2026-08-31 (informational only):** a second table listing price-stable names whose volume that session was ≥3x their own prior 20-day average (min $1M traded). Stored under a **separate** state key `last_volume_spikes` — `buy_consistent_losers.py` reads only `last_qualifiers`, so nothing here is ever auto-traded. **Key design point:** this list uses *price* stability only and deliberately drops the volume-stability half of the screen. Requiring 20 days of flat volume *and then* a volume explosion is self-defeating — measured live on the 2026-08-28 session, the full screen passed 21 names with a max ratio of 1.97x and **zero** at ≥2x, because the ±50% volume rule structurally selects for stocks that can't spike. Price-stability-only passed 2,465 names (1,191 liquid) and surfaced real events (SOLS 11.4x/+12.9%, ESI 7.2x, PCG 4.6x on $140M). The losers list keeps the original full screen unchanged since it drives real orders.

**ETFs/funds excluded from the spike list (`EXCLUDE_FUNDS_FROM_SPIKES`, 2026-08-31):** unfiltered, the top spikes were dominated by bond/index ETFs (IGLB, USIG, BSCT, ...) whose volume surges reflect institutional rebalancing rather than a company-specific event — 19 of 28 candidates on the 2026-08-28 session. Alpaca's asset record has **no ETF/fund flag** (verified — `class` is just `us_equity` for both), so `get_universe()` now returns `{symbol: name}` and `FUND_NAME_RE` matches on the name. It deliberately does **not** match a bare "Trust": 270 universe names are operating companies or REITs named that way (Arbor Realty Trust, Acadia Realty Trust, American Assets Trust), so blanket-matching would wrongly drop them all; "Trust" only counts alongside a fund/commodity word, which still catches holdings like "iShares Gold Trust Micro". Validated against the live universe: 22/22 known ETFs caught, 0 false positives across a control set of stocks and Trust-named REITs. After filtering, the same session yielded ESI, PCG, AEG, WIT, UNF — all real operating companies. Set the flag to `False` to include funds again.

**Losers list fund-filtered too as of 2026-09-01 (`EXCLUDE_FUNDS_FROM_LOSERS`) — this one changes what gets bought.** SPY, LQD, VUG and SVXY had been qualifying as "surprise drops" and getting real orders; the portfolio holds 8 separate SPY buys and 1 LQD buy from that. An index or bond fund falling 1% is the whole market moving, not the company-specific surprise this strategy is premised on. Measured on the 2026-08-31 session: 3 of 18 loser candidates were funds (VUG, SPY, LQD) — all correctly dropped, while "Invesco LTD" (IVZ, the asset manager, not a fund) was correctly kept. Top-5 was unchanged that day because the funds ranked 12th/13th/18th by % loss — ETFs are diversified so they rarely post the biggest single-day drops. The filter therefore bites mainly on quiet days when few individual names qualify, which is exactly how those 8 SPY buys happened. **Existing SPY/LQD positions are untouched** — the filter gates new buys only; they still exit via their own staged stops.

**Partial-session bug fixed 2026-08-31:** the script anchored on `calendar[-1]`, which is only the last *completed* session when it runs on schedule (~4:10 PM ET). Run at any other time — manually, or after a schedule slip — that's today's partial or not-yet-started bar, and every metric silently computes off it. Volume ratios are the worst case: a partial day's volume against full-day averages always reads low, so the volume list comes back empty rather than erroring (observed live: a run that slipped past midnight anchored on an unstarted Monday and returned ~0.1x ratios universe-wide). `resolve_anchor_index()` now consults the Alpaca clock and steps back a day when the latest calendar date hasn't closed yet.

**State:** `signals\states\consistent_losers.json` (`last_qualifiers` = losers/tradeable, `last_volume_spikes` = informational), `signals\states\buy_consistent_losers.json` (buy run tracking)
**Logs:** `logs\consistent_losers.log`, `logs\buy_consistent_losers.log`

### Current holdings
Do NOT keep a static holdings table for this strategy in this file — positions turn over daily (new $500 buys each morning, old ones exit via their trailing stop whenever it fires) and a table here would be stale within days, the exact trap already documented elsewhere in this file for `govt_contracts_state.json`-style staleness. Check Alpaca directly (`GET /v2/positions`) for current holdings. Snapshot for reference only, **as of 2026-08-27**: 29 open positions, portfolio value $47,501.58, cash $13,106.76, largest positions SPY/FHB/HRL/JPM/AMP — all built by this strategy, none inherited from the wheel or any other prior strategy.

---

## Heartbeat Monitor

**Script:** `scripts\heartbeat.py` | **Task:** `\Alpaca\Heartbeat`, every 15 min 9:40 AM–5:10 PM ET Mon-Fri (created 2026-09-16, allowed to run on battery) | **State:** `signals\states\heartbeat.json` | **Log:** `logs\heartbeat.log` | **Tests:** `python -m unittest scripts.tests.test_heartbeat`

Built because every failure of the live strategy was silent. Checks: (1) the three live tasks are enabled with exit code 0; (2) each job has a completed run as recent as its schedule demands, and its latest run didn't start without finishing; (3) any ERROR/CRITICAL log line since the last check; (4) at the broker — no short positions, every whole share covered by a resting sell order, no symbol with more shares queued to sell than held, and `dynamic_stops.json` lots matching real holdings. Emails only when the *set* of problems changes (plus a reminder every 3h while they persist, and an all-clear on recovery), and sends one daily status email after 4:45 PM on trading days — **if that daily email stops arriving, the heartbeat itself is down.**

Depends on each live script logging `Run complete.` **only on a normal exit** — changed 2026-09-16 in `dynamic_stop_manager.py` (was inside a `finally`, so it logged even on a crash), `buy_consistent_losers.py` (never logged it) and `consistent_losers.py`. Keep that invariant when editing those scripts. Supersedes the wheel-era `strategies\strategy_watchdog.py` (still Disabled, checks retired strategies). While a task is Disabled the heartbeat reports that once (`{job}:state`) and skips its freshness/unfinished-run checks (2026-09-22 — the paused BuyConsistentLosers was otherwise flagged as "crashed" forever); new ERROR lines are still reported. While the strategy is paused, expect the two disabled-task alerts plus any shorts.

---

## Consistent-Losers Backtest

**Script:** `scripts\backtest_consistent_losers.py` | **Tests:** `python -m unittest scripts.tests.test_backtest_consistent_losers` | **Cache:** `backtests\cache\` (gitignored, ~2 yrs IEX daily bars, 12,921 symbols incl. delisted) | **Results:** `backtests\results\<timestamp>\` (report.txt, trades.csv, summary.json)

Imports the live thresholds, fund regex, earnings regex (`signals\consistent_losers.py`) and `compute_stage` (`scripts\dynamic_stop_manager.py`), so it can't drift from live logic; a unit test checks the vectorized screen matches live `check_stability()` cell for cell, and each run reports how many actual live picks it reproduces from `logs\buy_consistent_losers.log`. Compares against random quiet names, random names ≥$5 (same trade count and exits), SPY over each trade's window, and the same picks held 5/20 days with no stops. Earnings filter uses only news published by the signal day's close (no look-ahead). Limits: daily bars approximate the 15-min stop ratchet; split-adjusted prices (live uses raw).

**First result (2026-09-16, signals 2024-09-03..2026-09-14, 10bps slippage, earnings filter on; reproduced 115/127 actual live picks):** the strategy has **no edge**. 1,249 trades, 21% win rate, mean -0.32%/trade (t=-2.9), -$1,694, profit factor 0.79, -0.71%/trade vs SPY over the same windows (t=-6.3), while SPY returned +37.7% over the period. Random quiet names with the same exits averaged -0.38% (the strategy beat 75% of 20 runs, i.e. noise), so ranking by "biggest drop" adds nothing. Random names ≥$5 did *better* (-0.04%). Same picks held 5 days with no stops: -0.25%, so the staged stop doesn't rescue it (the tight 3% stop is hit by ordinary noise; median trade -1.32%). Both halves were negative, the second one worse.

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

### Signal Monitors
| Script | Schedule | Source | What It Watches |
|--------|----------|--------|-----------------|
| `signal_digest.py` | 8:30 AM Mon-Fri | All | Morning summary email — portfolio + all signals |
| `earnings_calendar.py` | 8:00 AM Mon-Fri | Alpaca corporate actions | Warns 2d and 1d before any holding reports earnings — **confirmed broken 2026-08-27: Alpaca's corporate-actions endpoint doesn't have an "earnings" type at all (live HTTP 400), so this has silently returned nothing every run it's ever had.** If re-enabling, port it to the news-headline-matching approach in `consistent_losers.py`'s `has_nearby_earnings()` instead. |
| `contract_awards.py` | 6:00 PM Mon-Fri | USASpending.gov | Federal contracts ≥$1M for LMT, BWXT, RKLB, PLTR, IBM, MSFT, NVDA, IONQ |
| `insider_trades.py` | Every 2h market hours | SEC Form 4 RSS | CEO/director buying in portfolio holdings (≥$10K) |
| `hedge_fund_13f.py` | Monday 7:00 AM | SEC EDGAR | New 13F filings from Buffett, Ackman, Burry, Citadel, etc. |
| `activist_monitor.py` | Every 4h market hours | SEC EDGAR 13D/13G | Activists taking 5%+ stakes in portfolio holdings |
| `etf_flows.py` | 9:45 AM Mon-Fri | Alpaca Data | Unusual volume (>2x 20d avg) in SOXX, XLE, XLK, VNQ, ITA, ARKK |
| `short_interest.py` | Mon & Wed 7:30 AM | FINRA | Squeeze setups (SI drops >20%) or bear warnings (SI rises >30%) |
| `unusual_options.py` | Every 30min market hours | Alpaca Options | IV spikes (>1.8x hist vol) or put/call skew >1.6 in holdings |
| `consistent_losers.py` | ~4:10 PM Mon-Fri | Whole US equity universe (~13,000 symbols) | Stable stocks hit by a surprise drop — feeds the live Buy Consistent-History Losers strategy (see above), not just an alert |
| ~~`senate_disclosures.py`~~ | REMOVED 2026-06-11 | Capitol Trades | Discontinued with politician copy trader — positions liquidated, task deleted |

Confirmed via Task Scheduler on 2026-08-27: every monitor under `\Alpaca\Signals\` is Disabled except `ConsistentLosers` (Ready/running). This table describes what each script does, not a guarantee it's currently scheduled — verify with Task Scheduler before assuming one is running.

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
| Buy Consistent-History Losers (RETIRED 2026-09-22) | `scripts\buy_consistent_losers.py` |
| Dynamic per-lot stop manager (RETIRED 2026-09-22) | `scripts\dynamic_stop_manager.py` |
| **Heartbeat monitor (live)** | `scripts\heartbeat.py` |
| Consistent-Losers backtest | `scripts\backtest_consistent_losers.py` |
| Consistent-Losers signal (live) | `signals\consistent_losers.py` |
| Dynamic stop state (active lots) | `signals\states\dynamic_stops.json` |
| Politician config | `politician-copy-trader\config.json` |
| Politician state | `politician-copy-trader\state.json` |
| QBTS wheel state | `strategies\qbts_wheel_state.json` |
| Regime state | `strategies\regime_state.json` |
| Per-symbol stop states | `strategies\states\{SYMBOL}_state.json` |
| Signal states | `signals\states\{monitor}.json` |
| Buy-consistent-losers state | `signals\states\buy_consistent_losers.json` |
| Copy trader log | `logs\copy_trader.log` |
| Strategy manager log | `logs\portfolio_monitor.log` |
| QBTS wheel log | `logs\qbts_wheel.log` |
| Buy-consistent-losers log | `logs\buy_consistent_losers.log` |
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
