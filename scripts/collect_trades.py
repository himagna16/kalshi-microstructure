#!/usr/bin/env python3
"""
Kalshi trade-tape collector.

Polls the public global trades feed (GET /markets/trades, newest-first with
cursor pagination) every POLL_S seconds and stores every print in the crypto
series we study into its own SQLite DB.

Deliberately a SEPARATE process and SEPARATE database file from the orderbook
collector: SQLite allows one writer per DB, and the snapshot collector's
19-day unbroken streak is not something to gamble on. This script is
READ-ONLY against Kalshi — market data endpoints are public, no credentials.

Restart-safe: a watermark (last seen trade time) is persisted in the DB, each
poll re-reads a 2-minute overlap, and trade_id is the primary key, so
duplicates are impossible and restarts lose nothing that is still in the feed.
"""

import logging
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
DB_PATH = "/root/kalshi/trades_data.db"
SERIES_PREFIXES = ("KXBTCD-", "KXBTC-", "KXETHD-", "KXSOLD-",
                   "KXXRPD-", "KXDOGED-")
POLL_S = 15          # tape granularity; one request + pagination per poll
OVERLAP_S = 120      # re-read window so a crashed poll can never lose prints
PAGE_LIMIT = 1000    # max the API allows per page
BACKFILL_S = 3600    # first run: pull the last hour
LOG_EVERY_S = 3600

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tape")

running = True
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("running", False))
signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("running", False))


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL;")
    db.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            trade_id   TEXT PRIMARY KEY,
            ticker     TEXT NOT NULL,
            created_ms INTEGER NOT NULL,   -- unix ms
            yes_price  REAL,               -- dollars
            no_price   REAL,
            count      REAL,               -- contracts (count_fp, fractional)
            taker_side TEXT,               -- 'yes' / 'no'
            taker_book_side TEXT,          -- 'bid' / 'ask'
            is_block   INTEGER
        )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_trades_ticker_ts"
               " ON trades(ticker, created_ms)")
    db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    db.commit()
    return db


def get_watermark(db):
    row = db.execute("SELECT v FROM kv WHERE k = 'last_ts'").fetchone()
    return float(row[0]) if row else time.time() - BACKFILL_S


def set_watermark(db, ts):
    db.execute("INSERT INTO kv (k, v) VALUES ('last_ts', ?)"
               " ON CONFLICT(k) DO UPDATE SET v = excluded.v", (str(ts),))


def parse_ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def poll_once(db, session, watermark):
    """Page newest->oldest until we're past watermark-OVERLAP. Returns new watermark."""
    floor_ts = watermark - OVERLAP_S
    cursor, newest = None, watermark
    rows, pages = [], 0
    while pages < 30:                       # hard cap: 30k trades per poll
        params = {"limit": PAGE_LIMIT, "min_ts": int(floor_ts)}
        if cursor:
            params["cursor"] = cursor
        r = session.get(f"{BASE}/markets/trades", params=params, timeout=20)
        r.raise_for_status()
        body = r.json()
        trades = body.get("trades", [])
        pages += 1
        if not trades:
            break
        oldest_in_page = None
        for t in trades:
            ts = parse_ts(t["created_time"])
            oldest_in_page = ts
            newest = max(newest, ts)
            if t["ticker"].startswith(SERIES_PREFIXES):
                rows.append((
                    t["trade_id"], t["ticker"], int(ts * 1000),
                    float(t["yes_price_dollars"]), float(t["no_price_dollars"]),
                    float(t["count_fp"]), t.get("taker_side"),
                    t.get("taker_book_side"), int(t.get("is_block_trade", False)),
                ))
        cursor = body.get("cursor")
        if not cursor or (oldest_in_page and oldest_in_page < floor_ts):
            break
    if rows:
        db.executemany(
            "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?)", rows)
    set_watermark(db, newest)
    db.commit()
    return newest, len(rows), pages


def main():
    db = init_db()
    session = requests.Session()
    watermark = get_watermark(db)
    log.info("starting; watermark=%s", datetime.fromtimestamp(
        watermark, tz=timezone.utc).isoformat())
    inserted_total, last_log = 0, time.time()
    while running:
        t0 = time.time()
        try:
            watermark, n, pages = poll_once(db, session, watermark)
            inserted_total += n
        except Exception as e:              # noqa: BLE001 — keep the tape alive
            log.warning("poll failed: %s; backing off 60s", e)
            time.sleep(60)
            continue
        if time.time() - last_log >= LOG_EVERY_S:
            (total,) = db.execute("SELECT COUNT(*) FROM trades").fetchone()
            log.info("hourly: +%d rows this hour, %d total", inserted_total, total)
            inserted_total, last_log = 0, time.time()
        time.sleep(max(0.0, POLL_S - (time.time() - t0)))
    log.info("SIGTERM: clean shutdown")
    db.commit()
    db.close()


if __name__ == "__main__":
    main()
