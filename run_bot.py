#!/usr/bin/env python3
"""
probabilisticobot -- Polymarket probabilistic trading bot.

Usage:
  python run_bot.py                           # paper trading with defaults
  python run_bot.py --config config.toml      # use config file
  python run_bot.py --once                    # run single cycle
  python run_bot.py --dashboard               # run dashboard only
  python run_bot.py --metrics                 # print metrics report
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from polybot.config import BotConfig
from polybot.jobs.cycle import BotCycle
from polybot.reporting.dashboard import DashboardServer
from polybot import db as storage
from polybot.analytics.metrics import MetricsEngine


async def main():
    parser = argparse.ArgumentParser(description="Polymarket Probabilistic Bot")
    parser.add_argument("--config", default=None, help="Path to config.toml")
    parser.add_argument("--once", action="store_true", help="Run single cycle then exit")
    parser.add_argument("--dashboard", action="store_true", help="Run dashboard only")
    parser.add_argument("--metrics", action="store_true", help="Print metrics report")
    parser.add_argument("--db", default=None, help="Override DB path")
    args = parser.parse_args()

    # Load config
    if args.config:
        config = BotConfig.from_toml(args.config)
    else:
        config = BotConfig.defaults()

    # DB path: --db flag > POLYBOT_DB_PATH env var > config default
    if args.db:
        config.operation.db_path = args.db
    elif os.environ.get("POLYBOT_DB_PATH"):
        config.operation.db_path = os.environ["POLYBOT_DB_PATH"]

    await storage.ensure_schema(config.operation.db_path)

    if args.metrics:
        trades = await storage.load_all_trades(config.operation.db_path)
        metrics = MetricsEngine().compute(trades)
        print(f"\n{'='*60}")
        print(f"  PERFORMANCE METRICS")
        print(f"{'='*60}")
        print(f"  Trades:       {metrics.n_trades} ({metrics.n_wins}W / {metrics.n_losses}L)")
        print(f"  Hit Rate:     {metrics.hit_rate:.1%}")
        print(f"  PnL Gross:    ${metrics.pnl_gross_usd:.2f}")
        print(f"  PnL Net:      ${metrics.pnl_net_usd:.2f}")
        print(f"  Brier Score:  {metrics.brier_score:.4f}")
        print(f"  Log Loss:     {metrics.log_loss:.4f}")
        print(f"  EV Expected:  ${metrics.ev_expected_usd:.2f}")
        print(f"  EV Realized:  ${metrics.ev_realized_usd:.2f}")
        print(f"  EV Efficiency:{metrics.ev_efficiency:.2%}")
        print(f"  Profit Factor:{metrics.profit_factor:.2f}")
        print(f"  Max Drawdown: ${metrics.max_drawdown_usd:.2f}")
        print(f"  Avg Hold:     {metrics.avg_hold_hours:.1f}h")
        if metrics.calibration_buckets:
            print(f"\n  CALIBRATION BUCKETS:")
            for b in metrics.calibration_buckets:
                bar = chr(9608) * int(b.calibration_error * 50)
                print(f"    [{b.bucket_label}] n={b.n_trades:3d}  p_model={b.avg_p_model:.3f}  win_rate={b.observed_win_rate:.3f}  err={b.calibration_error:.3f} {bar}")
        print()
        return

    if args.dashboard:
        server = DashboardServer(config)
        await server.start()
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            await server.stop()
        return

    cycle = BotCycle(config)

    if args.once:
        result = await cycle.run_once()
        print(f"\nCycle complete: {result}")
        return

    # Run dashboard + bot together
    server = DashboardServer(config)
    await server.start()

    try:
        await cycle.run_loop()
    except KeyboardInterrupt:
        cycle.stop()
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
