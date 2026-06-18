#!/usr/bin/env python3
"""
Build the site's history CSV from the local block cache.

Reads `blocks_cache.sqlite` (populated by executionRewards.py) and writes
`block_rewards_percentiles.csv` in the exact format the dashboard loads:

    day,blocks,p50,p80,p90,p99
    2026-01-01 00:00:00.000 UTC,7178,0.0071,0.0118,0.0145,0.0363

Each row is one UTC day's distribution of per-block execution-layer reward
(priority-fee sum, Σ(effectiveGasPrice - baseFee)·gasUsed, in ETH). Unlike the
old Dune export this is a complete census (every block) and has a real p80
(the Dune query had a p80==p50 bug).

Usage:
    python3 build_history.py                 # writes block_rewards_percentiles.csv
    python3 build_history.py --min-blocks 0  # include partial (e.g. current) days

Only days with at least --min-blocks blocks are written (default 7000), so a
still-in-progress current day is omitted until complete.
"""

import os
import sys
import json
import sqlite3
import argparse
from datetime import datetime, timezone

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DB = os.path.join(BASE, "blocks_cache.sqlite")
OUT_CSV = os.path.join(BASE, "block_rewards_percentiles.csv")


def main():
    ap = argparse.ArgumentParser(description="Build history CSV from the block cache.")
    ap.add_argument("--min-blocks", type=int, default=7000,
                    help="Minimum blocks for a day to be written (default 7000).")
    ap.add_argument("--out", default=OUT_CSV, help="Output CSV path.")
    args = ap.parse_args()

    if not os.path.exists(CACHE_DB):
        sys.exit(f"No cache at {CACHE_DB} — run executionRewards.py first.")

    conn = sqlite3.connect(CACHE_DB)
    days = {}  # 'YYYY-MM-DD' -> list of per-block rewards (ETH)
    n = 0
    for bd, rd in conn.execute(
        "SELECT b.data, r.data FROM blocks b "
        "LEFT JOIN receipts r ON b.number = r.number WHERE b.full = 1"
    ):
        if bd is None:
            continue
        block = json.loads(bd)
        ts = block.get("timestamp")
        if ts is None:
            continue
        day = datetime.fromtimestamp(int(ts, 16), tz=timezone.utc).strftime("%Y-%m-%d")
        base = int(block["baseFeePerGas"], 16)
        if not block.get("transactions") or rd is None:
            reward = 0.0
        else:
            reward = sum(
                (int(x["effectiveGasPrice"], 16) - base) * int(x["gasUsed"], 16)
                for x in json.loads(rd)
                if int(x["effectiveGasPrice"], 16) > base
            ) / 1e18
        days.setdefault(day, []).append(reward)
        n += 1

    rows = []
    pooled = []   # every block of every complete day, for true full-period percentiles
    for day in sorted(days):
        vals = days[day]
        if len(vals) < args.min_blocks:
            continue
        pooled.extend(vals)
        a = np.array(vals)
        rows.append((
            day, len(a),
            float(np.percentile(a, 50)), float(np.percentile(a, 80)),
            float(np.percentile(a, 90)), float(np.percentile(a, 99)),
        ))

    # 1) Write the daily_percentiles table into the cache DB (the site reads this
    #    via the server's /api/history endpoint — the primary path for option B).
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_percentiles (
        day TEXT PRIMARY KEY, blocks INTEGER, p50 REAL, p80 REAL, p90 REAL, p99 REAL)""")
    conn.execute("DELETE FROM daily_percentiles")
    conn.executemany(
        "INSERT INTO daily_percentiles (day,blocks,p50,p80,p90,p99) VALUES (?,?,?,?,?,?)",
        rows)

    # 1b) Pooled full-period percentiles over every block (a different statistic
    #     from the daily values: the 90th percentile of the whole distribution).
    conn.execute("CREATE TABLE IF NOT EXISTS summary (key TEXT PRIMARY KEY, value REAL)")
    conn.execute("DELETE FROM summary")
    if pooled:
        pa = np.array(pooled)
        summary = {
            "blocks": float(len(pa)),
            "p50": float(np.percentile(pa, 50)), "p80": float(np.percentile(pa, 80)),
            "p90": float(np.percentile(pa, 90)), "p95": float(np.percentile(pa, 95)),
            "p99": float(np.percentile(pa, 99)), "max": float(pa.max()),
        }
        conn.executemany("INSERT INTO summary (key,value) VALUES (?,?)", list(summary.items()))
    conn.commit()

    # 2) Also write the CSV (fallback if the API/DB is unavailable).
    with open(args.out, "w") as f:
        f.write("day,blocks,p50,p80,p90,p99\n")
        for day, blocks, p50, p80, p90, p99 in rows:
            f.write(f"{day} 00:00:00.000 UTC,{blocks},{p50},{p80},{p90},{p99}\n")

    print(f"Scanned {n:,} cached blocks.")
    if rows:
        print(f"Wrote {len(rows)} day rows ({rows[0][0]} → {rows[-1][0]}) "
              f"to daily_percentiles table + {args.out}")
    else:
        print("No complete days found — cache may be empty or below --min-blocks.")


if __name__ == "__main__":
    main()
