#!/usr/bin/env python3
"""
Two execution-aware backtests on the collected books + settlements.
Runs on the droplet; writes CSV/JSON results to /root/analysis_out/.

A) Settlement-hold taker trades: at the LAST two-sided quote in the window
   [close-6h, close-15min], enter at the displayed price + taker fee
   (0.07*P*(1-P) per contract), hold to settlement. Strategies:
     - fade_no:  buy NO when YES ask is 3-20c (fading the longshot)
     - lotto_yes: buy YES at the ask in the same band (the retail trade)
     - fav_yes:  buy YES when YES ask is 80-97c (backing the favorite)
   Fills assume 1 contract at the displayed touch — top-of-book sizes here
   average hundreds of dollars, so this is realistic for small size.

B) Maker simulation on BTC hourly (KXBTC): at each snapshot, quote both
   sides joined at the touch. A resting quote is filled only when a LATER
   snapshot's opposite side trades strictly THROUGH the price (conservative:
   ignores queue position, requires price improvement through us). Fills pay
   no fee (maker), held to settlement, inventory capped at +/-5 per market.
"""

import csv
import json
import os
import sqlite3

DB = "/root/kalshi/kalshi_data.db"
OUT = "/root/analysis_out"
os.makedirs(OUT, exist_ok=True)

MIN_CLOSE_MS = int(os.environ.get("MIN_CLOSE_MS", 0))
MAX_CLOSE_MS = int(os.environ.get("MAX_CLOSE_MS", 1 << 62))

db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
TWO_SIDED = "yes_bid IS NOT NULL AND yes_ask IS NOT NULL"


def fee(p):
    return 0.07 * p * (1 - p)


# ------------------------------------------------ A) settlement-hold takers
print("A) taker entries at last quote before close", flush=True)
settled = db.execute("""
    SELECT s.ticker, s.series, s.result = 'yes', s.close_ms
    FROM settled_markets s
    WHERE s.close_ms IS NOT NULL
      AND s.close_ms >= ? AND s.close_ms < ?
      AND EXISTS (SELECT 1 FROM book_snapshots b WHERE b.ticker = s.ticker)
""", (MIN_CLOSE_MS, MAX_CLOSE_MS)).fetchall()

H_MIN_MS = 15 * 60 * 1000          # entry no later than 15 min before close
H_MAX_MS = 6 * 3600 * 1000         # and no earlier than 6h before close
trades = []
for ticker, series, won, close_ms in settled:
    row = db.execute(
        f"""SELECT yes_bid, yes_ask FROM book_snapshots
            WHERE ticker = ? AND ts <= ? AND ts >= ? AND {TWO_SIDED}
            ORDER BY ts DESC LIMIT 1""",
        (ticker, close_ms - H_MIN_MS, close_ms - H_MAX_MS)).fetchone()
    if not row:
        continue
    yes_bid, yes_ask = row
    if not (0 < yes_bid <= yes_ask < 1):
        continue
    if 0.03 <= yes_ask <= 0.20:
        p_no = 1 - yes_bid                       # NO ask = 1 - YES bid
        trades.append([ticker, series, "fade_no", yes_ask, p_no,
                       (0 if won else 1) - p_no - fee(p_no)])
        trades.append([ticker, series, "lotto_yes", yes_ask, yes_ask,
                       (1 if won else 0) - yes_ask - fee(yes_ask)])
    if 0.80 <= yes_ask <= 0.97:
        trades.append([ticker, series, "fav_yes", yes_ask, yes_ask,
                       (1 if won else 0) - yes_ask - fee(yes_ask)])

with open(os.path.join(OUT, "taker_trades.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["ticker", "series", "strategy", "yes_ask", "entry_price", "pnl"])
    w.writerows(trades)
print(f"   {len(trades)} trade records", flush=True)

# ------------------------------------------------ B) maker sim on KXBTC
print("B) maker sim on KXBTC (through-price fills)", flush=True)
results = {t: r for t, r in db.execute(
    "SELECT ticker, result = 'yes' FROM settled_markets"
    " WHERE (series = 'KXBTC' OR ticker LIKE 'KXBTC-%')"
    " AND close_ms >= ? AND close_ms < ?", (MIN_CLOSE_MS, MAX_CLOSE_MS))}

INV_CAP = 5
fills, mkt_pnl = [], {}
cur = db.execute(f"""
    SELECT ticker, ts, yes_bid, yes_ask FROM book_snapshots
    WHERE ticker LIKE 'KXBTC-%' AND {TWO_SIDED}
    ORDER BY ticker, ts""")
prev_ticker, my_bid, my_ask, inv = None, None, None, 0
for ticker, ts, bid, ask in cur:
    if ticker not in results:
        continue
    won = results[ticker]
    if ticker != prev_ticker:
        prev_ticker, my_bid, my_ask, inv = ticker, None, None, 0
    # fills against quotes posted at the previous snapshot
    if my_bid is not None and ask <= my_bid - 0.001 and inv < INV_CAP:
        fills.append([ticker, ts, "buy", my_bid, won - my_bid])
        inv += 1
    if my_ask is not None and bid >= my_ask + 0.001 and inv > -INV_CAP:
        fills.append([ticker, ts, "sell", my_ask, my_ask - won])
        inv -= 1
    if 0 < bid <= ask < 1 and ask - bid <= 0.10:
        my_bid, my_ask = bid, ask               # re-join the touch
    else:
        my_bid, my_ask = None, None             # stand aside in junk books

with open(os.path.join(OUT, "maker_fills.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["ticker", "ts", "side", "price", "pnl"])
    w.writerows(fills)

n_mkts = len({f[0] for f in fills})
summary = {
    "taker_trades": len(trades),
    "maker_fills": len(fills),
    "maker_markets": n_mkts,
    "maker_total_pnl": round(sum(f[4] for f in fills), 2),
}
with open(os.path.join(OUT, "backtest_summary.json"), "w") as f:
    json.dump(summary, f, indent=2)
print(f"   {len(fills)} fills across {n_mkts} markets; DONE", flush=True)
