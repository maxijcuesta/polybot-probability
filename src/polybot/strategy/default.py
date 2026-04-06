from __future__ import annotations
from ..probability_model.inefficiency_model import InefficiencyModel
from ..calibration.calibrator import IdentityCalibrator
from ..config import BotConfig


class DefaultStrategy:
    """
    Default strategy: InefficiencyModel + IdentityCalibrator.

    The InefficiencyModel uses microprice (depth-weighted mid) as baseline
    P(YES), grounded in market microstructure. The final_trade_score from
    InefficiencyScorer handles candidate ranking in cycle.py.

    Replace with a trained model once you have sufficient resolved trades.
    See strategy/README for instructions.
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.model = InefficiencyModel()
        # Use IdentityCalibrator until we have 30+ trades for calibration
        self.calibrator = IdentityCalibrator()

    def upgrade_calibrator(self, p_predicted: list[float], outcomes: list[int]) -> None:
        """
        Call this once you have enough resolved trades.
        Upgrades to IsotonicCalibrator.
        """
        from ..calibration.calibrator import IsotonicCalibrator
        cal = IsotonicCalibrator()
        cal.fit(p_predicted, outcomes)
        if cal.is_fitted():
            self.calibrator = cal
            print(f"[strategy] Upgraded to IsotonicCalibrator with {len(p_predicted)} samples")
