#!/usr/bin/env python3
"""
Aggregate Kalshi orderbook snapshots into small CSV summaries.

Runs ON the droplet (1 vCPU / 1GB RAM) against kalshi_data.db, so everything
streams through SQL aggregates — nothing loads the 8M-row table into memory.
Outputs land in /root/analysis_out/ and get scp'd to the analysis machine.

Data model reminder:
  book_snapshots(ts ms, cycle ms, ticker, yes_bid, yes_bid_size,
                 yes_ask, yes_ask_size, yes_depth JSON, no_depth JSON)
  settled_markets(ticker PK, series, result 'yes'/'no', close_time, close_ms, ...)

Kalshi taker fee per contract: 0.07 * P * (1 - P) dollars (rounded up to the
cent per order in reality; we use the continuous form).
"""

import csv
import json
import os
import sqlite3
import time

DB = "/root/kalshi/kalshi_data.db"
OUT = "/root/analysis_out"
ET_OFFSET_MS = -4 * 3600 * 1000  # EDT (Aug 2026). Collection window is all summer.

os.makedirs(OUT, exist_ok=True)
db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)  # read-only; temp tables still allowed

TWO_SIDED = "yes_bid IS NOT NULL AND yes_ask IS NOT NULL"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def write_csv(name, header, rows):
    with open(os.path.join(OUT, name), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    log(f"wrote {name} ({len(rows)} rows)")


# ---------------------------------------------------------------- 1. summary
log("1/8 global summary")
n, ts_min, ts_max, n_tickers = db.execute(
    "SELECT COUNT(*), MIN(ts), MAX(ts), COUNT(DISTINCT ticker) FROM book_snapshots"
).fetchone()
n_cycles = db.execute("SELECT COUNT(DISTINCT cycle) FROM book_snapshots").fetchone()[0]
n_settled, n_yes = db.execute(
    "SELECT COUNT(*), SUM(result = 'yes') FROM settled_markets"
).fetchone()

# ------------------------------------------------------- 2. quote-state census
log("2/8 quote-state census (one scan)")
two, one, empty = db.execute(f"""
    SELECT
      SUM({TWO_SIDED}),
      SUM((yes_bid IS NULL) != (yes_ask IS NULL)),
      SUM(yes_bid IS NULL AND yes_ask IS NULL)
    FROM book_snapshots
""").fetchone()
write_csv("quote_state.csv", ["state", "snapshots"],
          [["two_sided", two], ["one_sided", one], ["empty", empty]])

# ---------------------------------------- 3. spread histogram + price buckets
log("3/8 spread stats by price bucket")
rows = db.execute(f"""
    SELECT
      MIN(CAST((yes_bid + yes_ask) * 5 AS INT), 9)      AS bucket,  -- mid deciles
      CAST(ROUND((yes_ask - yes_bid) * 100) AS INT)      AS spread_c,
      COUNT(*),
      AVG(yes_bid_size), AVG(yes_ask_size)
    FROM book_snapshots WHERE {TWO_SIDED} AND yes_ask >= yes_bid
    GROUP BY 1, 2
""").fetchall()
write_csv("spread_by_bucket.csv",
          ["mid_decile", "spread_cents", "n", "avg_bid_size", "avg_ask_size"], rows)

# ------------------------------------------------------- 4. per-ticker rollup
log("4/8 per-ticker rollup -> series table + liquidity distribution")
db.execute("""
    CREATE TEMP TABLE per_ticker AS
    SELECT ticker,
           CASE WHEN instr(ticker, '-') > 0
                THEN substr(ticker, 1, instr(ticker, '-') - 1)
                ELSE ticker END                          AS series,
           COUNT(*)                                      AS n_snaps,
           SUM(yes_bid IS NOT NULL AND yes_ask IS NOT NULL) AS n_two_sided,
           AVG(CASE WHEN yes_bid IS NOT NULL AND yes_ask IS NOT NULL
                    THEN yes_ask - yes_bid END)          AS avg_spread,
           AVG(CASE WHEN yes_bid IS NOT NULL AND yes_ask IS NOT NULL
                    THEN yes_bid_size * yes_bid + yes_ask_size * yes_ask END)
                                                         AS avg_top_notional
    FROM book_snapshots GROUP BY ticker
""")
rows = db.execute("""
    SELECT series, COUNT(*), SUM(n_snaps), SUM(n_two_sided),
           1.0 * SUM(n_two_sided) / SUM(n_snaps),
           AVG(avg_spread), AVG(avg_top_notional)
    FROM per_ticker GROUP BY series
    ORDER BY SUM(n_two_sided) DESC LIMIT 40
""").fetchall()
write_csv("top_series.csv",
          ["series", "n_tickers", "n_snaps", "n_two_sided",
           "frac_two_sided", "avg_spread", "avg_top_notional"], rows)

rows = db.execute("""
    SELECT CASE
             WHEN n_two_sided = 0                 THEN 'never_quoted'
             WHEN 1.0 * n_two_sided / n_snaps < 0.5 THEN 'sometimes_quoted'
             ELSE 'usually_quoted' END            AS bucket,
           COUNT(*)
    FROM per_ticker GROUP BY 1
""").fetchall()
write_csv("ticker_liquidity_census.csv", ["bucket", "n_tickers"], rows)

# ------------------------------------------------------------- 5. calibration
log("5/8 calibration at 4 horizons (index seeks per settled ticker)")
HORIZONS_H = [0, 1, 6, 24]
MAX_STALE_MS = 24 * 3600 * 1000
settled = db.execute("""
    SELECT s.ticker, s.result = 'yes', s.close_ms
    FROM settled_markets s
    WHERE s.close_ms IS NOT NULL
      AND EXISTS (SELECT 1 FROM book_snapshots b WHERE b.ticker = s.ticker)
""").fetchall()
log(f"   {len(settled)} settled tickers with book history")

cal_rows, brier = [], {}
for h in HORIZONS_H:
    h_ms = h * 3600 * 1000
    bins = {}          # bin (0..19) -> [n, wins, sum_mid]
    se_sum, n_obs = 0.0, 0
    for ticker, won, close_ms in settled:
        cutoff = close_ms - h_ms
        row = db.execute(
            f"""SELECT yes_bid, yes_ask FROM book_snapshots
                WHERE ticker = ? AND ts <= ? AND ts >= ? AND {TWO_SIDED}
                ORDER BY ts DESC LIMIT 1""",
            (ticker, cutoff, cutoff - MAX_STALE_MS),
        ).fetchone()
        if not row:
            continue
        mid = (row[0] + row[1]) / 2
        if not (0 <= mid <= 1):
            continue
        b = min(int(mid * 20), 19)
        st = bins.setdefault(b, [0, 0, 0.0])
        st[0] += 1
        st[1] += won
        st[2] += mid
        se_sum += (mid - won) ** 2
        n_obs += 1
    brier[h] = {"brier": se_sum / n_obs if n_obs else None, "n": n_obs}
    for b, (bn, bw, bs) in sorted(bins.items()):
        cal_rows.append([h, b * 5, bn, bw, bs / bn])
    log(f"   horizon {h}h done: n={n_obs}")
write_csv("calibration.csv",
          ["horizon_h", "bin_low_cents", "n", "yes_wins", "avg_mid"], cal_rows)

# ------------------------------------------------------------ 6. cost to trade
log("6/8 round-trip taker cost by price bucket")
rows = db.execute(f"""
    SELECT
      MIN(CAST((yes_bid + yes_ask) * 5 AS INT), 9),
      COUNT(*),
      AVG(yes_ask - yes_bid),
      AVG(2 * 0.07 * ((yes_bid + yes_ask) / 2) * (1 - (yes_bid + yes_ask) / 2)),
      AVG((yes_ask - yes_bid)
          + 2 * 0.07 * ((yes_bid + yes_ask) / 2) * (1 - (yes_bid + yes_ask) / 2))
    FROM book_snapshots WHERE {TWO_SIDED} AND yes_ask >= yes_bid
    GROUP BY 1
""").fetchall()
write_csv("cost_to_trade.csv",
          ["mid_decile", "n", "avg_spread", "avg_round_trip_fee",
           "avg_breakeven_move"], rows)

# ------------------------------------------------------------- 7. time of day
log("7/8 time-of-day profile (ET)")
rows = db.execute(f"""
    SELECT CAST(strftime('%H', (ts + {ET_OFFSET_MS}) / 1000, 'unixepoch') AS INT),
           COUNT(*),
           AVG({TWO_SIDED}),
           AVG(CASE WHEN {TWO_SIDED} THEN yes_ask - yes_bid END)
    FROM book_snapshots GROUP BY 1 ORDER BY 1
""").fetchall()
write_csv("time_of_day.csv",
          ["hour_et", "n_snaps", "frac_two_sided", "avg_spread"], rows)

# ---------------------------------------------------------------- 8. by day
log("8/8 daily activity")
rows = db.execute(f"""
    SELECT date(ts / 1000, 'unixepoch'), COUNT(*),
           COUNT(DISTINCT ticker),
           COUNT(DISTINCT CASE WHEN {TWO_SIDED} THEN ticker END)
    FROM book_snapshots GROUP BY 1 ORDER BY 1
""").fetchall()
write_csv("daily.csv", ["date", "n_snaps", "uniq_tickers", "uniq_quoted"], rows)

summary = {
    "snapshots": n, "ts_min_ms": ts_min, "ts_max_ms": ts_max,
    "distinct_tickers": n_tickers, "cycles": n_cycles,
    "settled_markets": n_settled, "settled_yes": n_yes,
    "settled_with_books": len(settled),
    "quote_state": {"two_sided": two, "one_sided": one, "empty": empty},
    "brier_by_horizon": brier,
}
with open(os.path.join(OUT, "summary.json"), "w") as f:
    json.dump(summary, f, indent=2)
log("DONE")
