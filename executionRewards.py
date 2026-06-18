"""
Ethereum Execution Layer Reward Analyser
========================================
Queries a given execution-layer RPC endpoint, samples blocks across a date
range, computes the exact priority-fee reward paid to each block's proposer,
and prints mean / median / percentile statistics.

Requirements:
    pip install requests numpy tqdm

Usage:
    python eth_execution_rewards.py

Edit the CONFIG section below to change the RPC URL, date range, or sample size.
"""

import os
import json
import time
import sqlite3
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
import numpy as np
from tqdm import tqdm

# ─── CONFIG ───────────────────────────────────────────────────────────────────

# Credentials come from the SAME .env the web server (server.py) uses, so there is
# one secret file to manage and it is never committed (.env is gitignored). The
# bearer token is sent as "Authorization: Bearer <token>". credentials.py is an
# optional local-dev fallback. Recognised .env keys: EL_RPC_URL, EL_RPC_TOKEN,
# RPC_VERIFY (or INSECURE=1) — identical to server.py.
def _load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env

_ENV = _load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
def _cfg(key, default=None):
    return os.environ.get(key) or _ENV.get(key) or default

RPC_URL    = _cfg("EL_RPC_URL") or _cfg("RPC_URL") or "https://mainnet-user-el.attestant.io"
_token     = _cfg("EL_RPC_TOKEN") or _cfg("RPC_TOKEN") or ""
RPC_HEADERS = {"Authorization": f"Bearer {_token}"} if _token else {}
RPC_AUTH   = None
RPC_VERIFY = not ((_cfg("INSECURE") == "1") or (_cfg("RPC_VERIFY", "true").lower() == "false"))

# Optional credentials.py fallback for local dev (gitignored, never committed).
try:
    import credentials as _creds
    RPC_URL = getattr(_creds, "RPC_URL", "") or RPC_URL
    if hasattr(_creds, "RPC_VERIFY"):
        RPC_VERIFY = _creds.RPC_VERIFY
    if not _token and getattr(_creds, "RPC_TOKEN", ""):
        RPC_HEADERS = {"Authorization": f"Bearer {_creds.RPC_TOKEN}"}
    elif not _token and getattr(_creds, "RPC_USERNAME", ""):
        RPC_AUTH = (_creds.RPC_USERNAME, _creds.RPC_PASSWORD)
except ImportError:
    pass

# The endpoint uses a self-signed cert; if verification is disabled, silence the
# noisy per-request InsecureRequestWarning.
if RPC_VERIFY is False:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Shared, connection-pooled HTTP session so concurrent workers reuse keep-alive
# connections instead of opening a new socket per call.
_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)
# "Last 7 days", snapped down to the top of the current hour so that repeated
# runs within the same hour use an identical window — and therefore sample the
# same block numbers and hit the local cache. Without snapping, a now-based
# window shifts every run and the sampling grid never lines up with the cache.
_now_hour   = int(datetime.now(timezone.utc).replace(minute=0, second=0,
                                                     microsecond=0).timestamp())
END_TS      = _now_hour
START_TS    = END_TS - 7 * 24 * 60 * 60                            # last 7 days
N_SAMPLES   = 500          # number of blocks to sample across the range
COMPLETE    = False        # if True, fetch EVERY block in the range (no sampling)
SAMPLE_RANDOM = False      # if True, sample N blocks at random (else even grid)
REVERSE     = False        # if True, fetch newest block first (backward fill)
WORKERS     = 8            # concurrent blocks fetched at once (1 = serial)
TIMEOUT     = 30           # seconds per RPC call
SLEEP_MS    = 0            # ms delay between live calls (0 = none; endpoint unthrottled)
MAX_RETRIES = 8            # retries on rate-limit / transient errors
BACKOFF_BASE = 1.0         # seconds; exponential backoff on HTTP 429
CACHE_DB    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blocks_cache.sqlite")

# ─── LOCAL CACHE ────────────────────────────────────────────────────────────────
# Historical block data is immutable, so we persist every RPC response to a local
# SQLite database. On each run we look in the DB first and only hit the (rate-
# limited) RPC endpoint on a cache miss. The cache therefore grows with every run
# and repeated runs over overlapping ranges get dramatically faster.

_conn = None
_db_lock = threading.Lock()   # serialises DB access when fetches run concurrently
cache_hits = 0
cache_misses = 0


def init_cache():
    global _conn
    # check_same_thread=False so the worker threads (block + receipts fetched in
    # parallel) may share the connection; all access is serialised via _db_lock.
    _conn = sqlite3.connect(CACHE_DB, check_same_thread=False)
    # WAL lets the live web server read the cache while a refresh writes to it;
    # busy_timeout makes any contended access wait rather than erroring.
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=30000")
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS blocks ("
        "  number INTEGER NOT NULL,"
        "  full   INTEGER NOT NULL,"  # 1 = full transaction objects, 0 = tx hashes only
        "  data   TEXT,"
        "  PRIMARY KEY (number, full)"
        ")"
    )
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS receipts ("
        "  number INTEGER PRIMARY KEY,"
        "  data   TEXT"
        ")"
    )
    # Resolved "block whose timestamp is closest to target_ts" lookups. The binary
    # search that finds the date-range boundaries always converges to the same
    # block for a given timestamp, so we cache the answer and skip the search
    # (and its ~20 RPC probes) entirely on later runs.
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS ts_index ("
        "  target_ts INTEGER PRIMARY KEY,"
        "  number    INTEGER"
        ")"
    )
    _conn.commit()


def cached_block_at_timestamp(target_ts: int, lo: int, hi: int) -> int:
    """block_at_timestamp() with a persistent cache keyed on the target timestamp."""
    global cache_hits, cache_misses
    row = _conn.execute(
        "SELECT number FROM ts_index WHERE target_ts = ?", (target_ts,)
    ).fetchone()
    if row is not None:
        cache_hits += 1
        return row[0]

    cache_misses += 1
    number = block_at_timestamp(target_ts, lo, hi)
    _cache_put("ts_index", "target_ts, number", "?, ?", (target_ts, number))
    return number


def _cache_get(table: str, where: str, params: tuple):
    with _db_lock:
        row = _conn.execute(
            f"SELECT data FROM {table} WHERE {where}", params
        ).fetchone()
    if row is None:
        return None, False          # cache miss
    return (json.loads(row[0]) if row[0] is not None else None), True


def _cache_put(table: str, columns: str, placeholders: str, params: tuple):
    with _db_lock:
        _conn.execute(
            f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})", params
        )
        _conn.commit()

# ─── CACHE SLIMMING ─────────────────────────────────────────────────────────────
# Full block/receipt JSON includes per-transaction `input` calldata, logs, sigs,
# etc. — none of which the reward calculation touches. We project each response
# down to just the fields the maths needs before storing, which shrinks the DB by
# ~10-50x. The slim shape is still a valid input to the compute functions.

# Per-transaction fields needed by the reward calc (exact + approximate modes).
_TX_KEEP      = ("hash", "gas", "gasPrice", "maxFeePerGas", "maxPriorityFeePerGas")
_BLOCK_KEEP   = ("number", "timestamp", "baseFeePerGas")
_RECEIPT_KEEP = ("transactionHash", "gasUsed", "effectiveGasPrice")


def _slim_block(block):
    """Project a block down to header fields + the tx fields the reward calc uses."""
    if not isinstance(block, dict):
        return block
    slim = {k: block[k] for k in _BLOCK_KEEP if k in block}
    txs = []
    for tx in block.get("transactions", []):
        if isinstance(tx, str):
            txs.append(tx)                       # hash-only block (full=False)
        else:
            txs.append({k: tx[k] for k in _TX_KEEP if k in tx})
    slim["transactions"] = txs
    return slim


def _slim_receipts(receipts):
    if not isinstance(receipts, list):
        return receipts
    return [{k: r[k] for k in _RECEIPT_KEEP if k in r} for r in receipts]

# ─── RPC HELPERS ──────────────────────────────────────────────────────────────

_id = 0

def rpc(method: str, params: list):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": _id}

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = _session.post(RPC_URL, json=payload, timeout=TIMEOUT,
                                 auth=RPC_AUTH, headers=RPC_HEADERS, verify=RPC_VERIFY)
        except requests.exceptions.RequestException as e:
            # Connection / DNS / timeout error — transient. Back off and retry
            # rather than letting the caller skip (and never cache) this block.
            if attempt < MAX_RETRIES:
                wait = BACKOFF_BASE * (2 ** attempt)
                tqdm.write(f"  connection error ({type(e).__name__}); "
                           f"backing off {wait:.1f}s …")
                time.sleep(wait)
                continue
            raise

        # Rate-limited / transient server error → exponential backoff and retry.
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < MAX_RETRIES:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() \
                    else BACKOFF_BASE * (2 ** attempt)
                tqdm.write(f"  rate-limited ({resp.status_code}); "
                           f"backing off {wait:.1f}s …")
                time.sleep(wait)
                continue
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        return data["result"]

    raise RuntimeError(f"RPC failed after {MAX_RETRIES} retries: {method}")


def get_block(number: int, full_txns: bool = True):
    global cache_hits, cache_misses
    full_flag = 1 if full_txns else 0

    # 1) Exact match in cache?
    cached, found = _cache_get("blocks", "number = ? AND full = ?", (number, full_flag))
    if found:
        cache_hits += 1
        return cached

    # 2) A full block is a superset of a hash-only block — reuse it for full=False.
    if not full_txns:
        cached, found = _cache_get("blocks", "number = ? AND full = 1", (number,))
        if found:
            cache_hits += 1
            return cached

    # 3) Cache miss — fetch from the RPC endpoint and store it.
    cache_misses += 1
    result = _slim_block(rpc("eth_getBlockByNumber", [hex(number), full_txns]))
    time.sleep(SLEEP_MS / 1000)
    _cache_put(
        "blocks", "number, full, data", "?, ?, ?",
        (number, full_flag, json.dumps(result)),
    )
    return result


def get_block_receipts(number: int):
    """Try eth_getBlockReceipts (Geth >=1.13 / Nethermind / Erigon)."""
    global cache_hits, cache_misses

    cached, found = _cache_get("receipts", "number = ?", (number,))
    if found:
        cache_hits += 1
        return cached

    cache_misses += 1
    result = _slim_receipts(rpc("eth_getBlockReceipts", [hex(number)]))
    time.sleep(SLEEP_MS / 1000)
    _cache_put("receipts", "number, data", "?, ?", (number, json.dumps(result)))
    return result


def current_block_number() -> int:
    return int(rpc("eth_blockNumber", []), 16)

# ─── BINARY SEARCH FOR BLOCK BY TIMESTAMP ─────────────────────────────────────

def block_at_timestamp(target_ts: int, lo: int, hi: int) -> int:
    """Return the block number whose timestamp is closest to target_ts."""
    while lo < hi:
        mid = (lo + hi) // 2
        blk = get_block(mid, False)
        blk_ts = int(blk["timestamp"], 16)
        if blk_ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo

# ─── EXECUTION REWARD CALCULATION ─────────────────────────────────────────────

def compute_execution_reward_from_receipts(block: dict, receipts: list) -> float:
    """
    Exact calculation using per-transaction gasUsed from receipts.
    execution_reward = Σ (effectiveGasPrice - baseFeePerGas) * gasUsed
    """
    base_fee = int(block["baseFeePerGas"], 16)
    reward_wei = 0

    # Fast path: post-London receipts carry both effectiveGasPrice and gasUsed, which
    # is everything the priority-fee maths needs. Sum straight over the receipts in
    # O(n). (The old code matched each tx back to its receipt with a linear scan,
    # making it O(n²) per block — ~6 ms/block, painfully slow over a full census.)
    if receipts and "effectiveGasPrice" in receipts[0]:
        for r in receipts:
            tip_per_gas = int(r["effectiveGasPrice"], 16) - base_fee
            if tip_per_gas > 0:
                reward_wei += tip_per_gas * int(r["gasUsed"], 16)
        return reward_wei / 1e18

    # Fallback (receipts lack effectiveGasPrice): derive the tip from each tx's fee
    # fields, matched to gasUsed via a hash map.
    gas_used_map = {r["transactionHash"].lower(): int(r["gasUsed"], 16) for r in receipts}
    for tx in block["transactions"]:
        gas_used = gas_used_map.get(tx["hash"].lower())
        if gas_used is None:
            continue  # shouldn't happen
        if "maxFeePerGas" in tx and tx["maxFeePerGas"]:
            max_fee      = int(tx["maxFeePerGas"], 16)
            max_priority = int(tx["maxPriorityFeePerGas"], 16)
            tip_per_gas  = min(max_priority, max_fee - base_fee)
        else:
            gas_price   = int(tx["gasPrice"], 16)
            tip_per_gas = max(gas_price - base_fee, 0)
        reward_wei += max(tip_per_gas, 0) * gas_used

    return reward_wei / 1e18  # convert to ETH


def compute_execution_reward_approx(block: dict) -> float:
    """
    Approximation when eth_getBlockReceipts is unavailable.
    Uses gas LIMIT instead of gasUsed — will overestimate by ~50% on average.
    Marked clearly so the caller knows it's approximate.
    """
    base_fee = int(block["baseFeePerGas"], 16)
    reward_wei = 0
    for tx in block["transactions"]:
        gas_limit = int(tx["gas"], 16)
        if "maxFeePerGas" in tx and tx["maxFeePerGas"]:
            max_fee      = int(tx["maxFeePerGas"], 16)
            max_priority = int(tx["maxPriorityFeePerGas"], 16)
            tip_per_gas  = min(max_priority, max_fee - base_fee)
        else:
            gas_price   = int(tx["gasPrice"], 16)
            tip_per_gas = max(gas_price - base_fee, 0)
        reward_wei += tip_per_gas * gas_limit
    return reward_wei / 1e18


# ─── STATS OUTPUT ───────────────────────────────────────────────────────────────

def print_reward_stats(rewards_eth: list, header_lines: list):
    """Print the standard mean / median / percentile summary for a list of rewards."""
    arr = np.array(rewards_eth)
    print("\n" + "=" * 60)
    for line in header_lines:
        print(line)
    print("=" * 60)
    print(f"  Mean:          {np.mean(arr):.6f} ETH")
    print(f"  Median (p50):  {np.median(arr):.6f} ETH")
    print(f"  Std dev:       {np.std(arr):.6f} ETH")
    print()
    print("  Percentiles:")
    for p in [5, 10, 25, 50, 75, 90, 95, 99, 99.9]:
        print(f"    p{p:5.1f}:  {np.percentile(arr, p):.6f} ETH")
    print()
    print(f"  Min:    {np.min(arr):.6f} ETH  (block with lowest EL reward)")
    print(f"  Max:    {np.max(arr):.6f} ETH  (highest MEV block in sample)")
    print(f"  Blocks with zero reward: {int(np.sum(arr == 0)):,}")
    print("=" * 60)


# ─── CACHE-ONLY ANALYSIS ─────────────────────────────────────────────────────────

def analyze_from_cache(start_ts: int, end_ts: int):
    """Compute reward stats purely from cached blocks/receipts in [start_ts, end_ts].

    Makes no network calls — uses every cached full block whose timestamp falls in
    the window. Handy for re-analysing a period you've already sampled, regardless
    of how the sampling grid has changed since.
    """
    init_cache()
    rows = _conn.execute(
        "SELECT b.number, b.data, r.data "
        "FROM blocks b LEFT JOIN receipts r ON b.number = r.number "
        "WHERE b.full = 1"
    ).fetchall()

    rewards, n_blocks, no_receipts = [], 0, 0
    for _number, bdata, rdata in rows:
        if bdata is None:
            continue
        block = json.loads(bdata)
        ts = block.get("timestamp")
        if ts is None or not (start_ts <= int(ts, 16) <= end_ts):
            continue
        n_blocks += 1
        if not block.get("transactions"):
            rewards.append(0.0)
        elif rdata is not None:
            rewards.append(compute_execution_reward_from_receipts(block, json.loads(rdata)))
        else:
            no_receipts += 1

    print("=" * 60)
    print("Ethereum Execution Layer Reward Analyser — CACHE-ONLY MODE")
    print("=" * 60)
    print(f"Cache:  {CACHE_DB}")
    print(f"Period: {datetime.fromtimestamp(start_ts, tz=timezone.utc)} → "
          f"{datetime.fromtimestamp(end_ts, tz=timezone.utc)}")

    if not rewards:
        print("\nNo cached blocks found in this window. Run a normal pass first to "
              "populate the cache, then retry.")
        return

    # Coverage: how much of the window is actually cached (mainnet ≈ 12s/block).
    expected = max(1, (end_ts - start_ts) // 12)
    coverage = 100.0 * n_blocks / expected
    print_reward_stats(rewards, [
        "RESULTS  [EXACT, from cache]",
        f"Blocks in window: {n_blocks:,}  |  computed: {len(rewards):,}  "
        f"|  missing receipts: {no_receipts:,}",
        f"Coverage: {n_blocks:,} cached / ~{expected:,} expected (≈{coverage:.0f}%)",
    ])


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Ethereum Execution Layer Reward Analyser")
    print("=" * 60)
    print(f"RPC:    {RPC_URL}")
    print(f"Period: {datetime.fromtimestamp(START_TS, tz=timezone.utc).date()} → "
          f"{datetime.fromtimestamp(END_TS, tz=timezone.utc).date()}")
    print(f"Target sample: {N_SAMPLES} blocks")
    print(f"Cache:  {CACHE_DB}")
    print()

    init_cache()

    # ── Step 1: find block range ──────────────────────────────────────────────
    print("Step 1/3  Finding block range …")
    tip = current_block_number()
    print(f"  Current block: {tip:,}")

    # Conservative search window: ~1.1M blocks covers ~154 days at 12s/block
    # We search from (tip - 1,100,000) to tip for the start block
    search_lo = max(0, tip - 1_150_000)
    start_block = cached_block_at_timestamp(START_TS, search_lo, tip)
    end_block   = cached_block_at_timestamp(END_TS,   start_block, tip)
    n_blocks    = end_block - start_block

    print(f"  Start block ({datetime.fromtimestamp(START_TS, tz=timezone.utc)}):  ~{start_block:,}")
    print(f"  End block   ({datetime.fromtimestamp(END_TS, tz=timezone.utc)}):  ~{end_block:,}")
    print(f"  Total blocks in range: {n_blocks:,}")

    # ── Step 2: check eth_getBlockReceipts support ────────────────────────────
    print("\nStep 2/3  Checking node capabilities …")
    use_exact = False
    try:
        test_receipts = get_block_receipts(end_block)
        if isinstance(test_receipts, list):
            use_exact = True
            print("  eth_getBlockReceipts: SUPPORTED ✓ (exact mode)")
        else:
            print("  eth_getBlockReceipts: unexpected response — using approximate mode")
    except Exception as e:
        print(f"  eth_getBlockReceipts: NOT supported ({e}) — using approximate mode")

    if not use_exact:
        print("  NOTE: Approximate mode uses gas LIMIT not gas USED.")
        print("        Results will overestimate by ~40-60%. For exact results,")
        print("        use a node that supports eth_getBlockReceipts.")

    # ── Step 3: sample and compute rewards ───────────────────────────────────
    if COMPLETE:
        # Every block in the range — a complete census, no sampling.
        sampled_blocks = list(range(start_block, end_block + 1))
        print(f"\nStep 3/3  Fetching ALL {len(sampled_blocks):,} blocks in range …")
    elif SAMPLE_RANDOM:
        # Uniform random sample. Unseeded, so each run picks different blocks —
        # ideal for steadily accumulating fresh coverage in the cache.
        import random
        k = min(N_SAMPLES, n_blocks)
        sampled_blocks = sorted(random.sample(range(start_block, end_block + 1), k))
        print(f"\nStep 3/3  Randomly sampling {k:,} blocks …")
    else:
        step = max(1, n_blocks // N_SAMPLES)
        # Align sample points to a fixed global grid (multiples of `step`) rather
        # than to start_block. Overlapping windows from different runs then share
        # the same block numbers, maximising cache reuse.
        first = ((start_block + step - 1) // step) * step
        sampled_blocks = list(range(first, end_block, step))[:N_SAMPLES]
        print(f"\nStep 3/3  Sampling {N_SAMPLES} blocks …")

    # Newest-first ordering: fill coverage backward from the recent (often already
    # cached) end, so partial progress is a contiguous "last N days" as it grows.
    if REVERSE:
        sampled_blocks = sampled_blocks[::-1]
        print("  (reverse order: newest block first)")

    rewards_eth = []
    errors = 0

    def _reward_for(blk_num):
        """Fetch one block (+receipts in exact mode) and return its reward, or None."""
        block = get_block(blk_num, full_txns=True)
        if block is None or "baseFeePerGas" not in block:
            return None
        if not block["transactions"]:
            return 0.0
        if use_exact:
            return compute_execution_reward_from_receipts(block, get_block_receipts(blk_num))
        return compute_execution_reward_approx(block)

    # Fetch many blocks concurrently. The endpoint is the proposer's own node (no
    # rate limit), so throughput scales with worker count; cache writes are
    # serialised by _db_lock and the HTTP session pools connections.
    print(f"  ({WORKERS} concurrent workers)")
    with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as ex:
        futs = {ex.submit(_reward_for, b): b for b in sampled_blocks}
        for fut in tqdm(as_completed(futs), total=len(futs), unit="block"):
            try:
                r = fut.result()
                if r is not None:
                    rewards_eth.append(r)
            except Exception as e:
                errors += 1
                if errors <= 5:
                    tqdm.write(f"  Warning: block {futs[fut]} failed — {e}")

    if not rewards_eth:
        print("ERROR: No data collected. Check your RPC URL.")
        return

    # ── Results ───────────────────────────────────────────────────────────────
    mode_label = "EXACT" if use_exact else "APPROXIMATE (gas limit used)"
    total_lookups = cache_hits + cache_misses
    hit_rate = (100.0 * cache_hits / total_lookups) if total_lookups else 0.0
    print_reward_stats(rewards_eth, [
        f"RESULTS  [{mode_label}]",
        f"Period:  {datetime.fromtimestamp(START_TS, tz=timezone.utc)} → "
        f"{datetime.fromtimestamp(END_TS, tz=timezone.utc)}",
        f"Blocks sampled: {len(rewards_eth):,}  |  Errors: {errors}",
        f"Cache:  {cache_hits:,} hits / {cache_misses:,} misses "
        f"({hit_rate:.1f}% served locally)",
    ])

    # ── Spot-check blocks for Etherscan verification ──────────────────────────
    print("\nSpot-check these blocks on Etherscan to verify:")
    indices = [0, len(sampled_blocks)//4, len(sampled_blocks)//2,
               3*len(sampled_blocks)//4, -1]
    for i in indices:
        bn = sampled_blocks[i]
        if i < len(rewards_eth):
            r = rewards_eth[i] if i >= 0 else rewards_eth[i]
            print(f"  Block {bn:,}:  {r:.6f} ETH  → https://etherscan.io/block/{bn}")


def _parse_date(s: str) -> int:
    """Parse a YYYY-MM-DD (or full ISO) date to a UTC unix timestamp."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Ethereum execution-layer reward analyser.")
    parser.add_argument(
        "--from-cache", nargs=2, metavar=("START", "END"),
        help="Compute stats from already-cached blocks in [START, END] "
             "(YYYY-MM-DD), making no network calls.")
    parser.add_argument(
        "--cache-hours", type=int, metavar="N",
        help="Compute stats from cached blocks in the last N hours, no network calls.")
    parser.add_argument("--start", metavar="YYYY-MM-DD",
                        help="Start date of the window (default: last 7 days).")
    parser.add_argument("--end", metavar="YYYY-MM-DD",
                        help="End date of the window (default: now). Lets you census "
                             "a specific past range, e.g. --start ... --end ...")
    parser.add_argument("--samples", type=int, metavar="N",
                        help="Number of evenly-spaced blocks to sample.")
    parser.add_argument("--hours", type=int, metavar="H",
                        help="Window = last H hours (snapped to the hour).")
    parser.add_argument("--complete", action="store_true",
                        help="Fetch EVERY block in the range (no sampling).")
    parser.add_argument("--random", action="store_true",
                        help="Sample blocks at random (unseeded) instead of an even "
                             "grid — accumulates fresh cache coverage each run.")
    parser.add_argument("--reverse", action="store_true",
                        help="Fetch newest block first (backward fill), so partial "
                             "progress is a contiguous recent window.")
    parser.add_argument("--workers", type=int, metavar="N",
                        help="Concurrent blocks to fetch at once (default 8).")
    args = parser.parse_args()

    if args.cache_hours:
        end = int(datetime.now(timezone.utc).timestamp())
        analyze_from_cache(end - args.cache_hours * 60 * 60, end)
    elif args.from_cache:
        start = _parse_date(args.from_cache[0])
        # If END is a bare date, extend to the end of that day so it's inclusive.
        end = _parse_date(args.from_cache[1])
        if len(args.from_cache[1]) <= 10:
            end += 24 * 60 * 60 - 1
        analyze_from_cache(start, end)
    else:
        if args.hours:
            # Use real "now" (up to the chain tip) so a recent capture is current.
            END_TS = int(datetime.now(timezone.utc).timestamp())
            START_TS = END_TS - args.hours * 60 * 60
        if args.start:
            START_TS = _parse_date(args.start)
        if args.end:
            END_TS = _parse_date(args.end)
        if args.samples:
            N_SAMPLES = args.samples
        if args.complete:
            COMPLETE = True
        if args.random:
            SAMPLE_RANDOM = True
        if args.reverse:
            REVERSE = True
        if args.workers:
            WORKERS = args.workers
        main()
