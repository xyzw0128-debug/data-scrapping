"""Configuration loading helpers for the collector MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProviderConfig:
    """Provider settings used by the collector."""

    name: str
    enabled: bool
    api_key_env: str
    api_key_envs: tuple[str, ...] = field(default_factory=tuple)
    daily_limit: int = 0
    daily_reserve: int = 0
    per_minute_limit: int = 0
    max_symbols_per_run: int = 0
    base_url: str = ""
    interval: str = "1day"
    outputsize: int | str = 5000

    @property
    def usable_daily_budget(self) -> int:
        """Return the daily calls/credits that can be used without touching reserve."""
        return max(0, self.daily_limit - self.daily_reserve)

    def resolved_key_envs(self) -> list[str]:
        """Return the ordered list of API key env var names for this provider.

        Prefers api_key_envs if set, otherwise falls back to [api_key_env].
        Deduplicates while preserving order.
        """
        envs: list[str] = []
        seen: set[str] = set()
        candidates = list(self.api_key_envs) if self.api_key_envs else [self.api_key_env]
        for env in candidates:
            env = env.strip()
            if env and env not in seen:
                seen.add(env)
                envs.append(env)
        return envs or [self.api_key_env]


def _coerce_scalar(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value.strip().isdigit():
        return int(value.strip())
    return value.strip()


def _load_simple_yaml(path: Path) -> dict[str, dict[str, Any]]:
    """Load the tiny subset of YAML used by config/providers.yaml."""
    result: dict[str, dict[str, Any]] = {}
    current_section: str | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw_line.startswith(" ") and stripped.endswith(":"):
            current_section = stripped[:-1]
            result[current_section] = {}
            continue
        if current_section is None or ":" not in stripped:
            raise ValueError(f"Unsupported providers.yaml line: {raw_line}")
        key, value = stripped.split(":", 1)
        result[current_section][key.strip()] = _coerce_scalar(value)

    return result


def load_symbols(path: Path) -> list[str]:
    """Load enabled symbols from a simple one-symbol-per-line file."""
    symbols: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        symbol = line.strip()
        if not symbol or symbol.startswith("#"):
            continue
        symbols.append(symbol)
    return symbols


def load_provider_config(path: Path, provider_name: str) -> ProviderConfig:
    """Load one provider configuration by name."""
    providers = _load_simple_yaml(path)
    if provider_name not in providers:
        available = ", ".join(sorted(providers)) or "none"
        raise ValueError(f"Provider {provider_name!r} not found. Available: {available}")

    raw = providers[provider_name]

    # Parse api_key_envs: comma-separated string → tuple of env var names.
    raw_envs = str(raw.get("api_key_envs", "")).strip()
    api_key_envs: tuple[str, ...] = tuple(
        e.strip() for e in raw_envs.split(",") if e.strip()
    ) if raw_envs else ()

    return ProviderConfig(
        name=provider_name,
        enabled=bool(raw.get("enabled", False)),
        api_key_env=str(raw.get("api_key_env", "")),
        api_key_envs=api_key_envs,
        daily_limit=int(raw.get("daily_limit", 0)),
        daily_reserve=int(raw.get("daily_reserve", 0)),
        per_minute_limit=int(raw.get("per_minute_limit", 0)),
        max_symbols_per_run=int(raw.get("max_symbols_per_run", 0)),
        base_url=str(raw.get("base_url", "")),
        interval=str(raw.get("interval", "1day")),
        outputsize=raw.get("outputsize", 5000),
    )