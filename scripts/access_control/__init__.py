"""Fail-closed product-plan and geography authorization primitives.

The package is intentionally transport-free. HTTP routes must authenticate a
request first, resolve it to an account ``user_id``, and then call
``access_control.evaluator.evaluate`` with an exact feature, area, and
publication lane.
"""

from .evaluator import evaluate
from .models import AccessDecision, AccessRequest

__all__ = ["AccessDecision", "AccessRequest", "evaluate"]
