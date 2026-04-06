from __future__ import annotations
from ..probability_model.naive import NaiveModel
from ..calibration.calibrator import IdentityCalibrator
from ..config import BotConfig

class DefaultStrategy:
    """
    Default strategy: NaiveModel + IdentityCalibrator.

    The NaiveModel uses market mid-price as baseline probability
    and applies small adjustments for depth imbalance and volume.

    Replace this with a trained model when you have enough historical data.
    See strategy/README for instructions.
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.model = NaiveModel(
            depth_weight=0.03,
            volume_weight=0.01,
        )
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
