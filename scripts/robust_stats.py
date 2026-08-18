#!/usr/bin/env python3
"""
Cluster-robust statistics for the settlement-hold backtests.

Same-hour strikes settle on the same underlying print, so their outcomes are
mechanically correlated and naive per-trade t-stats overstate significance.
Cluster = (series, event date-hour token from the ticker), e.g. every
KXBTC-26AUG1719-* strike is one cluster.

SE is Liang-Zeger for the mean: sqrt(sum_g (sum_{i in g} (x_i - xbar))^2) / n.
Reads data/taker_trades.csv, writes data/robust_stats.json.
"""

import json
import os

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
t = pd.read_csv(os.path.join(ROOT, "data", "taker_trades.csv"))
t["cluster"] = t.series + "-" + t.ticker.str.split("-").str[1]

out = {}
for strat, d in t.groupby("strategy"):
    mean = d.pnl.mean()
    resid_sums = d.assign(e=d.pnl - mean).groupby("cluster").e.sum()
    se_cl = (resid_sums.pow(2).sum()) ** 0.5 / len(d)
    se_iid = d.pnl.std() / len(d) ** 0.5
    out[strat] = {
        "n_trades": int(len(d)),
        "n_clusters": int(d.cluster.nunique()),
        "mean_pnl": round(mean, 5),
        "se_iid": round(se_iid, 5),
        "t_iid": round(mean / se_iid, 2),
        "se_clustered": round(se_cl, 5),
        "t_clustered": round(mean / se_cl, 2),
    }
    print(f"{strat:10s} n={len(d):5d} clusters={d.cluster.nunique():4d} "
          f"mean={mean*100:+.2f}c  t_iid={mean/se_iid:+.2f}  "
          f"t_clustered={mean/se_cl:+.2f}")

with open(os.path.join(ROOT, "data", "robust_stats.json"), "w") as f:
    json.dump(out, f, indent=2)
