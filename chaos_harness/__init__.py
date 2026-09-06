"""Chaos harness: the barge-in fuzzer and state-integrity auditor.

Public surface:

* :mod:`chaos_harness.injector`  -- timestamp-keyed barge-in arithmetic.
* :mod:`chaos_harness.targets`   -- the naive / fenced / dummy backends.
* :mod:`chaos_harness.driver`    -- the sweep engine.
* :mod:`chaos_harness.trial_log` -- the frozen JSONL log schema.
* :mod:`chaos_harness.cli`       -- ``python -m chaos_harness.cli``.
"""

from __future__ import annotations

__all__ = [
    "BargeInPlan",
    "SweepConfig",
    "SweepResult",
    "TrialRecord",
    "get_target",
    "plan_barge_in",
    "run_sweep",
]


def __getattr__(name: str):  # pragma: no cover - lazy import shim
    """Lazily re-export so ``import chaos_harness`` stays cheap.

    Importing the CLI pulls in click and rich; a test that only needs the
    injector arithmetic should not pay for that.
    """
    if name in ("BargeInPlan", "plan_barge_in"):
        from . import injector

        return getattr(injector, name)
    if name in ("SweepConfig", "SweepResult", "run_sweep"):
        from . import driver

        return getattr(driver, name)
    if name == "TrialRecord":
        from .trial_log import TrialRecord

        return TrialRecord
    if name == "get_target":
        from .targets import get_target

        return get_target
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
