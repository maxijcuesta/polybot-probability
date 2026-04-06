from __future__ import annotations

import asyncio
import json
import time
import webbrowser
import structlog
from pathlib import Path
from aiohttp import web

from ..config import BotConfig
from .. import db as storage

logger = structlog.get_logger(__name__)

# ─── HTML / CSS / JS ──────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>probabilisticobot · Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  /* ── RESET & BASE ── */
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #0d0f12;
    --surface:   #161a20;
    --surface2:  #1c2128;
    --border:    #252d38;
    --border2:   #2d3748;
    --text:      #e8edf2;
    --text2:     #8b95a5;
    --text3:     #5a6578;
    --green:     #00d27a;
    --green-dim: #00d27a22;
    --red:       #ff5a5f;
    --red-dim:   #ff5a5f22;
    --blue:      #4f67ff;
    --blue-dim:  #4f67ff22;
    --yellow:    #f59e0b;
    --yellow-dim:#f59e0b22;
    --purple:    #8b5cf6;
    --radius:    10px;
    --radius-sm: 6px;
  }

  body {
    font-family: 'Inter', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    font-size: 14px;
    line-height: 1.5;
  }

  .container { max-width: 1280px; margin: 0 auto; padding: 0 20px; }

  /* ── NAVBAR ── */
  .navbar {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 14px 0;
    position: sticky;
    top: 0;
    z-index: 100;
    backdrop-filter: blur(8px);
  }
  .navbar .container {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 700;
    font-size: 16px;
    color: var(--text);
    text-decoration: none;
  }
  .brand-icon {
    width: 32px; height: 32px;
    background: linear-gradient(135deg, var(--blue), var(--purple));
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-size: 16px;
  }
  .nav-right {
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.05em;
  }
  .badge-paper {
    background: var(--blue-dim);
    color: var(--blue);
    border: 1px solid var(--blue)44;
  }
  .badge-live {
    background: var(--green-dim);
    color: var(--green);
    border: 1px solid var(--green)44;
  }
  .status-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--green);
    animation: pulse 2s infinite;
  }
  .status-dot.offline { background: var(--red); animation: none; }
  @keyframes pulse {
    0%,100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  .last-update { font-size: 11px; color: var(--text3); }
  .pnl-nav { font-size: 15px; font-weight: 700; }

  /* ── PAGE BODY ── */
  .page { padding: 28px 0 60px; }

  /* ── SECTION TITLE ── */
  .section-title {
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--text2);
    margin-bottom: 14px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .section-title::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border);
  }

  /* ── METRICS ROW ── */
  .metrics-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-bottom: 28px;
  }
  @media (max-width: 768px) { .metrics-grid { grid-template-columns: repeat(2, 1fr); } }

  .metric-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px 20px;
    transition: border-color .2s;
  }
  .metric-card:hover { border-color: var(--border2); }
  .metric-label {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text2);
    margin-bottom: 8px;
  }
  .metric-value { font-size: 26px; font-weight: 700; line-height: 1.1; }
  .metric-sub { font-size: 12px; color: var(--text2); margin-top: 4px; }
  .positive { color: var(--green); }
  .negative { color: var(--red); }
  .neutral  { color: var(--text); }

  /* ── CARDS ── */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    margin-bottom: 16px;
  }
  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 20px;
    border-bottom: 1px solid var(--border);
  }
  .card-title {
    font-weight: 600;
    font-size: 14px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .card-count {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 2px 8px;
    font-size: 11px;
    color: var(--text2);
    font-weight: 500;
  }

  /* ── TABLES ── */
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; }
  th {
    text-align: left;
    padding: 10px 16px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text2);
    background: var(--surface2);
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  th.right, td.right { text-align: right; }
  td {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border)88;
    font-size: 13px;
    vertical-align: middle;
    white-space: nowrap;
  }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: var(--surface2); }
  .market-name {
    font-weight: 500;
    color: var(--text);
    max-width: 260px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    display: block;
  }
  .market-sub { font-size: 11px; color: var(--text3); margin-top: 2px; }

  /* ── BADGES ── */
  .side-badge {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.05em;
  }
  .side-YES { background: var(--green-dim); color: var(--green); }
  .side-NO  { background: var(--red-dim);   color: var(--red); }
  .side-buy  { background: var(--green-dim); color: var(--green); }
  .side-sell { background: var(--red-dim);   color: var(--red); }

  .reason-badge { font-size: 11px; color: var(--text2); }

  /* ── CALIBRATION BAR ── */
  .cal-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 20px;
    border-bottom: 1px solid var(--border)55;
    font-size: 12px;
  }
  .cal-row:last-child { border-bottom: none; }
  .cal-label { width: 80px; color: var(--text2); font-family: monospace; font-size: 11px; }
  .cal-bar-wrap { flex: 1; height: 6px; background: var(--surface2); border-radius: 3px; overflow: hidden; position: relative; }
  .cal-bar-model  { position: absolute; top: 0; height: 100%; background: var(--blue); opacity: 0.7; border-radius: 3px; }
  .cal-bar-actual { position: absolute; top: 0; height: 100%; background: var(--green); opacity: 0.7; border-radius: 3px; }
  .cal-stats { text-align: right; min-width: 140px; color: var(--text3); font-size: 11px; }

  /* ── EDGE BREAKDOWN BARS ── */
  .signal-row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 20px;
    border-bottom: 1px solid var(--border)66;
  }
  .signal-row:last-child { border-bottom: none; }
  .signal-row-name { width: 100px; font-size: 12px; font-weight: 500; color: var(--text2); }
  .bar-wrap { flex: 1; height: 6px; background: var(--surface2); border-radius: 3px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 3px; transition: width .6s ease; }
  .bar-positive { background: var(--green); }
  .bar-negative { background: var(--red); }
  .signal-row-stats { text-align: right; min-width: 130px; font-size: 12px; }

  /* ── DAILY CHART ── */
  .daily-chart {
    display: flex;
    align-items: flex-end;
    gap: 6px;
    padding: 20px 20px 0;
    height: 100px;
    overflow-x: auto;
  }
  .day-bar-wrap {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    min-width: 36px;
    flex: 1;
  }
  .day-bar { width: 100%; border-radius: 3px 3px 0 0; min-height: 3px; }
  .day-label { font-size: 10px; color: var(--text3); white-space: nowrap; padding: 6px 0 14px; text-align: center; }
  .day-bar.pos { background: var(--green); }
  .day-bar.neg { background: var(--red); }

  /* ── FUNNEL ── */
  .funnel-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1px;
    background: var(--border);
  }
  .funnel-cell { background: var(--surface); padding: 16px 20px; text-align: center; }
  .funnel-count { font-size: 24px; font-weight: 700; color: var(--text); }
  .funnel-label { font-size: 11px; color: var(--text2); margin-top: 4px; }
  .funnel-pct   { font-size: 11px; color: var(--text3); margin-top: 2px; }

  /* ── TWO-COLUMN LAYOUT ── */
  .two-col {
    display: grid;
    grid-template-columns: 1fr 380px;
    gap: 16px;
    margin-bottom: 16px;
  }
  @media (max-width: 1024px) { .two-col { grid-template-columns: 1fr; } }

  /* ── EMPTY STATE ── */
  .empty-state { padding: 48px 24px; text-align: center; color: var(--text2); }
  .empty-icon { font-size: 40px; margin-bottom: 12px; }
  .empty-title { font-weight: 600; font-size: 15px; color: var(--text); margin-bottom: 6px; }
  .empty-sub { font-size: 13px; color: var(--text2); }

  /* ── LOADING OVERLAY ── */
  .loading {
    position: fixed; inset: 0;
    background: var(--bg);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 16px;
    z-index: 999;
    transition: opacity .4s;
  }
  .loading.hidden { opacity: 0; pointer-events: none; }
  .spinner {
    width: 36px; height: 36px;
    border: 3px solid var(--border);
    border-top-color: var(--blue);
    border-radius: 50%;
    animation: spin .8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loading-text { font-size: 13px; color: var(--text2); }

  /* ── TOAST ── */
  .toast {
    position: fixed; bottom: 24px; right: 24px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 12px 18px;
    font-size: 13px;
    color: var(--text);
    display: flex; align-items: center; gap: 10px;
    transform: translateY(80px);
    opacity: 0;
    transition: all .3s;
    z-index: 200;
  }
  .toast.show { transform: translateY(0); opacity: 1; }

  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }
</style>
</head>
<body>

<!-- Loading overlay -->
<div class="loading" id="loading">
  <div class="spinner"></div>
  <div class="loading-text">Connecting to DB...</div>
</div>

<!-- Toast notification -->
<div class="toast" id="toast">
  <span id="toast-icon">&#x1f504;</span>
  <span id="toast-text">Data updated</span>
</div>

<!-- NAVBAR -->
<nav class="navbar">
  <div class="container">
    <a class="brand" href="#">
      <div class="brand-icon">&#x1f916;</div>
      probabilisticobot
    </a>
    <div class="nav-right">
      <span class="badge badge-paper" id="mode-badge">
        <span class="status-dot" id="status-dot"></span>
        PAPER
      </span>
      <span class="pnl-nav" id="nav-pnl">&#x2014;</span>
      <span class="last-update" id="last-update">Loading...</span>
    </div>
  </div>
</nav>

<!-- MAIN PAGE -->
<div class="page">
  <div class="container">

    <!-- TOP METRICS -->
    <div class="metrics-grid" id="metrics">
      <div class="metric-card">
        <div class="metric-label">Net PnL</div>
        <div class="metric-value neutral" id="m-pnl">&#x2014;</div>
        <div class="metric-sub" id="m-pnl-sub">&#x2014;</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Hit Rate</div>
        <div class="metric-value neutral" id="m-wr">&#x2014;</div>
        <div class="metric-sub" id="m-wr-sub">&#x2014;</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Exposure</div>
        <div class="metric-value neutral" id="m-exp">&#x2014;</div>
        <div class="metric-sub" id="m-exp-sub">&#x2014;</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Brier Score</div>
        <div class="metric-value neutral" id="m-brier">&#x2014;</div>
        <div class="metric-sub" id="m-brier-sub">&#x2014;</div>
      </div>
    </div>

    <!-- SECOND METRICS ROW -->
    <div class="metrics-grid" style="margin-bottom:28px">
      <div class="metric-card">
        <div class="metric-label">EV Expected</div>
        <div class="metric-value neutral" id="m-ev-exp">&#x2014;</div>
        <div class="metric-sub">vs realized: <span id="m-ev-real">&#x2014;</span></div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Profit Factor</div>
        <div class="metric-value neutral" id="m-pf">&#x2014;</div>
        <div class="metric-sub" id="m-pf-sub">gross/loss ratio</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Max Drawdown</div>
        <div class="metric-value neutral" id="m-dd">&#x2014;</div>
        <div class="metric-sub">from peak</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">Avg Hold</div>
        <div class="metric-value neutral" id="m-hold">&#x2014;</div>
        <div class="metric-sub">per trade</div>
      </div>
    </div>

    <!-- OPEN POSITIONS -->
    <div class="section-title">Open Positions</div>
    <div class="card" id="open-card">
      <div class="card-header">
        <span class="card-title">Active positions
          <span class="card-count" id="open-count">0</span>
        </span>
        <span style="font-size:12px;color:var(--text2)">Unrealized: <b id="open-float">&#x2014;</b></span>
      </div>
      <div class="table-wrap" id="open-body"></div>
    </div>

    <!-- CLOSED POSITIONS + EDGE BREAKDOWN -->
    <div class="section-title">Performance</div>
    <div class="two-col">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Closed positions
            <span class="card-count" id="closed-count">0</span>
          </span>
        </div>
        <div class="table-wrap" id="closed-body"></div>
      </div>

      <div class="card">
        <div class="card-header">
          <span class="card-title">PnL by edge bucket</span>
        </div>
        <div id="edge-body"></div>
      </div>
    </div>

    <!-- RECENT SIGNALS -->
    <div class="section-title">Recent Signals</div>
    <div class="card">
      <div class="card-header">
        <span class="card-title">Latest signals
          <span class="card-count" id="signals-count">0</span>
        </span>
        <span style="font-size:12px;color:var(--text2)">actionable only</span>
      </div>
      <div class="table-wrap" id="signals-body"></div>
    </div>

    <!-- CALIBRATION BUCKETS -->
    <div class="section-title">Calibration</div>
    <div class="card">
      <div class="card-header">
        <span class="card-title">Probability calibration buckets</span>
        <span style="font-size:12px;color:var(--text2)">model vs actual win rate</span>
      </div>
      <div id="cal-body"></div>
    </div>

    <!-- DAILY PnL -->
    <div class="section-title">Daily PnL</div>
    <div class="card">
      <div class="card-header">
        <span class="card-title">Last 14 days</span>
      </div>
      <div class="daily-chart" id="daily-chart"></div>
    </div>

    <!-- FUNNEL -->
    <div class="section-title">Market Funnel</div>
    <div class="card" style="overflow:hidden">
      <div class="funnel-grid" id="funnel-body"></div>
    </div>

  </div>
</div>

<script>
const REFRESH_MS = 30000;

function fmtUsdc(v, sign=false) {
  if (v == null) return '\u2014';
  const s = Math.abs(v).toFixed(2);
  if (sign) return (v >= 0 ? '+' : '-') + '$' + s;
  return '$' + s;
}
function fmtPct(v, dec=1) {
  if (v == null) return '\u2014';
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(dec) + '%';
}
function pnlClass(v) {
  if (v == null) return 'neutral';
  return v >= 0 ? 'positive' : 'negative';
}
function fmtTime(ts) {
  if (!ts) return '\u2014';
  const d = new Date(ts);
  return d.toLocaleDateString('en', {month:'short', day:'numeric'})
       + ' ' + d.toLocaleTimeString('en', {hour:'2-digit', minute:'2-digit'});
}
function showToast(msg, icon='\u1f504') {
  const t = document.getElementById('toast');
  document.getElementById('toast-icon').textContent = icon;
  document.getElementById('toast-text').textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}

function renderMetrics(d) {
  const m = d.metrics || {};
  const open = d.open_positions || [];

  // Net PnL
  const pnl = m.pnl_net_usd ?? null;
  const mPnl = document.getElementById('m-pnl');
  mPnl.textContent = pnl != null ? fmtUsdc(pnl, true) : '\u2014';
  mPnl.className = 'metric-value ' + pnlClass(pnl);
  document.getElementById('m-pnl-sub').textContent =
    m.n_trades ? `from ${m.n_trades} closed trades` : 'no closed trades';

  // Hit Rate
  const wr = m.hit_rate ?? null;
  const mWr = document.getElementById('m-wr');
  mWr.textContent = wr != null ? (wr * 100).toFixed(1) + '%' : '\u2014';
  mWr.className = 'metric-value ' + (wr != null ? (wr >= 0.5 ? 'positive' : 'negative') : 'neutral');
  document.getElementById('m-wr-sub').textContent =
    m.n_trades ? `${m.n_wins || 0}W / ${m.n_losses || 0}L` : 'no data';

  // Exposure
  const exp = open.reduce((s, p) => s + (p.entry_size_usd || 0), 0);
  const mExp = document.getElementById('m-exp');
  mExp.textContent = exp > 0 ? '$' + exp.toFixed(2) : '$0.00';
  mExp.className = 'metric-value neutral';
  document.getElementById('m-exp-sub').textContent = `${open.length} open positions`;

  // Brier score
  const brier = m.brier_score ?? null;
  const mBrier = document.getElementById('m-brier');
  mBrier.textContent = brier != null ? brier.toFixed(4) : '\u2014';
  mBrier.className = 'metric-value ' + (brier != null ? (brier < 0.25 ? 'positive' : 'negative') : 'neutral');
  document.getElementById('m-brier-sub').textContent = brier != null
    ? (brier < 0.25 ? 'good calibration' : 'needs recalibration')
    : 'no resolved trades';

  // EV
  document.getElementById('m-ev-exp').textContent = m.ev_expected_usd != null ? fmtUsdc(m.ev_expected_usd, true) : '\u2014';
  document.getElementById('m-ev-exp').className = 'metric-value ' + pnlClass(m.ev_expected_usd ?? null);
  document.getElementById('m-ev-real').textContent = m.ev_realized_usd != null ? fmtUsdc(m.ev_realized_usd, true) : '\u2014';

  // Profit factor
  const pf = m.profit_factor ?? null;
  document.getElementById('m-pf').textContent = pf != null ? (pf === Infinity ? 'inf' : pf.toFixed(2)) : '\u2014';
  document.getElementById('m-pf').className = 'metric-value ' + (pf != null ? (pf >= 1 ? 'positive' : 'negative') : 'neutral');

  // Max drawdown
  const dd = m.max_drawdown_usd ?? null;
  document.getElementById('m-dd').textContent = dd != null ? '$' + dd.toFixed(2) : '\u2014';
  document.getElementById('m-dd').className = 'metric-value ' + (dd != null && dd > 0 ? 'negative' : 'neutral');

  // Avg hold
  const hold = m.avg_hold_hours ?? null;
  document.getElementById('m-hold').textContent = hold != null ? hold.toFixed(1) + 'h' : '\u2014';
  document.getElementById('m-hold').className = 'metric-value neutral';

  // Navbar
  const navPnl = document.getElementById('nav-pnl');
  if (pnl != null) {
    navPnl.textContent = fmtUsdc(pnl, true);
    navPnl.className = 'pnl-nav ' + pnlClass(pnl);
  } else {
    navPnl.textContent = 'Paper Mode';
    navPnl.className = 'pnl-nav neutral';
  }

  // Unrealized float
  const float = open.reduce((s, p) => s + (p.unrealized_pnl || 0), 0);
  const floatEl = document.getElementById('open-float');
  floatEl.textContent = fmtUsdc(float, true);
  floatEl.className = pnlClass(float);
}

function renderOpenPositions(positions) {
  document.getElementById('open-count').textContent = positions.length;
  const body = document.getElementById('open-body');
  if (!positions.length) {
    body.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">&#x1f4a4;</div>
        <div class="empty-title">No open positions</div>
        <div class="empty-sub">Bot is scanning markets for opportunities</div>
      </div>`;
    return;
  }
  const rows = positions.map(p => {
    const pnlV = p.unrealized_pnl ?? 0;
    const hoursHeld = p.entry_time
      ? ((Date.now() - new Date(p.entry_time).getTime()) / 3600000).toFixed(1)
      : '\u2014';
    return `
      <tr>
        <td>
          <span class="market-name" title="${p.market_id}">${p.market_id.substring(0,36)}...</span>
          <span class="market-sub">edge_net=${p.edge_net?.toFixed(4) ?? '\u2014'} &middot; p_cal=${p.p_calibrated?.toFixed(3) ?? '\u2014'}</span>
        </td>
        <td><span class="side-badge side-${p.side}">${p.side}</span></td>
        <td class="right">$${p.entry_size_usd?.toFixed(2) ?? '\u2014'}</td>
        <td class="right">${p.entry_price?.toFixed(4) ?? '\u2014'}</td>
        <td class="right ${pnlClass(pnlV)}">${fmtUsdc(pnlV, true)}</td>
        <td class="right">${hoursHeld}h</td>
      </tr>`;
  }).join('');
  body.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Market</th>
          <th>Side</th>
          <th class="right">Size</th>
          <th class="right">Entry</th>
          <th class="right">PnL</th>
          <th class="right">Held</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderClosedPositions(positions) {
  document.getElementById('closed-count').textContent = positions.length;
  const body = document.getElementById('closed-body');
  if (!positions.length) {
    body.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">&#x1f3c1;</div>
        <div class="empty-title">No closed trades yet</div>
        <div class="empty-sub">Results will appear here once trades close</div>
      </div>`;
    return;
  }
  const rows = positions.map(p => {
    const pnlV = p.pnl_usd ?? 0;
    return `
      <tr>
        <td>
          <span class="market-name" title="${p.market_id}">${p.market_id.substring(0,28)}...</span>
          <span class="market-sub">${p.side} &middot; edge=${p.edge_net?.toFixed(3) ?? '\u2014'}</span>
        </td>
        <td class="right ${pnlClass(pnlV)}">${fmtUsdc(pnlV, true)}</td>
        <td class="right ${pnlClass(p.pnl_pct)}">${fmtPct(p.pnl_pct)}</td>
        <td><span class="reason-badge">${(p.exit_reason || '\u2014').replace('_',' ')}</span></td>
        <td class="right">${p.hold_hours?.toFixed(1) ?? '\u2014'}h</td>
      </tr>`;
  }).join('');
  body.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Market</th>
          <th class="right">PnL $</th>
          <th class="right">PnL %</th>
          <th>Exit reason</th>
          <th class="right">Hold</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderEdgeBreakdown(edge_by_bucket) {
  const body = document.getElementById('edge-body');
  const entries = Object.entries(edge_by_bucket || {});
  if (!entries.length) {
    body.innerHTML = `
      <div class="empty-state" style="padding:32px">
        <div class="empty-icon">&#x1f3af;</div>
        <div class="empty-title">No edge data yet</div>
        <div class="empty-sub">Will show once trades close</div>
      </div>`;
    return;
  }
  const maxAbs = Math.max(...entries.map(([,s]) => Math.abs(s.pnl || 0)), 0.01);
  const rows = entries.map(([label, s]) => {
    const pct = Math.abs((s.pnl || 0) / maxAbs) * 100;
    const cls = (s.pnl || 0) >= 0 ? 'bar-positive' : 'bar-negative';
    const wr = s.n > 0 ? (s.hit_rate * 100).toFixed(0) + '%' : '\u2014';
    return `
      <div class="signal-row">
        <div class="signal-row-name">${label}</div>
        <div class="bar-wrap">
          <div class="bar-fill ${cls}" style="width:${pct}%"></div>
        </div>
        <div class="signal-row-stats">
          <span class="${pnlClass(s.pnl)}">${fmtUsdc(s.pnl, true)}</span>
          <span style="color:var(--text3)"> &middot; ${s.n}t &middot; ${wr}</span>
        </div>
      </div>`;
  }).join('');
  body.innerHTML = rows;
}

function renderSignals(signals) {
  document.getElementById('signals-count').textContent = signals.length;
  const body = document.getElementById('signals-body');
  if (!signals.length) {
    body.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">&#x1f4e1;</div>
        <div class="empty-title">No signals yet</div>
        <div class="empty-sub">Run the bot to generate signals</div>
      </div>`;
    return;
  }
  const rows = signals.map(s => {
    return `
      <tr>
        <td>
          <span class="market-name" title="${s.market_id}">${s.market_id.substring(0,32)}...</span>
          <span class="market-sub">${fmtTime(s.created_at)}</span>
        </td>
        <td><span class="side-badge side-${s.side}">${s.side}</span></td>
        <td class="right">${s.p_market?.toFixed(4) ?? '\u2014'}</td>
        <td class="right">${s.p_calibrated?.toFixed(4) ?? '\u2014'}</td>
        <td class="right ${pnlClass(s.edge_raw)}">${s.edge_raw?.toFixed(4) ?? '\u2014'}</td>
        <td class="right ${pnlClass(s.edge_net)}">${s.edge_net?.toFixed(4) ?? '\u2014'}</td>
      </tr>`;
  }).join('');
  body.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Market</th>
          <th>Side</th>
          <th class="right">P market</th>
          <th class="right">P model</th>
          <th class="right">Edge raw</th>
          <th class="right">Edge net</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderCalibration(buckets) {
  const body = document.getElementById('cal-body');
  if (!buckets || !buckets.length) {
    body.innerHTML = `
      <div class="empty-state" style="padding:32px">
        <div class="empty-icon">&#x1f4ca;</div>
        <div class="empty-title">No calibration data yet</div>
        <div class="empty-sub">Need resolved trades to compute calibration</div>
      </div>`;
    return;
  }
  const rows = buckets.map(b => {
    const modelPct = (b.avg_p_model * 100).toFixed(0);
    const actualPct = (b.observed_win_rate * 100).toFixed(0);
    const errClass = b.calibration_error > 0.1 ? 'negative' : (b.calibration_error < 0.05 ? 'positive' : 'neutral');
    return `
      <div class="cal-row">
        <div class="cal-label">${b.bucket_label}</div>
        <div class="cal-bar-wrap">
          <div class="cal-bar-model"  style="width:${modelPct}%"></div>
          <div class="cal-bar-actual" style="width:${actualPct}%"></div>
        </div>
        <div class="cal-stats">
          <span class="${errClass}">err=${b.calibration_error.toFixed(3)}</span>
          &nbsp;model=${b.avg_p_model.toFixed(3)} actual=${b.observed_win_rate.toFixed(3)} n=${b.n_trades}
        </div>
      </div>`;
  }).join('');
  body.innerHTML = rows;
}

function renderDailyChart(daily) {
  const chart = document.getElementById('daily-chart');
  if (!daily || !daily.length) {
    chart.innerHTML = `<div style="width:100%;text-align:center;color:var(--text3);padding:24px;font-size:13px">No daily data yet</div>`;
    return;
  }
  const days = [...daily].reverse().slice(-14);
  const maxAbs = Math.max(...days.map(d => Math.abs(d.pnl_usd || 0)), 0.01);
  const maxPx = 72;
  chart.innerHTML = days.map(d => {
    const pnl = d.pnl_usd || 0;
    const px  = Math.max(Math.round(Math.abs(pnl) / maxAbs * maxPx), 3);
    const cls = pnl >= 0 ? 'pos' : 'neg';
    const label = (d.date || '').substring(5);
    return `
      <div class="day-bar-wrap" title="${label}: ${fmtUsdc(pnl, true)} (${d.n_trades || 0} trades)">
        <div class="day-bar ${cls}" style="height:${px}px"></div>
        <div class="day-label">${label}</div>
      </div>`;
  }).join('');
}

function renderFunnel(funnel) {
  const body = document.getElementById('funnel-body');
  const order  = ['scanned', 'passed_guards', 'signals', 'executed'];
  const labels = {scanned:'Scanned', passed_guards:'Passed Guards', signals:'Signals', executed:'Executed'};
  const total  = Math.max(funnel.scanned || 0, 1);
  const cells  = order.map(k => {
    const count = funnel[k] || 0;
    const pct   = total > 0 ? (count / total * 100).toFixed(0) + '%' : '\u2014';
    return `
      <div class="funnel-cell">
        <div class="funnel-count">${count}</div>
        <div class="funnel-label">${labels[k] || k}</div>
        <div class="funnel-pct">${pct} of scanned</div>
      </div>`;
  }).join('');
  body.innerHTML = cells || `<div class="funnel-cell" style="grid-column:1/-1">
    <div class="funnel-count">\u2014</div>
    <div class="funnel-label">No data yet</div>
  </div>`;
}

async function fetchData() {
  try {
    const res = await fetch('/api/data');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  } catch(e) {
    console.error('Error fetching data:', e);
    return null;
  }
}

async function refresh(initial=false) {
  const data = await fetchData();
  if (!data) {
    document.getElementById('status-dot').className = 'status-dot offline';
    if (!initial) showToast('Failed to load data', 'x');
    return;
  }

  document.getElementById('status-dot').className = 'status-dot';

  renderMetrics(data);
  renderOpenPositions(data.open_positions || []);
  renderClosedPositions(data.closed_positions || []);
  renderEdgeBreakdown(data.edge_by_bucket || {});
  renderSignals(data.recent_signals || []);
  renderCalibration(data.calibration_buckets || []);
  renderDailyChart(data.daily_pnl || []);
  renderFunnel(data.funnel || {});

  const now = new Date().toLocaleTimeString('en', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  document.getElementById('last-update').textContent = `Updated: ${now}`;

  const mode = (data.mode || 'paper').toUpperCase();
  const badge = document.getElementById('mode-badge');
  badge.className = 'badge badge-' + (mode === 'LIVE' ? 'live' : 'paper');
  badge.innerHTML = `<span class="status-dot"></span> ${mode}`;

  if (!initial) showToast('Data refreshed');

  const loading = document.getElementById('loading');
  if (loading && !loading.classList.contains('hidden')) {
    loading.classList.add('hidden');
    setTimeout(() => loading.remove(), 500);
  }
}

refresh(true);
setInterval(() => refresh(false), REFRESH_MS);
</script>
</body>
</html>
"""

# ─── DATA FETCHER ─────────────────────────────────────────────────────────────

async def fetch_dashboard_data(config: BotConfig) -> dict:
    """Queries the DB and returns all data for the dashboard."""
    import aiosqlite
    db_path = config.operation.db_path

    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row

            async def q(sql: str, params=()) -> list:
                async with db.execute(sql, params) as cur:
                    rows = await cur.fetchall()
                return [dict(r) for r in rows]

            async def q1(sql: str, params=()) -> dict | None:
                async with db.execute(sql, params) as cur:
                    row = await cur.fetchone()
                return dict(row) if row else None

            # Operation mode
            mode = "paper" if config.operation.paper_trade else "live"

            # Open trades
            open_trades = await q("""
                SELECT trade_id, market_id, side, entry_price, entry_size_usd,
                       entry_shares, entry_time, edge_raw, edge_net,
                       p_model, p_calibrated, p_market_entry
                  FROM trades
                 WHERE status = 'open'
                 ORDER BY entry_time DESC
            """)
            # Add placeholder unrealized_pnl (0 since we don't have live price here)
            for t in open_trades:
                t["unrealized_pnl"] = 0.0

            # Closed trades (last 50)
            closed_trades = await q("""
                SELECT trade_id, market_id, side, entry_price, exit_price,
                       entry_size_usd, pnl_usd, pnl_pct, exit_reason,
                       edge_net, entry_time, exit_time
                  FROM trades
                 WHERE status = 'closed'
                 ORDER BY exit_time DESC
                 LIMIT 50
            """)
            for t in closed_trades:
                if t.get("entry_time") and t.get("exit_time"):
                    try:
                        from datetime import datetime
                        # handle both ISO strings and unix timestamps
                        def parse_ts(v):
                            if isinstance(v, (int, float)):
                                return datetime.fromtimestamp(v)
                            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                        entry = parse_ts(t["entry_time"])
                        exit_ = parse_ts(t["exit_time"])
                        t["hold_hours"] = (exit_ - entry).total_seconds() / 3600
                    except Exception:
                        t["hold_hours"] = None
                else:
                    t["hold_hours"] = None

            # Metrics from trades table
            metrics_row = await q1("""
                SELECT
                    COUNT(*) as n_trades,
                    SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as n_wins,
                    SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) as n_losses,
                    SUM(pnl_usd) as pnl_gross_usd,
                    AVG(pnl_pct) as avg_pnl_pct,
                    SUM(edge_net * entry_size_usd) as ev_expected_usd,
                    SUM(pnl_usd) as ev_realized_usd
                  FROM trades
                 WHERE status = 'closed'
            """)

            metrics: dict = {}
            if metrics_row and (metrics_row.get("n_trades") or 0) > 0:
                n = metrics_row["n_trades"] or 0
                w = metrics_row["n_wins"] or 0
                l = metrics_row["n_losses"] or 0
                gross = metrics_row["pnl_gross_usd"] or 0.0
                ev_exp = metrics_row["ev_expected_usd"] or 0.0
                metrics = {
                    "n_trades": n,
                    "n_wins": w,
                    "n_losses": l,
                    "hit_rate": round(w / n, 4) if n > 0 else 0,
                    "pnl_gross_usd": round(gross, 2),
                    "pnl_net_usd": round(gross, 2),  # approximation
                    "ev_expected_usd": round(ev_exp, 4),
                    "ev_realized_usd": round(gross, 4),
                    "brier_score": None,
                    "profit_factor": None,
                    "max_drawdown_usd": None,
                    "avg_hold_hours": None,
                }
                # Brier score from resolved trades
                resolved = await q("""
                    SELECT p_calibrated, outcome FROM trades
                     WHERE status = 'closed' AND outcome IS NOT NULL
                """)
                if resolved:
                    import math
                    brier = sum((r["p_calibrated"] - r["outcome"])**2 for r in resolved) / len(resolved)
                    metrics["brier_score"] = round(brier, 6)

                # Profit factor
                gp_row = await q1("SELECT SUM(pnl_usd) as gp FROM trades WHERE status='closed' AND pnl_usd > 0")
                gl_row = await q1("SELECT SUM(ABS(pnl_usd)) as gl FROM trades WHERE status='closed' AND pnl_usd <= 0")
                gp = (gp_row or {}).get("gp") or 0
                gl = (gl_row or {}).get("gl") or 0
                metrics["profit_factor"] = round(gp / gl, 4) if gl > 0 else None

                # Max drawdown
                all_pnls = await q("SELECT pnl_usd FROM trades WHERE status='closed' ORDER BY exit_time ASC")
                if all_pnls:
                    cum = 0; peak = 0; max_dd = 0
                    for row in all_pnls:
                        cum += row["pnl_usd"] or 0
                        peak = max(peak, cum)
                        max_dd = max(max_dd, peak - cum)
                    metrics["max_drawdown_usd"] = round(max_dd, 2)

                # Avg hold hours
                hold_row = await q1("""
                    SELECT AVG((julianday(exit_time) - julianday(entry_time)) * 24) as avg_hold
                      FROM trades
                     WHERE status = 'closed' AND exit_time IS NOT NULL AND entry_time IS NOT NULL
                """)
                if hold_row and hold_row.get("avg_hold") is not None:
                    metrics["avg_hold_hours"] = round(hold_row["avg_hold"], 2)

            # Calibration buckets from resolved trades
            cal_buckets = []
            resolved_all = await q("""
                SELECT p_calibrated, outcome FROM trades
                 WHERE status = 'closed' AND outcome IS NOT NULL
            """)
            if resolved_all:
                from collections import defaultdict
                bkt: dict = defaultdict(list)
                for r in resolved_all:
                    idx = min(int((r["p_calibrated"] or 0) * 10), 9)
                    bkt[idx].append(r)
                for i in range(10):
                    items = bkt.get(i, [])
                    if not items:
                        continue
                    avg_p = sum(r["p_calibrated"] for r in items) / len(items)
                    wr_b = sum(1 for r in items if (r["outcome"] or 0) == 1) / len(items)
                    cal_buckets.append({
                        "bucket_label": f"{i/10:.1f}-{(i+1)/10:.1f}",
                        "avg_p_model": round(avg_p, 4),
                        "observed_win_rate": round(wr_b, 4),
                        "calibration_error": round(abs(avg_p - wr_b), 4),
                        "n_trades": len(items),
                    })

            # Edge by bucket
            edge_by_bucket: dict = {}
            for label, lo, hi in [("0.00-0.02", 0, 0.02), ("0.02-0.05", 0.02, 0.05),
                                   ("0.05-0.10", 0.05, 0.10), ("0.10+", 0.10, 1.0)]:
                rows_e = await q("""
                    SELECT pnl_usd FROM trades
                     WHERE status='closed'
                       AND ABS(edge_net) >= ? AND ABS(edge_net) < ?
                """, (lo, hi))
                if rows_e:
                    wins_e = sum(1 for r in rows_e if (r["pnl_usd"] or 0) > 0)
                    edge_by_bucket[label] = {
                        "n": len(rows_e),
                        "pnl": round(sum(r["pnl_usd"] or 0 for r in rows_e), 2),
                        "hit_rate": round(wins_e / len(rows_e), 4),
                    }

            # Recent signals (last 30 actionable)
            recent_signals: list = []
            try:
                recent_signals = await q("""
                    SELECT signal_id, market_id, side, p_market, p_model, p_calibrated,
                           edge_raw, edge_net, created_at
                      FROM signals
                     WHERE edge_net > 0
                     ORDER BY created_at DESC
                     LIMIT 30
                """)
            except Exception:
                pass

            # Daily PnL (last 14 days)
            daily_pnl: list = []
            try:
                daily_pnl = await q("""
                    SELECT date(exit_time) as date,
                           COUNT(*) as n_trades,
                           SUM(pnl_usd) as pnl_usd
                      FROM trades
                     WHERE status = 'closed' AND exit_time IS NOT NULL
                     GROUP BY date(exit_time)
                     ORDER BY date DESC
                     LIMIT 14
                """)
            except Exception:
                pass

            # Funnel counts from last cycle summary (approx from DB counts)
            # market_snapshots = table name in schema (was incorrectly "snapshots")
            try:
                markets_count_row = await q1(
                    "SELECT COUNT(DISTINCT market_id) as n FROM market_snapshots"
                )
            except Exception:
                markets_count_row = None
            try:
                signals_count_row = await q1(
                    "SELECT COUNT(*) as n FROM signals WHERE edge_net > 0"
                )
            except Exception:
                signals_count_row = None
            try:
                executed_count_row = await q1("SELECT COUNT(*) as n FROM trades")
            except Exception:
                executed_count_row = None

            funnel = {
                "scanned": (markets_count_row or {}).get("n") or 0,
                "passed_guards": (markets_count_row or {}).get("n") or 0,
                "signals": (signals_count_row or {}).get("n") or 0,
                "executed": (executed_count_row or {}).get("n") or 0,
            }

    except Exception as e:
        logger.error("dashboard.fetch_error", error=str(e))
        return {
            "error": str(e),
            "mode": "paper",
            "open_positions": [],
            "closed_positions": [],
            "metrics": {},
            "calibration_buckets": [],
            "edge_by_bucket": {},
            "recent_signals": [],
            "daily_pnl": [],
            "funnel": {},
            "ts": time.time(),
        }

    return {
        "mode": mode,
        "open_positions": open_trades,
        "closed_positions": closed_trades,
        "metrics": metrics,
        "calibration_buckets": cal_buckets,
        "edge_by_bucket": edge_by_bucket,
        "recent_signals": recent_signals,
        "daily_pnl": daily_pnl,
        "funnel": funnel,
        "ts": time.time(),
    }


# ─── AIOHTTP SERVER ───────────────────────────────────────────────────────────

class DashboardServer:
    """
    Aiohttp-based dashboard server with Polymarket dark aesthetic.
    Serves live data from the polybot DB.
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        cfg = self.config.dashboard
        app = self._build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, cfg.host, cfg.port)
        await self._site.start()

        url = f"http://{cfg.host}:{cfg.port}"
        logger.info("dashboard.started", url=url, db=self.config.operation.db_path)
        print(f"\n  Dashboard running at {url}")
        print(f"  DB: {self.config.operation.db_path}")
        print(f"  Auto-refresh every {cfg.refresh_seconds}s\n")

        if cfg.auto_open and cfg.host == "127.0.0.1":
            await asyncio.sleep(0.3)
            webbrowser.open(url)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            logger.info("dashboard.stopped")

    def _build_app(self) -> web.Application:
        async def handle_index(_request: web.Request) -> web.Response:
            return web.Response(text=HTML, content_type="text/html")

        async def handle_data(_request: web.Request) -> web.Response:
            try:
                data = await fetch_dashboard_data(self.config)
                return web.Response(
                    text=json.dumps(data, default=str),
                    content_type="application/json",
                )
            except Exception as e:
                logger.error("dashboard.api_error", error=str(e))
                return web.Response(
                    text=json.dumps({"error": str(e)}),
                    content_type="application/json",
                    status=500,
                )

        async def handle_health(_request: web.Request) -> web.Response:
            return web.Response(
                text=json.dumps({
                    "status": "ok",
                    "mode": "paper" if self.config.operation.paper_trade else "live",
                    "db": self.config.operation.db_path,
                    "ts": time.time(),
                }),
                content_type="application/json",
            )

        app = web.Application()
        app.router.add_get("/", handle_index)
        app.router.add_get("/api/data", handle_data)
        app.router.add_get("/health", handle_health)
        return app
