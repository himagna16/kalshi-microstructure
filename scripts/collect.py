#!/usr/bin/env python3
"""
Kalshi orderbook collector.

Polls the most active Bitcoin markets on Kalshi and records the top of each
orderbook into a local SQLite database.

This script is READ-ONLY. It never places an order, and it needs no API
credentials at all -- Kalshi's market data endpoints are public. Nothing here
can spend money.

Run it directly to test, or under systemd for continuous collection.
"""

import json
import logging
import signal
import sqlite3
import sys
import time

import requests

# ─────────────────────────── configuration ───────────────────────────
# Everything you'd want to tune lives here, so you never go hunting in the
# code below to change a number.

BASE = "https://api.elections.kalshi.com/trade-api/v2"
DB_PATH = "/root/kalshi/kalshi_data.db"

# Every crypto series Kalshi actually runs. KX<COIN> are range markets
# ("price between X and Y"), KX<COIN>D are threshold markets ("price above X") --
# different structures, worth collecting both for the two liquid coins.
#
# DOGE is deliberately included as a CONTROL: 78% of its markets quote at a 1c
# spread on ~3k total volume, i.e. tight spreads nobody trades. If penny spreads
# without volume turn out to be unfillable, we want that measured, not assumed.
SERIES = [
    "KXBTCD",   # Bitcoin threshold  -- 41 penny-wide, 2.4M volume (the main event)
    "KXBTC",    # Bitcoin range      -- 19 penny-wide, 171k volume
    "KXETHD",   # Ethereum threshold -- 14 penny-wide, 151k volume (clear #2)
    "KXSOLD",   # Solana threshold   -- 48k volume, 0.02 median spread
    "KXXRPD",   # XRP threshold      -- thin, 0.07 median spread
    "KXDOGED",  # Dogecoin threshold -- the control described above
]
TOP_N = 15                    # most-active markets to watch per series
# SELECTION BIAS FIX (2026-08-01): ranking purely by volume meant we only ever
# observed markets in their final hour -- Kalshi crypto markets accumulate
# volume as they approach expiry. The strategy quotes at 240+ minutes out, so
# its entire operating regime was invisible in our own data. These extra slots
# deliberately sample markets FAR from expiry regardless of volume.
FAR_N = 10                    # extra markets per series with >= FAR_MINUTES left
FAR_MINUTES = 240
# HORIZON GAP FIX (2026-08-02): the lead-time histogram of settled markets
# showed coverage collapsing from 550 markets at 60min-before-close to 30 at
# 90min. Cause: volume ranking catches markets in their liquid final hour,
# FAR_N catches them while >= 240min out, and NOTHING held them through the
# 1-4h window between -- which is exactly the horizon the bot trades (its
# expiry cutoff is 240min) and therefore the horizon the thesis must be
# tested at. Two more bands close the hole:
#   MID:  most-active markets with 45-240 min left (the donut hole itself)
#   TAIL: tail-priced markets (best bid within TAIL_MAX_CENTS of either
#         extreme) at any horizon >= 45min. Volume ranking alone underweights
#         these -- tails trade less than at-the-money strikes -- yet they are
#         the only markets the strategy actually quotes.
MID_N = 15
MID_MINUTES = 45
# Sized against reality on 2026-08-02: 304 tail-priced markets existed across
# the six series (74 on KXBTCD alone). 12 covered under a quarter of them;
# 18 x 6 series still fits the cycle budget with headroom (measured 217
# markets -> 31.9s of a 50s cycle).
TAIL_N = 18
TAIL_MAX_CENTS = 12
# 87 markets take ~12.8s to poll, so a 15s cycle left almost no slack -- any
# Kalshi slowdown would make cycles overrun and drift. 20s keeps ~7s of headroom
# and costs nothing analytically: spreads persist for minutes, not seconds.
# 145 markets take ~22s to poll, which overran the old 20s budget and made
# cycles run back-to-back. 35s restores headroom; spread dynamics persist for
# minutes so the coarser sampling costs nothing analytically.
# The MID/TAIL bands (2026-08-02) lift the worst case to ~300 markets ~ 45s
# of polling; 50s keeps headroom at the same ~4-5 requests/sec Kalshi has
# tolerated for days. Rows/day stays ~flat (more markets, slower cadence).
# MEASURED after 24h at 253 markets (audit 2026-08-03): polling averages
# 43.1s and 32% of cycles exceed 50s (max 73.8s). Overruns just stretch that
# cycle -- the loop sleeps only the remainder, cadence self-corrects (avg
# 50.8s, zero data gaps) -- but treat any band growth as needing a POLL bump,
# because the paper headroom is already spent in the p95 tail.
POLL_SECONDS = 50             # how often to snapshot every watched market
REFRESH_MINUTES = 10          # how often to re-pick which markets are active
DEPTH_LEVELS = 5              # how many orderbook price levels to keep per side
REQUEST_TIMEOUT = 10          # give up on a single HTTP request after this long
INTER_REQUEST_SLEEP = 0.05    # small gap between requests; Kalshi allows ~10/sec

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("collector")


# ─────────────────────────── database ───────────────────────────

def open_db(path=DB_PATH):
    """Open the database, creating the file and table if they don't exist yet."""
    conn = sqlite3.connect(path)

    # WAL mode lets another process read the database while this one is
    # writing. Without it, querying mid-collection can hit "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS book_snapshots (
            ts           INTEGER NOT NULL,  -- unix ms when THIS row was fetched
            cycle        INTEGER NOT NULL,  -- unix ms when the polling round began
            ticker       TEXT    NOT NULL,
            yes_bid      REAL,              -- best YES bid in dollars, NULL if book empty
            yes_bid_size REAL,
            yes_ask      REAL,              -- derived as 1 - best NO bid
            yes_ask_size REAL,
            yes_depth    TEXT,              -- JSON array, top DEPTH_LEVELS YES levels
            no_depth     TEXT,              -- JSON array, top DEPTH_LEVELS NO levels
            PRIMARY KEY (ts, ticker)
        )
    """)
    # Makes "give me one ticker's history in time order" fast, which is the
    # query you'll run constantly during analysis.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ticker_ts ON book_snapshots(ticker, ts)")
    # Every dashboard query filters on `cycle` (WHERE cycle = MAX(cycle)).
    # Without this index that's a full table scan -- ~5s at 580k rows, and
    # growing by ~100MB/day.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cycle ON book_snapshots(cycle)")
    conn.commit()
    return conn


def save_rows(conn, rows):
    """Write a batch of snapshot rows in a single transaction."""
    if not rows:
        return
    conn.executemany(
        """INSERT OR REPLACE INTO book_snapshots
           (ts, cycle, ticker, yes_bid, yes_bid_size, yes_ask, yes_ask_size,
            yes_depth, no_depth)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


# ─────────────────────────── Kalshi API ───────────────────────────

def _close_ms(iso_str):
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(
            iso_str.replace("Z", "+00:00")).timestamp() * 1000
    except (TypeError, ValueError, AttributeError):
        return None


def get_active_tickers(series, top_n=TOP_N):
    """
    Pick which markets in `series` to poll, in four bands:

      1. top_n most-traded, any horizon      -- the liquid near-expiry core
      2. MID_N most-traded, 45-240min left   -- the 1-4h donut hole
      3. FAR_N most-traded, >= 240min left   -- the bot's trading regime
      4. TAIL_N tail-priced, >= 45min left   -- the strikes the bot quotes

    Bands 2-4 exist because pure volume ranking only ever saw markets in
    their final hour (the 2026-08-01/02 selection-bias fixes). Each band
    skips tickers an earlier band already took, so quotas are real.
    """
    resp = requests.get(
        f"{BASE}/markets",
        params={"series_ticker": series, "status": "open", "limit": 1000},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()  # turn a 4xx/5xx response into an exception
    markets = resp.json()["markets"]
    now_ms = time.time() * 1000

    # volume_fp arrives as a STRING like "0.00", so it needs float() before
    # any comparison or sorting.
    def vol(m):
        return float(m.get("volume_fp") or 0)

    picked, seen = [], set()

    def take(candidates, n):
        fresh = [m for m in candidates if m["ticker"] not in seen]
        fresh.sort(key=vol, reverse=True)
        for m in fresh[:n]:
            seen.add(m["ticker"])
            picked.append(m["ticker"])

    # Band 1: the markets actually trading right now.
    take([m for m in markets if vol(m) > 0], top_n)

    # Sort every remaining market into its horizon/price bands.
    mids, fars, tails = [], [], []
    for m in markets:
        cms = _close_ms(m.get("close_time"))
        if cms is None:
            continue
        mins_left = (cms - now_ms) / 60000.0
        if mins_left < MID_MINUTES:
            continue                      # final-hour markets: band 1's job
        if mins_left < FAR_MINUTES:
            mids.append(m)
        else:
            fars.append(m)
        # Tail-priced: best bid within TAIL_MAX_CENTS of either extreme.
        # bid == 0 means no bid at all -- a dead book, not a cheap one.
        try:
            bid = float(m.get("yes_bid_dollars") or 0)
        except (TypeError, ValueError):
            bid = 0.0
        if 0 < bid <= TAIL_MAX_CENTS / 100.0 or bid >= 1 - TAIL_MAX_CENTS / 100.0:
            tails.append(m)

    take(mids, MID_N)     # band 2
    take(fars, FAR_N)     # band 3
    take(tails, TAIL_N)   # band 4
    return picked


def best_level(levels):
    """
    Return (price, size) of the best bid, or (None, None) if there are none.

    Kalshi returns levels sorted ascending by price, so the BEST bid is the
    LAST entry -- hence [-1]. Values are strings and need float().
    """
    if not levels:
        return None, None
    price, size = levels[-1]
    return float(price), float(size)


def get_orderbook(ticker):
    """
    Fetch one market's orderbook and return a flat dict of the interesting bits.

    A quirk worth understanding: Kalshi's orderbook has no "ask" side. It gives
    you resting bids on YES and resting bids on NO. Because YES + NO always
    settle to $1, a NO bid at 0.86 is exactly a YES ask at 0.14. That's where
    yes_ask comes from below.
    """
    resp = requests.get(f"{BASE}/markets/{ticker}/orderbook", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    book = resp.json()["orderbook_fp"]

    yes_levels = book.get("yes_dollars") or []
    no_levels = book.get("no_dollars") or []

    yes_bid, yes_bid_size = best_level(yes_levels)
    no_bid, no_bid_size = best_level(no_levels)

    # Convert the best NO bid into the YES ask. If nobody is bidding NO, then
    # there is no YES offer, so leave it as None rather than inventing a price.
    yes_ask = round(1.0 - no_bid, 4) if no_bid is not None else None
    yes_ask_size = no_bid_size

    return {
        "ticker": ticker,
        "yes_bid": yes_bid,
        "yes_bid_size": yes_bid_size,
        "yes_ask": yes_ask,
        "yes_ask_size": yes_ask_size,
        # Keep the top few levels as JSON text. Storing the raw depth now means
        # you can study queue dynamics later; you can always derive top-of-book
        # from depth, but you can never recover depth you didn't save.
        "yes_depth": json.dumps(yes_levels[-DEPTH_LEVELS:]),
        "no_depth": json.dumps(no_levels[-DEPTH_LEVELS:]),
    }


# ─────────────────────────── main loop ───────────────────────────

class Stopper:
    """Catches Ctrl-C / systemd stop so the loop exits cleanly mid-cycle."""

    def __init__(self):
        self.stop = False
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, signum, frame):
        log.info("shutdown signal received, finishing current cycle")
        self.stop = True


def refresh_watchlist():
    """Rebuild the list of tickers to poll, across every configured series."""
    tickers = []
    for series in SERIES:
        try:
            found = get_active_tickers(series)
            tickers.extend(found)
            log.info("watchlist: %s -> %d markets", series, len(found))
        except Exception as exc:
            # One series failing shouldn't lose the others.
            log.warning("watchlist: %s failed: %s", series, exc)
    return tickers


def main():
    stopper = Stopper()
    conn = open_db()
    log.info("collector starting | db=%s | poll=%ss", DB_PATH, POLL_SECONDS)

    watchlist = refresh_watchlist()
    last_refresh = time.time()

    if not watchlist:
        log.error("no markets found on startup -- check connectivity")
        return 1

    cycles = 0
    while not stopper.stop:
        cycle_start = time.time()
        cycle_ms = int(cycle_start * 1000)
        rows = []
        errors = 0

        for ticker in watchlist:
            if stopper.stop:
                break
            try:
                snap = get_orderbook(ticker)
                rows.append((
                    int(time.time() * 1000),
                    cycle_ms,
                    snap["ticker"],
                    snap["yes_bid"],
                    snap["yes_bid_size"],
                    snap["yes_ask"],
                    snap["yes_ask_size"],
                    snap["yes_depth"],
                    snap["no_depth"],
                ))
            except Exception as exc:
                # A single bad market must never kill a 24/7 process. Count it,
                # log it, move on.
                errors += 1
                log.warning("%s failed: %s", ticker, exc)
            time.sleep(INTER_REQUEST_SLEEP)

        try:
            save_rows(conn, rows)
        except Exception as exc:
            log.error("database write failed: %s", exc)

        cycles += 1
        elapsed = time.time() - cycle_start
        log.info("cycle %d: %d rows in %.1fs (%d errors)",
                 cycles, len(rows), elapsed, errors)

        # Re-pick the active markets periodically. Kalshi markets expire and new
        # strikes open, so a watchlist chosen once would slowly go stale and
        # end up polling nothing but dead markets.
        if time.time() - last_refresh > REFRESH_MINUTES * 60:
            new_list = refresh_watchlist()
            if new_list:            # keep the old list if the refresh failed
                watchlist = new_list
            last_refresh = time.time()

        # Sleep only the remaining time, so cycles stay on a steady cadence
        # regardless of how long the requests took.
        remaining = POLL_SECONDS - (time.time() - cycle_start)
        if remaining > 0 and not stopper.stop:
            time.sleep(remaining)

    conn.close()
    log.info("collector stopped after %d cycles", cycles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
