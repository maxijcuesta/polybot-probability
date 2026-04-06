"""
Naive probability model: market price as baseline P(YES) with small adjustments.

This is the default starting model. It doesn't require training data and
produces real edge calculations immediately. Replace with a trained model
once you have enough historical trades.

Adjustments:
  - Depth imbalance: more buy pressure → slightly higher YES probability
  - Volume/OI ratio: active market → trust mid-price more
  - Spread: wide spread → lower confidence
"""
from __future__ import annotations

from ..models import MarketFeatures, ModelPrediction


class NaiveModel:
    """
    Naive probability model: uses market mid-price as baseline P(YES),
    then applies small adjustments for:
    - Depth imbalance (order flow pressure)
    - Volume/OI ratio (activity level)
    - Spread (information asymmetry)

    This is the starting point. Replace with a trained model in strategy/.
    """

    model_type = "naive"

    def __init__(
        self,
        depth_weight: float = 0.03,
        volume_weight: float = 0.01,
    ) -> None:
        """
        Args:
            depth_weight: how much depth imbalance shifts the probability
                          (0.03 = 3% max shift from full imbalance)
            volume_weight: how much volume/OI ratio shifts the probability
                           (0.01 = 1% max shift)
        """
        self._depth_weight = depth_weight
        self._volume_weight = volume_weight
        self._fitted = True  # naive model doesn't need training

    def predict(self, features: MarketFeatures) -> ModelPrediction:
        """
        Predict P(YES) for a market.

        Uses mid-price as the base probability (market's own estimate),
        then adjusts for microstructure signals.
        """
        p_base = features.mid_price

        # Adjustment 1: depth imbalance
        # depth_imbalance ∈ [-1, 1]; positive = more bid pressure = higher YES prob
        adj_depth = self._depth_weight * features.depth_imbalance

        # Adjustment 2: volume activity
        # High vol/OI ratio = price is being discovered actively
        # Normalize to [0, 1] then center around 0
        vol_factor = min(features.volume_oi_ratio, 2.0) / 2.0  # clamp and normalize
        adj_volume = self._volume_weight * (vol_factor - 0.5)   # center: 0 = neutral

        p_yes = max(0.01, min(0.99, p_base + adj_depth + adj_volume))

        # Confidence: penalize wide spreads (information asymmetry)
        # spread_pct = 0 → confidence = 0.7 (baseline)
        # spread_pct = 0.10 → confidence = 0.2 (low)
        spread_penalty = min(features.spread_pct * 5.0, 0.6)
        confidence = max(0.1, 0.7 - spread_penalty)

        return ModelPrediction(
            market_id=features.market_id,
            p_yes=p_yes,
            p_no=1.0 - p_yes,
            confidence=confidence,
            model_type=self.model_type,
        )

    def predict_batch(self, features_list: list[MarketFeatures]) -> list[ModelPrediction]:
        """Predict for a list of markets."""
        return [self.predict(f) for f in features_list]

    def fit(self, X: list[list[float]], y: list[int]) -> None:
        """No-op: naive model has no learnable parameters."""
        self._fitted = True

    def is_fitted(self) -> bool:
        return self._fitted
