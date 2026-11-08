"""Harness catalog and per-machine harness registry.

Built-in harness definitions live in :data:`BUILTIN_HARNESSES`. Per-machine
overrides and custom harnesses live in ``harnesses.toml`` next to the rest of
the dotagents configuration::

    [cursor]
    enabled = true

    [claude]
    enabled = true
    target_dir = "~/.claude"
    rule_style = "single"
    rule_file_name = "CLAUDE.md"

Built-ins start disabled; enable the ones this machine uses. A built-in entry
only needs the fields it overrides. Custom harnesses require ``target_dir``;
the remaining fields default to ``enabled = true``, ``rule_style = "single"``
and ``rule_file_name = "AGENTS.md"``.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

import tomli_w

from dotagents import config as config_mod

HARNESSES_FILENAME = "harnesses.toml"
HARNESS_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

RuleStyle = Literal["mdc", "single"]


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    """A harness definition, with paths resolved against the config dir."""

    name: str
    enabled: bool = True
    target_dir: Path | None = None
    rule_style: RuleStyle = "single"
    rule_file_name: str = "AGENTS.md"


BUILTIN_HARNESSES: dict[str, HarnessSpec] = {
    "cursor": HarnessSpec(
        name="cursor",
        enabled=False,
        target_dir=Path("~/.cursor"),
        rule_style="mdc",
        rule_file_name="AGENTS.md",
    ),
    "opencode": HarnessSpec(
        name="opencode",
        enabled=False,
        target_dir=Path("~/.config/opencode"),
        rule_style="single",
        rule_file_name="AGENTS.md",
    ),
    "codex": HarnessSpec(
        name="codex",
        enabled=False,
        target_dir=Path("~/.codex"),
        rule_style="single",
        rule_file_name="AGENTS.md",
    ),
}

_ALLOWED_KEYS: frozenset[str] = frozenset({"enabled", "target_dir", "rule_style", "rule_file_name"})
_RULE_STYLES: frozenset[str] = frozenset({"mdc", "single"})


def harnesses_file(config_dir: Path) -> Path:
    return config_dir / HARNESSES_FILENAME


def validate_name(name: str) -> None:
    if not HARNESS_NAME_RE.match(name):
        raise config_mod.ConfigError(
            f"invalid harness name {name!r}; use letters, digits, '-' or '_'"
        )


def validate_rule_file_name(name: str) -> None:
    if not name or name != name.strip():
        raise config_mod.ConfigError("rule_file_name must be a non-empty file name")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise config_mod.ConfigError(f"rule_file_name must be a file name, not a path: {name!r}")


def _read_tables(config_dir: Path) -> dict[str, dict[str, object]]:
    path = harnesses_file(config_dir)
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            data: object = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise config_mod.ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise config_mod.ConfigError(f"invalid TOML in {path}: top level must be a table")
    tables: dict[str, dict[str, object]] = {}
    for name, raw in data.items():
        if not isinstance(raw, dict):
            raise config_mod.ConfigError(f"{path}: [{name}] must be a table")
        tables[str(name)] = {str(key): value for key, value in raw.items()}
    return tables


def _read_enabled(raw: object, origin: str) -> bool:
    if not isinstance(raw, bool):
        raise config_mod.ConfigError(f"{origin}: 'enabled' must be a boolean")
    return raw


def _read_target(raw: object, origin: str, config_dir: Path) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise config_mod.ConfigError(f"{origin}: 'target_dir' must be a non-empty path string")
    return config_mod.resolve_path(raw, config_dir)


def _read_style(raw: object, origin: str) -> RuleStyle:
    if not isinstance(raw, str) or raw not in _RULE_STYLES:
        raise config_mod.ConfigError(
            f"{origin}: 'rule_style' must be one of {sorted(_RULE_STYLES)}"
        )
    return cast("RuleStyle", raw)


def _read_rule_file_name(raw: object, origin: str) -> str:
    if not isinstance(raw, str):
        raise config_mod.ConfigError(f"{origin}: 'rule_file_name' must be a string")
    validate_rule_file_name(raw)
    return raw


def _spec_from_table(
    name: str,
    raw: dict[str, object],
    base: HarnessSpec | None,
    config_dir: Path,
) -> HarnessSpec:
    validate_name(name)
    origin = f"{HARNESSES_FILENAME}: [{name}]"
    unknown = set(raw) - _ALLOWED_KEYS
    if unknown:
        raise config_mod.ConfigError(
            f"{origin}: unknown key(s) {sorted(unknown)}; allowed: {sorted(_ALLOWED_KEYS)}"
        )

    enabled = base.enabled if base is not None else True
    if "enabled" in raw:
        enabled = _read_enabled(raw["enabled"], origin)

    if "target_dir" in raw:
        target_dir: Path | None = _read_target(raw["target_dir"], origin, config_dir)
    elif base is not None and base.target_dir is not None:
        target_dir = config_mod.resolve_path(base.target_dir, config_dir)
    else:
        raise config_mod.ConfigError(f"{origin}: custom harness requires 'target_dir'")

    rule_style = base.rule_style if base is not None else "single"
    if "rule_style" in raw:
        rule_style = _read_style(raw["rule_style"], origin)

    rule_file_name = base.rule_file_name if base is not None else "AGENTS.md"
    if "rule_file_name" in raw:
        rule_file_name = _read_rule_file_name(raw["rule_file_name"], origin)

    return HarnessSpec(
        name=name,
        enabled=enabled,
        target_dir=target_dir,
        rule_style=rule_style,
        rule_file_name=rule_file_name,
    )


def read_harnesses(config_dir: Path) -> dict[str, HarnessSpec]:
    """Return the effective built-in + custom harness definitions."""
    tables = _read_tables(config_dir)
    specs: dict[str, HarnessSpec] = {}
    for name, builtin in BUILTIN_HARNESSES.items():
        specs[name] = _spec_from_table(name, tables.get(name, {}), builtin, config_dir)
    for name, raw in tables.items():
        if name in specs:
            continue
        specs[name] = _spec_from_table(name, raw, None, config_dir)
    return specs


def _spec_table(spec: HarnessSpec, config_dir: Path) -> dict[str, object]:
    builtin = BUILTIN_HARNESSES.get(spec.name)
    if builtin is None:
        if spec.target_dir is None:
            raise config_mod.ConfigError(f"harness {spec.name!r} has no target_dir")
        return {
            "enabled": spec.enabled,
            "target_dir": str(spec.target_dir),
            "rule_style": spec.rule_style,
            "rule_file_name": spec.rule_file_name,
        }

    table: dict[str, object] = {}
    if spec.enabled != builtin.enabled:
        table["enabled"] = spec.enabled
    default_target = (
        config_mod.resolve_path(builtin.target_dir, config_dir)
        if builtin.target_dir is not None
        else None
    )
    if spec.target_dir != default_target:
        if spec.target_dir is None:
            raise config_mod.ConfigError(f"harness {spec.name!r} has no target_dir")
        table["target_dir"] = str(spec.target_dir)
    if spec.rule_style != builtin.rule_style:
        table["rule_style"] = spec.rule_style
    if spec.rule_file_name != builtin.rule_file_name:
        table["rule_file_name"] = spec.rule_file_name
    return table


def write_harnesses(config_dir: Path, specs: Sequence[HarnessSpec]) -> None:
    """Persist effective specs, omitting built-in defaults."""
    data: dict[str, dict[str, object]] = {}
    for spec in specs:
        table = _spec_table(spec, config_dir)
        if table:
            data[spec.name] = table
    path = harnesses_file(config_dir)
    if not data:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        tomli_w.dumps(dict(sorted(data.items()))),
        encoding="utf-8",
    )


def add_harness(config_dir: Path, spec: HarnessSpec) -> HarnessSpec:
    validate_name(spec.name)
    if spec.name in BUILTIN_HARNESSES:
        raise config_mod.ConfigError(
            f"{spec.name!r} is a built-in harness; use enable/disable instead"
        )
    if spec.target_dir is None:
        raise config_mod.ConfigError("custom harness requires a target directory")
    validate_rule_file_name(spec.rule_file_name)
    specs = read_harnesses(config_dir)
    if spec.name in specs:
        raise config_mod.ConfigError(f"harness already declared: {spec.name}")
    specs[spec.name] = spec
    write_harnesses(config_dir, list(specs.values()))
    return spec


def remove_harness(config_dir: Path, name: str) -> HarnessSpec:
    if name in BUILTIN_HARNESSES:
        raise config_mod.ConfigError(
            f"{name!r} is a built-in harness; disable it instead of removing it"
        )
    specs = read_harnesses(config_dir)
    if name not in specs:
        raise config_mod.ConfigError(f"harness not declared: {name}")
    spec = specs.pop(name)
    write_harnesses(config_dir, list(specs.values()))
    return spec


def set_enabled(config_dir: Path, name: str, enabled: bool) -> HarnessSpec:
    specs = read_harnesses(config_dir)
    if name not in specs:
        raise config_mod.ConfigError(f"unknown harness: {name}")
    updated = replace(specs[name], enabled=enabled)
    specs[name] = updated
    write_harnesses(config_dir, list(specs.values()))
    return updated
