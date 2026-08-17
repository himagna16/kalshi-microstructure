#!/usr/bin/env python3
"""
Monthly cold-storage archiver for the Kalshi book snapshot DB.

Policy: a calendar month is archived only once EVERY row in it is older than
RETAIN_DAYS. Each cold month is streamed to archive/books-YYYYMM.csv.gz,
the written row count is verified against the DB, and only then are the rows
deleted. VACUUM reclaims the space (collector + bot paused for that step only).

Idempotent: existing archive files are never overwritten; a month with a
matching verified file is deleted-if-still-present and otherwise skipped.
Runs from kalshi-archive.timer on the 1st of each month.
"""

import csv
import gzip
import os
import sqlite3
import subprocess
import sys
import time

DB = "/root/kalshi/kalshi_data.db"
ARCHIVE_DIR = "/root/kalshi/archive"
RETAIN_DAYS = 30
PAUSE_UNITS = ["kalshi-collector.service", "kalshi-mmbot.service"]
COLS = ["ts", "cycle", "ticker", "yes_bid", "yes_bid_size",
        "yes_ask", "yes_ask_size", "yes_depth", "no_depth"]


def log(msg):
    print(f"[archive] {msg}", flush=True)


def month_bounds_ms(yyyymm):
    y, m = int(yyyymm[:4]), int(yyyymm[4:])
    start = time.mktime((y, m, 1, 0, 0, 0, 0, 0, 0))
    end = time.mktime((y + (m == 12), m % 12 + 1, 1, 0, 0, 0, 0, 0, 0))
    return int(start * 1000), int(end * 1000)


def main():
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    cutoff_ms = int((time.time() - RETAIN_DAYS * 86400) * 1000)

    db = sqlite3.connect(DB, timeout=60)
    months = [r[0] for r in db.execute(
        "SELECT DISTINCT strftime('%Y%m', ts/1000, 'unixepoch') FROM book_snapshots"
    )]
    # cold = the whole month ends before the cutoff
    cold = [m for m in months if month_bounds_ms(m)[1] <= cutoff_ms]
    if not cold:
        log(f"no cold months (have {sorted(months)}, cutoff {RETAIN_DAYS}d) — nothing to do")
        return 0

    deleted_any = False
    for m in sorted(cold):
        lo, hi = month_bounds_ms(m)
        where = "ts >= ? AND ts < ?"
        (expected,) = db.execute(
            f"SELECT COUNT(*) FROM book_snapshots WHERE {where}", (lo, hi)).fetchone()
        if expected == 0:
            continue
        path = os.path.join(ARCHIVE_DIR, f"books-{m}.csv.gz")

        if not os.path.exists(path):
            log(f"{m}: writing {expected:,} rows -> {path}")
            tmp = path + ".tmp"
            written = 0
            with gzip.open(tmp, "wt", newline="", compresslevel=6) as f:
                w = csv.writer(f)
                w.writerow(COLS)
                cur = db.execute(
                    f"SELECT {','.join(COLS)} FROM book_snapshots "
                    f"WHERE {where} ORDER BY ts", (lo, hi))
                while rows := cur.fetchmany(20000):
                    w.writerows(rows)
                    written += len(rows)
            if written != expected:
                os.remove(tmp)
                log(f"{m}: VERIFY FAILED wrote {written:,} != {expected:,}; aborting")
                return 1
            os.replace(tmp, path)
        else:
            # verify the existing file before trusting it
            with gzip.open(path, "rt") as f:
                written = sum(1 for _ in f) - 1
            if written != expected:
                log(f"{m}: existing archive has {written:,} rows, DB has "
                    f"{expected:,}; NOT deleting — investigate")
                return 1
            log(f"{m}: archive already present and verified")

        log(f"{m}: deleting {expected:,} archived rows")
        db.execute(f"DELETE FROM book_snapshots WHERE {where}", (lo, hi))
        db.commit()
        deleted_any = True

    db.close()
    if deleted_any:
        log("pausing collector + bot for VACUUM")
        for u in PAUSE_UNITS:
            subprocess.run(["systemctl", "stop", u], check=False)
        try:
            v = sqlite3.connect(DB, timeout=120)
            v.execute("VACUUM")
            v.close()
            log("VACUUM done")
        finally:
            for u in PAUSE_UNITS:
                subprocess.run(["systemctl", "start", u], check=False)
            log("services restarted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
