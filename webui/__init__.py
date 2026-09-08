"""Demo web UI + mobile call simulator for BlackBox Audit.

This package is a *presentation layer only*. It imports the existing project
modules (``shared.audio_fence``, ``shared.booking_store``, ``chaos_harness``,
``reporting``) and never reimplements the fencing rule, so anything shown in
the browser is produced by the same code the acceptance sweep and the live
LiveKit agents run.

Design constraints deliberately honoured here:

* **No change to the existing project structure.** Nothing outside ``webui/``
  is modified; every number rendered comes from the real modules.
* **Zero API keys required.** The mobile "call" is driven by the cached Rime
  timeline fixture plus the browser's own Web Speech API (free, on-device),
  so the demo works with no Rime/Deepgram/OpenAI/LiveKit credentials. When
  real keys are present the same screens describe the live wiring.
* **The fence is never simulated.** ``webui/service.py`` feeds the real
  ``AudioFence`` state machine and writes to the real SQLite store.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
