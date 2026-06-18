#!/usr/bin/env python3
"""
Blockspace dashboard — thin execution-RPC wrapper (Phase 2a, no DB).

Serves the static site + the two Dune CSVs + a small JSON API on one port.
The RPC token is read from .env and stays server-side — it is never sent to
the browser. Live data is an in-memory rolling window only (no persistence).

Endpoints:
  GET /api/health              -> {ok, head, window}
  GET /api/head                -> {head}
  GET /api/block/<id>          -> computed block (<id> = number | 0xhash | latest)
  GET /api/live/recent?n=120   -> in-memory rolling window, newest first
  GET /api/tx/<hash>           -> tx + receipt: priority fee paid, status, block/index
  GET /api/gas                 -> base-fee + priority-fee percentiles, spot prices, tips
  GET /api/mempool             -> txpool counts (if exposed) + pending-block preview
  GET /api/address/<addr>      -> balance, nonce, contract-or-EOA (point lookup, no history)

Reward (ported from dune-queries.txt CASE — both metrics returned):
  fees = Σ (effectiveGasPrice - baseFee) * gasUsed / 1e18          # priority-fee sum
  take = fees for vanilla blocks; builder->proposer MEV payment otherwise
Run:  python3 server.py        (PORT env, default 8137)
"""

import json
import os
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.request
import urllib.error
from collections import deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DB = os.path.join(BASE, "blocks_cache.sqlite")   # built by executionRewards.py + build_history.py


def _git(*args):
    try:
        return subprocess.check_output(
            ["git", "-C", BASE, *args], stderr=subprocess.DEVNULL, timeout=3).decode().strip()
    except Exception:
        return ""

# Resolved once at startup so the footer shows the running code version.
VERSION = _git("rev-parse", "--short", "HEAD") or "unknown"
COMMIT_DATE = _git("log", "-1", "--date=short", "--format=%cd")


def _cache_conn():
    if not os.path.exists(CACHE_DB):
        return None
    try:
        conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
        conn.execute("PRAGMA busy_timeout=5000")   # wait, don't error, if a refresh is mid-write
        return conn
    except sqlite3.Error:
        return None


def history_rows():
    """Daily reward percentiles from the cache DB's daily_percentiles table.

    Returns [] if the cache/table is absent, so the front end falls back to the CSV.
    """
    conn = _cache_conn()
    if conn is None:
        return []
    try:
        cur = conn.execute(
            "SELECT day, blocks, p50, p80, p90, p99 FROM daily_percentiles ORDER BY day")
        cols = ("day", "blocks", "p50", "p80", "p90", "p99")
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def summary_dict():
    """Pooled full-period percentiles (90th pct of every block), or {} if absent."""
    conn = _cache_conn()
    if conn is None:
        return {}
    try:
        return {k: v for k, v in conn.execute("SELECT key, value FROM summary")}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def data_through():
    """Most recent day present in daily_percentiles (the data freshness mark)."""
    conn = _cache_conn()
    if conn is None:
        return None
    try:
        r = conn.execute("SELECT max(day) FROM daily_percentiles").fetchone()
        return r[0] if r else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def bidwait_rows():
    """Bid & win rows from the cache DB's bid_winnable table; [] if absent."""
    conn = _cache_conn()
    if conn is None:
        return []
    try:
        cur = conn.execute(
            "SELECT day, my_bid, winnable_blocks, max_wait_min, max_wait_hours "
            "FROM bid_winnable ORDER BY day, my_bid")
        cols = ("day", "my_bid", "winnable_blocks", "max_wait_min", "max_wait_hours")
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
WINDOW_MAX = 200          # rolling live blocks kept in memory
SEED_N = 60               # blocks computed on startup
POLL_SECS = 2             # head poll interval — catch new blocks fast

# ---- config / secrets (server-side only) -------------------------------
def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env

_ENV = load_env(os.path.join(BASE, ".env"))
# config resolves from the process environment first, then .env
def _cfg(key, default=None):
    return os.environ.get(key) or _ENV.get(key) or default
RPC_URL = _cfg("EL_RPC_URL") or _cfg("RPC_URL") or _cfg("BEACON_URL")      # execution-layer JSON-RPC
RPC_TOKEN = _cfg("EL_RPC_TOKEN") or _cfg("RPC_TOKEN") or _cfg("BEACON_TOKEN")
PORT = int(_cfg("PORT", "8137"))
INSECURE = (_cfg("INSECURE") == "1") or (_cfg("RPC_VERIFY", "true").lower() == "false")
if not RPC_URL:
    raise SystemExit("No RPC URL — set EL_RPC_URL in .env (copy .env.example)")

_SSL = ssl._create_unverified_context() if INSECURE else None

# ---- RPC ----------------------------------------------------------------
_rpc_id = 0
_rpc_lock = threading.Lock()

def rpc(method, params, _tries=5):
    global _rpc_id
    with _rpc_lock:
        _rpc_id += 1
        rid = _rpc_id
    payload = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode()
    headers = {"Content-Type": "application/json"}
    if RPC_TOKEN:
        headers["Authorization"] = "Bearer " + RPC_TOKEN
    delay = 0.5
    for attempt in range(_tries):
        try:
            req = urllib.request.Request(RPC_URL, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30, context=_SSL) as resp:
                out = json.loads(resp.read())
            if "error" in out and out["error"]:
                raise RuntimeError(out["error"])
            return out["result"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < _tries - 1:
                time.sleep(delay); delay *= 2; continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < _tries - 1:
                time.sleep(delay); delay *= 2; continue
            raise
    raise RuntimeError("rpc retries exhausted: " + method)

def head_number():
    return int(rpc("eth_blockNumber", []), 16)

# ---- reward computation (Dune CASE) -------------------------------------
def _hexint(x, default=0):
    if x is None:
        return default
    return int(x, 16) if isinstance(x, str) else int(x)

def compute(block, receipts):
    base = _hexint(block.get("baseFeePerGas"))
    extra = block.get("extraData", "0x") or "0x"
    try:
        builder = bytes.fromhex(extra[2:]).decode("utf-8", "replace")
    except ValueError:
        builder = ""
    # strip control chars + the replacement glyph (non-UTF8 extraData) for clean display
    builder = "".join(c for c in builder if c >= " " and c != "�").strip()
    fees_wei = 0
    for r in receipts:
        fees_wei += (_hexint(r.get("effectiveGasPrice")) - base) * _hexint(r.get("gasUsed"))
    fees = fees_wei / 1e18

    txs = block.get("transactions", [])
    num_tx = len(txs)
    take, branch = fees, "fees"
    if num_tx:
        last = txs[-1]
        mev_pay = _hexint(last.get("value")) / 1e18
        data = last.get("input", "0x") or "0x"
        maxprio = _hexint(last.get("maxPriorityFeePerGas"))
        bl = builder.lower()
        vanilla = ("geth" in bl or "nethermind" in bl or len(builder) < 2
                   or mev_pay == 0 or data != "0x" or maxprio > 0)
        if not vanilla:
            take, branch = mev_pay, "mev"

    ts = _hexint(block.get("timestamp"))
    return {
        "block_number": _hexint(block.get("number")),
        "time": datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z"),
        "builder": builder.strip() or "—",
        "num_tx": num_tx,
        "base_fee_gwei": round(base / 1e9, 3),
        "reward_take": take,
        "reward_fees": fees,
        "branch": branch,
    }

# ---- block fetch + small LRU --------------------------------------------
_cache = OrderedDict()           # block_number -> computed
_cache_lock = threading.Lock()
_CACHE_MAX = 512

def _cache_get(n):
    with _cache_lock:
        if n in _cache:
            _cache.move_to_end(n)
            return _cache[n]
    return None

def _cache_put(n, v):
    with _cache_lock:
        _cache[n] = v
        _cache.move_to_end(n)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)

def fetch_block(tag, by_hash=False):
    """tag: hex number string, 'latest', or 0x-hash (with by_hash=True)."""
    if by_hash:
        block = rpc("eth_getBlockByHash", [tag, True])
    else:
        block = rpc("eth_getBlockByNumber", [tag, True])
    if not block:
        return None
    num = _hexint(block.get("number"))
    receipts = rpc("eth_getBlockReceipts", [hex(num)])
    return compute(block, receipts or [])

def compute_number(n):
    hit = _cache_get(n)
    if hit is not None:
        return hit
    val = fetch_block(hex(n))
    if val is not None:
        _cache_put(n, val)
    return val

# ---- live rolling window ------------------------------------------------
WINDOW = deque(maxlen=WINDOW_MAX)   # newest first
_win_lock = threading.Lock()
HEAD = 0

def _push_newest_first(blocks):
    # blocks given oldest->newest; appendleft so newest ends up at front
    with _win_lock:
        for b in blocks:
            WINDOW.appendleft(b)

def poller():
    global HEAD
    try:
        h = head_number()
    except Exception as e:
        print("poller: initial head failed:", e); return
    HEAD = h
    nums = list(range(h - SEED_N + 1, h + 1))
    with ThreadPoolExecutor(max_workers=12) as ex:
        seeded = list(ex.map(lambda n: _safe(compute_number, n), nums))
    _push_newest_first([b for b in seeded if b])
    print(f"seeded {sum(1 for b in seeded if b)} blocks up to #{h}")
    while True:
        time.sleep(POLL_SECS)
        try:
            h = head_number()
        except Exception:
            continue
        if h > HEAD:
            new = []
            for n in range(HEAD + 1, h + 1):
                b = _safe(compute_number, n)
                if b:
                    new.append(b)
            _push_newest_first(new)
            HEAD = h

def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        print("compute error:", e)
        return None

# ---- search: tx / gas / mempool / address (read-only, point lookups) ----
_ttl = {}                 # key -> (expires_at, value)
_ttl_lock = threading.Lock()

def _ttl_get(key):
    with _ttl_lock:
        e = _ttl.get(key)
        if e and e[0] > time.time():
            return e[1]
    return None

def _ttl_put(key, value, secs=4):
    with _ttl_lock:
        _ttl[key] = (time.time() + secs, value)

def _gwei(wei):
    return round(_hexint(wei) / 1e9, 3)

def _is_hash(s):
    return len(s) == 66 and s.startswith("0x")

def _is_addr(s):
    return len(s) == 42 and s.startswith("0x")

def tx_detail(h):
    """Tx + receipt + the block's base fee → effective priority fee paid (ETH)."""
    tx = rpc("eth_getTransactionByHash", [h])
    if not tx:
        return None
    rcpt = rpc("eth_getTransactionReceipt", [h])
    pending = tx.get("blockNumber") is None
    base = 0
    if not pending:
        blk = rpc("eth_getBlockByNumber", [tx["blockNumber"], False])
        base = _hexint(blk.get("baseFeePerGas")) if blk else 0
    eff = _hexint((rcpt or {}).get("effectiveGasPrice")) if rcpt else _hexint(tx.get("gasPrice"))
    gas_used = _hexint((rcpt or {}).get("gasUsed")) if rcpt else 0
    prio_fee = (eff - base) * gas_used / 1e18 if (not pending and rcpt) else None
    status_hex = (rcpt or {}).get("status") if rcpt else None
    status = _hexint(status_hex) if status_hex is not None else None
    return {
        "hash": tx.get("hash"),
        "from": tx.get("from"),
        "to": tx.get("to"),
        "value_eth": _hexint(tx.get("value")) / 1e18,
        "nonce": _hexint(tx.get("nonce")),
        "type": _hexint(tx.get("type")),
        "status": status,
        "block_number": _hexint(tx.get("blockNumber")) if not pending else None,
        "tx_index": _hexint(tx.get("transactionIndex")) if not pending else None,
        "gas_used": gas_used,
        "effective_gas_price_gwei": round(eff / 1e9, 3),
        "base_fee_gwei": round(base / 1e9, 3),
        "priority_fee_eth": prio_fee,
        "pending": pending,
    }

def gas_oracle():
    """Recent base-fee + priority-fee percentiles + spot prices + recommended tips."""
    hit = _ttl_get("gas")
    if hit is not None:
        return hit
    fh = rpc("eth_feeHistory", [hex(30), "latest", [10, 50, 90]]) or {}
    base = [round(_hexint(x) / 1e9, 3) for x in fh.get("baseFeePerGas", [])]
    rewards = fh.get("reward", []) or []
    p10 = [round(_hexint(r[0]) / 1e9, 3) for r in rewards]
    p50 = [round(_hexint(r[1]) / 1e9, 3) for r in rewards]
    p90 = [round(_hexint(r[2]) / 1e9, 3) for r in rewards]
    # feeHistory returns N+1 base fees: the last is the *next* block's base fee.
    next_base = base[-1] if base else None
    base_series = base[:-1] if len(base) > 1 else base
    recent = lambda s: round(sum(s[-5:]) / len(s[-5:]), 3) if s else 0.0
    val = {
        "base_fee_series": base_series,
        "prio_p10": p10, "prio_p50": p50, "prio_p90": p90,
        "next_base_fee_gwei": next_base,
        "gas_price_gwei": _gwei(rpc("eth_gasPrice", [])),
        "max_priority_gwei": _gwei(rpc("eth_maxPriorityFeePerGas", [])),
        "tip_standard_gwei": recent(p50),
        "tip_fast_gwei": recent(p90),
        "oldest_block": _hexint(fh.get("oldestBlock")),
    }
    _ttl_put("gas", val)
    return val

def mempool_status():
    """txpool counts (if exposed) + a preview of the pending block."""
    hit = _ttl_get("mempool")
    if hit is not None:
        return hit
    pending_count = queued_count = None
    try:
        st = rpc("txpool_status", [])
        pending_count = _hexint(st.get("pending"))
        queued_count = _hexint(st.get("queued"))
    except Exception:
        pass  # endpoint may not expose txpool_* — degrade to the pending-block preview
    pb = None
    blk = _safe(rpc, "eth_getBlockByNumber", ["pending", False])
    if blk:
        pb = {
            "num_tx": len(blk.get("transactions", [])),
            "gas_used": _hexint(blk.get("gasUsed")),
            "gas_limit": _hexint(blk.get("gasLimit")),
            "base_fee_gwei": _gwei(blk.get("baseFeePerGas")),
        }
    val = {"pending_count": pending_count, "queued_count": queued_count, "pending_block": pb}
    _ttl_put("mempool", val)
    return val

def address_detail(a):
    """Point lookups only: balance, nonce, contract-or-EOA. No tx history (no DB)."""
    bal = _hexint(rpc("eth_getBalance", [a, "latest"]))
    nonce = _hexint(rpc("eth_getTransactionCount", [a, "latest"]))
    code = rpc("eth_getCode", [a, "latest"]) or "0x"
    return {
        "address": a,
        "balance_eth": bal / 1e18,
        "nonce": nonce,
        "is_contract": len(code) > 2,
    }

# ---- HTTP ---------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=BASE, **k)

    def log_message(self, *a):
        pass  # quiet

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path.startswith("/api/"):
            return self._api(path, parse_qs(u.query))
        # never serve secrets / dotfiles
        if path.endswith(".env") or "/." in ("/" + path.lstrip("/")):
            return self.send_error(404)
        return super().do_GET()

    def _api(self, path, q):
        try:
            if path == "/api/health":
                return self._json({
                    "ok": True, "head": HEAD, "window": len(WINDOW),
                    "version": VERSION, "commit_date": COMMIT_DATE,
                    "data_through": data_through(), "refreshed_at": summary_dict().get("built_at"),
                })
            if path == "/api/head":
                return self._json({"head": head_number()})
            if path == "/api/history":
                return self._json({"days": history_rows(), "pooled": summary_dict()})
            if path == "/api/bidwait":
                return self._json({"bids": bidwait_rows()})
            if path == "/api/live/recent":
                n = min(int((q.get("n", ["120"])[0])), WINDOW_MAX)
                with _win_lock:
                    blocks = list(WINDOW)[:n]
                return self._json({"head": HEAD, "blocks": blocks})
            if path.startswith("/api/block/"):
                ident = path[len("/api/block/"):]
                if ident == "latest":
                    val = fetch_block("latest")
                elif ident.startswith("0x") and len(ident) > 42:
                    val = fetch_block(ident, by_hash=True)
                else:
                    val = compute_number(int(ident))
                if not val:
                    return self._json({"error": "not found"}, 404)
                return self._json(val)
            if path.startswith("/api/tx/"):
                h = path[len("/api/tx/"):]
                if not _is_hash(h):
                    return self._json({"error": "bad tx hash"}, 404)
                val = tx_detail(h)
                if not val:
                    return self._json({"error": "not found"}, 404)
                return self._json(val)
            if path == "/api/gas":
                return self._json(gas_oracle())
            if path == "/api/mempool":
                return self._json(mempool_status())
            if path.startswith("/api/address/"):
                a = path[len("/api/address/"):]
                if not _is_addr(a):
                    return self._json({"error": "bad address"}, 404)
                return self._json(address_detail(a))
            return self._json({"error": "unknown endpoint"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 502)

def main():
    threading.Thread(target=poller, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"blockspace server on http://localhost:{PORT}  (RPC: {urlparse(RPC_URL).netloc}, verify={'off' if INSECURE else 'on'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()

if __name__ == "__main__":
    main()
