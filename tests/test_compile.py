"""Tests for scripts/compile.py."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compile as compile_mod  # noqa: E402
from compile import (  # noqa: E402
    CompileConfig,
    applies_to,
    demote_headings,
    load_config,
    load_rules,
    load_skills,
    normalize_globs,
    parse_rule,
    parse_skill,
    render_agents_md,
    run,
    skill_applies_to,
    sync_cursor_symlinks,
    sync_skill_links,
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
) -> compile_mod.Rule:
    return compile_mod.Rule(
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
        skills_dir=tmp_path / "skills",
        enabled=frozenset({"opencode"}),
        opencode_out=tmp_path / "autogen-opencode.md",
        codex_out=None,
        cursor_rules_dir=None,
        agents_skills_dir=tmp_path / "agents-skills",
        opencode_skills_dir=tmp_path / "opencode-skills",
        cursor_skills_dir=None,
        codex_skills_dir=None,
    )
    assert config.codex_out is None
    assert config.cursor_rules_dir is None
    assert config.codex_skills_dir is None


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


# --- skills ---------------------------------------------------------------


def write_skill(
    tmp_path: Path,
    name: str,
    frontmatter: str,
    body: str = "# Skill\n\ncontent",
) -> Path:
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return d


def test_parse_skill_platforms(tmp_path: Path) -> None:
    d = write_skill(
        tmp_path,
        "create-pet",
        "platforms:\n- codex\nname: create-pet\ndescription: make a pet",
    )
    skill = parse_skill(d, tmp_path)
    assert skill.name == "create-pet"
    assert skill.platforms == ("codex",)
    assert skill_applies_to(skill, "codex")
    assert not skill_applies_to(skill, "opencode")


def test_parse_skill_universal_without_platforms(tmp_path: Path) -> None:
    d = write_skill(tmp_path, "s", "name: s\ndescription: d")
    skill = parse_skill(d, tmp_path)
    assert skill.platforms is None


def test_parse_skill_all_platforms_is_universal(tmp_path: Path) -> None:
    d = write_skill(
        tmp_path,
        "s",
        "platforms: [cursor, opencode, codex]\nname: s\ndescription: d",
    )
    assert parse_skill(d, tmp_path).platforms is None


def test_parse_skill_requires_name_and_description(tmp_path: Path) -> None:
    d = write_skill(tmp_path, "s", "name: s")
    with pytest.raises(ValueError, match="description"):
        parse_skill(d, tmp_path)
    d2 = write_skill(tmp_path, "s2", "description: d")
    with pytest.raises(ValueError, match="'name'"):
        parse_skill(d2, tmp_path)


def test_parse_skill_extra_keys_allowed(tmp_path: Path) -> None:
    d = write_skill(
        tmp_path, "s", 'name: s\ndescription: d\nlicense: MIT\ncompatibility: "x"'
    )
    assert parse_skill(d, tmp_path).name == "s"


def test_parse_skill_name_mismatch_uses_dir(tmp_path: Path) -> None:
    d = write_skill(tmp_path, "dir-name", "name: other\ndescription: d")
    assert parse_skill(d, tmp_path).name == "dir-name"


def test_load_skills_skips_dirs_without_skill_md(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    write_skill(skills_dir, "good", "name: good\ndescription: d")
    (skills_dir / "empty").mkdir()
    (skills_dir / "_draft").mkdir()
    skills = load_skills(skills_dir, tmp_path)
    assert [s.name for s in skills] == ["good"]


def test_load_skills_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert load_skills(tmp_path / "nope", tmp_path) == []


def test_sync_skill_links_universal_and_tagged(tmp_path: Path) -> None:
    src = tmp_path / "skills"
    src.mkdir()
    write_skill(src, "uni", "name: uni\ndescription: d")
    write_skill(src, "cx", "platforms: [codex]\nname: cx\ndescription: d")
    skills = load_skills(src, tmp_path)

    shared = tmp_path / "shared"
    created, _, _ = sync_skill_links(skills, shared, platform=None)
    assert created == 1
    assert (shared / "uni").is_symlink()

    codex_dir = tmp_path / "codex"
    created, _, _ = sync_skill_links(skills, codex_dir, platform="codex")
    assert created == 1
    assert (codex_dir / "cx").is_symlink()
    assert not (codex_dir / "uni").exists()


def test_sync_skill_links_prunes_and_keeps_regular_dirs(tmp_path: Path) -> None:
    src = tmp_path / "skills"
    src.mkdir()
    write_skill(src, "uni", "name: uni\ndescription: d")
    skills = load_skills(src, tmp_path)

    target = tmp_path / "target"
    target.mkdir()
    stale = target / "gone"
    stale.symlink_to(src / "uni", target_is_directory=True)
    regular = target / "user-dir"
    regular.mkdir()

    created, kept, pruned = sync_skill_links(skills, target, platform=None)
    assert (target / "uni").is_symlink()
    assert not stale.exists() or stale == target / "uni"
    assert regular.is_dir() and not regular.is_symlink()
    assert pruned == 1
    assert created == 1
    assert kept == 0


def test_config_skill_dirs_disabled_platform(tmp_path: Path) -> None:
    (tmp_path / "dotagents.toml").write_text(
        '[platforms]\nenabled = ["opencode"]\n', encoding="utf-8"
    )
    config = load_config(tmp_path)
    assert config.opencode_skills_dir is not None
    assert config.cursor_skills_dir is None
    assert config.codex_skills_dir is None
    assert config.agents_skills_dir is not None


def test_run_end_to_end_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    rules_dir = repo / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "r.mdc").write_text(
        "---\nalwaysApply: true\n---\n\n# R\n", encoding="utf-8"
    )
    skills_dir = repo / "skills"
    skills_dir.mkdir()
    write_skill(skills_dir, "uni", "name: uni\ndescription: d")
    write_skill(skills_dir, "cx", "platforms: [codex]\nname: cx\ndescription: d")

    home = tmp_path / "home"
    config = CompileConfig(
        repo_root=repo,
        rules_dir=rules_dir,
        skills_dir=skills_dir,
        enabled=frozenset({"opencode", "codex"}),
        opencode_out=repo / "autogen-opencode.md",
        codex_out=repo / "autogen-codex.md",
        cursor_rules_dir=None,
        agents_skills_dir=home / ".agents" / "skills",
        opencode_skills_dir=home / ".config" / "opencode" / "skills",
        cursor_skills_dir=None,
        codex_skills_dir=home / ".codex" / "skills",
    )
    assert run(config) == 0
    assert (repo / "autogen-opencode.md").is_file()
    assert (home / ".agents" / "skills" / "uni").is_symlink()
    assert (home / ".codex" / "skills" / "cx").is_symlink()
    assert not (home / ".config" / "opencode" / "skills").exists()
