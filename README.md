# Kalshi Crypto Market Microstructure

**19 days of continuous orderbook data from Kalshi's crypto event contracts —
8.06M snapshots across 54,380 contracts — with an analysis of liquidity,
trading costs, and price calibration.**

Between July 30 and August 17, 2026, a collector on a $6/month DigitalOcean
droplet polled the Kalshi API roughly every 42 seconds (36,611 polling cycles,
zero missed days) and recorded top-of-book quotes, sizes, and depth for every
active contract in six crypto series — BTC, ETH, SOL, XRP, and DOGE hourly and
daily threshold markets — plus the settlement outcome of 427,017 markets.

![Collector uptime](charts/daily_collection.png)

## TL;DR

1. **Most listed contracts are never tradable.** 79% of the 54,380 contracts
   observed never showed a two-sided book. Liquidity concentrates in ~9k
   near-the-money strikes.
2. **Where books exist, they're tight** — 44% of two-sided snapshots have a 1¢
   spread; 84% are 3¢ or less.
3. **But a round trip in a mid-priced contract costs ~9–11¢.** Spread plus
   Kalshi's taker fee means a 40–50¢ contract must move ~10.7¢ in your favor
   before you make a cent. This is why naive taker momentum strategies die here.
4. **Prices are extremely well calibrated.** Brier score 0.009 at close, 0.062
   even 24h out (vs ~0.24 for guessing the base rate). The market knows.
5. **Classic favorite–longshot bias.** At close, contracts priced 10–15¢
   realize YES only 4% of the time; contracts priced 95¢+ realize 99.9%
   (vs 98.0 implied). Longshots are systematically overpriced.

## The dataset

| | |
|---|---|
| Window | 2026-07-30 → 2026-08-17, no gaps |
| Snapshots | 8,055,608 (~455k/day) |
| Contracts observed | 54,380 across 6 series |
| Polling cadence | ~42s per cycle, ~220 active markets/cycle |
| Settlements recorded | 427,017 (42% YES), 52,732 with book history |
| Snapshot fields | best YES bid/ask, sizes, top depth levels (JSON), timestamps |

Schema: `book_snapshots(ts, cycle, ticker, yes_bid, yes_bid_size, yes_ask,
yes_ask_size, yes_depth, no_depth)` + `settled_markets(ticker, series, result,
close_time, close_ms, ...)` in SQLite (WAL mode, written by a long-running
`systemd` service).

## Findings

### 1. The liquidity illusion

Of 54,380 contracts listed during the window:

| | contracts | share |
|---|---|---|
| Never had a two-sided book | 43,200 | 79.4% |
| Two-sided under half the time | 2,285 | 4.2% |
| Two-sided most of the time | 8,895 | 16.4% |

Snapshot-level: only **38%** of all snapshots had both a bid and an ask; 61%
were one-sided, 1% empty. Threshold-ladder markets list dozens of strikes, and
market makers only quote near the money.

The hierarchy across series is stark:

| Series | Avg spread | Avg top-of-book notional | % two-sided |
|---|---|---|---|
| BTC daily (KXBTCD) | 1.1¢ | $2,472 | 66% |
| ETH daily (KXETHD) | 1.7¢ | $1,701 | 32% |
| BTC hourly (KXBTC) | 2.8¢ | $181 | 72% |
| SOL daily (KXSOLD) | 2.5¢ | $1,135 | 37% |
| XRP daily (KXXRPD) | 3.1¢ | $894 | 19% |
| DOGE daily (KXDOGED) | 7.3¢ | $891 | 5% |

BTC hourly is *frequently* quoted but *thin* ($181 at the touch) — you can
almost always trade it, but not in size. DOGE is functionally dead.

### 2. Spreads are tight where books exist

![Spread distribution](charts/spread_distribution.png)

The modal two-sided book is 1¢ wide. The trap isn't the spread on liquid
contracts — it's what happens when you add fees.

### 3. The real cost of a round trip

Kalshi charges takers `0.07 × P × (1−P)` per contract per side. That fee is
maximized exactly where event trading is interesting — at uncertain prices —
and it stacks on top of a spread that is also widest mid-range:

![Cost to trade](charts/cost_to_trade.png)

A contract trading at 40–50¢ must move **10.7¢** (≈ 11 percentage points of
implied probability) before a taker round trip breaks even. Even at the tails
the hurdle is 2.6¢. For comparison: the entire 24h Brier improvement from 24h
to close (0.062 → 0.009) implies typical repricing on the order of a few
cents — the market usually doesn't move enough, predictably enough, to clear
the fee hurdle from the taker side. **This kills naive taker momentum
strategies structurally, not incidentally.** Any viable strategy here is
maker-side (fee-free fills, earn the spread) or holds to settlement.

### 4. Prices are well calibrated — with a longshot bias

For every settled market, take the last two-sided mid at a given horizon
before close and compare implied probability to realized outcomes:

![Calibration](charts/calibration.png)

- **Brier 0.009 at close** (n=10,217), 0.048 at 6h, 0.062 at 24h. Guessing the
  42% base rate every time scores ~0.244 — the market at close has ~96% skill.
- The small long-horizon samples are themselves a finding: most of these
  markets **live for less than a day** (hourlies for less than an hour), so
  very few even exist 24h before close.
- **Favorite–longshot bias, textbook form:** at close, the 10–15¢ bin
  (avg mid 12.1¢) realizes YES just **4.0%** of the time; the 15–20¢ bin
  realizes 8.0%. On the other side, 75–85¢ contracts realized 100% and 95¢+
  realized 99.87% vs 98.0 implied. Buying cheap lottery tickets is
  systematically -EV; the market overprices tail risk in the direction retail
  wants to bet, mirroring decades of results from horse racing and sports
  betting markets.

### 5. Liquidity has a schedule

![Time of day](charts/time_of_day.png)

Quoting rates crater between 1–4pm ET (28% two-sided at 3pm vs ~42% overnight)
and average spreads spike around 3–4am ET. If you need to get in or out of
these markets, the afternoon air-pocket is the worst time to need it.

### 6. What survives execution costs — backtests

Finding #4 says longshots are overpriced *at the mid*. Can you actually make
money on that? Three settlement-hold strategies, entered at the **last
displayed quote** between 6h and 15min before close, one contract at the
touch, taker fees paid ([scripts/strategy_backtest.py](scripts/strategy_backtest.py)):

![Strategy P&L](charts/strategy_pnl.png)

- **Buying longshots at the ask loses 3.1¢/contract (t = -8.9).** On a ~10¢
  ticket that's a ~-25% return per trade. The retail lottery is provably,
  massively -EV.
- **But fading them earns nothing (+0.2¢, t = 0.6).** To sell the longshot you
  buy NO at ~90¢: crossing that spread plus the fee consumes the entire bias.
  The market is *inefficient at the mid and efficient at the touch* — the
  textbook no-free-lunch result, measured.
- **Backing favorites at 80–97¢ nets +1.5¢/contract (t = 2.2)** — the one
  candidate edge. Treat the t-stat skeptically: same-hour strikes settle on
  the same underlying print, so outcomes are correlated and effective sample
  size is well below n = 1,145; and it's the best of three tested strategies.
- **The passive maker gets run over.** Quoting both sides at the touch on BTC
  hourly, with fills only when price trades *through* the quote: buys lose
  4.1¢/fill, sells 8.2¢/fill, -$913 total across 14,690 fills — versus 1–3¢
  of spread capture. Through-price fills are the pessimistic bound (every fill
  is by definition adversely selected, and benign at-price fills are missed),
  but the size of the number shows why market making without pull logic or a
  fair-value signal is structurally losing, even fee-free.

## Caveats — read before believing

- **Six crypto series ≠ Kalshi.** Politics, econ, weather markets may differ.
- **At-close mids can be stale.** As books thin near expiry, "last two-sided
  quote" may predate close by up to 24h (the staleness cap). Some calibration
  error at short horizons is quote-staleness, not mispricing.
- **Fee model is simplified.** Continuous `0.07·P·(1−P)` per side, ignoring
  per-order rounding-up and assuming taker both ways. Maker fills pay no fee
  on these series — which is the point of finding #3.
- **Longshot-bias bins are small** mid-range (tens of observations). The tail
  bins (n=174–4,925) carry the conclusion.
- **Top-of-book only** for spread/cost stats; depth JSON is collected but not
  yet used in this analysis.

## Repo layout

```
scripts/aggregate_droplet.py   # runs on the droplet, streams SQL aggregates → CSV
scripts/make_charts.py         # renders charts/ from data/
data/                          # small CSV aggregates (the 2.6GB raw DB stays on the droplet)
charts/                        # PNGs used above
```

Reproduce: run `aggregate_droplet.py` wherever the SQLite DB lives, copy
`analysis_out/` to `data/`, then `python scripts/make_charts.py`.

## Infrastructure & the storage problem

Everything runs on one 1 vCPU / 1GB / 24GB droplet ($6/mo):

- Book DB: **2.6GB, growing ~145MB/day** (~4.4GB/month)
- Nightly `trading_state` backups: **2.5GB and compounding** (14 kept, each
  ~19MB and growing)
- Headroom: 14GB free → **~2.8 months at current burn**

Fixes now in place:

1. **Per-prefix backup retention** — the big append-only book DB keeps 2 daily
   + 1 weekly nightly copies; the small critical trading state keeps 5 + 4.
   Freed 1.4GB immediately and bounds backup growth.
2. **Monthly cold archive** ([scripts/archive_books.py](scripts/archive_books.py),
   installed as `kalshi-archive.timer`) — once a calendar month is fully older
   than 30 days it's streamed to `books-YYYYMM.csv.gz` (~6× smaller), the row
   count is verified against the DB, and only then are rows deleted and the DB
   vacuumed (collector paused for the vacuum only). Caps the live DB at ~two
   months of data.
3. Off-box copies of the latest backups live on a local machine; DigitalOcean
   block storage at $1/10GB/mo is the fallback if the archive falls behind.

## What's next

- **Collect the trade tape.** Kalshi exposes a trades endpoint the collector
  doesn't poll yet. With actual prints, the maker sim gets queue-aware fills
  instead of the through-price bound — the single highest-value data upgrade.
- **The favorites edge, properly.** Cluster-robust errors (by settlement
  hour × underlying) on the +1.5¢ result, and out-of-sample validation as
  more data accumulates.
- **Depth-weighted mids**: the depth JSON is sitting there unused — recompute
  calibration with size-weighted prices.
- **Cross-market consistency**: BTC hourly vs daily threshold ladders imply
  overlapping distributions — do they agree?
