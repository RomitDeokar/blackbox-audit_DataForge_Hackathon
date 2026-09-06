"""Fixed test fixtures shared by the agents, harness, and reports.

These values are a contract: the acceptance test measures interruption offsets
relative to the gating word inside CONFIRMATION_PHRASE, so changing the phrase
invalidates any previously recorded sweep.
"""

from __future__ import annotations

RESTAURANT_NAME = "BlackBox Bistro"
TEST_CUSTOMER_NAME = "Amitoj"
TEST_PARTY_SIZE = 4
TEST_BOOKING_TIME = "7:00 PM"
TEST_BOOKING_REQUEST = "Book a table for 4 at 7 PM."
CONFIRMATION_PHRASE = "Okay, you're confirmed for a table for 4 at 7 PM."
INTERRUPTION_PHRASE = "Wait! Make that 8 PM instead."

# The word whose completed playback authorises the database commit. Everything
# in the acceptance sweep is measured relative to the END of this word.
CONFIRMATION_GATING_WORD = "confirmed"

# Template the agents speak. Both variants MUST use the identical sentence so
# the only difference between them is the commit gating policy.
CONFIRMATION_TEMPLATE = "Okay, you're confirmed for a table of {party_size} at {time_str}."

# --- Acceptance-test parameters (docs/ACCEPTANCE_TEST.md) ---------------
SWEEP_OFFSET_MIN_MS = -500
SWEEP_OFFSET_MAX_MS = 500
SWEEP_OFFSET_STEP_MS = 10
SWEEP_RUNS_PER_OFFSET = 3

# 101 offsets x 3 runs = 303 trials per agent variant.
SWEEP_OFFSET_COUNT = (SWEEP_OFFSET_MAX_MS - SWEEP_OFFSET_MIN_MS) // SWEEP_OFFSET_STEP_MS + 1
SWEEP_TRIALS_PER_TARGET = SWEEP_OFFSET_COUNT * SWEEP_RUNS_PER_OFFSET

# --- Agent variant names (must match shared.booking_store) --------------
VARIANT_NAIVE = "naive"
VARIANT_FENCED = "fenced"
VARIANT_DUMMY = "dummy"


def confirmation_sentence(party_size: int, time_str: str) -> str:
    """Render the locked confirmation sentence."""
    return CONFIRMATION_TEMPLATE.format(party_size=party_size, time_str=time_str)


def sweep_offsets_ms() -> list[int]:
    """The acceptance test's offset ladder, in milliseconds."""
    return list(range(SWEEP_OFFSET_MIN_MS, SWEEP_OFFSET_MAX_MS + 1, SWEEP_OFFSET_STEP_MS))
