"""Configuration for Helix.

Everything is local-first: state lives under ``HELIX_HOME`` (default ``~/.helix``)
unless a workspace is given explicitly. Models can be remote (any OpenAI-compatible
endpoint) or fully local (Ollama / LM Studio / llama.cpp server).
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


def _default_home() -> Path:
    return Path(os.environ.get("HELIX_HOME", Path.home() / ".helix"))


class ModelConfig(BaseModel):
    """An OpenAI-compatible chat model endpoint."""

    name: str = "local"
    base_url: str = "http://localhost:11434/v1"  # Ollama default
    api_key: str = "not-needed"
    model: str = "qwen2.5-coder:7b"
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: float = 120.0


class HelixConfig(BaseModel):
    home: Path = Field(default_factory=_default_home)
    # Root directory the agent is allowed to touch. Defaults to the cwd at startup.
    workspace: Path = Field(default_factory=Path.cwd)
    models: list[ModelConfig] = Field(default_factory=lambda: [ModelConfig()])
    # Safety: require diff approval before writing to disk unless explicitly bypassed.
    require_diff_approval: bool = True
    # Shell tool timeouts.
    shell_timeout: float = 120.0
    # Max solver loop iterations before giving up.
    max_solver_steps: int = 40

    def model_post_init(self, __context: object) -> None:  # noqa: D401
        self.home = Path(self.home).expanduser().resolve()
        self.workspace = Path(self.workspace).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls) -> "HelixConfig":
        """Load config from environment, falling back to sane local defaults."""
        cfg = cls()
        # Allow HELIX_MODEL_* env overrides for the primary model.
        primary = cfg.models[0]
        primary.base_url = os.environ.get("HELIX_BASE_URL", primary.base_url)
        primary.api_key = os.environ.get("HELIX_API_KEY", primary.api_key)
        primary.model = os.environ.get("HELIX_MODEL", primary.model)
        if "HELIX_WORKSPACE" in os.environ:
            cfg.workspace = Path(os.environ["HELIX_WORKSPACE"]).expanduser().resolve()
        return cfg


# Convenience: a single process-wide config object.
CONFIG = HelixConfig.load()
