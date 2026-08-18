# Pre-registered findings — frozen 2026-08-17

This file freezes the in-sample results so the out-of-sample test cannot be
moved after the fact. The collectors keep running; nothing in this file may be
edited after the freeze (see git tag `v1.0-insample`).

## Sample split

- **In-sample:** markets with `close_ms < 1787011200000` (2026-08-18 00:00 UTC).
  Everything in the README as of the tag derives from this window
  (2026-07-30 → 2026-08-17, 8.06M snapshots).
- **Out-of-sample:** markets with `close_ms >= 1787011200000`. Untouched:
  no query in this repo has read them for strategy evaluation.

## Frozen in-sample claims

| # | Claim | In-sample value |
|---|---|---|
| 1 | Calibration at close | Brier 0.009 (n = 10,217) |
| 2 | Longshot bias at close | 10–15¢ bin realizes 4.0% YES |
| 3 | `lotto_yes` (buy longshots at ask) | **-3.13¢**/contract, t_cl = -9.3 |
| 4 | `fade_no` (buy NO at ask) | +0.20¢/contract, t_cl = +0.6 (≈ zero) |
| 5 | `fav_yes` (buy favorites 80–97¢ at ask) | **+1.55¢**/contract, t_cl = +2.1 |
| 6 | Passive maker, through-price fills | -4.1¢/fill buys, -8.2¢/fill sells |

t_cl = cluster-robust t (clusters = series × event date-hour;
`scripts/robust_stats.py`).

## Pre-registered protocol

**On or after 2026-09-17** (≥ 30 days of new settlements), run on the droplet:

```
MIN_CLOSE_MS=1787011200000 python3 strategy_backtest.py
```

then `robust_stats.py` on the fresh `taker_trades.csv`, with **no changes** to
entry windows (6h → 15min before close), price bands (3–20¢ / 80–97¢), fee
model, or cluster definition. Evaluation, decided now:

- **Primary (claim 5):** `fav_yes` is *validated* if its OOS mean P&L is
  positive with cluster-robust t ≥ 1.0; *refuted* if the mean is ≤ 0;
  otherwise inconclusive — extend one more month, once, and stop.
- **Secondary (claims 3–4):** `lotto_yes` remains negative (t_cl ≤ -2);
  `fade_no` remains inside ±1¢.
- **Calibration (claims 1–2):** OOS Brier at close ≤ 0.03 and the sub-20¢
  bins still realize below their implied probability.

Failures get reported in the README as prominently as successes — a refuted
edge in an efficient market is the expected outcome and is still a result.

## Known limitations accepted at freeze time

- 19 days, one market regime (BTC drifting sideways-up in Aug 2026).
- `fav_yes` is the best of three tested strategies (selection effect — this is
  exactly what the OOS test exists to catch).
- Fee model is continuous `0.07·P·(1−P)`, ignoring per-order cent rounding.
