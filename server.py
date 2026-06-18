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

Reward (ported from dune-queries.txt CASE — both metrics returned):
  fees = Σ (effectiveGasPrice - baseFee) * gasUsed / 1e18          # priority-fee sum
  take = fees for vanilla blocks; builder->proposer MEV payment otherwise
Run:  python3 server.py        (PORT env, default 8137)
"""

import json
import os
import sqlite3
import ssl
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


def _cache_conn():
    if not os.path.exists(CACHE_DB):
        return None
    try:
        return sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
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
                return self._json({"ok": True, "head": HEAD, "window": len(WINDOW)})
            if path == "/api/head":
                return self._json({"head": head_number()})
            if path == "/api/history":
                return self._json({"days": history_rows(), "pooled": summary_dict()})
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
