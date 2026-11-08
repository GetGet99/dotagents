"""Global dotagents configuration and source registry.

Settings live at ``${XDG_CONFIG_HOME:-~/.config}/dotagents/config.toml``.
Registered content sources live next to it in ``sources``: one path per line,
managed by the CLI. Source order is preserved for deterministic builds;
duplicate rule/skill names across sources are rejected by the compiler.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import cast

CONFIG_DIR_ENV = "DOTAGENTS_CONFIG_DIR"
CONFIG_FILENAME = "config.toml"
SOURCES_FILENAME = "sources"


class ConfigError(ValueError):
    """Raised for invalid configuration or source registration."""


def default_config_dir() -> Path:
    """Per-user config directory, honoring ``DOTAGENTS_CONFIG_DIR``."""
    override = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "dotagents"


def config_file(config_dir: Path) -> Path:
    return config_dir / CONFIG_FILENAME


def sources_file(config_dir: Path) -> Path:
    return config_dir / SOURCES_FILENAME


def resolve_path(value: str | Path, base: Path) -> Path:
    """Expand ``~`` and resolve relative paths against *base*."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def _canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def read_settings(config_dir: Path) -> dict[str, object]:
    """Load ``config.toml``; a missing file yields an empty mapping."""
    path = config_file(config_dir)
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            data: object = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"invalid TOML in {path}: top level must be a table")
    return cast("dict[str, object]", data)


def read_sources(config_dir: Path) -> list[Path]:
    """Registered source directories in registration order."""
    path = sources_file(config_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    sources: list[Path] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        sources.append(_canonical(resolve_path(stripped, config_dir)))
    return sources


def write_sources(config_dir: Path, sources: Sequence[Path]) -> None:
    path = sources_file(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{source}\n" for source in sources), encoding="utf-8")


def add_source(config_dir: Path, source: Path) -> Path:
    """Validate and append a source; returns the canonical path."""
    resolved = _canonical(source)
    if not resolved.is_dir():
        raise ConfigError(f"source is not a directory: {resolved}")
    sources = read_sources(config_dir)
    if resolved in sources:
        raise ConfigError(f"source already registered: {resolved}")
    sources.append(resolved)
    write_sources(config_dir, sources)
    return resolved


def remove_source(config_dir: Path, source: Path) -> Path:
    """Remove a registered source; returns the canonical path."""
    resolved = _canonical(source)
    sources = read_sources(config_dir)
    if resolved not in sources:
        raise ConfigError(f"source not registered: {resolved}")
    sources.remove(resolved)
    write_sources(config_dir, sources)
    return resolved
