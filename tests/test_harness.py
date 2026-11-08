"""Tests for dotagents.harness."""

from __future__ import annotations

from pathlib import Path

import pytest

from dotagents import config, harness


def test_builtin_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    specs = harness.read_harnesses(tmp_path / "cfg")
    assert set(specs) == {"cursor", "opencode", "codex"}
    assert specs["cursor"].rule_style == "mdc"
    assert specs["cursor"].target_dir == tmp_path / ".cursor"
    assert specs["opencode"].rule_style == "single"
    assert specs["opencode"].rule_file_name == "AGENTS.md"
    assert all(not spec.enabled for spec in specs.values())


def test_enable_builtin_writes_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config_dir = tmp_path / "cfg"
    harness.set_enabled(config_dir, "cursor", True)
    text = (config_dir / harness.HARNESSES_FILENAME).read_text(encoding="utf-8")
    assert "enabled = true" in text
    assert "target_dir" not in text
    assert harness.read_harnesses(config_dir)["cursor"].enabled is True

    harness.set_enabled(config_dir, "cursor", False)
    assert not (config_dir / harness.HARNESSES_FILENAME).exists()


def test_custom_roundtrip(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    spec = harness.HarnessSpec(
        name="claude",
        target_dir=tmp_path / "claude",
        rule_style="single",
        rule_file_name="CLAUDE.md",
    )
    harness.add_harness(config_dir, spec)
    loaded = harness.read_harnesses(config_dir)["claude"]
    assert loaded == spec


def test_add_rejects_builtin_duplicate_and_missing_target(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    builtin = harness.HarnessSpec(name="cursor", target_dir=tmp_path / "x")
    with pytest.raises(config.ConfigError, match="built-in"):
        harness.add_harness(config_dir, builtin)

    no_target = harness.HarnessSpec(name="custom")
    with pytest.raises(config.ConfigError, match="target"):
        harness.add_harness(config_dir, no_target)

    spec = harness.HarnessSpec(name="custom", target_dir=tmp_path / "custom")
    harness.add_harness(config_dir, spec)
    with pytest.raises(config.ConfigError, match="already declared"):
        harness.add_harness(config_dir, spec)


def test_remove_builtin_and_custom(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    with pytest.raises(config.ConfigError, match="built-in"):
        harness.remove_harness(config_dir, "cursor")

    spec = harness.HarnessSpec(name="custom", target_dir=tmp_path / "custom")
    harness.add_harness(config_dir, spec)
    assert harness.remove_harness(config_dir, "custom") == spec
    assert "custom" not in harness.read_harnesses(config_dir)


def test_set_enabled_unknown(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="unknown harness"):
        harness.set_enabled(tmp_path / "cfg", "nope", False)


def test_invalid_name(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="invalid harness name"):
        harness.add_harness(
            tmp_path / "cfg",
            harness.HarnessSpec(name="bad name", target_dir=tmp_path / "x"),
        )


def test_invalid_rule_file_name(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="file name"):
        harness.add_harness(
            tmp_path / "cfg",
            harness.HarnessSpec(
                name="custom",
                target_dir=tmp_path / "x",
                rule_file_name="dir/AGENTS.md",
            ),
        )


def test_unknown_key_rejected(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / harness.HARNESSES_FILENAME).write_text(
        '[custom]\ntarget_dir = "x"\nbogus = true\n', encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="unknown key"):
        harness.read_harnesses(config_dir)


def test_invalid_toml_rejected(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / harness.HARNESSES_FILENAME).write_text("[bad\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="invalid TOML"):
        harness.read_harnesses(config_dir)


def test_custom_missing_target_rejected(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / harness.HARNESSES_FILENAME).write_text(
        '[custom]\nrule_style = "single"\n', encoding="utf-8"
    )
    with pytest.raises(config.ConfigError, match="target_dir"):
        harness.read_harnesses(config_dir)


def test_writing_defaults_removes_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config_dir = tmp_path / "cfg"
    specs = harness.read_harnesses(config_dir)
    harness.write_harnesses(config_dir, list(specs.values()))
    assert not (config_dir / harness.HARNESSES_FILENAME).exists()
