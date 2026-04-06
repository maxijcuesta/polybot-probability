from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]
from typing import Any


def _load_toml(path: str | Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass(slots=True)
class OperationConfig:
    paper_trade: bool = True       # paper mode by default
    live_trade: bool = False       # live requires explicit True
    dry_run: bool = True           # no real orders
    log_level: str = "INFO"
    log_json: bool = True
    db_path: str = "./data/polybot.db"
    scan_interval_seconds: int = 60
    max_concurrent_positions: int = 5
    heartbeat_interval_minutes: int = 30


@dataclass(slots=True)
class PolymarketConfig:
    clob_base: str = "https://clob.polymarket.com"
    gamma_base: str = "https://gamma-api.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    wallet_address: str = ""
    private_key: str = ""          # NEVER log this
    chain_id: int = 137            # Polygon


@dataclass(slots=True)
class GuardsConfig:
    """Operational guards — all must pass before entering a position."""
    min_volume_24h_usd: float = 10_000.0
    min_open_interest_usd: float = 5_000.0
    max_spread_pct: float = 0.05         # 5% max spread
    min_depth_usd: float = 500.0         # min $500 on each side
    max_hours_since_last_trade: float = 4.0   # stale market protection
    min_yes_price: float = 0.02          # avoid 0/1 edge cases
    max_yes_price: float = 0.98
    min_time_to_resolution_hours: float = 4.0  # avoid last-minute only
    max_time_to_resolution_days: float = 90.0


@dataclass(slots=True)
class ModelConfig:
    """Probability model configuration."""
    model_type: str = "naive"            # naive | logistic | xgboost
    min_edge_raw: float = 0.04           # 4% raw edge minimum
    min_edge_net: float = 0.02           # 2% net edge minimum (after costs)
    calibration_method: str = "isotonic" # isotonic | platt | none
    calibration_min_samples: int = 30    # min trades before calibrating


@dataclass(slots=True)
class CostConfig:
    """Transaction cost model."""
    taker_fee_pct: float = 0.0          # Polymarket: 0% maker, 0% taker (currently)
    maker_fee_pct: float = 0.0
    slippage_model_pct: float = 0.005   # 0.5% simulated slippage
    gas_cost_usd: float = 0.01          # polygon gas


@dataclass(slots=True)
class RiskConfig:
    """Risk and position sizing."""
    bankroll_usd: float = 1_000.0
    max_risk_per_trade_pct: float = 2.0      # % of bankroll per trade
    max_portfolio_exposure_pct: float = 20.0  # max total exposure
    kelly_fraction: float = 0.25             # fractional Kelly
    use_kelly: bool = True                   # else: fixed fraction
    daily_loss_limit_usd: float = 50.0
    max_drawdown_pct: float = 15.0


@dataclass(slots=True)
class ExitConfig:
    """Exit conditions."""
    take_profit_pct: float = 0.15        # 15% profit
    stop_loss_pct: float = 0.10          # 10% loss
    max_hold_hours: float = 72.0
    exit_on_edge_flip: bool = True       # exit if edge goes negative
    trailing_stop_pct: float | None = None  # optional trailing stop


@dataclass(slots=True)
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    auto_open: bool = True
    refresh_seconds: int = 30


@dataclass(slots=True)
class BotConfig:
    """Root configuration object."""
    operation: OperationConfig = field(default_factory=OperationConfig)
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    guards: GuardsConfig = field(default_factory=GuardsConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)

    @classmethod
    def from_toml(cls, path: str | Path) -> "BotConfig":
        """Load from TOML file, falling back to defaults for missing keys."""
        raw = _load_toml(path)
        op = raw.get("operation", {})
        pm = raw.get("polymarket", {})
        gu = raw.get("guards", {})
        mo = raw.get("model", {})
        co = raw.get("costs", {})
        ri = raw.get("risk", {})
        ex = raw.get("exit", {})
        da = raw.get("dashboard", {})
        return cls(
            operation=OperationConfig(**{k: v for k, v in op.items() if hasattr(OperationConfig, k)}),
            polymarket=PolymarketConfig(**{k: v for k, v in pm.items() if hasattr(PolymarketConfig, k)}),
            guards=GuardsConfig(**{k: v for k, v in gu.items() if hasattr(GuardsConfig, k)}),
            model=ModelConfig(**{k: v for k, v in mo.items() if hasattr(ModelConfig, k)}),
            costs=CostConfig(**{k: v for k, v in co.items() if hasattr(CostConfig, k)}),
            risk=RiskConfig(**{k: v for k, v in ri.items() if hasattr(RiskConfig, k)}),
            exit=ExitConfig(**{k: v for k, v in ex.items() if hasattr(ExitConfig, k)}),
            dashboard=DashboardConfig(**{k: v for k, v in da.items() if hasattr(DashboardConfig, k)}),
        )

    @classmethod
    def defaults(cls) -> "BotConfig":
        return cls()
