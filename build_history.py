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
from datetime import datetime, timezone, timedelta

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DB = os.path.join(BASE, "blocks_cache.sqlite")
OUT_CSV = os.path.join(BASE, "block_rewards_percentiles.csv")


def main():
    ap = argparse.ArgumentParser(description="Build history CSV from the block cache.")
    ap.add_argument("--min-blocks", type=int, default=7000,
                    help="Minimum blocks for a day to be written (default 7000).")
    ap.add_argument("--report-gaps", action="store_true",
                    help="List interior days that are missing or short of --min-blocks "
                         "(these show as empty cells in the Overview calendar).")
    ap.add_argument("--out", default=OUT_CSV, help="Output CSV path.")
    args = ap.parse_args()

    if not os.path.exists(CACHE_DB):
        sys.exit(f"No cache at {CACHE_DB} — run executionRewards.py first.")

    conn = sqlite3.connect(CACHE_DB)
    conn.execute("PRAGMA busy_timeout=60000")   # wait out the live server / catch-up locks
    days = {}      # 'YYYY-MM-DD' -> list of (unix ts, reward ETH)
    day_nums = {}  # 'YYYY-MM-DD' -> list of block numbers (for the contiguity gap check)
    n = 0
    for num, bd, rd in conn.execute(
        "SELECT b.number, b.data, r.data FROM blocks b "
        "LEFT JOIN receipts r ON b.number = r.number WHERE b.full = 1"
    ):
        if bd is None:
            continue
        block = json.loads(bd)
        ts = block.get("timestamp")
        if ts is None:
            continue
        tsi = int(ts, 16)
        day = datetime.fromtimestamp(tsi, tz=timezone.utc).strftime("%Y-%m-%d")
        base = int(block["baseFeePerGas"], 16)
        if not block.get("transactions") or rd is None:
            reward = 0.0
        else:
            reward = sum(
                (int(x["effectiveGasPrice"], 16) - base) * int(x["gasUsed"], 16)
                for x in json.loads(rd)
                if int(x["effectiveGasPrice"], 16) > base
            ) / 1e18
        days.setdefault(day, []).append((tsi, reward))
        day_nums.setdefault(day, []).append(num)
        n += 1

    # Bid rungs for the "Bid & win" tab (match the retired Dune export so the
    # app's finer-rung interpolation still works).
    BIDS = [0.02, 0.05, 0.10, 0.25, 0.50, 1.00, 2.00, 5.00]

    # The only day that can be incomplete is the most recent one (the catch-up
    # appends the chain tip), so that's the single day we hold back until it has
    # a full day's blocks. Every earlier day is written regardless of count — a
    # genuinely light day (e.g. a high-missed-slot day with ~6,800 blocks) is real
    # data, not a partial; --min-blocks only gates the in-progress tail.
    latest_day = max(days) if days else None

    rows = []          # daily_percentiles
    pooled = []        # every block reward, for the pooled full-period percentiles
    day_rewards = {}   # written day -> np.array of per-block rewards (for windowed pooled pctiles)
    bidrows = []       # bid_winnable: (day, my_bid, winnable_blocks, max_wait_min, max_wait_hours)
    for day in sorted(days):
        vals = days[day]
        if day == latest_day and len(vals) < args.min_blocks:
            continue                      # current day still filling — hold back
        vals.sort()                       # by timestamp
        ts = [t for t, _ in vals]
        rewards = [r for _, r in vals]
        pooled.extend(rewards)
        day_rewards[day] = np.asarray(rewards)
        a = np.array(rewards)
        rows.append((
            day, len(a),
            float(np.percentile(a, 50)), float(np.percentile(a, 80)),
            float(np.percentile(a, 90)), float(np.percentile(a, 99)),
        ))

        # Bid & win: you "win" a block if your bid >= its value (reward <= bid).
        # max_wait = longest gap (minutes) between winnable blocks, including the
        # stretch from day start to the first win and the last win to day end.
        day_start = int(datetime.strptime(day, "%Y-%m-%d")
                        .replace(tzinfo=timezone.utc).timestamp())
        day_end = day_start + 86400
        for bid in BIDS:
            wins = [ts[i] for i in range(len(ts)) if rewards[i] <= bid]
            n_win = len(wins)
            if n_win == 0:
                max_wait_s = 86400
            else:
                gaps = [wins[0] - day_start] + \
                       [wins[i + 1] - wins[i] for i in range(n_win - 1)] + \
                       [day_end - wins[-1]]
                max_wait_s = max(gaps)
            bidrows.append((day, bid, n_win, max_wait_s / 60.0, max_wait_s / 3600.0))

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
            "built_at": datetime.now(timezone.utc).timestamp(),   # last refresh time
        }

        # Windowed pooled p90s: the true 90th percentile of every block in each
        # trailing window (not an average of daily p90s), so they stay on the same
        # footing as the full-period "everything" p90 above. Windows are anchored on
        # the most recent qualifying day so the figures are deterministic.
        qual_days = sorted(day_rewards)            # ascending; same set as `rows`
        latest_year = qual_days[-1][:4]

        def pooled_pct(day_list, q):
            if not day_list:
                return float("nan")
            arr = np.concatenate([day_rewards[d] for d in day_list])
            return float(np.percentile(arr, q))

        summary["p90_7d"] = pooled_pct(qual_days[-7:], 90)
        summary["p90_30d"] = pooled_pct(qual_days[-30:], 90)
        summary["p90_90d"] = pooled_pct(qual_days[-90:], 90)
        summary["p90_ytd"] = pooled_pct([d for d in qual_days if d[:4] == latest_year], 90)
        # Rolling trailing year: the headline pricing anchor, and the single basis
        # every headline figure shares. Always the most recent 365 days (fewer only
        # until a full year of history exists), so it doesn't drift as the cache
        # grows past a year.
        ry_days = qual_days[-365:]
        summary["p50_365d"] = pooled_pct(ry_days, 50)
        summary["p90_365d"] = pooled_pct(ry_days, 90)
        summary["p99_365d"] = pooled_pct(ry_days, 99)

        # Hot days: days within the rolling year whose daily p90 is >= 1.5x the
        # rolling-year pooled p90 (a "clearly elevated" regime, e.g. the late-April
        # surge). rows[i] = (day, blocks, p50, p80, p90, p99) so daily p90 is idx 4.
        daily_p90 = {r[0]: r[4] for r in rows}
        hot_threshold = 1.5 * summary["p90_365d"]
        summary["hot_threshold"] = hot_threshold
        summary["hot_days"] = float(sum(1 for d in ry_days if daily_p90[d] >= hot_threshold))
        summary["hot_total_days"] = float(len(ry_days))
        summary["hot_peak"] = float(max((daily_p90[d] for d in ry_days), default=0.0))

        conn.executemany("INSERT INTO summary (key,value) VALUES (?,?)", list(summary.items()))

    # 1c) Bid & win table (the "Bid & win" tab reads this via /api/bidwait).
    conn.execute("""CREATE TABLE IF NOT EXISTS bid_winnable (
        day TEXT, my_bid REAL, winnable_blocks INTEGER,
        max_wait_min REAL, max_wait_hours REAL, PRIMARY KEY (day, my_bid))""")
    conn.execute("DELETE FROM bid_winnable")
    conn.executemany(
        "INSERT INTO bid_winnable (day,my_bid,winnable_blocks,max_wait_min,max_wait_hours) "
        "VALUES (?,?,?,?,?)", bidrows)
    conn.commit()

    # 2) Also write the CSVs (fallback if the API/DB is unavailable).
    with open(args.out, "w") as f:
        f.write("day,blocks,p50,p80,p90,p99\n")
        for day, blocks, p50, p80, p90, p99 in rows:
            f.write(f"{day} 00:00:00.000 UTC,{blocks},{p50},{p80},{p90},{p99}\n")
    with open(os.path.join(BASE, "blockspace_max_wait.csv"), "w") as f:
        f.write("day,my_bid,winnable_blocks,max_wait_min,max_wait_hours\n")
        for day, bid, nwin, wmin, whr in bidrows:
            f.write(f"{day} 00:00:00.000 UTC,{bid},{nwin},{wmin:.6f},{whr:.6f}\n")

    print(f"Scanned {n:,} cached blocks.")
    if rows:
        print(f"Wrote {len(rows)} day rows + {len(bidrows)} bid rows "
              f"({rows[0][0]} → {rows[-1][0]}) to DB tables + CSVs")
    else:
        print("No complete days found — cache may be empty or below --min-blocks.")

    # Interior coverage report. Block numbers are globally contiguous (a missed
    # slot produces no block and consumes no number), so a real census gap shows
    # up as a hole in the number sequence — that's the authoritative test, not a
    # raw block count (a genuinely light day has a full, hole-free number run).
    # Reports two kinds of gap, both of which leave empty Overview-calendar cells:
    #   • missing day  — a calendar day inside the span with no blocks at all
    #   • partial day  — a day whose block numbers have a hole (some not censused)
    if args.report_gaps and rows:
        first = datetime.strptime(rows[0][0], "%Y-%m-%d").date()
        last = datetime.strptime(rows[-1][0], "%Y-%m-%d").date()
        gaps = []
        d = first
        while d <= last:
            key = d.strftime("%Y-%m-%d")
            nums = day_nums.get(key)
            if not nums:
                gaps.append((key, "missing — no blocks censused"))
            else:
                lo, hi = min(nums), max(nums)
                holes = (hi - lo + 1) - len(nums)
                if holes:
                    gaps.append((key, f"partial — {holes:,} block number(s) "
                                      f"missing in {lo:,}…{hi:,}"))
            d += timedelta(days=1)
        if gaps:
            print(f"\n⚠ {len(gaps)} interior day(s) with gaps ({first} → {last}) "
                  f"— empty/incomplete on the Overview calendar:")
            for day, why in gaps:
                print(f"    {day}  {why}")
            print("  Backfill with: executionRewards.py --start <day> --end <next-day> "
                  "--complete  (then re-run build_history.py)")
        else:
            print(f"\n✓ No interior gaps: block numbers are contiguous across "
                  f"{first} → {last} (every day fully censused).")


if __name__ == "__main__":
    main()
