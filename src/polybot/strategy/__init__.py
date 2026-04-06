"""
strategy/ -- Isolated strategy logic.

This package contains ONLY the probability estimation and signal logic.
It has NO dependencies on execution, risk, or storage.

To add a new strategy:
1. Implement ProbabilityModel protocol from probability_model/base.py
2. Optionally override hooks in a new file here
3. Pass your model to the cycle in run_bot.py

Current strategies:
- default.py: NaiveModel (price-implied probabilities)
"""
from .default import DefaultStrategy
__all__ = ["DefaultStrategy"]
