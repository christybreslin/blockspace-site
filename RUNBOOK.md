# Blockspace site — build & run runbook

A summary of what we run to produce and serve the Ethereum blockspace dashboard.
Server checkout: `/root/workspace/eth/blockspace/blockspace-site` (runs as root).

## The pipeline in one line

Census blocks from an execution-layer node → build the daily/summary/bid tables
from that cache → serve them as a small JSON API + static site. Two networks run
the same pipeline into separate caches: **mainnet** (`blocks_cache.sqlite`) and
**Sepolia** (`blocks_cache_sepolia.sqlite`).

```
executionRewards.py  →  blocks_cache*.sqlite  →  build_history.py  →  daily_percentiles / summary / bid_winnable  →  server.py
   (census)                 (raw block cache)        (analytics)              (tables the site reads)                  (API + site)
```

## Components

| File | Role |
|------|------|
| `executionRewards.py` | Census: fetch blocks + receipts from the EL RPC, compute per-block reward, store slimmed in `blocks_cache*.sqlite`. |
| `build_history.py` | Read the cache, write `daily_percentiles`, `summary`, `bid_winnable` tables (+ CSV fallbacks) back into it. |
| `server.py` | Stdlib-only HTTP server: serves the static site and the JSON API (`/api/history`, `/api/bidwait`, `/api/health`, live RPC endpoints). |
| `app.js` / `index.html` / `site.css` | The dashboard front end (static). |
| `deploy/` | systemd units + refresh scripts for production. |

## 1. One-time setup

```bash
cd /root/workspace/eth/blockspace/blockspace-site
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # requests, numpy, tqdm (census/build only)
```

Credentials live in `.env` (gitignored), read by both `server.py` and the census tool:

```bash
EL_RPC_URL=https://mainnet-user-el.attestant.io
EL_RPC_TOKEN=<bearer token>
RPC_VERIFY=false                # endpoint uses a self-signed cert (or INSECURE=1)
# Sepolia (optional overrides; inherits the shared token if unset):
SEPOLIA_EL_RPC_URL=https://USER:PASS@<sepolia endpoint>   # basic-auth may be inline in the URL
SEPOLIA_WORKERS=8
SEPOLIA_SLEEP_MS=0
```

On the server this file lives outside the repo at `/etc/blockspace/env` (loaded by
the systemd units, or `set -a; . /etc/blockspace/env; set +a` for a manual run).

## 2. Census the block data

Builds/extends the raw block cache. `--complete` = every block; `--reverse` fills
newest-first; the endpoint auth + slimming + backoff are handled internally.

```bash
# Mainnet — a full trailing year (the dashboard's max window):
.venv/bin/python executionRewards.py --start 2025-07-11 --complete --reverse

# Sepolia — same, into its own cache:
.venv/bin/python executionRewards.py --sepolia --start 2025-07-11 --complete --reverse

# Catch up to the chain tip (what the refresh timer runs):
.venv/bin/python executionRewards.py --hours 6 --complete
```

Resumable: the cache persists, so a re-run only fetches missing blocks. Check
coverage with `--report-gaps` on the build step (below).

## 3. Build the history tables

Turns the raw cache into the tables the site reads. `--incremental` parses only
blocks past a stored watermark (seconds); the first run (no cache) auto-falls-back
to a full scan and seeds it. Run a plain full `build_history.py` once after a
historical gap backfill.

```bash
.venv/bin/python build_history.py --incremental --report-gaps            # mainnet
.venv/bin/python build_history.py --sepolia --incremental --report-gaps  # sepolia
```

Writes `daily_percentiles`, `summary`, `bid_winnable` into the cache DB, plus
`block_rewards_percentiles*.csv` / `blockspace_max_wait*.csv` as fallbacks.

## 4. Serve the site

```bash
.venv/bin/python server.py            # PORT env, default 8137
```

`server.py` reads the tables per request, so it never needs restarting on a data
refresh — only on a code change to `server.py` itself. `app.js`/`index.html` are
static and load on the next browser refresh.

## 5. Production (systemd)

Units are in `deploy/` (copy to `/etc/systemd/system/`, `daemon-reload`, enable):

| Unit | What it does |
|------|--------------|
| `blockspace.service` | Runs `server.py` on boot. |
| `blockspace-refresh.{service,timer}` | Mainnet: every 3h, `refresh.sh` = catch-up census + `build_history.py --incremental`. |
| `blockspace-refresh-sepolia.{service,timer}` | Sepolia: every 3h (offset 1h), `refresh-sepolia.sh` = catch-up census + `build_history.py --sepolia --incremental`. |

```bash
cp deploy/*.service deploy/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now blockspace.service
systemctl enable --now blockspace-refresh.timer
systemctl enable --now blockspace-refresh-sepolia.timer
systemctl list-timers | grep blockspace
```

## 6. Deploying a change

```bash
cd /root/workspace/eth/blockspace/blockspace-site
git pull
# restart ONLY if server.py changed:
systemctl restart blockspace.service
# rebuild tables if build_history.py / the metric changed:
.venv/bin/python build_history.py --incremental
.venv/bin/python build_history.py --sepolia --incremental
```

Front-end-only changes (`app.js`/`index.html`/`site.css`) need just `git pull` and
a browser hard-refresh.

## Notes

- `.env`, `credentials.py`, `blocks_cache*.sqlite`, `*.sqlite`, `.venv/` are gitignored.
- The dashboard's network toggle serves Sepolia via `?network=sepolia`; Live and
  Search are mainnet-only (hidden on Sepolia).
- See `handover.md` (repo root, one level up) for background, findings, and history.
