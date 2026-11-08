"""Tests for dotagents.cli."""

from __future__ import annotations

from pathlib import Path

import pytest

from dotagents import config, harness
from dotagents.cli import main


def make_source(root: Path) -> Path:
    rules = root / "rules"
    rules.mkdir(parents=True)
    (rules / "r.mdc").write_text("---\nalwaysApply: true\n---\n\n# R\n", encoding="utf-8")
    return root


def test_source_add_and_remove_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_dir = tmp_path / "cfg"
    source = make_source(tmp_path / "source")

    assert main(["--config-dir", str(config_dir), "source", "add", str(source)]) == 0
    assert config.read_sources(config_dir) == [source.resolve()]

    assert main(["--config-dir", str(config_dir), "source", "list"]) == 0
    out = capsys.readouterr().out
    assert str(source.resolve()) in out
    assert "1 rule(s)" in out

    assert main(["--config-dir", str(config_dir), "source", "remove", str(source)]) == 0
    assert config.read_sources(config_dir) == []


def test_source_add_missing_fails(tmp_path: Path) -> None:
    assert (
        main(["--config-dir", str(tmp_path / "cfg"), "source", "add", str(tmp_path / "nope")]) == 2
    )


def test_source_remove_unregistered_fails(tmp_path: Path) -> None:
    assert (
        main(
            [
                "--config-dir",
                str(tmp_path / "cfg"),
                "source",
                "remove",
                str(tmp_path / "nope"),
            ]
        )
        == 2
    )


def test_build_uses_registered_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_dir = tmp_path / "cfg"
    source = make_source(tmp_path / "source")
    assert main(["--config-dir", str(config_dir), "source", "add", str(source)]) == 0
    assert main(["--config-dir", str(config_dir), "harness", "enable", "opencode"]) == 0

    assert main(["--config-dir", str(config_dir), "build"]) == 0
    out = config_dir / "build" / "autogen-opencode.md"
    assert out.is_file()
    assert "## r" in out.read_text(encoding="utf-8")


def test_build_source_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_dir = tmp_path / "cfg"
    source = make_source(tmp_path / "source")

    assert main(["--config-dir", str(config_dir), "harness", "enable", "opencode"]) == 0
    assert (
        main(
            [
                "--config-dir",
                str(config_dir),
                "build",
                "--source",
                str(source),
            ]
        )
        == 0
    )
    assert (config_dir / "build" / "autogen-opencode.md").is_file()
    assert config.read_sources(config_dir) == []


def test_build_without_sources_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    assert main(["--config-dir", str(tmp_path / "cfg"), "build"]) == 2


def test_build_falls_back_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_dir = tmp_path / "cfg"
    source = make_source(tmp_path / "repo")
    monkeypatch.chdir(source)

    assert main(["--config-dir", str(config_dir), "harness", "enable", "opencode"]) == 0
    assert main(["--config-dir", str(config_dir), "build"]) == 0
    assert (config_dir / "build" / "autogen-opencode.md").is_file()


def test_build_duplicate_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_dir = tmp_path / "cfg"
    one = make_source(tmp_path / "one")
    two = make_source(tmp_path / "two")

    assert (
        main(
            [
                "--config-dir",
                str(config_dir),
                "build",
                "--source",
                str(one),
                "--source",
                str(two),
            ]
        )
        == 2
    )


def test_harness_list_enable_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_dir = tmp_path / "cfg"

    assert main(["--config-dir", str(config_dir), "harness", "list"]) == 0
    out = capsys.readouterr().out
    assert "cursor" in out
    assert "builtin" in out
    assert "disabled" in out

    assert main(["--config-dir", str(config_dir), "harness", "enable", "cursor"]) == 0
    assert harness.read_harnesses(config_dir)["cursor"].enabled is True
    assert main(["--config-dir", str(config_dir), "harness", "list"]) == 0
    assert "enabled" in capsys.readouterr().out

    assert main(["--config-dir", str(config_dir), "harness", "disable", "cursor"]) == 0
    assert harness.read_harnesses(config_dir)["cursor"].enabled is False


def test_harness_add_non_interactive_and_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_dir = tmp_path / "cfg"
    source = make_source(tmp_path / "source")
    target = tmp_path / "claude"

    assert main(["--config-dir", str(config_dir), "source", "add", str(source)]) == 0
    assert (
        main(
            [
                "--config-dir",
                str(config_dir),
                "harness",
                "add",
                "claude",
                "--target-dir",
                str(target),
                "--rule-style",
                "single",
                "--rule-file-name",
                "CLAUDE.md",
            ]
        )
        == 0
    )
    assert harness.read_harnesses(config_dir)["claude"].rule_file_name == "CLAUDE.md"

    assert main(["--config-dir", str(config_dir), "build"]) == 0
    link = target / "CLAUDE.md"
    assert link.is_symlink()
    assert link.resolve() == (config_dir / "build" / "autogen-claude.md").resolve()


def test_harness_add_interactive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_dir = tmp_path / "cfg"
    target = tmp_path / "claude"
    answers = iter(["claude", str(target), "n", "CLAUDE.md"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["--config-dir", str(config_dir), "harness", "add"]) == 0
    spec = harness.read_harnesses(config_dir)["claude"]
    assert spec.target_dir == target
    assert spec.rule_style == "single"
    assert spec.rule_file_name == "CLAUDE.md"


def test_harness_add_mdc_skips_rule_file_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_dir = tmp_path / "cfg"
    target = tmp_path / "myagent"
    answers = iter(["myagent", str(target), "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["--config-dir", str(config_dir), "harness", "add"]) == 0
    spec = harness.read_harnesses(config_dir)["myagent"]
    assert spec.rule_style == "mdc"
    assert spec.rule_file_name == "AGENTS.md"


def test_harness_disable_prunes_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_dir = tmp_path / "cfg"
    source = make_source(tmp_path / "source")
    target = tmp_path / "claude"

    assert main(["--config-dir", str(config_dir), "source", "add", str(source)]) == 0
    assert (
        main(
            [
                "--config-dir",
                str(config_dir),
                "harness",
                "add",
                "claude",
                "--target-dir",
                str(target),
                "--rule-style",
                "single",
                "--rule-file-name",
                "AGENTS.md",
            ]
        )
        == 0
    )
    assert main(["--config-dir", str(config_dir), "build"]) == 0
    link = target / "AGENTS.md"
    assert link.is_symlink()

    assert main(["--config-dir", str(config_dir), "harness", "disable", "claude"]) == 0
    assert not link.exists()
    assert not (config_dir / "build" / "autogen-claude.md").exists()


def test_harness_remove_builtin_fails(tmp_path: Path) -> None:
    assert main(["--config-dir", str(tmp_path / "cfg"), "harness", "remove", "cursor"]) == 2


def test_harness_remove_custom(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_dir = tmp_path / "cfg"
    target = tmp_path / "claude"
    assert (
        main(
            [
                "--config-dir",
                str(config_dir),
                "harness",
                "add",
                "claude",
                "--target-dir",
                str(target),
                "--rule-style",
                "single",
                "--rule-file-name",
                "AGENTS.md",
            ]
        )
        == 0
    )
    assert main(["--config-dir", str(config_dir), "harness", "remove", "claude"]) == 0
    assert "claude" not in harness.read_harnesses(config_dir)


def test_harness_enable_unknown_fails(tmp_path: Path) -> None:
    assert main(["--config-dir", str(tmp_path / "cfg"), "harness", "enable", "nope"]) == 2
