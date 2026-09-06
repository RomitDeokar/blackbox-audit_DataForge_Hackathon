"""Environment-driven configuration shared by both agent variants.

Nothing here is hardcoded to a provider: swapping STT/LLM/TTS vendors is an
env-var change. Missing keys raise a single readable error listing everything
that is absent, rather than failing one variable at a time deep inside a
plugin constructor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from .constants import CONFIRMATION_GATING_WORD

__all__ = [
    "AgentConfig",
    "MissingConfigError",
    "load_env",
]

_ENV_LOADED = False


def load_env(override: bool = False) -> None:
    """Load ``.env`` once per process. Safe to call repeatedly."""
    global _ENV_LOADED
    if _ENV_LOADED and not override:
        return
    load_dotenv(override=override)
    _ENV_LOADED = True


class MissingConfigError(RuntimeError):
    """Raised when required environment variables are absent."""


@dataclass(frozen=True)
class AgentConfig:
    """Resolved model + credential configuration for an agent variant."""

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    rime_api_key: str = ""
    deepgram_api_key: str = ""
    openai_api_key: str = ""

    stt_model: str = "nova-3"
    llm_model: str = "gpt-4o-mini"
    tts_model: str = "mistv2"
    tts_speaker: str = "astra"

    gating_word: str = CONFIRMATION_GATING_WORD
    db_path: str = "./results/booking.db"

    _missing: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_env(cls) -> AgentConfig:
        """Build a config from the environment without raising.

        Use :meth:`require` to assert what a given code path actually needs --
        the offline harness needs no keys at all, so failing at import time
        would make the free paths unusable.
        """
        load_env()

        def get(name: str, default: str = "") -> str:
            return (os.environ.get(name) or default).strip()

        values = {
            "livekit_url": get("LIVEKIT_URL"),
            "livekit_api_key": get("LIVEKIT_API_KEY"),
            "livekit_api_secret": get("LIVEKIT_API_SECRET"),
            "rime_api_key": get("RIME_API_KEY"),
            "deepgram_api_key": get("DEEPGRAM_API_KEY"),
            "openai_api_key": get("OPENAI_API_KEY"),
            "stt_model": get("STT_MODEL", "nova-3"),
            "llm_model": get("LLM_MODEL", "gpt-4o-mini"),
            "tts_model": get("RIME_MODEL", "mistv2"),
            "tts_speaker": get("RIME_SPEAKER", "astra"),
            "gating_word": get("GATING_WORD", CONFIRMATION_GATING_WORD),
            "db_path": get("DB_PATH", "./results/booking.db"),
        }

        env_names = {
            "livekit_url": "LIVEKIT_URL",
            "livekit_api_key": "LIVEKIT_API_KEY",
            "livekit_api_secret": "LIVEKIT_API_SECRET",
            "rime_api_key": "RIME_API_KEY",
            "deepgram_api_key": "DEEPGRAM_API_KEY",
            "openai_api_key": "OPENAI_API_KEY",
        }
        missing = tuple(name for key, name in env_names.items() if not values[key])
        return cls(**values, _missing=missing)

    # -- assertions ------------------------------------------------------

    @property
    def missing(self) -> tuple[str, ...]:
        """Names of required credentials that are absent."""
        return self._missing

    def require(self, *names: str) -> None:
        """Raise if any of the named environment variables is missing.

        ``config.require("RIME_API_KEY")`` before a Rime call; a live agent
        calls ``require_live()``.
        """
        absent = [n for n in names if n in self._missing]
        if absent:
            raise MissingConfigError(
                "missing required environment variable(s): "
                + ", ".join(absent)
                + " — copy .env.example to .env and fill them in"
            )

    def require_live(self) -> None:
        """Assert everything a live LiveKit voice session needs."""
        self.require(
            "LIVEKIT_URL",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
            "RIME_API_KEY",
            "DEEPGRAM_API_KEY",
            "OPENAI_API_KEY",
        )

    def redacted(self) -> dict[str, str]:
        """Config summary safe to print or log -- secrets are never revealed."""

        def mask(value: str) -> str:
            return f"set({len(value)} chars)" if value else "MISSING"

        return {
            "LIVEKIT_URL": self.livekit_url or "MISSING",
            "LIVEKIT_API_KEY": mask(self.livekit_api_key),
            "LIVEKIT_API_SECRET": mask(self.livekit_api_secret),
            "RIME_API_KEY": mask(self.rime_api_key),
            "DEEPGRAM_API_KEY": mask(self.deepgram_api_key),
            "OPENAI_API_KEY": mask(self.openai_api_key),
            "STT_MODEL": self.stt_model,
            "LLM_MODEL": self.llm_model,
            "RIME_MODEL": self.tts_model,
            "RIME_SPEAKER": self.tts_speaker,
            "GATING_WORD": self.gating_word,
            "DB_PATH": self.db_path,
        }
