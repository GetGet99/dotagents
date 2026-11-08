"""Tests for dotagents.compile."""

from __future__ import annotations

from pathlib import Path

import pytest

import dotagents.compile as compile_mod
from dotagents.compile import (
    CompileConfig,
    HarnessConfig,
    Source,
    applies_to,
    demote_headings,
    existing_sources,
    find_duplicate_names,
    load_config,
    load_rules,
    load_skills,
    normalize_globs,
    parse_rule,
    parse_skill,
    prune_harness_artifacts,
    render_agents_md,
    run,
    scan_source_names,
    skill_applies_to,
    sync_mdc_rule_links,
    sync_single_rule,
    sync_skill_links,
    write_if_changed,
)


def write_rule(directory: Path, name: str, frontmatter: str, body: str = "# T\n\nbody") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.mdc"
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return path


def write_skill(
    directory: Path,
    name: str,
    frontmatter: str,
    body: str = "# Skill\n\ncontent",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    skill_dir = directory / name
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return skill_dir


def make_source(root: Path) -> Source:
    return Source(root)


def make_harness(
    tmp_path: Path,
    name: str = "h",
    *,
    enabled: bool = True,
    rule_style: str = "single",
    rule_file_name: str = "AGENTS.md",
    output_dir: Path | None = None,
) -> HarnessConfig:
    target = tmp_path / name
    out = output_dir if output_dir is not None else tmp_path / "out"
    return HarnessConfig(
        name=name,
        enabled=enabled,
        target_dir=target,
        rule_style=rule_style,  # type: ignore[arg-type]
        rule_file_name=rule_file_name,
        rules_dir=target / "rules" if rule_style == "mdc" else None,
        rule_output=out / f"autogen-{name}.md" if rule_style == "single" else None,
        skills_dir=target / "skills",
    )


# --- frontmatter ------------------------------------------------------------


def test_singular_platform_key_rejected(tmp_path: Path) -> None:
    path = write_rule(tmp_path, "r", "platform:\n- cursor")
    with pytest.raises(ValueError, match="platforms"):
        parse_rule(path)


def test_globs_string_vs_list_equal(tmp_path: Path) -> None:
    a = write_rule(tmp_path, "a", 'globs: "*.tsx, src/**"')
    b = write_rule(tmp_path, "b", 'globs:\n- "*.tsx"\n- src/**')
    assert parse_rule(a).globs == ("*.tsx", "src/**")
    assert parse_rule(b).globs == ("*.tsx", "src/**")


def test_globs_absent_is_empty(tmp_path: Path) -> None:
    path = write_rule(tmp_path, "r", "alwaysApply: true")
    assert parse_rule(path).globs == ()


def test_normalize_globs_invalid_type(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="globs"):
        normalize_globs(123, tmp_path)


def test_platforms_empty_means_everywhere(tmp_path: Path) -> None:
    path = write_rule(tmp_path, "r", "platforms: []")
    rule = parse_rule(path)
    assert rule.platforms is None
    assert applies_to(rule, "cursor")
    assert applies_to(rule, "opencode")


def test_invalid_harness_name_rejected(tmp_path: Path) -> None:
    path = write_rule(tmp_path, "r", 'platforms: ["bad name!"]')
    with pytest.raises(ValueError, match="invalid harness name"):
        parse_rule(path)


def test_custom_harness_name_allowed(tmp_path: Path) -> None:
    path = write_rule(tmp_path, "r", 'platforms: ["my-harness_2"]')
    assert parse_rule(path).platforms == ("my-harness_2",)


def test_always_apply_must_be_bool(tmp_path: Path) -> None:
    path = write_rule(tmp_path, "r", 'alwaysApply: "yes"')
    with pytest.raises(ValueError, match="alwaysApply"):
        parse_rule(path)


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
    rule = make_rule(tmp_path, "r", always=True, globs=("*.tsx",), body="# T\n\ntext")
    out = render_agents_md([rule], "opencode")
    assert "> Applies to: `*.tsx`" in out
    assert "### T" in out
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
    rule = make_rule(tmp_path, "r", always=False, globs=("*.tsx",), description="d")
    out = render_agents_md([rule], "opencode")
    assert "Globs: `*.tsx`" in out
    assert "matching those globs" in out


def test_demote_headings() -> None:
    assert demote_headings("# A") == "### A"
    assert demote_headings("## B") == "#### B"
    assert demote_headings("###### Deep") == "###### Deep"
    assert demote_headings("plain") == "plain"


def test_sort_always_first(tmp_path: Path) -> None:
    rules_dir = tmp_path / "rules"
    write_rule(rules_dir, "z-ad", "alwaysApply: false\ndescription: d", "# Z\n")
    write_rule(rules_dir, "a-always", "alwaysApply: true", "# A\n")
    rules = load_rules([make_source(tmp_path)])
    assert [r.slug for r in rules] == ["a-always", "z-ad"]


# --- multi-source -----------------------------------------------------------


def test_load_rules_merges_sources(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    write_rule(one / "rules", "alpha", "alwaysApply: true")
    write_rule(two / "rules", "beta", "alwaysApply: true")
    rules = load_rules([make_source(one), make_source(two)])
    assert [r.slug for r in rules] == ["alpha", "beta"]


def test_duplicate_rule_is_hard_error(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    write_rule(one / "rules", "same", "alwaysApply: true")
    write_rule(two / "rules", "same", "alwaysApply: true")
    with pytest.raises(ValueError, match="duplicate rule 'same'"):
        load_rules([make_source(one), make_source(two)])


def test_missing_source_is_skipped(tmp_path: Path) -> None:
    assert load_rules([make_source(tmp_path / "missing")]) == []
    assert load_skills([make_source(tmp_path / "missing")]) == []


def test_existing_sources_errors_when_none_exist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="none of the configured sources exist"):
        existing_sources([make_source(tmp_path / "missing")])


def test_find_duplicate_names(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    write_rule(one / "rules", "same", "alwaysApply: true")
    write_rule(two / "rules", "same", "alwaysApply: true")
    write_skill(one / "skills", "s", "name: s\ndescription: d")
    write_skill(two / "skills", "s", "name: s\ndescription: d")
    reports = find_duplicate_names([make_source(one), make_source(two)])
    assert any("duplicate rule 'same'" in report for report in reports)
    assert any("duplicate skill 's'" in report for report in reports)


# --- outputs ----------------------------------------------------------------


def test_write_if_changed_no_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "out.md"
    assert write_if_changed(path, "hi") is True
    assert write_if_changed(path, "hi") is False
    assert write_if_changed(path, "hi", dry_run=True) is False


def test_sync_mdc_never_deletes_regular_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = write_rule(source / "rules", "keep", "alwaysApply: true")
    rule = parse_rule(target)

    harness = make_harness(tmp_path, rule_style="mdc")
    assert harness.rules_dir is not None
    harness.rules_dir.mkdir(parents=True)
    regular = harness.rules_dir / "keep.mdc"
    regular.write_text("user content", encoding="utf-8")
    stale_regular = harness.rules_dir / "old.mdc"
    stale_regular.write_text("user content", encoding="utf-8")

    created, _, pruned = sync_mdc_rule_links([rule], harness)
    assert regular.read_text(encoding="utf-8") == "user content"
    assert stale_regular.exists()
    assert created == 0
    assert pruned == 0


def test_sync_mdc_prunes_stale_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = write_rule(source / "rules", "keep", "alwaysApply: true")
    rule = parse_rule(target)

    harness = make_harness(tmp_path, rule_style="mdc")
    assert harness.rules_dir is not None
    harness.rules_dir.mkdir(parents=True)
    stale = harness.rules_dir / "gone.mdc"
    stale.symlink_to(tmp_path / "nonexistent.mdc")

    created, _, pruned = sync_mdc_rule_links([rule], harness)
    assert created == 1
    assert pruned == 1
    assert not stale.exists()
    assert (harness.rules_dir / "keep.mdc").is_symlink()


def test_sync_single_rule_writes_and_links(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = write_rule(source / "rules", "r", "alwaysApply: true", "# R\n")
    rule = parse_rule(target)
    harness = make_harness(tmp_path)

    assert sync_single_rule([rule], harness) is True
    assert harness.rule_output is not None
    assert harness.rule_output.is_file()
    link = harness.target_dir / harness.rule_file_name
    assert link.is_symlink()
    assert link.resolve() == harness.rule_output.resolve()
    assert sync_single_rule([rule], harness) is False


def test_sync_single_rule_does_not_touch_regular_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = write_rule(source / "rules", "r", "alwaysApply: true")
    rule = parse_rule(target)
    harness = make_harness(tmp_path)
    harness.target_dir.mkdir(parents=True)
    regular = harness.target_dir / harness.rule_file_name
    regular.write_text("user content", encoding="utf-8")

    sync_single_rule([rule], harness)
    assert regular.read_text(encoding="utf-8") == "user content"
    assert not regular.is_symlink()


def test_prune_harness_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_rule(source / "rules", "r", "alwaysApply: true")
    write_skill(source / "skills", "s", "name: s\ndescription: d")
    harness = make_harness(tmp_path)
    config = CompileConfig(
        sources=(make_source(source),),
        harnesses=(harness,),
        output_dir=tmp_path / "out",
    )
    assert run(config) == 0
    assert harness.rule_output is not None and harness.rule_output.is_file()
    assert (harness.target_dir / harness.rule_file_name).is_symlink()
    assert (harness.skills_dir / "s").is_symlink()

    pruned = prune_harness_artifacts(harness, [make_source(source)], harness.rule_output.parent)
    assert pruned == 3
    assert not harness.rule_output.exists()
    assert not (harness.target_dir / harness.rule_file_name).exists()
    assert not (harness.skills_dir / "s").exists()


# --- config -----------------------------------------------------------------


def test_load_config_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DOTAGENTS_CONFIG_DIR", raising=False)
    monkeypatch.delenv("DOTAGENTS_OUTPUT_DIR", raising=False)
    config_dir = tmp_path / "cfg"
    config = load_config([tmp_path], config_dir=config_dir, cwd=tmp_path)
    assert {h.name for h in config.harnesses} == {"cursor", "opencode", "codex"}
    assert all(not h.enabled for h in config.harnesses)
    cursor = next(h for h in config.harnesses if h.name == "cursor")
    opencode = next(h for h in config.harnesses if h.name == "opencode")
    assert cursor.target_dir == tmp_path / ".cursor"
    assert cursor.rules_dir == tmp_path / ".cursor" / "rules"
    assert cursor.rule_output is None
    assert opencode.target_dir == tmp_path / ".config" / "opencode"
    assert opencode.rule_output == config_dir / "build" / "autogen-opencode.md"
    assert config.output_dir == config_dir / "build"
    assert not config_dir.exists()


def test_load_config_settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[build]\noutput_dir = "out"\n', encoding="utf-8")
    config = load_config([tmp_path], config_dir=config_dir, cwd=tmp_path)
    assert config.output_dir == config_dir / "out"


def test_load_config_cli_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config_dir = tmp_path / "cfg"
    config = load_config(
        [tmp_path],
        config_dir=config_dir,
        cwd=tmp_path,
        cli_output_dir=tmp_path / "cli-out",
    )
    assert config.output_dir == tmp_path / "cli-out"


def test_load_config_custom_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "harnesses.toml").write_text(
        '[claude]\ntarget_dir = "~/.claude"\nrule_style = "single"\nrule_file_name = "CLAUDE.md"\n',
        encoding="utf-8",
    )
    config = load_config([tmp_path], config_dir=config_dir, cwd=tmp_path)
    claude = next(h for h in config.harnesses if h.name == "claude")
    assert claude.target_dir == tmp_path / ".claude"
    assert claude.rule_output == config_dir / "build" / "autogen-claude.md"
    assert claude.rule_file_name == "CLAUDE.md"


def test_disabled_harness_has_paths(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, enabled=False)
    assert harness.enabled is False
    assert harness.skills_dir == tmp_path / "h" / "skills"


# --- skills ---------------------------------------------------------------


def test_parse_skill_platforms(tmp_path: Path) -> None:
    skill_dir = write_skill(
        tmp_path,
        "create-pet",
        "platforms:\n- codex\nname: create-pet\ndescription: make a pet",
    )
    skill = parse_skill(skill_dir)
    assert skill.name == "create-pet"
    assert skill.platforms == ("codex",)
    assert skill_applies_to(skill, "codex")
    assert not skill_applies_to(skill, "opencode")


def test_parse_skill_universal_without_platforms(tmp_path: Path) -> None:
    skill_dir = write_skill(tmp_path, "s", "name: s\ndescription: d")
    assert parse_skill(skill_dir).platforms is None


def test_parse_skill_requires_name_and_description(tmp_path: Path) -> None:
    skill_dir = write_skill(tmp_path, "s", "name: s")
    with pytest.raises(ValueError, match="description"):
        parse_skill(skill_dir)
    skill_dir2 = write_skill(tmp_path, "s2", "description: d")
    with pytest.raises(ValueError, match="'name'"):
        parse_skill(skill_dir2)


def test_parse_skill_extra_keys_allowed(tmp_path: Path) -> None:
    skill_dir = write_skill(
        tmp_path, "s", 'name: s\ndescription: d\nlicense: MIT\ncompatibility: "x"'
    )
    assert parse_skill(skill_dir).name == "s"


def test_parse_skill_name_mismatch_uses_dir(tmp_path: Path) -> None:
    skill_dir = write_skill(tmp_path, "dir-name", "name: other\ndescription: d")
    assert parse_skill(skill_dir).name == "dir-name"


def test_load_skills_skips_dirs_without_skill_md(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    write_skill(skills_dir, "good", "name: good\ndescription: d")
    (skills_dir / "empty").mkdir()
    (skills_dir / "_draft").mkdir()
    skills = load_skills([make_source(tmp_path)])
    assert [s.name for s in skills] == ["good"]


def test_load_skills_discovers_nested_skills(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    write_skill(skills_dir, "top", "name: top\ndescription: d")
    write_skill(
        skills_dir / "group",
        "nested",
        "name: nested\ndescription: d",
    )
    write_skill(
        skills_dir / "group" / "sub",
        "deep",
        "name: deep\ndescription: d",
    )
    (skills_dir / "group" / "no-skill").mkdir()
    skills = load_skills([make_source(tmp_path)])
    assert [s.name for s in skills] == ["top", "nested", "deep"]
    assert {s.source_dir.name for s in skills} == {"top", "nested", "deep"}


def test_scan_source_names_includes_nested_skills(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    write_skill(skills_dir, "top", "name: top\ndescription: d")
    write_skill(
        skills_dir / "group",
        "nested",
        "name: nested\ndescription: d",
    )
    _, names = scan_source_names(make_source(tmp_path))
    assert names == ["top", "nested"]


def test_load_skills_merges_sources_and_rejects_duplicates(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    write_skill(one / "skills", "a", "name: a\ndescription: d")
    write_skill(two / "skills", "b", "name: b\ndescription: d")
    skills = load_skills([make_source(one), make_source(two)])
    assert [s.name for s in skills] == ["a", "b"]

    write_skill(two / "skills", "a", "name: a\ndescription: d")
    with pytest.raises(ValueError, match="duplicate skill 'a'"):
        load_skills([make_source(one), make_source(two)])


def test_sync_skill_links_include_universal_and_tagged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_skill(source / "skills", "uni", "name: uni\ndescription: d")
    write_skill(source / "skills", "cx", "platforms: [codex]\nname: cx\ndescription: d")
    skills = load_skills([make_source(source)])

    codex_dir = tmp_path / "codex"
    created, _, _ = sync_skill_links(skills, codex_dir, harness_name="codex")
    assert created == 2
    assert (codex_dir / "uni").is_symlink()
    assert (codex_dir / "cx").is_symlink()

    cursor_dir = tmp_path / "cursor"
    created, _, _ = sync_skill_links(skills, cursor_dir, harness_name="cursor")
    assert created == 1
    assert (cursor_dir / "uni").is_symlink()
    assert not (cursor_dir / "cx").exists()


def test_sync_skill_links_prunes_and_keeps_regular_dirs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_skill(source / "skills", "uni", "name: uni\ndescription: d")
    skills = load_skills([make_source(source)])

    target = tmp_path / "target"
    target.mkdir()
    stale = target / "gone"
    stale.symlink_to(source / "skills" / "uni", target_is_directory=True)
    regular = target / "user-dir"
    regular.mkdir()

    created, kept, pruned = sync_skill_links(skills, target, harness_name="h")
    assert (target / "uni").is_symlink()
    assert not stale.exists()
    assert regular.is_dir() and not regular.is_symlink()
    assert pruned == 1
    assert created == 1
    assert kept == 0


# --- run ------------------------------------------------------------------


def test_run_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    source = tmp_path / "source"
    write_rule(source / "rules", "r", "alwaysApply: true", "# R\n")
    write_rule(source / "rules", "h2-only", "platforms: [h2]\nalwaysApply: true")
    write_skill(source / "skills", "uni", "name: uni\ndescription: d")
    write_skill(source / "skills", "h2", "platforms: [h2]\nname: h2\ndescription: d")

    mdc = make_harness(tmp_path, "h1", rule_style="mdc")
    single = make_harness(tmp_path, "h2", rule_style="single")
    config = CompileConfig(
        sources=(make_source(source),),
        harnesses=(mdc, single),
        output_dir=tmp_path / "out",
    )
    assert run(config) == 0

    assert mdc.rules_dir is not None
    assert (mdc.rules_dir / "r.mdc").is_symlink()
    assert not (mdc.rules_dir / "h2-only.mdc").exists()
    assert (mdc.skills_dir / "uni").is_symlink()
    assert not (mdc.skills_dir / "h2").exists()

    assert single.rule_output is not None
    content = single.rule_output.read_text(encoding="utf-8")
    assert "## r" in content
    assert "## h2-only" in content
    assert (single.target_dir / single.rule_file_name).is_symlink()
    assert (single.skills_dir / "uni").is_symlink()
    assert (single.skills_dir / "h2").is_symlink()


def test_run_disabled_prunes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    source = tmp_path / "source"
    write_rule(source / "rules", "r", "alwaysApply: true")
    write_skill(source / "skills", "s", "name: s\ndescription: d")

    harness = make_harness(tmp_path)
    enabled = CompileConfig(
        sources=(make_source(source),),
        harnesses=(harness,),
        output_dir=tmp_path / "out",
    )
    assert run(enabled) == 0
    assert (harness.target_dir / harness.rule_file_name).is_symlink()
    assert (harness.skills_dir / "s").is_symlink()

    disabled_harness = make_harness(tmp_path, enabled=False)
    disabled = CompileConfig(
        sources=(make_source(source),),
        harnesses=(disabled_harness,),
        output_dir=tmp_path / "out",
    )
    assert run(disabled) == 0
    assert not (harness.target_dir / harness.rule_file_name).exists()
    assert not (harness.skills_dir / "s").exists()
    assert harness.rule_output is not None and not harness.rule_output.exists()


def test_run_check_detects_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    source = tmp_path / "source"
    write_rule(source / "rules", "r", "alwaysApply: true")
    harness = make_harness(tmp_path)
    config = CompileConfig(
        sources=(make_source(source),),
        harnesses=(harness,),
        output_dir=tmp_path / "out",
    )
    assert run(config, check=True) == 1
    assert run(config) == 0
    assert run(config, check=True) == 0


def test_run_merges_rules_across_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    one = tmp_path / "one"
    two = tmp_path / "two"
    write_rule(one / "rules", "alpha", "alwaysApply: true", "# Alpha\n")
    write_rule(two / "rules", "beta", "alwaysApply: true", "# Beta\n")
    harness = make_harness(tmp_path)
    config = CompileConfig(
        sources=(make_source(one), make_source(two)),
        harnesses=(harness,),
        output_dir=tmp_path / "out",
    )
    assert run(config) == 0
    assert harness.rule_output is not None
    content = harness.rule_output.read_text(encoding="utf-8")
    assert "## alpha" in content
    assert "## beta" in content


def test_run_duplicate_skill_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    one = tmp_path / "one"
    two = tmp_path / "two"
    write_rule(one / "rules", "alpha", "alwaysApply: true", "# Alpha\n")
    write_skill(one / "skills", "same", "name: same\ndescription: d")
    write_skill(two / "skills", "same", "name: same\ndescription: d")
    harness = make_harness(tmp_path)
    config = CompileConfig(
        sources=(make_source(one), make_source(two)),
        harnesses=(harness,),
        output_dir=tmp_path / "out",
    )
    with pytest.raises(ValueError, match="duplicate skill 'same'"):
        run(config)
    assert harness.rule_output is not None
    assert not harness.rule_output.exists()
