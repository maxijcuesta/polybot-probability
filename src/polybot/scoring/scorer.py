"""
InefficiencyScorer: ranks Polymarket binary markets by opportunity quality.

Produces three composite scores per market:

    inefficiency_score  — how much microstructure signals a deviation from fair
                          value (the "is there something here?" signal)
    execution_score     — how tradeable the market is right now (liquidity + timing)
    final_trade_score   — weighted combination used to rank and select candidates

Design principles
-----------------
- Transversal: works on ANY binary Polymarket market with no topic dependency.
- Pure math: no ML, no training data, no calibration required.
- Degrades gracefully: when orderbook depth is unavailable (depth_imbalance=0),
  the spread, volume, and timing signals carry the score.
- Interpretable: every component is independently observable and auditable.

Component summary
-----------------
inefficiency_score (6 components):
    microprice_gap   — depth-implied price deviation from mid (needs orderbook)
    spread_signal    — spread as market-maker uncertainty proxy (peaks ~8%)
    book_imbalance   — |depth_imbalance| directional pressure magnitude
    price_centrality — distance from 0/1 boundaries (more uncertainty = more room)
    vol_activity     — volume/OI ratio (active markets = better price discovery)

execution_score (3 components):
    spread_cost      — inverse of spread; tight spread = cheap to execute
    staleness        — quote freshness (stale quotes = bad fills expected)
    resolution_window — is time-to-resolution in the tradeable sweet spot?

final_trade_score = 0.60 × inefficiency_score + 0.40 × execution_score
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..models import MarketFeatures, MarketSnapshot


@dataclass(slots=True)
class InefficiencyScore:
    """
    Full scoring breakdown for one market at one point in time.

    All component scores ∈ [0, 1]; higher = more interesting / better quality.
    """
    market_id: str

    # ── Inefficiency components ─────────────────────────────────────────────
    microprice_gap: float    # depth-implied deviation from mid (0 without orderbook)
    spread_signal: float     # spread as uncertainty proxy; peaks ~8%, 0 at 25%+
    book_imbalance: float    # |depth_imbalance|; directional pressure magnitude
    price_centrality: float  # 1 at mid=0.5, 0 at mid=0 or mid=1
    vol_activity: float      # vol/OI normalized to [0,1]

    # ── Execution components ────────────────────────────────────────────────
    spread_cost: float       # 1 − spread/MAX_EXEC_SPREAD; tight is better
    staleness: float         # 1 at 0h stale, 0 at 2h+ stale
    resolution_window: float # 1 in [4h–7d] sweet spot, tapers outside

    # ── Composites ──────────────────────────────────────────────────────────
    inefficiency_score: float
    execution_score: float
    final_trade_score: float  # the ranking key

    # ── Directional hint ────────────────────────────────────────────────────
    suggested_side: str      # "YES" or "NO"


class InefficiencyScorer:
    """
    Computes InefficiencyScore for a (snapshot, features) pair.

    All weights are class-level constants for easy auditing.
    Override in a subclass or pass custom weights for experimentation.
    """

    # ── Inefficiency component weights (must sum to 1.0) ──────────────────
    W_MICROPRICE_GAP = 0.30
    W_SPREAD_SIGNAL  = 0.25
    W_BOOK_IMBALANCE = 0.25
    W_CENTRALITY     = 0.15
    W_VOL_ACTIVITY   = 0.05

    # ── Execution component weights (must sum to 1.0) ─────────────────────
    W_SPREAD_COST    = 0.40
    W_STALENESS      = 0.35
    W_RES_WINDOW     = 0.25

    # ── Composite weights ─────────────────────────────────────────────────
    W_INEFFICIENCY   = 0.60
    W_EXECUTION      = 0.40

    # ── Calibration parameters ────────────────────────────────────────────
    # Spread at which signal peaks (~8%) and at which it goes to zero (25%)
    SPREAD_PEAK      = 0.08
    SPREAD_NOISE     = 0.25   # above this: too illiquid / noisy

    # Spread above which execution score starts degrading sharply
    MAX_EXEC_SPREAD  = 0.10

    # Quote staleness: 0 → 1 mapped linearly from 0h to this ceiling
    MAX_STALE_HOURS  = 2.0

    # Resolution timing sweet spot
    RES_MIN_HOURS    = 4.0    # below: whipsaw risk
    RES_SWEET_HOURS  = 168.0  # 7 days: sweet spot ends, decay begins
    RES_MAX_HOURS    = 720.0  # 30 days: floor
    RES_FLOOR_SCORE  = 0.30   # minimum score for very far-out markets

    def score(
        self,
        snapshot: MarketSnapshot,  # noqa: ARG002  (kept for future use / parity)
        features: MarketFeatures,
    ) -> InefficiencyScore:
        """Score one market. Returns InefficiencyScore with full breakdown."""

        mid    = features.mid_price
        spread = features.spread_pct

        # ── Inefficiency components ────────────────────────────────────────

        # 1. Microprice gap
        #    = |depth_imbalance × spread/2| / (SPREAD_NOISE/2)
        #    Represents: how far the depth-weighted fair value deviates from mid.
        #    Zero when depth_imbalance = 0 (no orderbook data).
        raw_gap = abs(features.depth_imbalance) * (spread / 2.0)
        max_gap = self.SPREAD_NOISE / 2.0
        microprice_gap = min(raw_gap / max(max_gap, 1e-9), 1.0)

        # 2. Spread signal
        #    Wide spread = market maker is uncertain = potential mis-pricing.
        #    Peaks at SPREAD_PEAK; goes to zero at SPREAD_NOISE.
        spread_signal = self._spread_signal(spread)

        # 3. Book imbalance
        #    |depth_imbalance| ∈ [0,1]. Pure directional pressure magnitude.
        book_imbalance = min(abs(features.depth_imbalance), 1.0)

        # 4. Price centrality
        #    1.0 at mid=0.5, 0.0 at mid=0 or mid=1.
        #    High centrality = genuine probability uncertainty = more modeling value.
        price_centrality = max(0.0, 1.0 - abs(mid - 0.5) * 2.0)

        # 5. Volume activity
        #    vol/OI ratio normalized to [0,1], capped at 1.0 when ≥ 2.0.
        vol_activity = min(features.volume_oi_ratio / 2.0, 1.0)

        # ── Execution components ───────────────────────────────────────────

        # 6. Spread cost quality
        #    Inverted spread: tight spread = cheap to transact.
        spread_cost = max(0.0, 1.0 - spread / self.MAX_EXEC_SPREAD)

        # 7. Staleness
        #    1.0 = fresh (0h since last trade), 0.0 = stale (≥ MAX_STALE_HOURS).
        staleness = max(0.0, 1.0 - features.hours_since_last_trade / self.MAX_STALE_HOURS)

        # 8. Resolution window
        resolution_window = self._resolution_score(features.hours_to_resolution)

        # ── Composite scores ───────────────────────────────────────────────

        inefficiency_score = (
            self.W_MICROPRICE_GAP * microprice_gap
            + self.W_SPREAD_SIGNAL  * spread_signal
            + self.W_BOOK_IMBALANCE * book_imbalance
            + self.W_CENTRALITY     * price_centrality
            + self.W_VOL_ACTIVITY   * vol_activity
        )

        execution_score = (
            self.W_SPREAD_COST * spread_cost
            + self.W_STALENESS   * staleness
            + self.W_RES_WINDOW  * resolution_window
        )

        final_trade_score = (
            self.W_INEFFICIENCY * inefficiency_score
            + self.W_EXECUTION    * execution_score
        )

        # ── Directional hint ───────────────────────────────────────────────
        # depth_imbalance > 0 → more bid depth → YES is in demand
        # depth_imbalance < 0 → more ask depth → NO is in demand
        # When imbalance is negligible: suggest the side below mid
        # (contrarian: markets below 0.5 have room to run YES)
        if features.depth_imbalance > 0.05:
            suggested_side = "YES"
        elif features.depth_imbalance < -0.05:
            suggested_side = "NO"
        else:
            suggested_side = "YES" if mid < 0.50 else "NO"

        return InefficiencyScore(
            market_id=features.market_id,
            microprice_gap=round(microprice_gap, 4),
            spread_signal=round(spread_signal, 4),
            book_imbalance=round(book_imbalance, 4),
            price_centrality=round(price_centrality, 4),
            vol_activity=round(vol_activity, 4),
            spread_cost=round(spread_cost, 4),
            staleness=round(staleness, 4),
            resolution_window=round(resolution_window, 4),
            inefficiency_score=round(inefficiency_score, 4),
            execution_score=round(execution_score, 4),
            final_trade_score=round(final_trade_score, 4),
            suggested_side=suggested_side,
        )

    def score_batch(
        self,
        snapshots: list[MarketSnapshot],
        features_list: list[MarketFeatures],
    ) -> list[InefficiencyScore]:
        return [self.score(s, f) for s, f in zip(snapshots, features_list)]

    # ── Private helpers ────────────────────────────────────────────────────────

    def _spread_signal(self, spread_pct: float) -> float:
        """
        Maps spread_pct → [0,1] opportunity signal.

        Shape:
            0.0% spread → 0.05 (tight = efficiently priced, low signal)
            8.0% spread → 1.00 (peak: genuine market maker uncertainty)
           25.0% spread → 0.00 (too wide: noise / no real orderbook)

        Uses log-scale rise to peak, linear decay after peak.
        """
        if spread_pct <= 0.001:
            return 0.05
        if spread_pct >= self.SPREAD_NOISE:
            return 0.0
        if spread_pct <= self.SPREAD_PEAK:
            # Log-scale rise from 0.001 to SPREAD_PEAK
            # log(x/0.001) / log(SPREAD_PEAK/0.001)
            normalized = math.log(spread_pct / 0.001) / math.log(self.SPREAD_PEAK / 0.001)
            return max(0.0, min(1.0, normalized))
        else:
            # Linear decay from SPREAD_PEAK → 0 at SPREAD_NOISE
            t = (spread_pct - self.SPREAD_PEAK) / (self.SPREAD_NOISE - self.SPREAD_PEAK)
            return max(0.0, 1.0 - t)

    def _resolution_score(self, hours_to_resolution: float) -> float:
        """
        Maps time-to-resolution → [0,1] timing quality.

        Sweet spot: 4h – 7 days.
        < 4h: whipsaw risk, spread widens near resolution → 0.5 linear ramp.
        > 7d – 30d: capital locked too long → linear decay to RES_FLOOR_SCORE.
        > 30d: RES_FLOOR_SCORE (not zero — market can still be interesting).
        9999 sentinel (unknown): treated as > RES_MAX_HOURS → floor.
        """
        h = hours_to_resolution
        if h <= 0.0:
            return 0.0
        if h >= 9000.0:                 # sentinel for unknown resolution time
            return self.RES_FLOOR_SCORE
        if h < self.RES_MIN_HOURS:
            return (h / self.RES_MIN_HOURS) * 0.5
        if h <= self.RES_SWEET_HOURS:
            return 1.0
        if h <= self.RES_MAX_HOURS:
            t = (h - self.RES_SWEET_HOURS) / (self.RES_MAX_HOURS - self.RES_SWEET_HOURS)
            return 1.0 - t * (1.0 - self.RES_FLOOR_SCORE)
        return self.RES_FLOOR_SCORE
