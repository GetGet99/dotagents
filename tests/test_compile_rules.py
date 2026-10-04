"""Tests for scripts/compile_rules.py."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compile_rules  # noqa: E402
from compile_rules import (  # noqa: E402
    CompileConfig,
    applies_to,
    demote_headings,
    load_config,
    load_rules,
    normalize_globs,
    parse_rule,
    render_agents_md,
    sync_cursor_symlinks,
    write_if_changed,
)


def write_rule(tmp_path: Path, name: str, frontmatter: str, body: str) -> Path:
    p = tmp_path / f"{name}.mdc"
    p.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return p


# --- frontmatter ------------------------------------------------------------


def test_singular_platform_key_rejected(tmp_path: Path) -> None:
    p = write_rule(tmp_path, "r", "platform:\n- cursor", "# T\n\nbody")
    with pytest.raises(ValueError, match="platforms"):
        parse_rule(p, tmp_path)


def test_globs_string_vs_list_equal(tmp_path: Path) -> None:
    a = write_rule(tmp_path, "a", 'globs: "*.tsx, src/**"', "# T\n\nb")
    b = write_rule(tmp_path, "b", 'globs:\n- "*.tsx"\n- src/**', "# T\n\nb")
    assert parse_rule(a, tmp_path).globs == ("*.tsx", "src/**")
    assert parse_rule(b, tmp_path).globs == ("*.tsx", "src/**")


def test_globs_absent_is_empty(tmp_path: Path) -> None:
    p = write_rule(tmp_path, "r", "alwaysApply: true", "# T\n\nb")
    assert parse_rule(p, tmp_path).globs == ()


def test_normalize_globs_invalid_type(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="globs"):
        normalize_globs(123, tmp_path)


def test_platforms_empty_means_everywhere(tmp_path: Path) -> None:
    p = write_rule(tmp_path, "r", "platforms: []", "# T\n\nb")
    rule = parse_rule(p, tmp_path)
    assert rule.platforms is None
    assert applies_to(rule, "cursor")
    assert applies_to(rule, "opencode")


def test_unknown_platform_rejected(tmp_path: Path) -> None:
    p = write_rule(tmp_path, "r", "platforms: [vim]", "# T\n\nb")
    with pytest.raises(ValueError, match="unknown platform"):
        parse_rule(p, tmp_path)


def test_always_apply_must_be_bool(tmp_path: Path) -> None:
    p = write_rule(tmp_path, "r", 'alwaysApply: "yes"', "# T\n\nb")
    with pytest.raises(ValueError, match="alwaysApply"):
        parse_rule(p, tmp_path)


# --- filtering / rendering --------------------------------------------------


def make_rule(
    tmp_path: Path,
    slug: str,
    *,
    always: bool = False,
    platforms: tuple[str, ...] | None = None,
    globs: tuple[str, ...] = (),
    description: str | None = "desc",
    body: str = "# Title\n\ncontent",
) -> compile_rules.Rule:
    return compile_rules.Rule(
        slug=slug,
        source_path=tmp_path / f"{slug}.mdc",
        body=body,
        title="Title",
        always_apply=always,
        description=description,
        platforms=platforms,
        globs=globs,
    )


def test_whitelist_filter(tmp_path: Path) -> None:
    cursor_only = make_rule(tmp_path, "c", platforms=("cursor",))
    assert applies_to(cursor_only, "cursor")
    assert not applies_to(cursor_only, "opencode")
    everywhere = make_rule(tmp_path, "e", platforms=None)
    assert applies_to(everywhere, "codex")


def test_render_platform_excludes_others(tmp_path: Path) -> None:
    rules = [
        make_rule(tmp_path, "cursor-only", always=True, platforms=("cursor",)),
        make_rule(tmp_path, "open", always=True),
    ]
    out = render_agents_md(rules, "opencode")
    assert "## open" in out
    assert "cursor-only" not in out


def test_always_inline_with_applies_to(tmp_path: Path) -> None:
    rule = make_rule(
        tmp_path, "r", always=True, globs=("*.tsx",), body="# T\n\ntext"
    )
    out = render_agents_md([rule], "opencode")
    assert "> Applies to: `*.tsx`" in out
    assert "### T" in out  # demoted H1
    assert "text" in out


def test_advertise_single_line_description_inline(tmp_path: Path) -> None:
    rule = make_rule(tmp_path, "r", always=False, description="one-liner")
    out = render_agents_md([rule], "opencode")
    assert "one-liner" in out
    assert "```" not in out
    assert str(rule.source_path) in out


def test_advertise_multiline_description_fenced(tmp_path: Path) -> None:
    rule = make_rule(tmp_path, "r", always=False, description="line1\nline2")
    out = render_agents_md([rule], "opencode")
    assert "```\nline1\nline2\n```" in out


def test_advertise_globs_pointer(tmp_path: Path) -> None:
    rule = make_rule(
        tmp_path, "r", always=False, globs=("*.tsx",), description="d"
    )
    out = render_agents_md([rule], "opencode")
    assert "Globs: `*.tsx`" in out
    assert "matching those globs" in out


def test_demote_headings() -> None:
    assert demote_headings("# A") == "### A"
    assert demote_headings("## B") == "#### B"
    assert demote_headings("###### Deep") == "###### Deep"  # clamped
    assert demote_headings("plain") == "plain"


def test_sort_always_first(tmp_path: Path) -> None:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "z-ad.mdc").write_text(
        "---\nalwaysApply: false\ndescription: d\n---\n\n# Z\n", encoding="utf-8"
    )
    (rules_dir / "a-always.mdc").write_text(
        "---\nalwaysApply: true\n---\n\n# A\n", encoding="utf-8"
    )
    rules = load_rules(rules_dir, tmp_path)
    assert [r.slug for r in rules] == ["a-always", "z-ad"]


# --- outputs ----------------------------------------------------------------


def test_write_if_changed_no_rewrite(tmp_path: Path) -> None:
    p = tmp_path / "out.md"
    assert write_if_changed(p, "hi") is True
    assert write_if_changed(p, "hi") is False
    assert write_if_changed(p, "hi", dry_run=True) is False


def test_sync_cursor_never_deletes_regular_files(tmp_path: Path) -> None:
    src = tmp_path / "rules"
    src.mkdir()
    target = src / "keep.mdc"
    target.write_text("---\n---\n\n# K\n", encoding="utf-8")
    rule = parse_rule(target, tmp_path)
    assert applies_to(rule, "cursor")

    cursor_dir = tmp_path / "cursor-rules"
    cursor_dir.mkdir()
    regular = cursor_dir / "keep.mdc"
    regular.write_text("user content", encoding="utf-8")
    stale_regular = cursor_dir / "old.mdc"
    stale_regular.write_text("user content", encoding="utf-8")

    created, _, pruned = sync_cursor_symlinks([rule], cursor_dir)
    # Regular files untouched: no link created over them, nothing pruned.
    assert regular.read_text(encoding="utf-8") == "user content"
    assert stale_regular.exists()
    assert created == 0
    assert pruned == 0


def test_sync_cursor_prunes_stale_symlinks(tmp_path: Path) -> None:
    cursor_dir = tmp_path / "c"
    cursor_dir.mkdir()
    stale = cursor_dir / "gone.mdc"
    stale.symlink_to(tmp_path / "nonexistent.mdc")
    created, kept, pruned = sync_cursor_symlinks([], cursor_dir)
    # No applicable rules -> dir left untouched, no creation/pruning.
    assert (created, kept, pruned) == (0, 0, 0)
    assert stale.is_symlink()  # untouched


def test_disabled_platform_touches_nothing(tmp_path: Path) -> None:
    config = CompileConfig(
        repo_root=tmp_path,
        rules_dir=tmp_path / "rules",
        enabled=frozenset({"opencode"}),
        opencode_out=tmp_path / "autogen-opencode.md",
        codex_out=None,
        cursor_rules_dir=None,
    )
    assert config.codex_out is None
    assert config.cursor_rules_dir is None


def test_load_config_defaults_to_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = load_config(tmp_path)
    assert config.enabled == frozenset({"cursor", "opencode", "codex"})
    assert config.opencode_out is not None
    assert config.cursor_rules_dir is not None
    # Must not create anything as a side effect.
    assert not config.cursor_rules_dir.exists()


def test_load_config_local_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "dotagents.toml").write_text(
        '[platforms]\nenabled = ["opencode", "codex", "cursor"]\n',
        encoding="utf-8",
    )
    (tmp_path / "dotagents.local.toml").write_text(
        '[platforms]\nenabled = ["opencode"]\n', encoding="utf-8"
    )
    monkeypatch.delenv("DOTAGENTS_PLATFORMS", raising=False)
    config = load_config(tmp_path)
    assert config.enabled == frozenset({"opencode"})
    assert config.cursor_rules_dir is None  # cursor disabled -> None


def test_load_config_env_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOTAGENTS_PLATFORMS", "codex")
    config = load_config(tmp_path)
    assert config.enabled == frozenset({"codex"})
    config2 = load_config(tmp_path, cli_platforms="opencode,codex")
    assert config2.enabled == frozenset({"opencode", "codex"})
    config3 = load_config(tmp_path, cli_platforms="all")
    assert config3.enabled == frozenset({"cursor", "opencode", "codex"})
    _ = os.environ.get("DOTAGENTS_PLATFORMS")
