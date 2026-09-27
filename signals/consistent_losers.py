#!/usr/bin/env python3
"""
Consistent-History Losers Monitor
==================================
Every trading day after the close, finds the top 5 biggest stock-price
losers among names that had STABLE price/volume behavior right up until
the drop -- filtering out stocks that are already choppy/erratic day to
day (penny stocks, warrants, chronic volatility) so the list highlights
genuine, unexpected breakdowns rather than routine noise.

Universe: every non-OTC tradable US equity (~13,000 symbols), not just
Alpaca's live top-50-losers screener. Pre-filtering to the day's biggest
losers before checking consistency doesn't work -- a stock only becomes
one of the day's most extreme losers because something was already
brewing (rumors, guidance drift, biotech jitters), so it almost always
fails the "was quiet before" check. Checking the whole universe instead
surfaces real "boring stock hit by a surprise drop" events, even when
the drop is more like -6% to -10% rather than -50%+.

Consistency filter (must hold over the 20 trading days BEFORE the drop):
  - No single day-over-day close move exceeded +/-5%
  - No single day-over-day volume change exceeded +/-50%
Symbols with missing/zero-volume days in that window are skipped (fail
closed -- can't verify consistency without clean data).

Earnings filter: candidates with earnings-related news in the last few days
(and, best-effort, the next few) are excluded, even if they otherwise pass
the consistency check. The 20-day lookback window deliberately excludes
today (the drop day) itself, so a stock that dropped BECAUSE it just
reported earnings sails through the "was quiet before" check trivially --
of course it was quiet, nothing happens until the report. That's a real
gap: HRL and TPR both qualified this way in August 2026 (HRL cut guidance
and missed Q2 revenue; TPR beat earnings but spooked the market with soft
FY27 guidance) and both kept falling instead of the noise-reversion this
strategy is betting on. Detection is keyword-matched Alpaca news headlines,
not a real forward earnings calendar -- Alpaca's /v1beta1/corporate-actions
endpoint does not support an "earnings" type at all (confirmed via a live
400 response; earnings_calendar.py was built on that same broken
assumption and has been returning nothing this whole time). News-based
detection is reliable for "just reported" and best-effort for "about to
report soon" -- see has_nearby_earnings() for the honest limits of that.

Two lists come out of the same stability screen, emailed together:

  1. LOSERS (the original list, and the ONLY one that feeds real buys via
     scripts/buy_consistent_losers.py): stable names that closed DOWN
     today, ranked by today's % loss. ETFs/funds are excluded as of
     2026-09-01 (EXCLUDE_FUNDS_FROM_LOSERS) -- SPY, LQD, VUG and SVXY had
     been qualifying and getting bought as though they were single-name
     surprise drops, which they are not: an index or bond fund falling
     1% is the whole market moving, not a company-specific event this
     strategy can have any edge on. Existing SPY/LQD positions from
     earlier buys are unaffected; the filter only gates new ones.
  2. VOLUME SPIKES (added 2026-08-29, informational only -- deliberately
     stored under a separate state key so the buy script never touches
     it): names whose volume today exploded versus their own prior 20-day
     average volume, ranked by that ratio. Direction-neutral -- a spike
     can come with a price gain or a loss, and today's % move is shown
     either way. The premise is the same as the losers list (something
     happened to a stock that was quiet until now), but volume is often
     the earlier tell: it surges on the day a story breaks, before price
     has finished repricing.

     IMPORTANT -- this list uses PRICE stability only, deliberately
     dropping the volume-stability half of the screen. Requiring 20 days
     of near-flat volume and then a volume explosion is self-defeating:
     measured on 2026-08-28 across the full universe, the full screen
     passed 21 names with a maximum ratio of 1.97x and ZERO at >=2x,
     because the +/-50% volume-stability rule structurally selects for
     stocks that cannot spike. Price-stability-only passed 2,465 names
     (1,191 liquid) and surfaced real events -- SOLS 11.4x on +12.9%,
     ESI 7.2x, PCG 4.6x on $140M traded. "Relatively flat before" is
     therefore interpreted as flat PRICE, which is what makes a volume
     explosion meaningful in the first place.

Runs daily ~4:10 PM ET Mon-Fri (after close, once EOD bars settle).
"""

import json
import re
import requests
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from signal_utils import (
    get_logger, load_state, save_state, send_signal_email,
    html_table, base_html
)

BASE_DIR = Path(__file__).parent.parent
with open(BASE_DIR / "config" / "alpaca_credentials.json") as f:
    creds = json.load(f)
BASE_URL = creds["endpoint"]
DATA_URL = "https://data.alpaca.markets"
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}

log = get_logger("consistent_losers", "consistent_losers.log")

LOOKBACK_DAYS          = 20    # trading days of history required before the drop
MAX_DAILY_PRICE_MOVE   = 5.0   # % -- max allowed day-over-day close move in lookback window
MAX_DAILY_VOLUME_MOVE  = 50.0  # % -- max allowed day-over-day volume change in lookback window
RESULT_COUNT           = 5
VOLUME_SPIKE_COUNT     = 5           # how many volume-spike names to show
MIN_VOLUME_RATIO       = 3.0         # today's volume must be >= this x the prior 20-day average.
                                     # 3x yielded ~28 liquid candidates on 2026-08-28; 5x yielded 6.
MIN_SPIKE_DOLLAR_VOLUME = 1_000_000  # today's $ volume floor -- a 10x ratio on a name that trades
                                     # $40k/day is noise, not a signal. Raise to tighten.
EXCLUDE_FUNDS_FROM_SPIKES = True     # drop ETFs/ETNs/funds from the volume-spike list. Their volume
                                     # surges are usually institutional rebalancing, not a
                                     # company-specific event. Set False to include them.
EXCLUDE_FUNDS_FROM_LOSERS = True     # drop ETFs/ETNs/funds from the losers list too (2026-09-01).
                                     # THIS ONE CHANGES WHAT GETS BOUGHT -- the losers list feeds
                                     # real orders via scripts/buy_consistent_losers.py. SPY, LQD,
                                     # VUG and SVXY previously qualified and were bought; SPY and
                                     # LQD are still open positions from that. Existing holdings are
                                     # untouched, this only gates future buys.
BATCH_SIZE             = 150
CAL_LOOKBACK_CAL_DAYS  = 45    # calendar days fetched, covers LOOKBACK_DAYS+buffer trading days
EARNINGS_DAYS_BEFORE   = 3     # exclude if earnings occurred within the last N calendar days
EARNINGS_DAYS_AFTER    = 5     # exclude if earnings is scheduled within the next N calendar days


def get_trading_calendar() -> list:
    start = (date.today() - timedelta(days=CAL_LOOKBACK_CAL_DAYS)).isoformat()
    end = date.today().isoformat()
    r = requests.get(f"{BASE_URL}/calendar", headers=HEADERS, params={"start": start, "end": end}, timeout=20)
    r.raise_for_status()
    return [d["date"] for d in r.json()]


def resolve_anchor_index(calendar: list) -> int:
    """Index of the last COMPLETE trading session in `calendar`.

    The task is scheduled for ~4:10 PM ET so calendar[-1] is normally the
    session that just closed. But run at any other time -- a manual run,
    a mis-set schedule, a retry before the open -- calendar[-1] is TODAY's
    partial (or not-yet-started) bar, and every metric computed off it is
    wrong in a way that reads as a normal result. Volume ratios are the
    worst case: a half-finished day's volume measured against full-day
    averages always looks low, so the volume-spike list silently comes
    back empty instead of erroring. Verified live -- a run that slipped
    past midnight anchored on an unstarted Monday and returned ratios of
    0.1x across the board."""
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS, timeout=15)
    r.raise_for_status()
    clock = r.json()
    today_iso = clock["timestamp"][:10]

    if calendar[-1] == today_iso:
        # Today is a trading day; it's only complete once its close has passed.
        # When the market is shut and next_close is still today, the session
        # hasn't happened yet (pre-open). Once it has, next_close rolls to a
        # later date.
        if clock.get("is_open") or clock.get("next_close", "")[:10] == today_iso:
            return len(calendar) - 2
    return len(calendar) - 1


def get_universe() -> dict:
    """{symbol: company name} for every non-OTC tradable US equity. Names are
    kept (not just symbols) so the volume-spike list can screen out ETFs --
    Alpaca's asset record has no fund/ETF flag, so the name is the only
    available signal."""
    r = requests.get(f"{BASE_URL}/assets", headers=HEADERS,
                      params={"status": "active", "asset_class": "us_equity"}, timeout=30)
    r.raise_for_status()
    assets = r.json()
    return {a["symbol"]: (a.get("name") or "")
            for a in assets if a.get("tradable") and a["exchange"] != "OTC"}


# Matches ETFs/ETNs/mutual+commodity funds by name. Deliberately does NOT match
# a bare "Trust": 270 names in the universe are operating companies or REITs
# with Trust in the name (Arbor Realty Trust, Acadia Realty Trust, American
# Assets Trust, ...) and blanket-matching would wrongly drop all of them.
# "Trust" only counts when paired with a fund/commodity word, which is what
# catches holdings like "iShares Gold Trust Micro". Validated against the
# universe: flags 5,810 of 13,096, with 22/22 known ETFs caught and 0 false
# positives across a control set of stocks and Trust-named REITs.
FUND_NAME_RE = re.compile(
    r"\bETFs?\b|\bETNs?\b|\bFunds?\b"
    r"|\b(?:Gold|Silver|Platinum|Palladium|Bitcoin|Ether|Ethereum|Crypto|Physical|"
    r"Commodity|Currency|Index|Shares)\b[^,]*\bTrust\b"
    # Fund issuers whose products often omit "ETF" from the name -- mostly
    # leveraged/inverse ProShares ("ProShares UltraShort QQQ" = QID, bought
    # 2026-09-14). Issuer names only: bare "Ultra"/"Vanguard"/"Amplify"/
    # "WisdomTree" would also hit Ultra Clean Holdings, American Vanguard,
    # Amplify Energy and WisdomTree Inc. (checked against the live universe).
    r"|\b(?:ProShares|Direxion|GraniteShares|iShares|SPDR|MiniShares)\b|\bInvesco QQQ\b",
    re.IGNORECASE)


def is_fund(name: str) -> bool:
    return bool(FUND_NAME_RE.search(name or ""))


def fetch_bars_bulk(symbols: list, start: str, end: str) -> dict:
    """Fetch daily bars for a list of symbols, handling per-batch pagination."""
    out = {}
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        page_token = None
        while True:
            params = {
                "symbols":    ",".join(batch),
                "timeframe":  "1Day",
                "start":      start,
                "end":        end,
                "limit":      10000,
                "feed":       "iex",
                "adjustment": "raw",
            }
            if page_token:
                params["page_token"] = page_token
            r = requests.get(f"{DATA_URL}/v2/stocks/bars", headers=HEADERS, params=params, timeout=60)
            if r.status_code != 200:
                log.warning(f"    batch at {i}: HTTP {r.status_code}")
                break
            d = r.json()
            for sym, bars in (d.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars)
            page_token = d.get("next_page_token")
            if not page_token:
                break
        time.sleep(0.2)
    return out


def check_stability(window_dates: list, bar_by_date: dict):
    """window_dates: 22 trading dates ascending, last = today.

    PRICE stability is the hard requirement (breach it and this returns
    None). Volume stability is reported as a `volume_stable` flag rather
    than enforced, because the two lists need different things from it:
    the losers list requires it (unchanged original behavior), while the
    volume-spike list must NOT require it -- see the module docstring.
    There is no price-direction filter here either, so a single pass over
    the bars feeds both lists. Returns None on missing or zero-volume
    days in the window."""
    if any(d not in bar_by_date for d in window_dates):
        return None
    bars = [bar_by_date[d] for d in window_dates]
    today_bar = bars[-1]
    prior = bars[:-1]  # 21 bars before today

    volume_stable = True
    for i in range(1, len(prior)):
        c0, c1 = float(prior[i - 1]["c"]), float(prior[i]["c"])
        v0, v1 = float(prior[i - 1]["v"]), float(prior[i]["v"])
        if c0 <= 0 or v0 <= 0 or v1 <= 0:
            return None
        if abs((c1 - c0) / c0) * 100 > MAX_DAILY_PRICE_MOVE:
            return None
        if abs((v1 - v0) / v0) * 100 > MAX_DAILY_VOLUME_MOVE:
            volume_stable = False

    prev_close   = float(prior[-1]["c"])
    today_close  = float(today_bar["c"])
    today_volume = float(today_bar["v"])
    if prev_close <= 0 or today_close <= 0:
        return None

    prior_vols = [float(b["v"]) for b in prior]
    avg_volume = sum(prior_vols) / len(prior_vols)
    if avg_volume <= 0:
        return None

    return {
        "price":         today_close,
        "volume":        today_volume,
        "pct_change":    (today_close - prev_close) / prev_close * 100,
        "avg_volume":    avg_volume,
        "volume_ratio":  today_volume / avg_volume,
        "dollar_volume": today_close * today_volume,
        "volume_stable": volume_stable,
    }


EARNINGS_HEADLINE_RE = re.compile(r"earnings|\bq[1-4]\s*20\d\d\b|\bq[1-4]\s+results\b", re.IGNORECASE)


def has_nearby_earnings(symbol: str, today: date) -> bool:
    """True if `symbol` has earnings-related news in the last few days, or
    (best-effort) a pre-earnings preview article already published for the
    days ahead.

    NOTE on data source: Alpaca's /v1beta1/corporate-actions endpoint does
    NOT support an "earnings" type at all (confirmed via HTTP 400 -- its
    valid types are all splits/dividends/mergers/etc). earnings_calendar.py
    was built on that same broken assumption and has been silently
    returning nothing this whole time, sitting Disabled and unverified.
    This uses Alpaca's /v1beta1/news instead and keyword-matches headlines
    -- verified against HRL and TPR's real August 2026 earnings reports,
    both of which show up as "... Earnings Call Transcript" / "... Ahead
    Of Q4 Earnings" headlines.

    This is reliable for "just reported" (real earnings triggers a flood of
    same-day headlines) but only best-effort for "about to report" -- it
    only catches an upcoming date if some outlet already published a
    preview mentioning it; there's no guarantee of that for a quiet small
    or mid-cap. Fails open (False) on any API error -- this is a
    supplementary safety filter, not the core consistency check, so a data
    gap shouldn't block a qualifier."""
    try:
        r = requests.get(
            f"{DATA_URL}/v1beta1/news",
            headers=HEADERS,
            params={
                "symbols": symbol,
                "start":   (today - timedelta(days=EARNINGS_DAYS_BEFORE)).isoformat(),
                "end":     (today + timedelta(days=EARNINGS_DAYS_AFTER)).isoformat(),
                "limit":   50,
            },
            timeout=10,
        )
        if r.status_code != 200:
            return False
        for article in r.json().get("news", []):
            if EARNINGS_HEADLINE_RE.search(article.get("headline", "")):
                return True
        return False
    except Exception:
        return False


def take_top_excluding_earnings(ranked: list, count: int, today_obj: date,
                                 cache: dict, excluded: set) -> list:
    """Walk an already-ranked list and take the first `count` entries that
    don't have nearby earnings. Checking lazily down the ranking (rather
    than filtering the whole qualifying set first) keeps the number of news
    API calls proportional to what's actually shown, and `cache` is shared
    across both lists so a symbol appearing in both is only checked once."""
    out = []
    for r in ranked:
        if len(out) >= count:
            break
        sym = r["symbol"]
        if sym not in cache:
            cache[sym] = has_nearby_earnings(sym, today_obj)
            time.sleep(0.15)
        if cache[sym]:
            excluded.add(sym)
            continue
        out.append(r)
    return out


def run():
    log.info("=" * 65)
    log.info(f"CONSISTENT-HISTORY LOSERS MONITOR  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("=" * 65)

    state     = load_state("consistent_losers.json")
    today_str = date.today().isoformat()
    if state.get("last_run") == today_str:
        log.info("  Already ran today — skipping.")
        return

    try:
        calendar = get_trading_calendar()
    except Exception as e:
        log.error(f"  Could not fetch trading calendar: {e}")
        return
    if len(calendar) < LOOKBACK_DAYS + 2:
        log.error(f"  Not enough calendar days ({len(calendar)}) — widen CAL_LOOKBACK_CAL_DAYS")
        return

    try:
        anchor_idx = resolve_anchor_index(calendar)
    except Exception as e:
        log.error(f"  Could not resolve the last complete session: {e}")
        return
    if anchor_idx < LOOKBACK_DAYS + 1:
        log.error(f"  Not enough history before the anchor session — widen CAL_LOOKBACK_CAL_DAYS")
        return

    window_dates = calendar[anchor_idx - (LOOKBACK_DAYS + 1): anchor_idx + 1]  # 22 dates
    today_date   = window_dates[-1]
    prev_date    = window_dates[-2]
    if today_date != calendar[-1]:
        log.info(f"  Note: calendar[-1]={calendar[-1]} is not a completed session — "
                  f"anchoring on {today_date} instead.")

    try:
        universe = get_universe()
    except Exception as e:
        log.error(f"  Could not fetch tradable universe: {e}")
        return
    log.info(f"  Universe: {len(universe)} non-OTC tradable equities")

    try:
        bars_raw = fetch_bars_bulk(list(universe), window_dates[0], today_date)
    except Exception as e:
        log.error(f"  Bulk bar fetch failed: {e}")
        return
    log.info(f"  Got bar data for {len(bars_raw)} symbols")

    bar_by_symbol_date = {}
    for sym, bars in bars_raw.items():
        bar_by_symbol_date[sym] = {b["t"][:10]: b for b in bars}

    losers_today = 0
    stable = []
    for sym, dmap in bar_by_symbol_date.items():
        if today_date not in dmap or prev_date not in dmap:
            continue
        c_prev = float(dmap[prev_date]["c"])
        c_t    = float(dmap[today_date]["c"])
        if c_prev <= 0:
            continue
        if c_t < c_prev:
            losers_today += 1
        result = check_stability(window_dates, dmap)
        if result:
            result["symbol"] = sym
            result["name"] = universe.get(sym, "")
            stable.append(result)
            # Price-stability alone passes thousands of names, so only log the
            # ones that actually land in a list rather than every survivor.
            if (result["volume_stable"] and result["pct_change"] < 0
                    and not (EXCLUDE_FUNDS_FROM_LOSERS and is_fund(result["name"]))):
                log.info(f"    LOSER  {sym}: {result['pct_change']:+.1f}% today, "
                          f"{result['volume_ratio']:.1f}x avg volume")
            elif (result["volume_ratio"] >= MIN_VOLUME_RATIO
                  and result["dollar_volume"] >= MIN_SPIKE_DOLLAR_VOLUME
                  and not (EXCLUDE_FUNDS_FROM_SPIKES and is_fund(result["name"]))):
                log.info(f"    SPIKE  {sym}: {result['volume_ratio']:.1f}x avg volume, "
                          f"{result['pct_change']:+.1f}% today, "
                          f"${result['dollar_volume']:,.0f} traded")

    # Two rankings off one pass. The losers list keeps the ORIGINAL screen
    # (price-stable AND volume-stable AND down) because it drives real
    # orders; the spike list drops the volume-stability half on purpose.
    loser_pool = [r for r in stable if r["volume_stable"] and r["pct_change"] < 0]
    loser_funds_dropped = 0
    if EXCLUDE_FUNDS_FROM_LOSERS:
        before = len(loser_pool)
        loser_pool = [r for r in loser_pool if not is_fund(r["name"])]
        loser_funds_dropped = before - len(loser_pool)
    ranked_losers = sorted(loser_pool, key=lambda x: x["pct_change"])
    spike_pool = [r for r in stable
                  if r["volume_ratio"] >= MIN_VOLUME_RATIO
                  and r["dollar_volume"] >= MIN_SPIKE_DOLLAR_VOLUME]
    funds_dropped = 0
    if EXCLUDE_FUNDS_FROM_SPIKES:
        before = len(spike_pool)
        spike_pool = [r for r in spike_pool if not is_fund(r["name"])]
        funds_dropped = before - len(spike_pool)
    ranked_spikes = sorted(spike_pool, key=lambda x: -x["volume_ratio"])

    today_date_obj = date.fromisoformat(today_date)
    earnings_cache: dict = {}
    earnings_excluded: set = set()
    top    = take_top_excluding_earnings(ranked_losers, RESULT_COUNT,
                                          today_date_obj, earnings_cache, earnings_excluded)
    spikes = take_top_excluding_earnings(ranked_spikes, VOLUME_SPIKE_COUNT,
                                          today_date_obj, earnings_cache, earnings_excluded)
    if earnings_excluded:
        log.info(f"    Excluded for earnings within -{EARNINGS_DAYS_BEFORE}/+{EARNINGS_DAYS_AFTER} days: "
                  f"{', '.join(sorted(earnings_excluded))}")

    state["last_run"] = today_str
    state["last_qualifiers_date"] = today_date
    state["last_qualifiers"] = top
    # Separate key on purpose: buy_consistent_losers.py reads ONLY
    # last_qualifiers, so the volume-spike list stays informational and
    # never turns into an order by itself.
    state["last_volume_spikes"] = spikes
    save_state("consistent_losers.json", state)

    log.info(f"  Session {today_date}: {losers_today:,} stocks down; {len(stable):,} price-stable; "
             f"{len(ranked_losers)} qualify as losers (also volume-stable, "
             f"{loser_funds_dropped} ETFs/funds dropped), "
             f"{len(ranked_spikes)} qualify as volume spikes (>={MIN_VOLUME_RATIO}x, "
             f">=${MIN_SPIKE_DOLLAR_VOLUME:,.0f}, {funds_dropped} ETFs/funds dropped); "
             f"emailing {len(top)} + {len(spikes)}")

    intro = (
        f"<p>Screened all {len(universe):,} non-OTC tradable US equities for the session of "
        f"<strong>{today_date}</strong>. {len(stable):,} were <strong>price-stable</strong> beforehand — no "
        f"day-over-day close move &gt;{MAX_DAILY_PRICE_MOVE:.0f}% across the prior {LOOKBACK_DAYS} trading "
        f"days — i.e. quiet until that day. Names with earnings reported in the last {EARNINGS_DAYS_BEFORE} "
        f"days or scheduled in the next {EARNINGS_DAYS_AFTER} are excluded from both lists.</p>"
    )

    # ── Section 1: losers (this is the list that feeds real buys) ──────────
    if top:
        loser_rows = [[
            f"<strong>{r['symbol']}</strong>",
            f"${r['price']:.2f}",
            f"<strong style='color:#ff6b6b'>{r['pct_change']:.1f}%</strong>",
            f"{r['volume']:,.0f}",
        ] for r in top]
        losers_section = (
            f"<h3 style='margin-top:22px'>📉 Biggest Losers ({losers_today:,} closed down)</h3>"
            f"<p style='color:#888;font-size:12px;margin:4px 0'>Additionally required to have been "
            f"<em>volume</em>-stable (no day-over-day volume swing &gt;{MAX_DAILY_VOLUME_MOVE:.0f}%), then "
            f"ranked by % loss."
            + (f" ETFs and funds excluded ({loser_funds_dropped} dropped this run)."
               if EXCLUDE_FUNDS_FROM_LOSERS else "")
            + f" <strong>This is the list the auto-buy strategy trades at the next open.</strong></p>"
            + html_table(["Symbol", "Close Price", "Today's Move", "Today's Volume"], loser_rows)
        )
    else:
        losers_section = (
            f"<h3 style='margin-top:22px'>📉 Biggest Losers</h3>"
            f"<p>None of the {losers_today:,} decliners today were stable beforehand — "
            f"nothing qualifies for tomorrow's buys.</p>"
        )

    # ── Section 2: volume spikes (informational only, never auto-traded) ───
    if spikes:
        spike_rows = [[
            f"<strong>{r['symbol']}</strong>",
            f"<span style='color:#aaa'>{(r.get('name') or '')[:42]}</span>",
            f"${r['price']:.2f}",
            f"<strong style='color:{'#4caf50' if r['pct_change'] >= 0 else '#ff6b6b'}'>"
            f"{r['pct_change']:+.1f}%</strong>",
            f"<strong style='color:#ffc107'>{r['volume_ratio']:.1f}x</strong>",
            f"{r['volume']:,.0f}",
            f"{r['avg_volume']:,.0f}",
        ] for r in spikes]
        spikes_section = (
            f"<h3 style='margin-top:28px'>📊 Volume Spikes on Previously-Quiet Names</h3>"
            f"<p style='color:#888;font-size:12px;margin:4px 0'>Price-stable names ranked by volume vs. "
            f"that stock's own prior {LOOKBACK_DAYS}-day average (min {MIN_VOLUME_RATIO:.0f}x, min "
            f"${MIN_SPIKE_DOLLAR_VOLUME:,.0f} traded). Volume stability is deliberately <em>not</em> required "
            f"here — demanding flat volume then a volume explosion is self-defeating. Direction-neutral: a "
            f"spike can come with a gain or a loss."
            + (f" ETFs and funds are excluded ({funds_dropped} dropped this run) — their volume surges are "
               f"usually institutional rebalancing rather than a company-specific event." if EXCLUDE_FUNDS_FROM_SPIKES else "")
            + f" <strong>Informational only; nothing here is bought automatically.</strong></p>"
            + html_table(
                ["Symbol", "Name", "Close Price", "Today's Move", "Vol vs Avg",
                 "Today's Volume", f"{LOOKBACK_DAYS}d Avg Volume"],
                spike_rows, color="#4a148c")
        )
    else:
        spikes_section = (
            f"<h3 style='margin-top:28px'>📊 Volume Spikes on Previously-Quiet Names</h3>"
            f"<p>No stable names traded at least {MIN_VOLUME_RATIO:.0f}x their {LOOKBACK_DAYS}-day average "
            f"volume today (with at least ${MIN_SPIKE_DOLLAR_VOLUME:,.0f} traded).</p>"
        )

    content = intro + losers_section + spikes_section

    body_text_parts = []
    if top:
        body_text_parts.append("LOSERS (auto-bought at the next open):\n" + "\n".join(
            f"  {r['symbol']}: {r['pct_change']:.1f}% on ${r['price']:.2f}" for r in top))
    else:
        body_text_parts.append("LOSERS: none qualified today.")
    if spikes:
        body_text_parts.append("VOLUME SPIKES (informational only):\n" + "\n".join(
            f"  {r['symbol']}: {r['volume_ratio']:.1f}x avg volume, {r['pct_change']:+.1f}% "
            f"on ${r['price']:.2f}" for r in spikes))
    else:
        body_text_parts.append("VOLUME SPIKES: none qualified today.")
    body_text = "\n\n".join(body_text_parts)

    # Label with the session actually analyzed, which is not always today
    # (weekend/holiday runs, or any run before that day's close).
    if top and spikes:
        subject = f"📉 {len(top)} Losers + 📊 {len(spikes)} Volume Spikes — {today_date}"
    elif top:
        subject = f"📉 Top {len(top)} Consistent-History Losers — {today_date}"
    elif spikes:
        subject = f"📊 {len(spikes)} Volume Spikes (no qualifying losers) — {today_date}"
    else:
        subject = f"📉 Consistent-History Losers — nothing qualified {today_date}"

    send_signal_email(subject, body_text, base_html("Consistent-History Losers", "📉", content))


if __name__ == "__main__":
    run()
    # Here rather than at the end of run() so early returns count as finished
    # too -- scripts/heartbeat.py treats a run with no "Run complete." as crashed.
    log.info("Run complete.\n")
