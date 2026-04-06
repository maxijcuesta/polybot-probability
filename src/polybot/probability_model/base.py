"""
Protocol interface for probability models.

All models must implement this Protocol to plug into the signal engine.
Using Protocol (structural subtyping) means no forced inheritance.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import MarketFeatures, ModelPrediction


@runtime_checkable
class ProbabilityModel(Protocol):
    """
    Interface for all probability models.

    Implement this Protocol to add new models without touching the framework.
    The signal engine calls predict() for each market in each scan cycle.

    To add a new model:
      1. Create src/polybot/probability_model/my_model.py
      2. Implement predict(), predict_batch(), fit(), is_fitted()
      3. Set model_type class attribute to a unique string
      4. Register in probability_model/__init__.py
    """

    model_type: str

    def predict(self, features: MarketFeatures) -> ModelPrediction:
        """
        Predict P(YES) for a single market.

        Args:
            features: computed MarketFeatures for the market

        Returns:
            ModelPrediction with p_yes, p_no, confidence, model_type
        """
        ...

    def predict_batch(self, features_list: list[MarketFeatures]) -> list[ModelPrediction]:
        """
        Predict P(YES) for multiple markets.

        Default implementation calls predict() in a loop.
        Override for vectorized models (e.g., sklearn, XGBoost).

        Args:
            features_list: list of MarketFeatures

        Returns:
            list of ModelPrediction in same order as input
        """
        ...

    def fit(self, X: list[list[float]], y: list[int]) -> None:
        """
        Fit the model on historical data.

        Args:
            X: list of feature vectors (from MarketFeatures.to_vector())
            y: list of outcomes (1 = YES won, 0 = NO won)
        """
        ...

    def is_fitted(self) -> bool:
        """Return True if model is ready to predict (fit() has been called)."""
        ...
