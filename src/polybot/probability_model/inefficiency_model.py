"""
Inefficiency model: microprice-based fair value estimation.

Replaces NaiveModel as the default. Same interface (ModelPrediction),
better math: uses the depth-weighted mid (microprice) as baseline P(YES)
instead of simple mid-price + arbitrary adjustments.

Microprice formula (Stoikov, 2018):
    p_micro = p_mid + depth_imbalance × (spread / 2)

    depth_imbalance = (Q_bid - Q_ask) / (Q_bid + Q_ask)  ∈ [-1, 1]
    Q_bid > Q_ask  → more buy pressure → p_micro > mid  → YES underpriced
    Q_bid < Q_ask  → more sell pressure → p_micro < mid  → NO underpriced

When orderbook data is absent (depth_imbalance = 0), the model falls back
to p_micro = mid (identical to market) and confidence reflects spread/volume
quality. The InefficiencyScorer (scorer.py) handles ranking in that regime.

Confidence decomposition:
    spread_quality  — tight spread = market maker is confident = we should be
    vol_activity    — high vol/OI = active price discovery
    centrality      — mid near 0 or 1 = market near certainty = less modelling value
"""
from __future__ import annotations

from ..models import MarketFeatures, ModelPrediction


class InefficiencyModel:
    """
    Microprice-based probability model. Drop-in replacement for NaiveModel.

    Interface identical to NaiveModel: predict() returns ModelPrediction.
    No training data required. Produces real microstructure-grounded edge.

    Edge ceiling:
        edge_raw = depth_imbalance × spread/2
        With spread=0.10, imbalance=0.5 → edge_raw = 0.025 (2.5%)
        vs NaiveModel: depth_weight=0.03, max adj ≈ 0.035 but always same sign

    The key difference: NaiveModel shifts p_yes by a fixed fraction of
    depth_imbalance regardless of the actual spread; InefficiencyModel
    ties the shift to spread — wider spread = more uncertainty = larger
    potential deviation from true value.
    """

    model_type = "inefficiency"

    def __init__(self) -> None:
        self._fitted = True

    def predict(self, features: MarketFeatures) -> ModelPrediction:
        mid = features.mid_price
        spread = features.spread_pct

        # ── Microprice ────────────────────────────────────────────────────────
        # depth_imbalance ∈ [-1, 1]:  positive = more bid depth = YES in demand
        # When depth_imbalance = 0 (no orderbook): p_micro = mid (no signal)
        microprice_shift = features.depth_imbalance * (spread / 2.0)
        p_yes = max(0.01, min(0.99, mid + microprice_shift))

        # ── Confidence ────────────────────────────────────────────────────────
        # 1. Spread quality: tight spread = market maker confident
        #    0.0 at 20%+ spread, 1.0 at 0% spread
        spread_quality = max(0.0, 1.0 - spread / 0.20)

        # 2. Volume activity: high vol/OI = active price discovery
        #    Capped at 1.0 when vol/OI ≥ 2.0
        vol_factor = min(features.volume_oi_ratio / 2.0, 1.0)

        # 3. Price centrality: mid near 0.5 = genuine uncertainty
        #    1.0 at mid=0.5, 0.0 at mid=0 or mid=1
        centrality = max(0.1, 1.0 - abs(mid - 0.5) * 2.0)

        confidence = (
            0.50 * spread_quality
            + 0.30 * vol_factor
            + 0.20 * centrality
        )
        confidence = max(0.05, min(0.95, confidence))

        return ModelPrediction(
            market_id=features.market_id,
            p_yes=p_yes,
            p_no=1.0 - p_yes,
            confidence=confidence,
            model_type=self.model_type,
        )

    def predict_batch(self, features_list: list[MarketFeatures]) -> list[ModelPrediction]:
        return [self.predict(f) for f in features_list]

    def fit(self, X: list[list[float]], y: list[int]) -> None:  # noqa: N803
        """No-op: no learnable parameters."""
        self._fitted = True

    def is_fitted(self) -> bool:
        return self._fitted
