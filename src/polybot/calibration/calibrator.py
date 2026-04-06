"""
Probability calibrators: correct model overconfidence/underconfidence.

Three implementations:
  - IdentityCalibrator: pass-through, no calibration
  - PlattCalibrator: logistic (Platt scaling)
  - IsotonicCalibrator: isotonic regression (Pool Adjacent Violators)

Usage:
    calibrator = IsotonicCalibrator()
    calibrator.fit(p_predicted_list, outcome_list)
    cal_pred = calibrator.calibrate(model_prediction)
"""
from __future__ import annotations

import math

from ..models import CalibratedPrediction, ModelPrediction


class IdentityCalibrator:
    """No calibration — pass predictions through unchanged."""

    method = "identity"

    def __init__(self) -> None:
        self._n_samples: int = 0

    def fit(self, p_predicted: list[float], outcomes: list[int]) -> None:
        """Record sample count only."""
        self._n_samples = len(p_predicted)

    def calibrate(self, prediction: ModelPrediction) -> CalibratedPrediction:
        """Return prediction unchanged."""
        return CalibratedPrediction(
            market_id=prediction.market_id,
            p_yes_raw=prediction.p_yes,
            p_yes_calibrated=prediction.p_yes,
            calibration_method=self.method,
            n_samples_used=self._n_samples,
        )

    def is_fitted(self) -> bool:
        """Identity calibrator is always ready."""
        return True


class PlattCalibrator:
    """
    Platt scaling: logistic regression on (p_predicted, outcome) pairs.

    Fits: p_calibrated = sigmoid(a * p_predicted + b)
    Uses gradient descent (200 steps). Works well with 5-100 samples.
    """

    method = "platt"

    def __init__(self) -> None:
        self._a: float = 1.0
        self._b: float = 0.0
        self._n_samples: int = 0
        self._fitted: bool = False

    def fit(self, p_predicted: list[float], outcomes: list[int]) -> None:
        """
        Fit logistic calibration via gradient descent.

        Requires at least 5 samples. Silently skips if fewer.
        """
        if len(p_predicted) < 5:
            return

        self._n_samples = len(p_predicted)

        # Gradient descent on logistic loss
        a, b = 1.0, 0.0
        lr = 0.1
        n = self._n_samples

        for _ in range(200):
            grad_a = 0.0
            grad_b = 0.0
            for p, y in zip(p_predicted, outcomes):
                z = a * p + b
                # Numerically stable sigmoid
                if z >= 0:
                    pred = 1.0 / (1.0 + math.exp(-z))
                else:
                    exp_z = math.exp(z)
                    pred = exp_z / (1.0 + exp_z)
                err = pred - y
                grad_a += err * p
                grad_b += err
            a -= lr * grad_a / n
            b -= lr * grad_b / n

        self._a = a
        self._b = b
        self._fitted = True

    def calibrate(self, prediction: ModelPrediction) -> CalibratedPrediction:
        """Apply Platt scaling to a model prediction."""
        if self._fitted:
            z = self._a * prediction.p_yes + self._b
            if z >= 0:
                p_cal = 1.0 / (1.0 + math.exp(-z))
            else:
                exp_z = math.exp(z)
                p_cal = exp_z / (1.0 + exp_z)
            p_cal = max(0.01, min(0.99, p_cal))
        else:
            p_cal = prediction.p_yes

        return CalibratedPrediction(
            market_id=prediction.market_id,
            p_yes_raw=prediction.p_yes,
            p_yes_calibrated=p_cal,
            calibration_method=self.method,
            n_samples_used=self._n_samples,
        )

    def is_fitted(self) -> bool:
        return self._fitted


class IsotonicCalibrator:
    """
    Isotonic regression calibrator using Pool Adjacent Violators (PAV).

    Most flexible calibrator — best for 30+ samples.
    Makes no parametric assumptions about the calibration curve shape.
    Uses piecewise-linear interpolation between breakpoints.
    """

    method = "isotonic"

    def __init__(self) -> None:
        # List of (p_avg, win_rate) breakpoints from PAV
        self._breakpoints: list[tuple[float, float]] = []
        self._n_samples: int = 0
        self._fitted: bool = False

    def fit(self, p_predicted: list[float], outcomes: list[int]) -> None:
        """
        Fit isotonic regression via Pool Adjacent Violators.

        Requires at least 10 samples. Groups samples into blocks with
        non-decreasing win rates (the isotonic constraint).
        """
        if len(p_predicted) < 10:
            return

        self._n_samples = len(p_predicted)

        # Sort by predicted probability
        pairs = sorted(zip(p_predicted, outcomes))

        # Pool Adjacent Violators algorithm
        # Each block is a list of (p, y) pairs with the same isotonic value
        blocks: list[list[tuple[float, int]]] = []

        for p, y in pairs:
            blocks.append([(p, y)])
            # Merge with previous block if it violates isotonic constraint
            while len(blocks) >= 2:
                b1 = blocks[-2]
                b2 = blocks[-1]
                avg1 = sum(o for _, o in b1) / len(b1)
                avg2 = sum(o for _, o in b2) / len(b2)
                if avg1 <= avg2:
                    break  # Constraint satisfied
                # Merge: b1 ∪ b2
                blocks[-2:] = [b1 + b2]

        # Build breakpoints: (mean_p, win_rate) for each block
        self._breakpoints = []
        for block in blocks:
            p_avg = sum(p for p, _ in block) / len(block)
            y_avg = sum(y for _, y in block) / len(block)
            self._breakpoints.append((p_avg, y_avg))

        self._fitted = True

    def calibrate(self, prediction: ModelPrediction) -> CalibratedPrediction:
        """Apply isotonic calibration via piecewise linear interpolation."""
        if not self._fitted or not self._breakpoints:
            p_cal = prediction.p_yes
        else:
            p = prediction.p_yes
            bps = self._breakpoints

            if p <= bps[0][0]:
                # Below lowest breakpoint: use leftmost value
                p_cal = bps[0][1]
            elif p >= bps[-1][0]:
                # Above highest breakpoint: use rightmost value
                p_cal = bps[-1][1]
            else:
                # Linear interpolation between surrounding breakpoints
                p_cal = prediction.p_yes  # fallback
                for i in range(len(bps) - 1):
                    p_lo, y_lo = bps[i]
                    p_hi, y_hi = bps[i + 1]
                    if p_lo <= p <= p_hi:
                        denom = p_hi - p_lo
                        if denom < 1e-10:
                            p_cal = (y_lo + y_hi) / 2.0
                        else:
                            t = (p - p_lo) / denom
                            p_cal = y_lo + t * (y_hi - y_lo)
                        break

            p_cal = max(0.01, min(0.99, p_cal))

        return CalibratedPrediction(
            market_id=prediction.market_id,
            p_yes_raw=prediction.p_yes,
            p_yes_calibrated=p_cal,
            calibration_method=self.method,
            n_samples_used=self._n_samples,
        )

    def is_fitted(self) -> bool:
        return self._fitted
