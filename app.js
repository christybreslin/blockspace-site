// ============================================================
//  Bitwise · Ethereum Blockspace analytics — site app
// ============================================================
//  PHASE 1 — frontend on illustrative DUMMY data.
//  All data flows through loadAll() + SimulatedLiveFeed. To go
//  live, swap ONLY those two for the execution-layer backend;
//  every renderer, chart, and table below is unchanged.
//
//  Data shapes (== the future /api responses, == the Dune queries):
//    blockValueDaily[]: { day, blocks, p50, p80, p90, p99 }   // ETH
//    bidWinDaily[]:     { day, my_bid, winnable_blocks, max_wait_min, max_wait_hours }
//    liveBlock:         { block_number, time, value_eth, builder, num_tx, base_fee_gwei }
// ============================================================

const BID_LADDER = [0.02, 0.05, 0.1, 0.15, 0.25, 0.5, 0.75, 1.0];   // ETH — capped at 1; 0.15 & 0.75 interpolated from the Q2 export
const WINDOW_DAYS = { "7d": 7, "30d": 30, "90d": 90, "1yr": 365 };
const LIVE_CAP = 200;        // rolling blocks kept in memory
const LIVE_ROLL = 50;        // rolling-stat window

const STATE = {
  window: "1yr",
  bid: 0.25,
  theme: "light",
  blockValueDaily: [],
  bidWinDaily: [],
  live: [],                 // newest first (from /api/live/recent)
  liveHead: 0,
  metric: "take",           // Live tab: "take" (proposer/Dune CASE) | "fees" (priority-fee sum)
  hmetric: "fees",          // history tabs (Overview/Block value): "fees" | "take" (proposer take)
  bvSortKey: "day",  bvSortDir: "desc", bvSearch: "",
  bwSortKey: "my_bid", bwSortDir: "asc",
  gas: null,                // latest /api/gas (Search tab)
  mempool: null,            // latest /api/mempool (Search tab)
  health: null,             // /api/health (version, data_through, refreshed_at)
};

// ----- math / format ----------------------------------------
function mulberry32(a) { return function () { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
function erf(x) {
  const t = 1 / (1 + 0.3275911 * Math.abs(x));
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
  return x >= 0 ? y : -y;
}
const normCdf = z => 0.5 * (1 + erf(z / Math.SQRT2));
function gauss() { let u = 0, v = 0; while (u === 0) u = Math.random(); while (v === 0) v = Math.random(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); }
const avg = a => a.length ? a.reduce((s, v) => s + v, 0) / a.length : 0;
function median(a) { if (!a.length) return 0; const s = [...a].sort((x, y) => x - y); const n = s.length; return n % 2 ? s[(n - 1) >> 1] : 0.5 * (s[n / 2 - 1] + s[n / 2]); }
function percentile(a, p) { if (!a.length) return 0; const s = [...a].sort((x, y) => x - y); const idx = p * (s.length - 1); const lo = Math.floor(idx), hi = Math.ceil(idx); return lo === hi ? s[lo] : s[lo] + (s[hi] - s[lo]) * (idx - lo); }
function niceStep(x) { const e = Math.pow(10, Math.floor(Math.log10(x))); const f = x / e; const nf = f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10; return nf * e; }

const ethF = v => v == null ? "—" : (v >= 1 ? v.toFixed(2) : v >= 0.1 ? v.toFixed(3) : v.toFixed(4));
const ethAxis = v => v >= 1 ? v.toFixed(1) : v >= 0.1 ? v.toFixed(2) : v >= 0.01 ? v.toFixed(3) : v.toFixed(4);
const numF = v => v == null ? "—" : Math.round(v).toLocaleString();
const kF = v => v >= 1e6 ? (v / 1e6).toFixed(2) + "M" : v >= 1000 ? (v / 1000).toFixed(1) + "k" : Math.round(v).toString();
const pctF = x => x == null ? "—" : (x * 100).toFixed(1) + "%";
const pct2 = x => x == null ? "—" : (x * 100).toFixed(2) + "%";
function waitF(mins) { if (mins == null) return "—"; if (mins >= 1440) return (mins / 1440).toFixed(1) + " d"; if (mins >= 60) return (mins / 60).toFixed(1) + " h"; return mins.toFixed(0) + " min"; }
const signed = x => x == null ? "—" : (Math.abs(x) < 0.0005 ? "0.0%" : (x > 0 ? "+" : "−") + Math.abs(x * 100).toFixed(1) + "%");
const escapeHtml = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
function dateShort(iso) { return new Date(iso + "T00:00:00").toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" }); }
function monthLabel(iso) { return new Date(iso + "T00:00:00").toLocaleDateString("en-US", { month: "short", year: "2-digit" }); }
function isoDay(d) { return d.toISOString().slice(0, 10); }

// ============================================================
//  REAL DATA
//   - History: the cache DB via /api (block value + bid/wait), CSV fallback.
//   - Live + search: the /api RPC wrapper (server.py).
//  This is the data seam — nothing below here knows the source.
// ============================================================
function parseCsv(text) {
  const lines = text.trim().split(/\r?\n/);
  const header = lines[0].split(",").map(h => h.trim());
  return lines.slice(1).map(line => {
    const cells = line.split(",");
    const row = {};
    header.forEach((h, i) => {
      const v = cells[i];
      const n = Number(v);
      row[h] = (v !== undefined && v !== "" && !Number.isNaN(n)) ? n : v;
    });
    return row;
  });
}
const csvDay = s => String(s).split(" ")[0];   // "2024-06-15 00:00:00.000 UTC" -> "2024-06-15"

// Daily block-value percentiles. Primary source is the server API backed by the
// cache DB (/api/history); falls back to the static CSV if the API is empty/down.
async function loadBlockValueDaily() {
  const shape = r => ({
    day: csvDay(r.day), blocks: +r.blocks,
    p50: +r.p50, p80: +r.p80, p90: +r.p90, p99: +r.p99,
    take_p50: +r.take_p50, take_p80: +r.take_p80, take_p90: +r.take_p90, take_p99: +r.take_p99,
  });
  const finalize = arr => arr.filter(r => r.day && isFinite(r.p50)).sort((a, b) => a.day < b.day ? -1 : 1);
  try {
    const res = await fetch("/api/history");
    if (res.ok) {
      const j = await res.json();
      if (j.days && j.days.length) {
        STATE.pooled = (j.pooled && isFinite(j.pooled.p90)) ? j.pooled : null;
        return finalize(j.days.map(shape));
      }
    }
  } catch (e) { /* fall through to CSV */ }
  const txt = await fetch("block_rewards_percentiles.csv").then(r => { if (!r.ok) throw new Error("percentiles.csv " + r.status); return r.text(); });
  return finalize(parseCsv(txt).map(shape));
}

// Bid & win daily rows. Primary source is the server API backed by the cache DB
// (/api/bidwait); falls back to the static CSV if the API is empty/down.
async function loadBidWinDaily() {
  const shape = r => ({ day: csvDay(r.day), my_bid: +r.my_bid, winnable_blocks: +r.winnable_blocks, max_wait_min: +r.max_wait_min, max_wait_hours: +r.max_wait_hours });
  const finalize = arr => arr.filter(r => r.day && isFinite(r.my_bid));
  try {
    const res = await fetch("/api/bidwait");
    if (res.ok) {
      const j = await res.json();
      if (j.bids && j.bids.length) return finalize(j.bids.map(shape));
    }
  } catch (e) { /* fall through to CSV */ }
  const txt = await fetch("blockspace_max_wait.csv").then(r => { if (!r.ok) throw new Error("max_wait.csv " + r.status); return r.text(); });
  return finalize(parseCsv(txt).map(shape));
}

async function loadAll() {
  STATE.blockValueDaily = await loadBlockValueDaily();
  STATE.bidWinDaily = await loadBidWinDaily();

  // The Q2 export only sampled 8 fixed bids. Add 2 finer rungs below 1 ETH by
  // log-interpolating each day's real neighbouring rows, so the new bids stay
  // consistent with the actual data (not fabricated).
  const csvIdx = new Map();
  for (const r of STATE.bidWinDaily) csvIdx.set(r.day + "|" + r.my_bid, r);
  const NEW_BIDS = [{ bid: 0.15, lo: 0.1, hi: 0.25 }, { bid: 0.75, lo: 0.5, hi: 1.0 }];
  const extra = [];
  for (const d of STATE.blockValueDaily) {
    for (const nb of NEW_BIDS) {
      const a = csvIdx.get(d.day + "|" + nb.lo), b = csvIdx.get(d.day + "|" + nb.hi);
      if (!a || !b) continue;
      const t = (Math.log(nb.bid) - Math.log(nb.lo)) / (Math.log(nb.hi) - Math.log(nb.lo));
      const wmin = a.max_wait_min + (b.max_wait_min - a.max_wait_min) * t;
      extra.push({
        day: d.day, my_bid: nb.bid,
        winnable_blocks: Math.round(a.winnable_blocks + (b.winnable_blocks - a.winnable_blocks) * t),
        max_wait_min: wmin, max_wait_hours: wmin / 60,
      });
    }
  }
  STATE.bidWinDaily.push(...extra);
}

// Real live feed — polls the server's in-memory rolling window (server.py
// computes each block from the execution RPC). Same block shape the UI
// expects, plus both reward metrics (reward_take / reward_fees).
class RealLiveFeed {
  constructor(onUpdate) { this.onUpdate = onUpdate; this.timer = null; }
  async poll() {
    try {
      const res = await fetch(`/api/live/recent?n=${LIVE_CAP}`);
      if (!res.ok) return;
      const d = await res.json();
      STATE.live = d.blocks || [];
      STATE.liveHead = d.head || 0;
      this.onUpdate();
    } catch { /* transient — keep last window */ }
  }
  start() { this.poll(); this.timer = setInterval(() => this.poll(), 2000); }
}
// Active per-block reward field, set by the metric toggle.
const liveValue = b => STATE.metric === "fees" ? b.reward_fees : b.reward_take;

// ----- window helpers ---------------------------------------
function windowDays() {
  const n = WINDOW_DAYS[STATE.window];
  return STATE.blockValueDaily.slice(-n);
}

// ----- history-metric accessors (fees | proposer take) ------
// Resolve a daily row's percentile (base = "p50"/"p80"/"p90"/"p99") or a pooled
// summary key to the active metric, falling back to fees when take data is absent
// (e.g. a CSV without take columns, or before the proposer-take re-pull lands).
const fin = (x, fallback) => isFinite(x) ? x : fallback;   // value or fallback when NaN/missing
const HMETRIC_LABEL = () => STATE.hmetric === "take" ? "proposer take" : "priority fees";
const hmPick = (row, base) => {
  if (STATE.hmetric === "take") { const v = row["take_" + base]; if (isFinite(v)) return v; }
  return row[base];
};
const hmP = (P, key) => {
  if (STATE.hmetric === "take") { const v = P["take_" + key]; if (isFinite(v)) return v; }
  return P[key];
};
// windowDays() with p50/p80/p90/p99 resolved to the active metric (for charts/tables).
function metricDays() {
  const d = windowDays();
  if (STATE.hmetric !== "take") return d;
  return d.map(r => ({ ...r, p50: hmPick(r, "p50"), p80: hmPick(r, "p80"), p90: hmPick(r, "p90"), p99: hmPick(r, "p99") }));
}
function bidAggregates() {
  const days = windowDays();
  const daySet = new Set(days.map(d => d.day));
  const blocksAvg = avg(days.map(d => d.blocks));
  return BID_LADDER.map(bid => {
    const rows = STATE.bidWinDaily.filter(r => r.my_bid === bid && daySet.has(r.day));
    const win = rows.map(r => r.winnable_blocks);
    const waits = rows.map(r => r.max_wait_min);
    const avgWin = avg(win);
    return { my_bid: bid, avgWin, avgShare: avgWin / blocksAvg, worstWait: Math.max(...waits), medWait: median(waits) };
  });
}

// ============================================================
//  RENDER — entry
// ============================================================
function renderAll() {
  renderOverview();
  renderBlockValue();
  renderBidWin();
  pulse();
}

function pulse() {
  document.querySelectorAll("[data-pulse]").forEach(el => {
    el.style.transition = "none"; el.style.opacity = "0";
    requestAnimationFrame(() => { el.style.transition = "opacity 400ms ease-out"; el.style.opacity = "1"; });
  });
}

// ----- Overview ---------------------------------------------
function renderOverview() {
  const bv = STATE.blockValueDaily;
  const days = windowDays();
  const latest = bv[bv.length - 1];

  const heroStamp = document.getElementById("hero-stamp");
  heroStamp.innerHTML =
    `<span class="mono">${dateShort(latest.day)}</span> · latest day · ${HMETRIC_LABEL()} · window <span class="mono">${STATE.window}</span> · <span class="mono">${days.length}</span> days${refreshStamp()}`;
  heroStamp.classList.toggle("stale", dataIsStale());

  // Pooled / windowed figures, resolved to the active metric (fees | take). Prefer
  // the server's true pooled percentiles; fall back to an average of daily values
  // only when running off the static CSV (no /api/history summary).
  const P = STATE.pooled || {};
  const year = latest.day.slice(0, 4);
  const ytdDays = bv.filter(d => d.day.slice(0, 4) === year);
  const p90_7d  = fin(hmP(P, "p90_7d"),  avg(bv.slice(-7).map(d => hmPick(d, "p90"))));
  const p90_30d = fin(hmP(P, "p90_30d"), avg(bv.slice(-30).map(d => hmPick(d, "p90"))));
  const p90_90d = fin(hmP(P, "p90_90d"), avg(bv.slice(-90).map(d => hmPick(d, "p90"))));
  const p90_ytd = fin(hmP(P, "p90_ytd"), avg(ytdDays.map(d => hmPick(d, "p90"))));
  const p50_1yr = fin(hmP(P, "p50_365d"), avg(bv.slice(-365).map(d => hmPick(d, "p50"))));   // rolling trailing year
  const p90_1yr = fin(hmP(P, "p90_365d"), avg(bv.slice(-365).map(d => hmPick(d, "p90"))));
  const p99_1yr = fin(hmP(P, "p99_365d"), avg(bv.slice(-365).map(d => hmPick(d, "p99"))));
  const hotThresh = fin(hmP(P, "hot_threshold"), 1.5 * p90_1yr);
  const hotDays   = fin(hmP(P, "hot_days"), bv.slice(-365).filter(d => hmPick(d, "p90") >= hotThresh).length);
  const hotTotal  = fin(hmP(P, "hot_total_days"), Math.min(bv.length, 365));
  const lp50 = hmPick(latest, "p50"), lp90 = hmPick(latest, "p90"), lp99 = hmPick(latest, "p99");

  // Two logical rows (12 tiles keeps every breakpoint's grid full):
  //  Row 1 — p90 across every horizon, short → long (the pricing ladder).
  //  Row 2 — the rest of the distribution (median, tail) latest vs rolling-year,
  //          plus volume and hot-day activity.
  const tiles = [
    { label: "p90 · latest", value: ethF(lp90), foot: "latest day", cls: "neutral" },
    { label: "p90 · 7d", value: ethF(p90_7d), foot: "trailing week", cls: "neutral" },
    { label: "p90 · 30d", value: ethF(p90_30d), foot: "trailing month", cls: "neutral" },
    { label: "p90 · 90d", value: ethF(p90_90d), foot: "trailing 90 days", cls: "neutral" },
    { label: "p90 · YTD", value: ethF(p90_ytd), foot: `${year} to date`, cls: "neutral" },
    { label: "p90 · 1yr", value: ethF(p90_1yr), foot: "rolling 365 days", cls: "neutral" },

    { label: "Median · latest", value: ethF(lp50), foot: "p50 · latest day", cls: "neutral" },
    { label: "Median · 1yr", value: ethF(p50_1yr), foot: "p50 · rolling 365 days", cls: "neutral" },
    { label: "Tail · latest", value: ethF(lp99), foot: "p99 · latest day", cls: "neutral" },
    { label: "Tail · 1yr", value: ethF(p99_1yr), foot: "p99 · rolling 365 days", cls: "neutral" },
    { label: "Blocks / day", value: numF(latest.blocks), foot: "latest · ~12s cadence", cls: "neutral" },
    { label: "Hot days", value: `${numF(hotDays)} / ${numF(hotTotal)}`, foot: `p90 ≥ ${ethF(hotThresh)} · list all →`, cls: hotDays > 0 ? "neg" : "neutral", href: "#hot-days" },
  ];
  document.getElementById("hero-kpis").innerHTML = tiles.map(t => {
    const inner = `
      <div class="label">${t.label}</div>
      <div class="value">${t.value}</div>
      <div class="foot ${t.cls}">${t.foot}</div>`;
    return t.href
      ? `<a class="dash-kpi is-link" href="${t.href}" data-pulse>${inner}</a>`
      : `<div class="dash-kpi" data-pulse>${inner}</div>`;
  }).join("");

  buildFanChart("ov-fan", {
    dates: days.map(d => d.day), p50: days.map(d => hmPick(d, "p50")), p90: days.map(d => hmPick(d, "p90")), p99: days.map(d => hmPick(d, "p99")),
    log: true, tooltipId: "ov-fan-tt",
  });
  // The regime calendar is always the rolling year — it's a long-horizon view, so
  // it ignores the window switch (a 7d/30d calendar would be sparse and the colour
  // scale would recalibrate to a meaningless range).
  buildCalendarHeatmap("ov-cal", bv.slice(-365), d => hmPick(d, "p90"), "ov-cal-tt");
}

// ----- Hot days (drill-down from the Overview KPI) ----------
// Every day in the rolling trailing year whose daily p90 cleared the hot
// threshold (1.5× the rolling-year pooled p90), most recent first — same basis
// as the "Hot days" KPI tile and every other headline figure.
function renderHotDays() {
  const bv = STATE.blockValueDaily;
  if (!bv.length) return;
  const P = STATE.pooled || {};
  const ry = bv.slice(-365);                                   // rolling year
  const pooledP90 = fin(hmP(P, "p90_365d"), avg(ry.map(d => hmPick(d, "p90"))));
  const thresh = fin(hmP(P, "hot_threshold"), 1.5 * pooledP90);
  const hot = ry.filter(d => hmPick(d, "p90") >= thresh).sort((a, b) => a.day < b.day ? 1 : -1);  // most recent first

  const hotDeck = document.getElementById("hot-deck");
  hotDeck.innerHTML =
    `<span class="mono">${numF(hot.length)}</span> of <span class="mono">${numF(ry.length)}</span> days · rolling year · ${HMETRIC_LABEL()} · ` +
    `p90 ≥ <span class="mono">${ethF(thresh)}</span> ETH (1.5× rolling-year pooled <span class="mono">${ethF(pooledP90)}</span>)${refreshStamp()}`;
  hotDeck.classList.toggle("stale", dataIsStale());

  const head = `<thead><tr>
    <th>Day</th><th>p90 · ETH</th><th>× pooled</th><th>p50 · ETH</th><th>p99 · ETH</th><th>Blocks</th>
  </tr></thead>`;
  const body = hot.map(d => `<tr>
    <td>${dateShort(d.day)}</td>
    <td class="accent">${ethF(hmPick(d, "p90"))}</td>
    <td>${(hmPick(d, "p90") / pooledP90).toFixed(1)}×</td>
    <td>${ethF(hmPick(d, "p50"))}</td>
    <td>${ethF(hmPick(d, "p99"))}</td>
    <td>${numF(d.blocks)}</td>
  </tr>`).join("");
  document.getElementById("hot-table").innerHTML = head + `<tbody>${body ||
    `<tr><td colspan="6">No hot days in range.</td></tr>`}</tbody>`;
}

// ----- Block value (Q1) -------------------------------------
function renderBlockValue() {
  const days = metricDays();
  const dates = days.map(d => d.day);

  const span = STATE.window === "1yr" ? 7 : STATE.window === "90d" ? 5 : STATE.window === "30d" ? 3 : 1;
  buildFanChart("bv-chart", {
    dates, log: true, tooltipId: "bv-tooltip",
    titleSuffix: span > 1 ? `${span}-day avg` : null,
    p50: rollingMean(days.map(d => d.p50), span),
    p90: rollingMean(days.map(d => d.p90), span),
    p99: rollingMean(days.map(d => d.p99), span),
  });

  const kpis = [
    { label: "Median · p50 · avg", value: ethF(avg(days.map(d => d.p50))), foot: "ETH per block" },
    { label: "Win 90% · p90 · avg", value: ethF(avg(days.map(d => d.p90))), foot: "ETH per block" },
    { label: "Tail · p99 · avg", value: ethF(avg(days.map(d => d.p99))), foot: "ETH per block" },
    { label: "Blocks · window", value: kF(days.reduce((s, d) => s + d.blocks, 0)), foot: `${days.length} days` },
  ];
  document.getElementById("bv-kpis").innerHTML = kpis.map(k => `
    <div class="dash-kpi" data-pulse><div class="label">${k.label}</div><div class="value">${k.value}</div><div class="foot neutral">${k.foot}</div></div>`).join("");

  const lo = days[0], hi = days[days.length - 1];
  const bvDeck = document.getElementById("bv-deck");
  bvDeck.innerHTML =
    `${dateShort(lo.day)} → ${dateShort(hi.day)} · ${days.length} days · ${HMETRIC_LABEL()}${span > 1 ? ` · ${span}-day avg` : ""}${refreshStamp()}`;
  bvDeck.classList.toggle("stale", dataIsStale());

  renderBvTable();
}

function renderBvTable() {
  const cols = [
    { key: "day", label: "Day", text: true, fmt: dateShort },
    { key: "blocks", label: "Blocks", fmt: numF },
    { key: "p50", label: "p50 · ETH", fmt: ethF },
    { key: "p90", label: "p90 · ETH", fmt: ethF, accent: true },
    { key: "p99", label: "p99 · ETH", fmt: ethF },
  ];
  let rows = [...metricDays()];
  const term = STATE.bvSearch.trim().toLowerCase();
  if (term) rows = rows.filter(r => r.day.includes(term) || dateShort(r.day).toLowerCase().includes(term));
  const dir = STATE.bvSortDir === "asc" ? 1 : -1;
  rows.sort((a, b) => {
    const av = a[STATE.bvSortKey], bv = b[STATE.bvSortKey];
    if (typeof av === "string") return dir * av.localeCompare(bv);
    return dir * (av - bv);
  });
  rows = rows.slice(0, 400);   // cap DOM; window already bounds the data

  const head = `<thead><tr>${cols.map(c => `<th class="${c.text ? "text" : ""}" data-key="${c.key}">${c.label}${STATE.bvSortKey === c.key ? ` <span class="arrow">${STATE.bvSortDir === "desc" ? "▼" : "▲"}</span>` : ""}</th>`).join("")}</tr></thead>`;
  const body = rows.map(r => `<tr>${cols.map(c => `<td class="${c.text ? "text" : ""}${c.accent ? " accent" : ""}">${c.fmt(r[c.key])}</td>`).join("")}</tr>`).join("");
  const t = document.getElementById("bv-table");
  t.innerHTML = head + `<tbody>${body}</tbody>`;
  t.querySelectorAll("thead th").forEach(th => th.addEventListener("click", () => {
    const k = th.dataset.key;
    if (STATE.bvSortKey === k) STATE.bvSortDir = STATE.bvSortDir === "desc" ? "asc" : "desc";
    else { STATE.bvSortKey = k; STATE.bvSortDir = k === "day" ? "desc" : "desc"; }
    renderBvTable();
  }));
}

// ----- Bid & win (Q2) ---------------------------------------
function renderBidLadder() {
  const host = document.getElementById("bid-ladder");
  host.innerHTML = BID_LADDER.map(b => `<button data-bid="${b}" class="${b === STATE.bid ? "active" : ""}">${ethF(b)} ETH</button>`).join("");
  host.querySelectorAll("button").forEach(btn => btn.addEventListener("click", () => {
    STATE.bid = +btn.dataset.bid;
    host.querySelectorAll("button").forEach(x => x.classList.toggle("active", +x.dataset.bid === STATE.bid));
    renderBidWin();
  }));
}

function renderBidWin() {
  const days = windowDays();
  const dates = days.map(d => d.day);
  const daySet = new Set(dates);
  const bid = STATE.bid;
  const rows = STATE.bidWinDaily.filter(r => r.my_bid === bid && daySet.has(r.day));
  const winSeries = rows.map(r => r.winnable_blocks);

  buildBidTimeHeatmap("bw-heat", "bw-heat-tt");

  buildLineChart("bw-chart", {
    dates, log: false, areaKey: "win", tooltipId: "bw-tooltip",
    yFmt: kF, valFmt: v => numF(v) + " blk",
    series: [{ key: "win", cls: "p90", label: "Winnable/day", data: winSeries }],
  });

  const aggs = bidAggregates();
  const sel = aggs.find(a => a.my_bid === bid);
  buildBarChart("bw-curve", aggs.map(a => ({
    label: `${ethF(a.my_bid)} ETH`,
    frac: a.avgShare,
    valText: pctF(a.avgShare),
    cls: a.my_bid === bid ? "bar-accent" : "bar-mid",
    valCls: a.my_bid === bid ? "accent" : "",
  })));

  const kpis = [
    { label: "Bid", value: ethF(bid), foot: "ETH per block" },
    { label: "Winnable / day", value: kF(sel.avgWin), foot: `${STATE.window} avg` },
    { label: "Winnable share", value: pctF(sel.avgShare), foot: "of daily blocks" },
    { label: "Worst-case wait", value: waitF(sel.worstWait), foot: `median ${waitF(sel.medWait)}` },
  ];
  document.getElementById("bw-kpis").innerHTML = kpis.map(k => `
    <div class="dash-kpi" data-pulse><div class="label">${k.label}</div><div class="value">${k.value}</div><div class="foot neutral">${k.foot}</div></div>`).join("");

  const bwDeck = document.getElementById("bw-deck");
  bwDeck.innerHTML =
    `bid ${ethF(bid)} ETH · ${kF(sel.avgWin)} blocks/day · ${pctF(sel.avgShare)} share · ${STATE.window} avg${refreshStamp()}`;
  bwDeck.classList.toggle("stale", dataIsStale());

  renderBwTable(aggs);
}

function renderBwTable(aggs) {
  const cols = [
    { key: "my_bid", label: "Bid · ETH", text: true, fmt: ethF },
    { key: "avgWin", label: "Winnable/day", fmt: kF },
    { key: "avgShare", label: "Share", fmt: pctF },
    { key: "worstWait", label: "Worst wait", fmt: waitF },
    { key: "medWait", label: "Median wait", fmt: waitF },
  ];
  const dir = STATE.bwSortDir === "asc" ? 1 : -1;
  const rows = [...aggs].sort((a, b) => dir * (a[STATE.bwSortKey] - b[STATE.bwSortKey]));
  const head = `<thead><tr>${cols.map(c => `<th class="${c.text ? "text" : ""}" data-key="${c.key}">${c.label}${STATE.bwSortKey === c.key ? ` <span class="arrow">${STATE.bwSortDir === "desc" ? "▼" : "▲"}</span>` : ""}</th>`).join("")}</tr></thead>`;
  const body = rows.map(r => {
    const isSel = r.my_bid === STATE.bid;
    return `<tr class="${isSel ? "win-row" : ""}">${cols.map(c => `<td class="${c.text ? "text" : ""}${isSel && c.text ? " accent" : ""}">${c.fmt(r[c.key])}</td>`).join("")}</tr>`;
  }).join("");
  const t = document.getElementById("bw-table");
  t.innerHTML = head + `<tbody>${body}</tbody>`;
  t.querySelectorAll("thead th").forEach(th => th.addEventListener("click", () => {
    const k = th.dataset.key;
    if (STATE.bwSortKey === k) STATE.bwSortDir = STATE.bwSortDir === "desc" ? "asc" : "desc";
    else { STATE.bwSortKey = k; STATE.bwSortDir = k === "my_bid" ? "asc" : "desc"; }
    renderBwTable(bidAggregates());
  }));
}

// ----- Live -------------------------------------------------
const METRIC_LABEL = () => STATE.metric === "fees" ? "priority fees" : "proposer take";

function renderLive() {
  // metric toggle active state (present even before data)
  document.querySelectorAll("#metric-toggle button").forEach(b => b.classList.toggle("active", b.dataset.metric === STATE.metric));
  if (!STATE.live.length) return;
  const recent = STATE.live.slice(0, LIVE_ROLL);
  const vals = recent.map(liveValue);
  const latest = STATE.live[0];
  const takeAcc = STATE.metric === "take", feesAcc = STATE.metric === "fees";

  const kpis = [
    { label: `Latest block · ETH`, value: ethF(liveValue(latest)), foot: `#${latest.block_number.toLocaleString()} · ${METRIC_LABEL()}` },
    { label: "Rolling p50 · ETH", value: ethF(percentile(vals, 0.5)), foot: `last ${recent.length} blocks` },
    { label: "Rolling p90 · ETH", value: ethF(percentile(vals, 0.9)), foot: `last ${recent.length} blocks` },
    { label: "Blocks held", value: numF(STATE.live.length), foot: STATE.liveHead ? `head #${STATE.liveHead.toLocaleString()}` : "—" },
  ];
  document.getElementById("live-kpis").innerHTML = kpis.map(k => `
    <div class="dash-kpi"><div class="label">${k.label}</div><div class="value">${k.value}</div><div class="foot neutral">${k.foot}</div></div>`).join("");

  buildLiveStrip("live-strip");

  // builder share of recent blocks
  const counts = {};
  for (const b of recent) counts[b.builder || "—"] = (counts[b.builder || "—"] || 0) + 1;
  const ranked = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const top = ranked.slice(0, 6);
  const otherN = ranked.slice(6).reduce((s, [, c]) => s + c, 0);
  if (otherN) top.push(["other", otherN]);
  buildBarChart("live-builders", top.map(([name, c]) => ({
    label: name.length > 18 ? name.slice(0, 17) + "…" : name,
    frac: c / recent.length, valText: `${c} · ${pctF(c / recent.length)}`, cls: "bar-mid",
  })));

  // recent reward distribution
  buildHistogram("live-hist", recent.map(liveValue).filter(v => v > 0));

  const now = Date.now();
  const rows = STATE.live.slice(0, 14).map((b, i) => {
    const age = Math.max(0, Math.round((now - new Date(b.time).getTime()) / 1000));
    return `<tr class="${i === 0 ? "flash-new" : ""}">
      <td class="text">${b.block_number.toLocaleString()}</td>
      <td>${age}s ago</td>
      <td class="${takeAcc ? "accent" : ""}">${ethF(b.reward_take)}</td>
      <td class="${feesAcc ? "accent" : ""}">${ethF(b.reward_fees)}</td>
      <td class="text" style="font-family:var(--mono);font-weight:400;color:var(--text)">${escapeHtml(b.builder)} <span class="mono" style="color:var(--text-faint)">${b.branch === "mev" ? "·mev" : "·fees"}</span></td>
      <td>${b.num_tx}</td>
      <td>${b.base_fee_gwei.toFixed(1)}</td>
    </tr>`;
  }).join("");
  document.getElementById("live-table").innerHTML =
    `<thead><tr><th class="text">Block</th><th>Age</th><th class="${takeAcc ? "" : ""}">Take · ETH</th><th>Fees · ETH</th><th class="text">Builder</th><th>Txs</th><th>Base fee · gwei</th></tr></thead><tbody>${rows}</tbody>`;
}

// ----- Block lookup -----------------------------------------
// Shared card markup: a kicker + identifier head, then a list of metric rows.
function lookupCardHTML(kicker, ident, rows) {
  return `
      <div class="lookup-card">
        <div class="lookup-head">
          <span class="kicker">${kicker}</span>
          <span class="lookup-num mono">${ident}</span>
        </div>
        <div class="lookup-metrics">
          ${rows.map(([k, v, acc]) => `<div class="tier-metric"><span class="k">${k}</span><span class="v ${acc ? "accent" : ""}">${v}</span></div>`).join("")}
        </div>
      </div>`;
}
function blockCardHTML(b) {
  const rows = [
    ["Proposer take", ethF(b.reward_take) + " ETH", b.branch === "mev"],
    ["Priority fees", ethF(b.reward_fees) + " ETH", b.branch === "fees"],
    ["Builder", escapeHtml(b.builder), false],
    ["Branch", b.branch === "mev" ? "MEV-Boost payment" : "priority-fee sum", false],
    ["Transactions", numF(b.num_tx), false],
    ["Base fee", b.base_fee_gwei.toFixed(3) + " gwei", false],
    ["Time", new Date(b.time).toLocaleString(), false],
  ];
  return lookupCardHTML("Block", `#${b.block_number.toLocaleString()}`, rows);
}
async function doLookup(idRaw) {
  const id = idRaw.trim();
  const host = document.getElementById("lookup-result");
  if (!id) { host.innerHTML = ""; return; }
  host.innerHTML = `<p class="dash-card-foot">Looking up <span class="mono">${escapeHtml(id)}</span>…</p>`;
  try {
    const res = await fetch(`/api/block/${encodeURIComponent(id)}`);
    const b = await res.json();
    if (!res.ok || b.error) { host.innerHTML = `<p class="dash-card-foot">Not found: <span class="mono">${escapeHtml(id)}</span></p>`; return; }
    host.innerHTML = blockCardHTML(b);
  } catch (e) {
    host.innerHTML = `<p class="dash-card-foot">Lookup failed: ${escapeHtml(String(e.message || e))}</p>`;
  }
}

// ----- Search tab: smart lookup + gas oracle + mempool ------
const gweiF = v => v == null ? "—" : (v >= 100 ? v.toFixed(1) : v >= 1 ? v.toFixed(2) : v.toFixed(3));
const hexV = s => `<span class="mono hex">${escapeHtml(s)}</span>`;   // full, never shortened

function txCardHTML(t) {
  const status = t.pending ? "pending" : t.status === 1 ? "success" : t.status === 0 ? "failed" : "—";
  const rows = [
    ["Status", status, t.status === 0],
    ["From", hexV(t.from), false],
    ["To", t.to ? hexV(t.to) : "contract creation", false],
    ["Value", ethF(t.value_eth) + " ETH", false],
    ["Priority fee paid", t.priority_fee_eth == null ? "—" : ethF(t.priority_fee_eth) + " ETH", true],
    ["Gas used", numF(t.gas_used), false],
    ["Effective gas price", gweiF(t.effective_gas_price_gwei) + " gwei", false],
    ["Base fee", gweiF(t.base_fee_gwei) + " gwei", false],
    ["Nonce", numF(t.nonce), false],
    ["Block", t.block_number == null ? "pending" : `#${t.block_number.toLocaleString()} · idx ${t.tx_index}`, false],
  ];
  return lookupCardHTML("Transaction", `<span class="hex">${escapeHtml(t.hash)}</span>`, rows);
}
function addrCardHTML(a) {
  const rows = [
    ["Type", a.is_contract ? "Contract" : "EOA · wallet", a.is_contract],
    ["Balance", ethF(a.balance_eth) + " ETH", false],
    ["Nonce · tx sent", numF(a.nonce), false],
  ];
  return lookupCardHTML("Address", `<span class="hex">${escapeHtml(a.address)}</span>`, rows);
}
async function doSearch(raw) {
  const q = raw.trim();
  const host = document.getElementById("search-result");
  if (!host) return;
  if (!q) { host.innerHTML = ""; return; }
  host.innerHTML = `<p class="dash-card-foot">Searching <span class="mono">${escapeHtml(q)}</span>…</p>`;
  const fail = msg => { host.innerHTML = `<p class="dash-card-foot">${msg}</p>`; };
  try {
    if (/^0x[0-9a-fA-F]{40}$/.test(q)) {                 // address
      const res = await fetch(`/api/address/${q}`);
      const a = await res.json();
      if (!res.ok || a.error) return fail(`Address not found: <span class="mono">${escapeHtml(q)}</span>`);
      host.innerHTML = addrCardHTML(a);
    } else if (/^0x[0-9a-fA-F]{64}$/.test(q)) {          // tx hash → fall back to block-by-hash
      let res = await fetch(`/api/tx/${q}`);
      let d = await res.json();
      if (res.ok && !d.error) { host.innerHTML = txCardHTML(d); return; }
      res = await fetch(`/api/block/${q}`);
      d = await res.json();
      if (res.ok && !d.error) { host.innerHTML = blockCardHTML(d); return; }
      return fail(`No tx or block for <span class="mono">${escapeHtml(q)}</span>`);
    } else if (/^\d+$/.test(q) || q.toLowerCase() === "latest") {   // block number / latest
      const res = await fetch(`/api/block/${encodeURIComponent(q)}`);
      const b = await res.json();
      if (!res.ok || b.error) return fail(`Block not found: <span class="mono">${escapeHtml(q)}</span>`);
      host.innerHTML = blockCardHTML(b);
    } else {
      return fail(`Unrecognized — enter a block number, a 0x tx hash (66), a 0x address (42), or "latest".`);
    }
  } catch (e) {
    fail(`Search failed: ${escapeHtml(String(e.message || e))}`);
  }
}
function renderGasOracle() {
  const g = STATE.gas, khost = document.getElementById("search-gas-kpis");
  if (!g || !khost) return;
  const tiles = [
    { label: "Next base fee", value: gweiF(g.next_base_fee_gwei), foot: "gwei · next block", cls: "neutral" },
    { label: "Tip · standard", value: gweiF(g.tip_standard_gwei), foot: "gwei · ~p50 recent", cls: "neutral" },
    { label: "Tip · fast", value: gweiF(g.tip_fast_gwei), foot: "gwei · ~p90 recent", cls: "neg" },
    { label: "Gas price · spot", value: gweiF(g.gas_price_gwei), foot: `tip ${gweiF(g.max_priority_gwei)} gwei`, cls: "neutral" },
  ];
  khost.innerHTML = tiles.map(t => `
    <div class="dash-kpi" data-pulse><div class="label">${t.label}</div><div class="value">${t.value}</div><div class="foot ${t.cls}">${t.foot}</div></div>`).join("");
  const base = g.base_fee_series || [];
  if (!base.length) return;
  const oldest = g.oldest_block || 0;
  const blocks = base.map((_, i) => String(oldest + i));
  buildLineChart("search-gas-chart", {
    dates: blocks,
    series: [
      { key: "base", cls: "p99", label: "base fee", data: base },
      { key: "p50", cls: "p50", label: "priority p50", data: g.prio_p50 || [] },
      { key: "p90", cls: "p90", label: "priority p90", data: g.prio_p90 || [] },
    ],
    yFmt: gweiF, valFmt: v => gweiF(v) + " gwei", tooltipId: "search-gas-tt",
    xFmt: b => "#" + b, xLabel: b => "Block #" + Number(b).toLocaleString(),
  });
}
function renderMempool() {
  const m = STATE.mempool, host = document.getElementById("search-mempool-kpis");
  if (!m || !host) return;
  const pb = m.pending_block || {};
  const pct = pb.gas_limit ? (pb.gas_used / pb.gas_limit * 100) : null;
  const tiles = [
    { label: "Mempool · pending", value: m.pending_count == null ? "—" : numF(m.pending_count), foot: "txs awaiting inclusion", cls: "neutral" },
    { label: "Mempool · queued", value: m.queued_count == null ? "—" : numF(m.queued_count), foot: "not yet executable", cls: "neutral" },
    { label: "Pending block · txs", value: pb.num_tx == null ? "—" : numF(pb.num_tx), foot: pct == null ? "next block preview" : `${pct.toFixed(0)}% gas used`, cls: "neutral" },
    { label: "Pending base fee", value: gweiF(pb.base_fee_gwei), foot: "gwei · next block", cls: "neutral" },
  ];
  host.innerHTML = tiles.map(t => `
    <div class="dash-kpi" data-pulse><div class="label">${t.label}</div><div class="value">${t.value}</div><div class="foot ${t.cls}">${t.foot}</div></div>`).join("");
}
function renderSearch() { renderGasOracle(); renderMempool(); }

// Polls the gas oracle + mempool while the Search tab is active (no RPC load off-tab).
class SearchFeed {
  constructor(onUpdate) { this.onUpdate = onUpdate; this.timer = null; }
  async poll() {
    if (STATE.tab !== "search") return;
    try {
      const [gr, mr] = await Promise.all([fetch("/api/gas"), fetch("/api/mempool")]);
      if (gr.ok) { const g = await gr.json(); if (!g.error) STATE.gas = g; }
      if (mr.ok) { const m = await mr.json(); if (!m.error) STATE.mempool = m; }
      this.onUpdate();
    } catch { /* transient — keep last */ }
  }
  start() { this.timer = setInterval(() => this.poll(), 5000); }
}

// ============================================================
//  SVG charts
// ============================================================
// Responsive viewBox width. Desktop keeps the fixed 1100 (unchanged look).
// On mobile we render the viewBox at the card's real pixel width so SVG text
// is drawn 1:1 (crisp) instead of being scaled down to ~3px.
const isNarrow = () => typeof window !== "undefined" && window.innerWidth <= 820;
function svgW(svg) {
  if (!isNarrow()) return 1100;
  const w = Math.round(svg.getBoundingClientRect().width);
  return w > 60 ? w : 1100;   // fall back if the tab isn't laid out yet
}

function buildLineChart(svgId, opts) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const { series, dates, log = false, yFmt = String, valFmt = String, areaKey = null, tooltipId = null, xFmt = monthLabel, xLabel = dateShort } = opts;
  const W = svgW(svg), narrow = W < 560, H = narrow ? 300 : 360, m = { l: narrow ? 46 : 64, r: narrow ? 12 : 20, t: 18, b: 34 };
  const plotW = W - m.l - m.r, plotH = H - m.t - m.b, n = dates.length;
  const allVals = []; series.forEach(s => s.data.forEach(v => { if (v != null && isFinite(v)) allVals.push(v); }));
  let yMin = Math.min(...allVals), yMax = Math.max(...allVals), yOf, ticks;
  if (log) {
    const lmin = Math.log10(yMin), lmax = Math.log10(yMax), pad = (lmax - lmin) * 0.06;
    const Lmin = lmin - pad, Lmax = lmax + pad;
    yOf = v => m.t + plotH - ((Math.log10(v) - Lmin) / (Lmax - Lmin)) * plotH;
    ticks = [];
    for (let e = Math.floor(Lmin); e <= Math.ceil(Lmax); e++) [1, 2, 5].forEach(mul => { const tv = mul * Math.pow(10, e); if (tv >= Math.pow(10, Lmin) && tv <= Math.pow(10, Lmax)) ticks.push(tv); });
  } else {
    const pad = yMax * 0.08, Ymax = yMax + pad;
    yOf = v => m.t + plotH - ((v - 0) / (Ymax - 0)) * plotH;
    ticks = []; const step = niceStep(Ymax / 5); for (let tv = 0; tv <= Ymax + 1e-9; tv += step) ticks.push(tv);
  }
  const xOf = i => m.l + (n <= 1 ? 0 : (i / (n - 1)) * plotW);

  const grid = ticks.map(tv => { const y = yOf(tv); return `<line class="grid-line" x1="${m.l}" y1="${y.toFixed(1)}" x2="${m.l + plotW}" y2="${y.toFixed(1)}"/><text class="axis-label" x="${m.l - 8}" y="${(y + 3).toFixed(1)}" text-anchor="end">${yFmt(tv)}</text>`; }).join("");
  const NT = narrow ? 4 : 6; const xticks = []; for (let k = 0; k < NT; k++) xticks.push(Math.round(k * (n - 1) / (NT - 1)));
  const xlab = xticks.map(i => `<text class="axis-label" x="${xOf(i).toFixed(1)}" y="${m.t + plotH + 22}" text-anchor="middle">${xFmt(dates[i])}</text>`).join("");

  let area = "";
  if (areaKey) {
    const s = series.find(x => x.key === areaKey);
    if (s) { let d = `M ${xOf(0).toFixed(1)} ${yOf(s.data[0]).toFixed(1)}`; for (let i = 1; i < n; i++) d += ` L ${xOf(i).toFixed(1)} ${yOf(s.data[i]).toFixed(1)}`; d += ` L ${xOf(n - 1).toFixed(1)} ${(m.t + plotH).toFixed(1)} L ${xOf(0).toFixed(1)} ${(m.t + plotH).toFixed(1)} Z`; area = `<path class="area-fill" d="${d}"/>`; }
  }
  const lines = series.map(s => { let d = ""; for (let i = 0; i < n; i++) { const v = s.data[i]; if (v == null || !isFinite(v)) continue; d += (d === "" ? "M" : "L") + ` ${xOf(i).toFixed(1)} ${yOf(v).toFixed(1)} `; } return `<path class="line ${s.cls}" d="${d}"/>`; }).join("");

  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = `${grid}${xlab}<line class="axis-line" x1="${m.l}" y1="${m.t}" x2="${m.l}" y2="${m.t + plotH}"/><line class="axis-line" x1="${m.l}" y1="${m.t + plotH}" x2="${m.l + plotW}" y2="${m.t + plotH}"/>${area}${lines}<line class="marker-rule" x1="0" y1="${m.t}" x2="0" y2="${m.t + plotH}" style="opacity:0"/><rect class="hit" x="${m.l}" y="${m.t}" width="${plotW}" height="${plotH}" fill="transparent"/>`;

  if (tooltipId) attachLineHover(svg, { series, dates, xOf, n, m, plotW, tooltipId, valFmt, xLabel });
}

function attachLineHover(svg, c) {
  const tt = document.getElementById(c.tooltipId);
  const host = svg.closest(".chart-host") || svg.closest(".dash-card");
  const rule = svg.querySelector(".marker-rule");
  const hit = svg.querySelector(".hit");
  if (!tt || !host || !hit) return;
  hit.addEventListener("mousemove", e => {
    const box = svg.getBoundingClientRect();
    const sx = (e.clientX - box.left) / box.width * 1100;
    let idx = Math.round((sx - c.m.l) / c.plotW * (c.n - 1));
    idx = Math.max(0, Math.min(c.n - 1, idx));
    const x = c.xOf(idx);
    rule.setAttribute("x1", x); rule.setAttribute("x2", x); rule.style.opacity = "1";
    const rows = c.series.map(s => `<span class="tt-row"><span class="k">${s.label}</span> ${s.data[idx] == null ? "—" : c.valFmt(s.data[idx])}</span>`).join("<br/>");
    const title = (c.xLabel || dateShort)(c.dates[idx]) + (c.titleSuffix ? ` · ${c.titleSuffix}` : "");
    tt.innerHTML = `<span class="tt-title">${title}</span>${rows}`;
    const hr = host.getBoundingClientRect();
    tt.style.left = (e.clientX - hr.left) + "px";
    tt.style.top = (e.clientY - hr.top) + "px";
    tt.classList.add("visible");
  });
  hit.addEventListener("mouseleave", () => { tt.classList.remove("visible"); rule.style.opacity = "0"; });
}

function buildBarChart(svgId, rows) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const W = svgW(svg), narrow = W < 560, rowH = 40, top = 8, labelW = narrow ? 92 : 130, valW = narrow ? 78 : 70, left = labelW + 14, right = W - valW - 12, barW = right - left;
  const H = top + rows.length * rowH + 6;
  const maxFrac = Math.max(...rows.map(r => r.frac), 1e-9);
  const body = rows.map((r, i) => {
    const y = top + i * rowH, cy = y + rowH / 2, bw = Math.max(2, (r.frac / maxFrac) * barW);
    return `<text class="row-label" x="${labelW}" y="${(cy + 4).toFixed(1)}" text-anchor="end">${r.label}</text>
      <rect class="bar ${r.cls || "bar-mid"}" x="${left}" y="${(cy - 9).toFixed(1)}" width="${bw.toFixed(1)}" height="18" rx="1"/>
      <text class="row-value ${r.valCls || ""}" x="${(left + bw + 8).toFixed(1)}" y="${(cy + 4).toFixed(1)}" text-anchor="start">${r.valText}</text>`;
  }).join("");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = body;
}

// centered, edge-safe moving average
function rollingMean(arr, span) {
  if (!span || span <= 1) return arr.slice();
  const half = Math.floor(span / 2), out = new Array(arr.length);
  for (let i = 0; i < arr.length; i++) {
    let s = 0, c = 0;
    for (let j = Math.max(0, i - half); j <= Math.min(arr.length - 1, i + half); j++) {
      const v = arr[j]; if (v != null && isFinite(v)) { s += v; c++; }
    }
    out[i] = c ? s / c : arr[i];
  }
  return out;
}

// ----- percentile fan (filled bands p50–p90, p90–p99) ------
function buildFanChart(svgId, opts) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const { dates, p50, p90, p99, log = true, yFmt = ethAxis, valFmt = v => ethF(v) + " ETH", tooltipId = null, titleSuffix = null } = opts;
  const W = svgW(svg), narrow = W < 560, H = narrow ? 300 : 360, m = { l: narrow ? 46 : 64, r: narrow ? 12 : 20, t: 18, b: 34 };
  const plotW = W - m.l - m.r, plotH = H - m.t - m.b, n = dates.length;
  const all = [...p50, ...p90, ...p99].filter(v => v != null && isFinite(v) && v > 0);
  let yMin = Math.min(...all), yMax = Math.max(...all), yOf, ticks;
  if (log) {
    const lmin = Math.log10(yMin), lmax = Math.log10(yMax), pad = (lmax - lmin) * 0.06, Lmin = lmin - pad, Lmax = lmax + pad;
    yOf = v => m.t + plotH - ((Math.log10(v) - Lmin) / (Lmax - Lmin)) * plotH;
    ticks = []; for (let e = Math.floor(Lmin); e <= Math.ceil(Lmax); e++) [1, 5].forEach(mul => { const tv = mul * 10 ** e; if (tv >= 10 ** Lmin && tv <= 10 ** Lmax) ticks.push(tv); });
  } else {
    const Ymax = yMax * 1.08; yOf = v => m.t + plotH - (v / Ymax) * plotH; ticks = []; const st = niceStep(Ymax / 5); for (let t = 0; t <= Ymax; t += st) ticks.push(t);
  }
  const xOf = i => m.l + (n <= 1 ? 0 : i / (n - 1) * plotW);
  const grid = ticks.map(tv => { const y = yOf(tv); return `<line class="grid-line" x1="${m.l}" y1="${y.toFixed(1)}" x2="${m.l + plotW}" y2="${y.toFixed(1)}"/><text class="axis-label" x="${m.l - 8}" y="${(y + 3).toFixed(1)}" text-anchor="end">${yFmt(tv)}</text>`; }).join("");
  const NT = narrow ? 4 : 6; const xt = []; for (let k = 0; k < NT; k++) xt.push(Math.round(k * (n - 1) / (NT - 1)));
  const xlab = xt.map(i => `<text class="axis-label" x="${xOf(i).toFixed(1)}" y="${m.t + plotH + 22}" text-anchor="middle">${monthLabel(dates[i])}</text>`).join("");
  const band = (top, bot) => { let d = `M ${xOf(0).toFixed(1)} ${yOf(top[0]).toFixed(1)}`; for (let i = 1; i < n; i++) d += ` L ${xOf(i).toFixed(1)} ${yOf(top[i]).toFixed(1)}`; for (let i = n - 1; i >= 0; i--) d += ` L ${xOf(i).toFixed(1)} ${yOf(bot[i]).toFixed(1)}`; return d + " Z"; };
  const line = arr => { let d = ""; for (let i = 0; i < n; i++) { const v = arr[i]; if (v == null || !isFinite(v)) continue; d += (d === "" ? "M" : "L") + ` ${xOf(i).toFixed(1)} ${yOf(v).toFixed(1)} `; } return d; };
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = `${grid}${xlab}
    <path class="band-outer" d="${band(p99, p90)}"/>
    <path class="band-inner" d="${band(p90, p50)}"/>
    <line class="axis-line" x1="${m.l}" y1="${m.t}" x2="${m.l}" y2="${m.t + plotH}"/>
    <line class="axis-line" x1="${m.l}" y1="${m.t + plotH}" x2="${m.l + plotW}" y2="${m.t + plotH}"/>
    <path class="line p99" d="${line(p99)}" style="opacity:.55"/>
    <path class="line p90" d="${line(p90)}"/>
    <path class="line p50" d="${line(p50)}"/>
    <line class="marker-rule" x1="0" y1="${m.t}" x2="0" y2="${m.t + plotH}" style="opacity:0"/>
    <rect class="hit" x="${m.l}" y="${m.t}" width="${plotW}" height="${plotH}" fill="transparent"/>`;
  if (tooltipId) attachLineHover(svg, { series: [{ label: "p50", data: p50 }, { label: "p90", data: p90 }, { label: "p99", data: p99 }], dates, xOf, n, m, plotW, tooltipId, valFmt, titleSuffix });
}

// ----- heatmap tier (1..5; 0 = empty) -----------------------
const tierLinear = (v, lo, hi) => v == null ? 0 : Math.max(1, Math.min(5, Math.ceil(((v - lo) / ((hi - lo) || 1)) * 5) || 1));
const tierLog = (v, lo, hi) => v == null || v <= 0 ? 0 : tierLinear(Math.log10(v), Math.log10(lo), Math.log10(hi));

// ----- bid × day winnable-share heatmap ---------------------
function buildBidTimeHeatmap(svgId, tooltipId) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const days = windowDays();
  const ncols = days.length, nrows = BID_LADDER.length;
  const colOf = new Map(days.map((d, i) => [d.day, i]));
  const blocksByDay = new Map(days.map(d => [d.day, d.blocks]));
  const share = Array.from({ length: nrows }, () => new Array(ncols).fill(null));
  const wait = Array.from({ length: nrows }, () => new Array(ncols).fill(null));
  for (const r of STATE.bidWinDaily) {
    const ci = colOf.get(r.day); if (ci == null) continue;
    const ri = BID_LADDER.indexOf(r.my_bid); if (ri < 0) continue;
    share[ri][ci] = r.winnable_blocks / (blocksByDay.get(r.day) || 7150);
    wait[ri][ci] = r.max_wait_min;
  }
  const W = svgW(svg), narrow = W < 560, m = { l: narrow ? 54 : 72, r: 12, t: 8, b: 26 }, rowH = 24, plotW = W - m.l - m.r, cw = plotW / ncols;
  const H = m.t + nrows * rowH + m.b;
  const order = BID_LADDER.map((_, i) => i).reverse();   // highest bid on top
  let cells = "";
  order.forEach((ri, rp) => {
    const y = m.t + rp * rowH;
    for (let ci = 0; ci < ncols; ci++) {
      const s = share[ri][ci], t = tierLinear(s, 0, 1);
      cells += `<rect class="hcell hc-${s == null ? 0 : t}" x="${(m.l + ci * cw).toFixed(2)}" y="${y}" width="${(cw + 0.6).toFixed(2)}" height="${rowH - 1}"/>`;
    }
    const sel = BID_LADDER[ri] === STATE.bid;
    cells += `<text class="heat-label ${sel ? "sel" : ""}" x="${m.l - 8}" y="${y + rowH / 2 + 3}" text-anchor="end">${ethF(BID_LADDER[ri])}</text>`;
    if (sel) cells += `<rect class="heat-sel" x="${m.l}" y="${y}" width="${plotW}" height="${rowH - 1}" />`;
  });
  const NT = narrow ? 4 : 6; const xt = []; for (let k = 0; k < NT; k++) xt.push(Math.round(k * (ncols - 1) / (NT - 1)));
  const xlab = xt.map(ci => `<text class="heat-label" x="${(m.l + ci * cw).toFixed(1)}" y="${m.t + nrows * rowH + 18}" text-anchor="middle">${monthLabel(days[ci].day)}</text>`).join("");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = cells + xlab + `<rect class="hit" x="${m.l}" y="${m.t}" width="${plotW}" height="${nrows * rowH}" fill="transparent"/>`;
  if (tooltipId) {
    const tt = document.getElementById(tooltipId), host = svg.closest(".dash-card") || svg.closest(".chart-host"), hit = svg.querySelector(".hit");
    hit.addEventListener("mousemove", e => {
      const box = svg.getBoundingClientRect(), sx = (e.clientX - box.left) / box.width * W, sy = (e.clientY - box.top) / box.height * H;
      let ci = Math.floor((sx - m.l) / cw), rp = Math.floor((sy - m.t) / rowH);
      ci = Math.max(0, Math.min(ncols - 1, ci)); rp = Math.max(0, Math.min(nrows - 1, rp));
      const ri = order[rp], s = share[ri][ci];
      tt.innerHTML = `<span class="tt-title">${dateShort(days[ci].day)}</span><span class="tt-row"><span class="k">bid</span> ${ethF(BID_LADDER[ri])} ETH</span><br/><span class="tt-row"><span class="k">winnable</span> ${pctF(s)}</span><br/><span class="tt-row"><span class="k">worst wait</span> ${waitF(wait[ri][ci])}</span>`;
      const hr = host.getBoundingClientRect(); tt.style.left = (e.clientX - hr.left) + "px"; tt.style.top = (e.clientY - hr.top) + "px"; tt.classList.add("visible");
    });
    hit.addEventListener("mouseleave", () => tt.classList.remove("visible"));
  }
}

// ----- calendar (regime) heatmap ----------------------------
function buildCalendarHeatmap(svgId, days, getVal, tooltipId) {
  const svg = document.getElementById(svgId);
  if (!svg || !days.length) return;
  const parse = d => new Date(d.day + "T00:00:00Z");
  const first = parse(days[0]);
  const firstSun = new Date(first); firstSun.setUTCDate(first.getUTCDate() - first.getUTCDay());
  const cells = days.map(d => { const dt = parse(d); const di = Math.floor((dt - firstSun) / 86400000); return { d, week: Math.floor(di / 7), wd: dt.getUTCDay(), v: getVal(d) }; });
  const nWeeks = Math.max(...cells.map(c => c.week)) + 1;
  const vals = cells.map(c => c.v).filter(v => v != null && isFinite(v) && v > 0);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const narrow = isNarrow();
  const m = { l: narrow ? 26 : 32, r: 10, t: 18, b: 6 }, gap = 2;
  let W, cw, cell;
  if (narrow) { cell = 9; cw = cell + gap; W = m.l + nWeeks * cw + m.r; }   // fixed cells → card scrolls
  else { W = 1100; cw = (W - m.l - m.r) / nWeeks; cell = Math.min(cw - gap, 15); }
  const H = m.t + 7 * (cell + gap) + m.b;
  let rects = ""; const map = new Map();
  for (const c of cells) {
    const t = tierLog(c.v, lo, hi), x = m.l + c.week * cw, y = m.t + c.wd * (cell + gap);
    map.set(c.week + "," + c.wd, c);
    rects += `<rect class="hcell hc-${c.v == null ? 0 : t}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${cell.toFixed(2)}" height="${cell.toFixed(2)}" rx="1.5"/>`;
  }
  let mlab = "", lastMo = -1, lastLabelX = -999;
  cells.forEach(c => {
    const dt = parse(c.d), mo = dt.getUTCMonth();
    if (mo === lastMo) return;
    lastMo = mo;
    const x = m.l + c.week * cw;
    if (x - lastLabelX < 78) return;   // skip labels that would crowd the previous one
    lastLabelX = x;
    mlab += `<text class="heat-label" x="${x.toFixed(1)}" y="${(m.t - 6).toFixed(1)}" text-anchor="start">${dt.toLocaleDateString("en-US", { month: "short", year: "2-digit" })}</text>`;
  });
  const wl = [[1, "Mon"], [3, "Wed"], [5, "Fri"]].map(([wd, t]) => `<text class="heat-label" x="${m.l - 6}" y="${(m.t + wd * (cell + gap) + cell).toFixed(1)}" text-anchor="end">${t}</text>`).join("");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  // on mobile the calendar keeps readable cells and scrolls inside its (scrollable) card
  svg.style.width = narrow ? W + "px" : "";
  svg.style.maxWidth = narrow ? "none" : "";
  svg.innerHTML = rects + mlab + wl;
  if (tooltipId) {
    const tt = document.getElementById(tooltipId), host = svg.closest(".dash-card") || svg.closest(".chart-host");
    svg.addEventListener("mousemove", e => {
      const box = svg.getBoundingClientRect(), sx = (e.clientX - box.left) / box.width * W, sy = (e.clientY - box.top) / box.height * H;
      const wk = Math.floor((sx - m.l) / cw), wd = Math.floor((sy - m.t) / (cell + gap)), c = map.get(wk + "," + wd);
      if (!c || c.v == null) { tt.classList.remove("visible"); return; }
      tt.innerHTML = `<span class="tt-title">${dateShort(c.d.day)}</span><span class="tt-row"><span class="k">p90</span> ${ethF(c.v)} ETH</span>`;
      const hr = host.getBoundingClientRect(); tt.style.left = (e.clientX - hr.left) + "px"; tt.style.top = (e.clientY - hr.top) + "px"; tt.classList.add("visible");
    });
    svg.addEventListener("mouseleave", () => tt.classList.remove("visible"));
  }
}

// ----- recent-reward histogram (log buckets) ----------------
function buildHistogram(svgId, values) {
  const svg = document.getElementById(svgId);
  if (!svg || !values.length) return;
  const W = svgW(svg), H = isNarrow() ? 240 : 300, m = { l: 42, r: 14, t: 14, b: 30 }, plotW = W - m.l - m.r, plotH = H - m.t - m.b;
  const pos = values.filter(v => v > 0);
  const lo = Math.min(...pos), hi = Math.max(...pos), Llo = Math.log10(lo), Lhi = Math.log10(hi) || Llo + 1;
  const NB = 16, bins = new Array(NB).fill(0);
  for (const v of pos) { let b = Math.floor((Math.log10(v) - Llo) / ((Lhi - Llo) || 1) * NB); b = Math.max(0, Math.min(NB - 1, b)); bins[b]++; }
  const maxC = Math.max(...bins, 1), bw = plotW / NB;
  let bars = bins.map((c, i) => {
    const h = (c / maxC) * plotH, x = m.l + i * bw, y = m.t + plotH - h;
    return `<rect class="bar bar-accent" x="${(x + 1).toFixed(1)}" y="${y.toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${h.toFixed(1)}" rx="1"/>`;
  }).join("");
  const axis = [lo, Math.sqrt(lo * hi), hi].map(v => `<text class="axis-label" x="${(m.l + ((Math.log10(v) - Llo) / ((Lhi - Llo) || 1)) * plotW).toFixed(1)}" y="${m.t + plotH + 20}" text-anchor="middle">${ethF(v)}</text>`).join("");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = `<line class="axis-line" x1="${m.l}" y1="${m.t + plotH}" x2="${m.l + plotW}" y2="${m.t + plotH}"/>${bars}${axis}`;
}

// ============================================================
//  Live-motion graphics (Live page)
// ============================================================
// log y-scale over a value array (returns yOf + lo/hi)
function logScaleY(vals, m, plotH) {
  const pos = vals.filter(v => v != null && isFinite(v) && v > 0);
  let lo = Math.min(...pos), hi = Math.max(...pos);
  if (!isFinite(lo) || lo === hi) { lo = lo || 0.001; hi = lo * 4; }
  const Llo = Math.log10(lo), Lhi = Math.log10(hi), pad = (Lhi - Llo) * 0.1 || 0.3;
  const A = Llo - pad, B = Lhi + pad;
  return { yOf: v => m.t + plotH - ((Math.log10(Math.max(v, 1e-9)) - A) / (B - A)) * plotH, lo, hi };
}
const ethFig = v => v >= 1 ? v.toFixed(2) : v >= 0.1 ? v.toFixed(3) : v.toFixed(4).replace(/^0/, "");

// A · block stream — value bars with figures, newest emphasized on the right
let _lastStripBlock = 0;
function buildLiveStrip(svgId) {
  const svg = document.getElementById(svgId);
  if (!svg || !STATE.live.length) return;
  const W = svgW(svg), narrow = W < 560;
  const N = narrow ? 12 : 24;
  const arr = STATE.live.slice(0, N).slice().reverse();   // oldest → newest (left → right)
  const H = narrow ? 210 : 250, m = { l: 16, r: 16, t: 30, b: 36 }, plotW = W - m.l - m.r, plotH = H - m.t - m.b;
  const { yOf } = logScaleY(arr.map(liveValue), m, plotH);
  const cw = plotW / N, gap = Math.min(7, cw * 0.22), baseY = m.t + plotH;
  const newestNum = STATE.live[0].block_number;
  const arrived = newestNum !== _lastStripBlock;          // only animate on a genuinely new block
  const tiles = arr.map((b, i) => {
    const v = liveValue(b), x = m.l + i * cw + gap / 2, w = cw - gap, y = yOf(v), h = Math.max(3, baseY - y);
    const cx = x + w / 2, isNewest = b.block_number === newestNum;
    const cls = `strip-tile${isNewest ? " newest" : ""}${isNewest && arrived ? " arrived" : ""}`;
    // on narrow screens only label the newest tile (figures would collide otherwise)
    const fig = (!narrow || isNewest)
      ? `<text class="strip-fig${isNewest ? " newest" : ""}" x="${cx.toFixed(1)}" y="${(y - 6).toFixed(1)}" text-anchor="middle">${ethFig(v)}</text>`
      : "";
    return `<rect class="${cls}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}" rx="2"><title>#${b.block_number} · ${ethF(v)} ETH</title></rect>${fig}`;
  }).join("");
  _lastStripBlock = newestNum;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = `<line class="strip-base" x1="${m.l}" y1="${baseY}" x2="${m.l + plotW}" y2="${baseY}"/>${tiles}
    <text class="axis-label" x="${m.l}" y="${H - 12}" text-anchor="start">${N} blocks ago</text>
    <text class="strip-now" x="${m.l + plotW}" y="${H - 12}" text-anchor="end">#${newestNum.toLocaleString()} · live</text>`;
}


// ============================================================
//  Theme / tabs / window wiring
// ============================================================
function applyTheme(t) {
  STATE.theme = t;
  document.body.classList.toggle("theme-dark", t === "dark");
  document.body.classList.toggle("theme-light", t === "light");
  try { localStorage.setItem("bw_blockspace_theme", t); } catch {}
}
function initTheme() {
  let t = "light";
  try { t = localStorage.getItem("bw_blockspace_theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); } catch {}
  applyTheme(t);
}
function wireTheme() { document.querySelector(".theme-toggle").addEventListener("click", () => applyTheme(STATE.theme === "dark" ? "light" : "dark")); }
function wireWindow() {
  document.querySelectorAll(".window-switch button").forEach(b => b.addEventListener("click", () => {
    STATE.window = b.dataset.window;
    document.querySelectorAll(".window-switch button").forEach(x => x.classList.toggle("active", x.dataset.window === STATE.window));
    renderAll();
  }));
}
function wireMetric() {
  document.querySelectorAll("#metric-toggle button").forEach(b => b.addEventListener("click", () => {
    STATE.metric = b.dataset.metric;
    document.querySelectorAll("#metric-toggle button").forEach(x => x.classList.toggle("active", x.dataset.metric === STATE.metric));
    if (STATE.tab === "live") renderLive();
  }));
}
// History value basis (fees | proposer take) — drives Overview + Block value.
// Both tabs share one #STATE.hmetric, so the two toggles stay in sync.
function wireHistMetric() {
  const sync = () => document.querySelectorAll(".hist-metric button")
    .forEach(b => b.classList.toggle("active", b.dataset.hmetric === STATE.hmetric));
  document.querySelectorAll(".hist-metric button").forEach(b => b.addEventListener("click", () => {
    STATE.hmetric = b.dataset.hmetric;
    sync();
    renderActiveTab();
  }));
  sync();
}
function wireLookup() {
  const input = document.getElementById("lookup-input");
  if (!input) return;
  const go = () => doLookup(input.value);
  document.getElementById("lookup-btn")?.addEventListener("click", go);
  input.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
}
function wireSearch() {
  const input = document.getElementById("search-input");
  if (!input) return;
  const go = () => doSearch(input.value);
  document.getElementById("search-btn")?.addEventListener("click", go);
  input.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
}
let searchFeed = null;   // assigned in boot; polled immediately on entering the Search tab
const TABS = ["overview", "hot-days", "block-value", "bid-win", "live", "search"];
// re-render the visible tab's charts so they measure the now-laid-out width (mobile sizing)
function renderActiveTab() {
  if (!STATE.blockValueDaily.length) return;
  const t = STATE.tab;
  if (t === "overview") renderOverview();
  else if (t === "hot-days") renderHotDays();
  else if (t === "block-value") renderBlockValue();
  else if (t === "bid-win") renderBidWin();
  else if (t === "live") renderLive();
  else if (t === "search") renderSearch();
}
function wireTabs() {
  const fromHash = () => { const h = (location.hash || "").replace(/^#/, ""); return TABS.includes(h) ? h : "overview"; };
  const apply = () => {
    const t = fromHash(); STATE.tab = t;
    document.querySelectorAll("[data-tab-content]").forEach(el => { el.hidden = el.dataset.tabContent !== t; });
    document.querySelectorAll("[data-tab-link]").forEach(a => a.classList.toggle("active", a.dataset.tabLink === t));
    renderActiveTab();   // recompute charts at the now-visible width
    if (t === "search" && searchFeed) searchFeed.poll();   // snappy first load, then 5s polling
    window.scrollTo(0, 0);
  };
  window.addEventListener("hashchange", apply);
  apply();
}

// /api/health → STATE.health (version, data_through, refreshed_at). The refresh
// time is shown in the Overview header (renderOverview); the footer keeps the
// running code version + data-through.
async function loadHealth() {
  try { STATE.health = await fetch("/api/health").then(r => r.json()); }
  catch (e) { STATE.health = null; }
}

// "refreshed <ts>" suffix for the Overview header, from the last build_history run.
function refreshStamp() {
  const h = STATE.health;
  if (!h || !h.refreshed_at) return "";
  const ts = new Date(h.refreshed_at * 1000).toISOString().slice(0, 16).replace("T", " ");
  return ` · refreshed <span class="mono">${ts}Z</span>`;
}

// True when the newest data day is more than 2 days behind today (drives the amber stamp).
function dataIsStale() {
  const h = STATE.health;
  if (!h || !h.data_through) return false;
  return (Date.now() / 1000 - Date.parse(h.data_through + "T00:00:00Z") / 1000) / 86400 > 2;
}

// Footer build stamp: running code version + data-through (refresh time now lives
// in the Overview header). Turns amber if the data is more than 2 days behind.
function renderBuildStamp() {
  const el = document.getElementById("build-stamp");
  const h = STATE.health;
  if (!el || !h) return;
  let txt = `v ${h.version || "?"}`;
  if (h.commit_date) txt += ` · ${h.commit_date}`;
  if (h.data_through) txt += ` · data ${h.data_through}`;
  el.textContent = txt;
  el.classList.toggle("stale", dataIsStale());
}

// ============================================================
//  Boot
// ============================================================
async function boot() {
  initTheme();
  wireTheme();
  wireWindow();
  wireTabs();
  wireMetric();
  wireHistMetric();
  wireLookup();
  wireSearch();
  let _rz; window.addEventListener("resize", () => { clearTimeout(_rz); _rz = setTimeout(renderActiveTab, 200); });
  try {
    await Promise.all([loadAll(), loadHealth()]);
    const last = STATE.blockValueDaily[STATE.blockValueDaily.length - 1];
    document.querySelectorAll(".pulled-stamp").forEach(el => el.textContent = last ? dateShort(last.day) : "—");
    renderBidLadder();
    renderAll();
    renderBuildStamp();

    // Real live feed — polls the server's in-memory window (server.py).
    const feed = new RealLiveFeed(() => { if (STATE.tab === "live") renderLive(); });
    feed.start();

    // Gas oracle + mempool — polls only while the Search tab is active (server.py).
    searchFeed = new SearchFeed(() => { if (STATE.tab === "search") renderSearch(); });
    searchFeed.start();
    if (STATE.tab === "search") searchFeed.poll();   // direct-load on #search

    const overlay = document.getElementById("boot-overlay");
    if (overlay) { overlay.classList.add("gone"); setTimeout(() => overlay.remove(), 500); }
  } catch (err) {
    const o = document.getElementById("boot-overlay");
    if (o) o.innerHTML = `<div style="text-align:center;max-width:480px;line-height:1.5">load failed: ${err.message}<br/><br/>run <code>python3 server.py</code> and open <code>http://localhost:8137</code></div>`;
    console.error(err);
  }
}
document.addEventListener("DOMContentLoaded", boot);
