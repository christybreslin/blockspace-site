# Blockspace

A Bitwise-styled analytics dashboard for the **price of Ethereum blockspace** — the
per-block execution-layer reward (the proposer's take). Historical distributions come
from Dune query exports; live blocks and single-block lookups come straight from an
execution-layer RPC, wrapped by a small zero-dependency Python server.

## Tabs

- **Overview** — headline percentiles, a smoothed percentile fan (24mo), and a daily-p90 regime calendar.
- **Block value** — p50/p90/p99 per-block reward over time (7-day smoothed) + a sortable daily table.
- **Bid & win** — for a fixed bid, how many blocks/day you'd win and the worst-case wait; a bid×day winnable-share heatmap.
- **Live** — real blocks from the RPC (~every block), a block-value stream, rolling stats, builder share, recent-reward distribution, a block table, and a block lookup (number / hash / `latest`).

## Requirements

- **Python 3** (standard library only — no pip install needed).
- An **execution-layer JSON-RPC** endpoint that supports `eth_getBlockByNumber`,
  `eth_getBlockReceipts`, and `eth_blockNumber`. An archive node is **not** required
  (block bodies + receipts suffice).

## Setup & run

```bash
cp .env.example .env          # then edit .env with your RPC URL + token
python3 server.py             # serves the site + API on http://0.0.0.0:8137
```

Open `http://localhost:8137`. Set `PORT` in `.env` to change the port.

The server (`server.py`):
- serves the static site and the two CSVs,
- reads `EL_RPC_URL` / `EL_RPC_TOKEN` from `.env` **server-side only** (the token is never sent to the browser),
- exposes a small JSON API and keeps an **in-memory rolling window** of recent blocks (no database).

### API

| Endpoint | Returns |
|---|---|
| `GET /api/health` | `{ ok, head, window }` |
| `GET /api/head` | latest block number |
| `GET /api/block/<id>` | computed block — `<id>` = number, `0x…` hash, or `latest` |
| `GET /api/live/recent?n=120` | the in-memory rolling window, newest first |

## Data

- **History** — `block_rewards_percentiles.csv` (daily p50/p80/p90/p99, in ETH) is now
  built from a **complete local census** of every block (see "Building history" below),
  not the old Dune export, so it is gap-free, current to the chain tip, and has a real p80.
  `blockspace_max_wait.csv` (bid winnable/wait) is still the Dune export for now.
- **Live + lookup** — computed on demand from the RPC.

## Building history from your own node

History is produced from a local SQLite cache of blocks, populated directly from your
execution-layer endpoint by `executionRewards.py`, then turned into the CSV the site loads
by `build_history.py`. Both read the **same `.env`** as the server, so there is one secret
to manage and the RPC token is never committed (`.env`, `credentials.py`, and the cache DB
are all gitignored).

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # requests, numpy, tqdm
cp .env.example .env            # set EL_RPC_URL, EL_RPC_TOKEN, RPC_VERIFY=false if self-signed

# 1. Build the cache from Jan 1 to the chain tip (complete census).
#    ~88 blocks/s with the default 8 workers; durable and resumable if interrupted.
.venv/bin/python executionRewards.py --start 2026-01-01 --complete

# 2. Generate the history CSV the dashboard loads.
.venv/bin/python build_history.py        # writes block_rewards_percentiles.csv

# 3. Serve the site (standard library only).
python3 server.py
```

Keep it current by re-running a catch-up plus the build (e.g. from cron):
`python3 executionRewards.py --hours 36 --complete && python3 build_history.py`.
The cache grows ~90 KB/block (~0.1 GB per month of full census). Run
`python3 executionRewards.py --help` for all modes.

**Metric note:** the census reward is the priority-fee sum
`Σ(effectiveGasPrice − baseFee)·gasUsed` for every block (the block-value proxy the server
also reports). The retired Dune history used the MEV-Boost builder→proposer payment for
MEV blocks, which ran ~15–20% lower at p90, so the two are not directly comparable.

**Reward definition** (mirrors the Dune query logic): priority-fee sum
`Σ(effectiveGasPrice − baseFee)·gasUsed` for vanilla blocks; the builder→proposer payment
(final transaction value) for MEV-Boost blocks. The server returns both metrics per block.

### Caveats

- No persistent cache/backfill yet — historical depth is bounded by the CSVs plus the live window. Querying arbitrary historical ranges from the RPC is a future addition.
- Block value is an on-chain **proxy** for the true MEV-Boost auction bid (which lives in the relays), with its known imprecision.
- The source Dune percentile query has a `p80` copy-paste bug (`p80 == p50`); the UI does not surface `p80`.

## Project layout

```
index.html        shell + tab panes
site.css          tokens + components (Bitwise design system)
app.js            data loading, rendering, charts (vanilla JS, no framework)
server.py         RPC wrapper + static server (stdlib only)
styles/fonts.css  @font-face declarations
assets/           fonts + wordmark
*.csv             Dune history exports
AGENTS.md         how to maintain the styling system
.env.example      copy to .env (gitignored)
```

> Fonts under `assets/fonts/` are licensed Bitwise brand fonts — keep this repository private.
