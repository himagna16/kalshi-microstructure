#!/usr/bin/env python3
"""Render the report charts from the droplet's CSV aggregates.

Reads  data/*.csv + data/summary.json   (produced by aggregate_droplet.py)
Writes charts/*.png                     (light mode, 2x scale)

Palette + chart chrome follow the validated reference palette:
categorical slots 1-3 (blue #2a78d6, orange #eb6834, aqua #1baf7a) are the
only slots used, which keeps every chart inside the all-pairs CVD-safe cap.
"""

import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CHARTS = os.path.join(ROOT, "charts")
os.makedirs(CHARTS, exist_ok=True)

# --- reference palette (light mode) ----------------------------------------
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE, "savefig.dpi": 200,
    "font.family": "sans-serif", "font.size": 10,
    "text.color": INK, "axes.labelcolor": INK_2,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.edgecolor": BASELINE, "axes.linewidth": 0.8,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.titlesize": 11, "axes.titleweight": "bold",
    "axes.titlecolor": INK, "legend.frameon": False,
})


def style(ax, ygrid_only=True):
    if ygrid_only:
        ax.grid(axis="x", visible=False)
    ax.set_axisbelow(True)


def save(fig, name):
    fig.tight_layout()
    fig.savefig(os.path.join(CHARTS, name), bbox_inches="tight")
    plt.close(fig)
    print(f"wrote charts/{name}")


summary = json.load(open(os.path.join(DATA, "summary.json")))

# --- 1. calibration (flagship) ----------------------------------------------
cal = pd.read_csv(os.path.join(DATA, "calibration.csv"))
fig, ax = plt.subplots(figsize=(7, 5.4))
ax.plot([0, 1], [0, 1], color=BASELINE, lw=1, ls="--", zorder=1)
MIN_BIN_N = 30
for h, color in [(0, BLUE), (6, ORANGE), (24, AQUA)]:
    d = cal[(cal.horizon_h == h) & (cal.n >= MIN_BIN_N)].sort_values("avg_mid")
    b = summary["brier_by_horizon"][str(h)]
    ax.plot(d.avg_mid, d.yes_wins / d.n, color=color, lw=2, marker="o",
            ms=4, label=(f"{h}h before close" if h else "At close")
                  + f"  (Brier {b['brier']:.3f}, n={b['n']:,})", zorder=3)
ax.set_xlim(0, 1); ax.set_ylim(0, 1)
ax.set_xlabel("Market mid price (implied probability of YES)")
ax.set_ylabel("Realized YES frequency")
ax.set_title("Kalshi mid prices are well calibrated — even 24h out")
ax.legend(loc="upper left", labelcolor=INK_2)
style(ax, ygrid_only=False)
save(fig, "calibration.png")

# --- 2. cost to trade --------------------------------------------------------
ctt = pd.read_csv(os.path.join(DATA, "cost_to_trade.csv")).sort_values("mid_decile")
labels = [f"{d*10}–{d*10+10}¢" for d in ctt.mid_decile]
x = range(len(ctt))
fig, ax = plt.subplots(figsize=(7, 4.6))
ax.bar(x, ctt.avg_spread * 100, color=BLUE, width=0.62, label="Avg bid–ask spread")
ax.bar(x, ctt.avg_round_trip_fee * 100, bottom=ctt.avg_spread * 100,
       color=ORANGE, width=0.62, label="Round-trip taker fees")
for i, total in enumerate(ctt.avg_breakeven_move * 100):
    ax.annotate(f"{total:.1f}", (i, total), textcoords="offset points",
                xytext=(0, 4), ha="center", fontsize=8.5, color=INK_2)
ax.set_xticks(list(x), labels)
ax.set_xlabel("Contract mid price")
ax.set_ylabel("Cents per contract")
ax.set_title("What a round trip costs: the move you must capture to break even")
ax.legend(labelcolor=INK_2)
style(ax)
save(fig, "cost_to_trade.png")

# --- 3. spread distribution --------------------------------------------------
sb = pd.read_csv(os.path.join(DATA, "spread_by_bucket.csv"))
hist = sb.groupby("spread_cents", as_index=False).n.sum()
hist["bucket"] = hist.spread_cents.clip(upper=20)
hist = hist.groupby("bucket", as_index=False).n.sum()
hist = hist[hist.bucket >= 1]
share = hist.n / hist.n.sum() * 100
fig, ax = plt.subplots(figsize=(7, 4.2))
ax.bar(hist.bucket, share, color=BLUE, width=0.7)
ax.set_xticks(range(1, 21),
              [str(c) if c < 20 else "20+" for c in range(1, 21)])
ax.set_xlabel("Bid–ask spread (cents)")
ax.set_ylabel("% of two-sided snapshots")
ax.set_title("Spread distribution across all two-sided books")
style(ax)
save(fig, "spread_distribution.png")

# --- 4. time of day (two panels, shared x — never a dual axis) --------------
tod = pd.read_csv(os.path.join(DATA, "time_of_day.csv")).sort_values("hour_et")
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7, 5.6), sharex=True)
ax1.plot(tod.hour_et, tod.frac_two_sided * 100, color=BLUE, lw=2)
ax1.set_ylabel("% books two-sided")
ax1.set_title("Liquidity by hour (ET): quoting rate…")
ax2.plot(tod.hour_et, tod.avg_spread * 100, color=BLUE, lw=2)
ax2.set_ylabel("Avg spread (¢)")
ax2.set_xlabel("Hour of day (ET)")
ax2.set_title("…and how wide the quotes are")
ax2.set_xticks(range(0, 24, 3))
for ax in (ax1, ax2):
    style(ax, ygrid_only=False)
save(fig, "time_of_day.png")

# --- 5. collection reliability ----------------------------------------------
daily = pd.read_csv(os.path.join(DATA, "daily.csv"))
daily = daily[daily.date < daily.date.max()]  # drop today's partial day
fig, ax = plt.subplots(figsize=(7, 3.8))
ax.bar(range(len(daily)), daily.n_snaps / 1000, color=BLUE, width=0.7)
ax.set_xticks(range(len(daily)),
              [d[5:] for d in daily.date], rotation=45, fontsize=8)
ax.set_ylabel("Snapshots (thousands)")
ax.set_title("Collector uptime: every day since launch, no gaps")
style(ax)
save(fig, "daily_collection.png")

print("all charts rendered")

# --- 6. strategy backtest (only if backtest results are present) -------------
tt_path = os.path.join(DATA, "taker_trades.csv")
if os.path.exists(tt_path):
    t = pd.read_csv(tt_path)
    m = pd.read_csv(os.path.join(DATA, "maker_fills.csv"))
    rows = []
    for label, pnl in [
        ("Buy longshots at ask\n(the retail trade)", t[t.strategy == "lotto_yes"].pnl),
        ("Fade longshots: buy NO\nat ask, hold to settle", t[t.strategy == "fade_no"].pnl),
        ("Back favorites at ask\n(80–97¢)", t[t.strategy == "fav_yes"].pnl),
        ("Passive maker, buys\n(through-price fills)", m[m.side == "buy"].pnl),
        ("Passive maker, sells\n(through-price fills)", m[m.side == "sell"].pnl),
    ]:
        rows.append((label, pnl.mean() * 100,
                     1.96 * pnl.std() * 100 / len(pnl) ** 0.5, len(pnl)))
    fig, ax = plt.subplots(figsize=(7.6, 4.8))
    ys = range(len(rows))[::-1]
    for y, (label, mean, ci, n) in zip(ys, rows):
        ax.barh(y, mean, color=BLUE if mean >= 0 else ORANGE, height=0.55)
        ax.errorbar(mean, y, xerr=ci, color=INK_2, capsize=3, lw=1)
        ax.annotate(f"{mean:+.1f}¢  (n={n:,})",
                    (mean + (ci + 0.35) * (1 if mean >= 0 else -1), y),
                    va="center", ha="left" if mean >= 0 else "right",
                    fontsize=8.5, color=INK_2)
    ax.axvline(0, color=BASELINE, lw=1)
    ax.set_yticks(list(ys), [r[0] for r in rows], fontsize=8.5)
    ax.set_xlabel("Mean P&L per contract (cents, after fees, 95% CI)")
    ax.set_title("What survives real execution costs — and what doesn't")
    ax.set_xlim(-13, 6.5)
    style(ax, ygrid_only=False)
    ax.grid(axis="y", visible=False)
    save(fig, "strategy_pnl.png")
