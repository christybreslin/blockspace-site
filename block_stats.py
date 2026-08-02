#!/usr/bin/env python3
"""
Ad-hoc per-block reward stats from the block cache, over a date range.

Reports, for blocks on/after --since (UTC day, inclusive) up to the latest cached:
  1) the lowest block reward in the period
  2) mean & median across ALL blocks
  3) mean & median across only LOCAL / mempool-built (non-MEV-Boost) blocks

Two bases are shown throughout:
  • priority fees   = Σ (effectiveGasPrice - baseFee)·gasUsed        (tips only)
  • validator reward = the MEV-Boost relay winning bid the proposer received,
                        which equals priority fees for a locally-built block.

"Local" blocks are those the proposer built itself (no relay payment); in the
take calc those have validator reward == priority fees, so we detect them as
take == fee.

Uses the day_cache table if present (fast); otherwise scans the raw blocks.

Usage:
  python3 block_stats.py --since 2026-04-01            # mainnet
  python3 block_stats.py --sepolia --since 2026-04-01  # sepolia
"""
import os
import sys
import json
import sqlite3
import argparse
from datetime import datetime, timezone

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
NETS = {"mainnet": "blocks_cache.sqlite", "sepolia": "blocks_cache_sepolia.sqlite"}


def efmt(v):
    """Readable ETH: fixed decimals for normal values, scientific for tiny ones so
    sub-micro-ETH rewards don't just render as 0.000000."""
    return f"{v:.6f}" if abs(v) >= 1e-4 else f"{v:.3e}"


def block_take(block, fees):
    """Validator reward: the builder→proposer payment for an MEV-Boost block, else
    the priority-fee sum (same logic as build_history.py / server.py)."""
    txs = block.get("transactions") or []
    if not txs:
        return fees
    last = txs[-1]
    if not isinstance(last, dict) or "value" not in last:
        return fees
    mev_pay = int(last["value"], 16) / 1e18
    data = last.get("input", "0x") or "0x"
    mp = last.get("maxPriorityFeePerGas")
    maxprio = int(mp, 16) if mp else 0
    extra = block.get("extraData", "0x") or "0x"
    try:
        builder = bytes.fromhex(extra[2:]).decode("utf-8", "replace")
    except ValueError:
        builder = ""
    builder = "".join(c for c in builder if c >= " " and c != "�").strip()
    bl = builder.lower()
    vanilla = ("geth" in bl or "nethermind" in bl or len(builder) < 2
               or mev_pay == 0 or data != "0x" or maxprio > 0)
    return fees if vanilla else mev_pay


def from_day_cache(conn, since):
    rows = conn.execute(
        "SELECT day, fee, take FROM day_cache WHERE day >= ? ORDER BY day", (since,)
    ).fetchall()
    if not rows:
        return None
    fee = np.concatenate([np.frombuffer(f, np.float64) for _, f, _ in rows])
    take = np.concatenate([np.frombuffer(t, np.float64) for _, _, t in rows])
    return fee, take, (rows[0][0], rows[-1][0])


def from_raw(conn, since):
    since_ts = int(datetime.strptime(since, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    fees, takes = [], []
    d0 = d1 = None
    for _num, bd, rd in conn.execute(
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
        if tsi < since_ts:
            continue
        base = int(block["baseFeePerGas"], 16)
        if not block.get("transactions") or rd is None:
            reward = 0.0
        else:
            reward = sum(
                (int(x["effectiveGasPrice"], 16) - base) * int(x["gasUsed"], 16)
                for x in json.loads(rd)
                if int(x["effectiveGasPrice"], 16) > base
            ) / 1e18
        fees.append(reward)
        takes.append(block_take(block, reward))
        day = datetime.fromtimestamp(tsi, tz=timezone.utc).strftime("%Y-%m-%d")
        d0 = day if d0 is None or day < d0 else d0
        d1 = day if d1 is None or day > d1 else d1
    if not fees:
        return None
    return np.asarray(fees), np.asarray(takes), (d0, d1)


def main():
    ap = argparse.ArgumentParser(description="Per-block reward stats over a date range.")
    ap.add_argument("--since", default="2026-04-01",
                    help="UTC day (YYYY-MM-DD), inclusive (default 2026-04-01).")
    ap.add_argument("--network", choices=sorted(NETS), default="mainnet")
    ap.add_argument("--sepolia", action="store_true", help="Shorthand for --network sepolia.")
    ap.add_argument("--low", type=int, default=0, metavar="N",
                    help="Also list the N lowest non-zero blocks (validator reward), "
                         "ascending with the ratio to the previous value, to reveal any gap.")
    ap.add_argument("--min-reward", type=float, default=0.0, metavar="ETH",
                    help="Drop blocks whose validator reward is below this (ETH) before any "
                         "stats — use it to eliminate zero/dust blocks once you've found the floor.")
    ap.add_argument("--bid", type=float, default=0.0, metavar="ETH",
                    help="Cap analysis: assuming we get every block, how much value sits "
                         "above this bid (left on the table if we cap each block at it), and "
                         "the total if every block realised a flat bid.")
    ap.add_argument("--min-bid", type=float, default=0.0, metavar="ETH",
                    help="MEV-Boost min-bid model: relay blocks whose bid is below this switch "
                         "to local building (earn priority fees instead of the relay bid). "
                         "Reports the change in execution-layer rewards, in ETH and basis points.")
    ap.add_argument("--el-apr", type=float, default=0.0, metavar="PCT",
                    help="Execution-layer APR in %% (e.g. 0.5). If given, the --min-bid impact is "
                         "also shown as basis points of total staking yield, not just of the "
                         "tips+MEV pool.")
    args = ap.parse_args()

    net = "sepolia" if args.sepolia else args.network
    db = os.path.join(BASE, NETS[net])
    if not os.path.exists(db):
        sys.exit(f"No cache at {db}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=60000")

    src = "day_cache"
    try:
        res = from_day_cache(conn, args.since)
    except sqlite3.OperationalError:
        res = None
    if res is None:
        src = "raw scan"
        res = from_raw(conn, args.since)
    if res is None:
        sys.exit(f"No blocks found on/after {args.since} in {os.path.basename(db)}")

    fee, take, (d0, d1) = res
    n0 = fee.size
    if args.min_reward > 0:
        keep = take >= args.min_reward
        fee, take = fee[keep], take[keep]
    n = fee.size
    local = take == fee                # non-MEV-Boost (proposer-built) blocks
    nz = fee > 0

    def mm(a):
        return (float(np.mean(a)), float(np.median(a))) if a.size else (float("nan"), float("nan"))

    print(f"[{net}] source: {src}")
    print(f"Period: {d0} → {d1}   ({n:,} blocks)")
    if args.min_reward > 0:
        print(f"Filter: validator reward >= {args.min_reward} ETH   "
              f"(dropped {n0 - n:,} of {n0:,} zero/dust blocks)")
    print(f"Local / mempool (non-MEV-Boost) blocks: {int(local.sum()):,} "
          f"({100 * local.mean():.1f}% of blocks)   |   zero-reward blocks: {int((~nz).sum()):,}")
    print("=" * 64)

    print("1) Lowest block reward in the period")
    print(f"     priority fees  : {efmt(fee.min())} ETH")
    print(f"     validator rwd  : {efmt(take.min())} ETH")
    if nz.any():
        print(f"     (lowest non-zero — fees {efmt(fee[nz].min())}, "
              f"validator {efmt(take[take > 0].min())} ETH)")
    print()

    mf, medf = mm(fee)
    mt, medt = mm(take)
    print("2) All blocks")
    print(f"     priority fees  : mean {mf:.6f}   median {medf:.6f} ETH")
    print(f"     validator rwd  : mean {mt:.6f}   median {medt:.6f} ETH")
    print()

    lmean, lmed = mm(fee[local])
    print("3) Local / mempool blocks only  (validator reward == priority fees)")
    print(f"     reward         : mean {lmean:.6f}   median {lmed:.6f} ETH")
    print()

    # 4) How much more a MEV-Boost (relay) block pays the proposer than a local one.
    mev = ~local
    mev_mean, mev_med = mm(take[mev])
    loc_mean, loc_med = mm(take[local])
    print("4) MEV-Boost vs local (validator reward received by the proposer)")
    print(f"     MEV-Boost blocks ({int(mev.sum()):,}) : mean {mev_mean:.6f}   median {mev_med:.6f} ETH")
    print(f"     local blocks     ({int(local.sum()):,}) : mean {loc_mean:.6f}   median {loc_med:.6f} ETH")
    if loc_mean > 0:
        print(f"     → a MEV-Boost block pays on average {mev_mean - loc_mean:+.6f} ETH "
              f"({mev_mean / loc_mean:.2f}× as much); median difference {mev_med - loc_med:+.6f} ETH")
    print(f"     (local group includes {int((take == fee)[~nz].sum()):,} zero-reward blocks — "
          f"negligible effect on the averages)")

    # Optional analyses are numbered dynamically so there is never a gap when only
    # one of --bid / --min-bid is used.
    sec = 4

    # Cap analysis: get every block, but cap what we realise at the bid.
    if args.bid > 0:
        sec += 1
        B = args.bid
        total = float(take.sum())
        n_above = int((take > B).sum())
        realized = float(np.minimum(take, B).sum())   # get every block, capped at B
        left = total - realized                       # = Σ max(take - B, 0)
        flat = B * n                                   # flat B on every block instead
        print()
        print(f"{sec}) Cap analysis at B = {B} ETH (validator reward, {n:,} blocks)")
        print(f"     total value of all blocks : {total:,.2f} ETH   (mean {total / n:.6f})")
        print(f"     blocks above B            : {n_above:,} ({100 * n_above / n:.1f}%)   "
              f"at/below B: {n - n_above:,} ({100 * (n - n_above) / n:.1f}%)")
        print(f"     capped at B (realise min(value,B) on every block):")
        print(f"        realised               : {realized:,.2f} ETH ({100 * realized / total:.1f}% of value)")
        print(f"        LEFT ON THE TABLE      : {left:,.2f} ETH ({100 * left / total:.1f}% of total value)")
        print(f"     flat B on every block:")
        print(f"        total                  : {flat:,.2f} ETH   "
              f"({flat - total:+,.2f} ETH vs the blocks' actual value — "
              f"{'more' if flat >= total else 'less'} than they are worth)")

    # MEV-Boost min-bid: relay blocks bidding below B build locally instead.
    if args.min_bid > 0:
        sec += 1
        B = args.min_bid
        baseline = float(take.sum())                 # take every relay bid (today)
        relay = take != fee                          # MEV-Boost blocks (bid = take)
        switch = relay & (take < B)                  # these fall back to local
        n_sw = int(switch.sum())
        lost_relay = float(take[switch].sum())       # relay value given up on them
        loc = fee[take == fee]                       # observed local blocks
        avg_local = float(loc.mean()) if loc.size else 0.0
        own_fees = float(fee[switch].sum())          # each switched slot's own priority fees

        # Like-for-like: compare only blocks BELOW the bid — the relay blocks that
        # switch vs local blocks in the same low-value range. Medians, so high-value
        # blocks (which never switch) don't distort either side.
        relay_below = take[switch]                    # relay bids that switch (all < B)
        local_below = take[local & (take < B)]        # local blocks below B (take == fee)
        med_relay_below = float(np.median(relay_below)) if relay_below.size else 0.0
        med_local_below = float(np.median(local_below)) if local_below.size else 0.0
        change_medb = n_sw * (med_local_below - med_relay_below)   # + gain / - loss

        # bps here is a share of the EXECUTION-LAYER reward pool (tips + MEV), NOT of
        # total staking yield. Pass --el-apr to also express it as bps of yield.
        def _elbps(x):
            return 10000 * x / baseline if baseline else 0.0

        def _yield(elbps):
            # yield bps = EL APR (pct pts) × (fractional change of the EL pool) × 100
            return (f"   ->  {args.el_apr * elbps / 100:+.1f} bps of yield"
                    if args.el_apr > 0 else "")

        change_own = own_fees - lost_relay               # earn each slot's own local fees
        change_avg = n_sw * avg_local - lost_relay       # earn the average local block
        print()
        print(f"{sec}) MEV-Boost min-bid {B} ETH — relay blocks below the bid build locally instead")
        print(f"     affected relay blocks : {n_sw:,} ({100 * n_sw / n:.1f}% of all blocks)")
        print(f"     execution-layer rewards in period (baseline) : {baseline:,.2f} ETH")
        print(f"     relay value given up on those blocks         : {lost_relay:,.2f} ETH "
              f"(avg bid {lost_relay / n_sw if n_sw else 0:.4f})")
        print(f"     net change in execution-layer rewards (+ gain / - loss):")
        print(f"        each slot's own priority fees (avg {own_fees / n_sw if n_sw else 0:.4f}) : "
              f"{change_own:+,.2f} ETH  =  {_elbps(change_own):+.1f} bps of EL rewards{_yield(_elbps(change_own))}")
        print(f"        average local block ({avg_local:.4f})               : "
              f"{change_avg:+,.2f} ETH  =  {_elbps(change_avg):+.1f} bps of EL rewards{_yield(_elbps(change_avg))}")
        print(f"        below-bid medians  relay {med_relay_below:.4f} vs local {med_local_below:.4f} "
              f"(gap {med_relay_below - med_local_below:+.4f}) : "
              f"{change_medb:+,.2f} ETH  =  {_elbps(change_medb):+.1f} bps of EL rewards{_yield(_elbps(change_medb))}")
        print(f"     (the below-bid median line is the like-for-like basis: only blocks under {B} ETH, "
              f"medians on both sides.)")
        if args.el_apr <= 0:
            print(f"     NOTE: 'bps of EL rewards' is a % of the tips+MEV pool, NOT of staking yield "
                  f"(pass --el-apr X to convert).")
        print(f"     NOTE: both local-yield proxies likely OVERstate local yield on these low-bid "
              f"slots (private orderflow / selection bias). A modeled 'gain' is not reliable — "
              f"the economically conservative expectation is a small loss to break-even.")

    if args.low:
        nzv = np.sort(take[take > 0])
        k = min(args.low, nzv.size)
        print()
        print(f"Lowest {k} non-zero blocks by validator reward (ascending; ratio to previous "
              f"— a big jump marks a gap between noise and the real floor):")
        prev = None
        for v in nzv[:k]:
            ratio = f"   ({v / prev:.1f}× prev)" if prev else ""
            print(f"     {efmt(v)} ETH{ratio}")
            prev = v


if __name__ == "__main__":
    main()
