# polybot-probability

**Polymarket probabilistic trading research bot.**

Paper trading and research tool for Polymarket prediction markets. Systematically
identifies pricing inefficiencies using a fractional Kelly criterion risk engine,
signal edge decomposition, and a full funnel audit pipeline.

> **Status: research / paper trading only.**
> This project is not ready for real capital. See [STATUS.md](STATUS.md).

---

## What it does

1. **Fetches** active binary markets from Polymarket's public Gamma API
2. **Filters** with operational guards (volume, spread, depth, time to resolution)
3. **Builds** market features (imbalance, spread %, volume/OI ratio, etc.)
4. **Predicts** using a NaiveModel (implied probability baseline)
5. **Calibrates** predictions (auto-upgrades to Isotonic calibration at 30+ trades)
6. **Computes signals** — edge_raw, edge_net after FillModel costs
7. **Sizes positions** with fractional Kelly (7 hard checks + 4 trim caps)
8. **Executes** in paper mode (no real orders)
9. **Audits** every cycle via a full funnel (10 EAV counters per cycle)
10. **Analyzes** performance with Brier score, log-loss, hit rate, EV efficiency

---

## Architecture

```
src/polybot/
├── market_discovery/   # Gamma API fetcher + operational guards
├── feature_builder/    # Market feature extraction
├── probability_model/  # NaiveModel baseline (replaceable)
├── calibration/        # Identity → Isotonic auto-upgrade
├── signal_engine/      # Edge computation + FillModel cost model
├── risk_engine/        # Fractional Kelly sizer + caps
├── execution_engine/   # Paper execution (dry-run safe)
├── analytics/          # Metrics, diagnostics, validation
├── reporting/          # aiohttp dashboard (port 8080)
├── jobs/cycle.py       # Main orchestration loop
├── db.py               # Async SQLite persistence layer
├── models.py           # All domain types
└── config.py           # BotConfig dataclass (no pydantic-settings)
```

**Key design choices:**
- Paper mode by default — live trading requires explicit config + env var
- Strategy isolated in `src/polybot/strategy/` — replaceable without touching the framework
- DB is function-based (no ORM) — all queries parameterized
- Funnel events stored in EAV format — extensible without schema changes
- `SizingDecision` always returned (approved or rejected) — full traceability

---

## Quick start

### 1. Install

```bash
pip install aiohttp aiosqlite httpx structlog
```

### 2. Configure

```bash
cp config.example.toml config.toml
# Edit config.toml if needed — defaults are fine for paper mode
```

No credentials needed for paper mode. The market fetcher uses Polymarket's
public Gamma API (read-only, no auth).

### 3. Run one cycle

```bash
python3.12 run_bot.py --once
```

### 4. Run continuously

```bash
python3.12 run_bot.py
# Dashboard available at http://localhost:8080
```

### 5. Check metrics

```bash
python3.12 run_bot.py --metrics
```

### 6. Smoke test (verify system integrity)

```bash
python3.12 scripts/smoke_test.py
```

Runs imports, schema check, one cycle, and funnel invariants. Exit 0 = all pass.

### 7. Funnel audit (after accumulating data)

```bash
python3.12 scripts/funnel_audit.py --db ./data/polybot.db
```

Produces a 5-section report: funnel pyramid, rejection breakdown, cohort tables
(spread/depth/time/volume/OI), capital deployed, and a verdict with action items.

---

## Funnel invariants

The system tracks 10 counters per cycle (EAV table `funnel_events`). These invariants
are verified by `smoke_test.py` and documented in `src/polybot/jobs/cycle.py`:

```
already_positioned + risk_approved + risk_rejected = positive_edge_net
executed    ≤ risk_approved
approved_size_usd ≤ requested_size_usd
```

---

## Configuration

See `config.example.toml` for all parameters. Key sections:

| Section | Purpose |
|---|---|
| `[operation]` | paper_trade, dry_run, scan interval, max positions |
| `[guards]` | Volume, spread, depth, staleness thresholds |
| `[model]` | Edge thresholds, calibration settings |
| `[costs]` | Fee model, slippage |
| `[risk]` | Bankroll, Kelly fraction, daily/event caps, drawdown limits |
| `[exit]` | Take profit, stop loss, max hold, trailing stop |

Credentials are never in the config file. Set via environment variables:

```bash
export POLYMARKET_API_KEY=...
export POLYMARKET_WALLET_ADDRESS=...
export POLYMARKET_PRIVATE_KEY=...   # only needed for live mode
```

---

## Scripts

| Script | Purpose |
|---|---|
| `scripts/smoke_test.py` | Minimal system verification (CI-safe) |
| `scripts/funnel_audit.py` | Full funnel report from DB |
| `scripts/audit_model.py` | Theoretical model diagnostics |
| `scripts/paper_report.py` | Paper trading performance report |
| `scripts/validate_connection.py` | Pre-deploy connectivity check |

---

## Warning

This project is a **research tool**. The NaiveModel has near-zero expected edge
by construction — it uses market-implied probabilities as its baseline.
Real edge requires calibration data from resolved markets (≥ 30 closed trades).

Do not use with real capital until:
- [ ] Hit rate verified with ≥ 30 resolved trades
- [ ] Funnel audit shows positive edge in specific cohorts
- [ ] All risk caps and their interactions are understood

---

## Related

- [skeleton](https://github.com/maxijcuesta/skeleton) — operational framework this project was based on

---

## License

No license declared. All rights reserved.
