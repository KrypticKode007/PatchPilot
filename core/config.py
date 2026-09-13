"""
PatchPilot configuration.

All tunables are environment-variable driven, matching the documented
PATCHPILOT_* variables. No config files required.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    if raw.strip() == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


@dataclass(frozen=True)
class Settings:
    host: str = field(default_factory=lambda: _env_str("PATCHPILOT_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PATCHPILOT_PORT", 8000))

    upload_dir: Path = field(
        default_factory=lambda: Path(_env_str("PATCHPILOT_UPLOAD_DIR", "./uploads"))
    )
    output_dir: Path = field(
        default_factory=lambda: Path(_env_str("PATCHPILOT_OUTPUT_DIR", "./outputs"))
    )
    temp_dir: Path = field(
        default_factory=lambda: Path(_env_str("PATCHPILOT_TEMP_DIR", "./tmp"))
    )

    max_upload_mb: int = field(
        default_factory=lambda: _env_int("PATCHPILOT_MAX_UPLOAD_MB", 200)
    )
    max_workers: int = field(
        default_factory=lambda: _env_int("PATCHPILOT_MAX_WORKERS", 1)
    )
    queue_maxsize: int = field(
        default_factory=lambda: _env_int("PATCHPILOT_QUEUE_MAXSIZE", 50)
    )
    timeout_seconds: int = field(
        default_factory=lambda: _env_int("PATCHPILOT_TIMEOUT_SECONDS", 1800)
    )
    matchering_cli: str = field(
        default_factory=lambda: _env_str("PATCHPILOT_MATCHERING_CLI", "mg_cli.py")
    )
    cors_origins: list[str] = field(
        default_factory=lambda: _env_list("PATCHPILOT_CORS_ORIGINS", "localhost")
    )

    allowed_extensions: frozenset[str] = frozenset(
        {".wav", ".flac", ".mp3", ".aiff", ".aif", ".ogg", ".m4a"}
    )

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def ensure_directories(self) -> None:
        for d in (self.upload_dir, self.output_dir, self.temp_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()