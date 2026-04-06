# probabilisticobot — TODO

## TIER 1 — Immediate (this week)

- [ ] Validate market fetcher returns data correctly
  - Run `python run_bot.py --once` and check output
  - Confirm markets are being fetched from Polymarket Gamma API
- [ ] Check that guards filter out illiquid/stale markets correctly
  - Review `src/polybot/market_discovery/filters.py`
  - Tune `min_volume_24h_usd`, `max_spread_pct` in config
- [ ] Verify paper trades are being saved to DB
  - Check `./data/polybot.db` after running `--once`
  - Run `python run_bot.py --metrics` to see initial stats
- [ ] Review signals in DB: are edges reasonable?
  - Open the dashboard: `python run_bot.py --dashboard`
  - Inspect the "Recent Signals" table for edge_net values
  - Target: edge_net consistently > 0.02 (2%) on at least some markets
- [ ] Confirm dashboard loads at http://127.0.0.1:8080
  - All 6 sections should render (metrics, open, closed, signals, calibration, daily)

---

## TIER 2 — Strategy improvements (next 2 weeks)

- [ ] Collect 30+ resolved trades for calibration data
  - Paper trade continuously; wait for markets to resolve
  - Use short-duration markets (< 1 week) to accumulate data faster
- [ ] Fit IsotonicCalibrator once enough data exists
  - `DefaultStrategy.upgrade_calibrator()` is called automatically
  - Check dashboard calibration chart: blue (model) vs green (actual) bars should align
- [ ] Add order flow features: recent trade direction, trade size clustering
  - Add to `FeatureBuilder.build()` in `feature_builder/builder.py`
  - New feature fields needed in `MarketFeatures` model
- [ ] Add time-series features: price momentum over last 1h/4h/24h
  - Requires storing historical snapshots in DB
  - Compute momentum in `FeatureBuilder` from snapshot history
- [ ] Experiment with LogisticRegression as probability model
  - Create `src/polybot/probability_model/logistic.py`
  - Fit on labeled historical trades
  - Swap into `DefaultStrategy` and compare Brier scores
- [ ] Implement category-specific guards (crypto vs politics vs sports)
  - Add `category` field to `MarketSnapshot`
  - Create category-aware guard thresholds in `GuardsConfig`

---

## TIER 3 — Model improvements (next month)

- [ ] Label historical data from Polymarket API (past resolved markets)
  - Fetch resolved markets from Gamma API with outcomes
  - Build feature vectors retrospectively for each snapshot
  - Store in a training dataset (parquet or SQLite table)
- [ ] Train XGBoost model on historical features vs outcomes
  - Install: `pip install xgboost scikit-learn`
  - Create `src/polybot/probability_model/xgboost_model.py`
  - Feature importance analysis to identify best predictors
- [ ] Implement proper train/validation/test split with time-based splits
  - Never use future data to predict past (lookahead bias)
  - Walk-forward validation: train on months 1-3, test on month 4, etc.
- [ ] Add feature importance analysis
  - Log top-N features in model predictions
  - Identify which features drive the most signal
- [ ] Implement ensemble: naive + logistic + xgboost
  - Average predictions weighted by recent Brier score
  - Fallback to naive if trained models have insufficient samples

---

## TIER 4 — Live trading preparation

- [ ] Paper trade for minimum 60 days with 100+ trades
  - Required for statistical significance (95% confidence)
  - Track all metrics daily in the dashboard
- [ ] Validate readiness criteria before going live:
  - Hit rate > 52% (demonstrates positive edge)
  - EV efficiency > 60% (model edge is being captured)
  - Brier score < 0.24 (calibration is adequate)
  - Max drawdown < 15% of bankroll
  - Sharpe ratio > 1.0 (annualized)
- [ ] Implement live execution engine (Polymarket CLOB orders)
  - Create `src/polybot/execution_engine/live.py`
  - Implement order placement via CLOB REST API
  - EIP-712 signing already available in `app/clients/auth.py`
- [ ] Set up alerting (Telegram/email on significant events)
  - Alert on: drawdown > 5%, daily loss > $20, kill switch triggered
  - Alert on: new positions opened, large wins/losses
- [ ] Review and harden all risk limits before going live
  - Start with 0.5% max risk per trade (vs 2% paper)
  - Set hard daily loss limit at $25 (vs $50 paper)
  - Test kill switch mechanism
- [ ] Legal/regulatory review for your jurisdiction
  - Verify prediction market legality in your country
  - Consider tax implications of frequent trading

---

## TIER 5 — Advanced

- [ ] Multi-market correlation detection (avoid correlated positions)
  - Detect markets asking the same underlying question
  - Cap combined exposure to correlated markets
- [ ] Dynamic edge threshold (adjust based on recent model performance)
  - Raise threshold when recent Brier score is high
  - Lower threshold when model is performing well
- [ ] Order book pressure features (bid/ask size dynamics)
  - Track depth changes over time
  - Large bid/ask imbalances as directional signals
- [ ] Thesis/consensus detection across related markets
  - Detect when multiple related markets imply inconsistent probabilities
  - Trade the inconsistency (e.g., candidate A wins market vs candidate A total votes)
- [ ] Automated recalibration schedule
  - Retrain calibrator every N new resolved trades
  - Alert when calibration degrades significantly
- [ ] Deploy to cloud (Fly.io or similar) for 24/7 operation
  - Use `--host 0.0.0.0` for remote dashboard access
  - Set `log_json = true` for structured cloud logging
  - Store DB on persistent volume
