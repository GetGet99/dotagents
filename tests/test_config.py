"""Tests for dotagents.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from dotagents import config


def test_default_config_dir_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOTAGENTS_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.default_config_dir() == tmp_path / "xdg" / "dotagents"


def test_default_config_dir_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOTAGENTS_CONFIG_DIR", str(tmp_path / "custom"))
    assert config.default_config_dir() == tmp_path / "custom"


def test_add_read_remove_sources(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    source = tmp_path / "source"
    source.mkdir()

    added = config.add_source(config_dir, source)
    assert added == source.resolve()
    assert config.read_sources(config_dir) == [source.resolve()]
    assert (config_dir / "sources").read_text(encoding="utf-8") == f"{source.resolve()}\n"

    removed = config.remove_source(config_dir, source)
    assert removed == source.resolve()
    assert config.read_sources(config_dir) == []


def test_add_source_rejects_missing_and_duplicate(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    with pytest.raises(config.ConfigError, match="not a directory"):
        config.add_source(config_dir, tmp_path / "missing")

    source = tmp_path / "source"
    source.mkdir()
    config.add_source(config_dir, source)
    with pytest.raises(config.ConfigError, match="already registered"):
        config.add_source(config_dir, source)


def test_remove_source_rejects_unregistered(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="not registered"):
        config.remove_source(tmp_path / "cfg", tmp_path / "source")


def test_read_sources_skips_blank_and_comments(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "sources").write_text(
        f"# comment\n\n{tmp_path / 'a'}\n  {tmp_path / 'b'}  \n",
        encoding="utf-8",
    )
    assert config.read_sources(config_dir) == [
        (tmp_path / "a").resolve(strict=False),
        (tmp_path / "b").resolve(strict=False),
    ]


def test_read_settings_missing_and_invalid(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    assert config.read_settings(config_dir) == {}
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("[bad\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="invalid TOML"):
        config.read_settings(config_dir)


def test_resolve_path(tmp_path: Path) -> None:
    assert config.resolve_path("sub", tmp_path) == tmp_path / "sub"
    assert config.resolve_path(tmp_path / "abs", tmp_path) == tmp_path / "abs"
