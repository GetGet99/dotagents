"""Compile rules and skills from one or more sources for enabled harnesses.

A source is a directory containing ``rules/*.mdc`` and/or
``skills/<name>/SKILL.md``. Sources are registered with
``dotagents source add`` and read from the per-user config directory (see
:mod:`dotagents.config`).

Source of truth for rules: `rules/*.mdc` files with YAML frontmatter::

    ---
    alwaysApply: true
    description: "Optional human/agent-facing summary"
    platforms: ["cursor"]          # harness names; omit/empty => every harness
    globs: ["*.tsx", "src/**"]     # or comma-separated string; omit => []
    ---

    # Rule body (markdown)

Source of truth for skills: `skills/<name>/SKILL.md` (Agent Skills format).
Only ``platforms`` is interpreted (same semantics as rules); ``name`` and
``description`` are required by the skills spec and validated. All other
frontmatter keys pass through untouched.

Harnesses are declared in ``harnesses.toml`` (see :mod:`dotagents.harness`).
Each enabled harness receives rules and skills as follows:

* ``rule_style = "mdc"``: symlink each applicable rule into
  ``<target_dir>/rules/<slug>.mdc``.
* ``rule_style = "single"``: render applicable rules to
  ``<output_dir>/autogen-<name>.md`` and symlink
  ``<target_dir>/<rule_file_name>`` to it.
* Skills are symlinked into ``<target_dir>/skills/<name>``. Universal skills
  (no ``platforms``) are materialized into every harness; tagged skills only
  into the harnesses they name.

Rendering contract for generated rule documents:

* ``alwaysApply: true`` → full body inlined (headings demoted by 2 so the
  rule's ``# Title`` nests under ``## <slug>`` as ``### Title``), plus a
  ``> Applies to: ...`` hint line when ``globs`` is non-empty.
* ``alwaysApply: false`` → advertising block: ``Description:`` (inline when
  single-line, fenced block when multi-line), ``Globs:`` line when globs are
  present, and a pointer sentence with the rule's absolute path.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from dotagents import config as config_mod
from dotagents import harness as harness_mod

ALLOWED_FRONTMATTER_KEYS: frozenset[str] = frozenset(
    {"alwaysApply", "description", "platforms", "globs"}
)

FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<fm>.*?)(?:\r?\n)?---[ \t]*\r?\n(?P<body>.*)\Z",
    re.DOTALL,
)
HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.*)$")
TITLE_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$", re.MULTILINE)

logger = logging.getLogger("compile")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rule:
    """A single parsed ``rules/*.mdc`` file."""

    slug: str
    source_path: Path  # absolute, resolved
    body: str  # markdown after frontmatter, stripped
    title: str | None  # first `# ...` in body, if any
    always_apply: bool
    description: str | None
    platforms: tuple[str, ...] | None  # None = everywhere
    globs: tuple[str, ...]  # () = no restriction


@dataclass(frozen=True, slots=True)
class Skill:
    """A single `skills/<name>/` directory with a `SKILL.md` file."""

    name: str  # directory name (filesystem reality wins over frontmatter)
    source_dir: Path  # absolute, resolved
    description: str | None
    platforms: tuple[str, ...] | None  # None = universal


@dataclass(frozen=True, slots=True)
class Source:
    """A registered content directory holding ``rules/`` and/or ``skills/``."""

    path: Path

    @property
    def rules_dir(self) -> Path:
        return self.path / "rules"

    @property
    def skills_dir(self) -> Path:
        return self.path / "skills"


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    """A resolved harness with absolute output paths."""

    name: str
    enabled: bool
    target_dir: Path
    rule_style: harness_mod.RuleStyle
    rule_file_name: str
    rules_dir: Path | None  # set for mdc harnesses
    rule_output: Path | None  # set for single-file harnesses
    skills_dir: Path


@dataclass(frozen=True, slots=True)
class CompileConfig:
    """Fully resolved configuration."""

    sources: tuple[Source, ...]
    harnesses: tuple[HarnessConfig, ...]
    output_dir: Path
    prune_stale: bool = True
    create_target_dirs: bool = True
    enable_skills: bool = True


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def normalize_globs(raw: object, path: Path) -> tuple[str, ...]:
    """Normalize the ``globs`` frontmatter value to a tuple of patterns."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        items: list[str] = [p.strip().strip("\"'") for p in raw.split(",")]
        return tuple(p for p in items if p)
    if isinstance(raw, list):
        normalized: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{path}: 'globs' list items must be non-empty strings")
            normalized.append(item.strip())
        return tuple(normalized)
    raise ValueError(f"{path}: 'globs' must be a string or list of strings")


def normalize_platforms(raw: object, path: Path) -> tuple[str, ...] | None:
    """Normalize ``platforms``; None means the rule applies everywhere."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError(f"{path}: 'platforms' must be a list of strings")
    if len(raw) == 0:
        return None
    normalized: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"{path}: 'platforms' items must be strings")
        name = item.strip()
        if not name:
            raise ValueError(f"{path}: 'platforms' items must be non-empty strings")
        if not harness_mod.HARNESS_NAME_RE.match(name):
            raise ValueError(
                f"{path}: invalid harness name {item!r}; use letters, digits, '-' or '_'"
            )
        normalized.append(name)
    return tuple(normalized)


def parse_rule(path: Path) -> Rule:
    """Parse one ``.mdc`` file into a :class:`Rule`."""
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if match is None:
        raise ValueError(f"{path}: missing YAML frontmatter (expected --- fences)")

    fm_raw = match.group("fm")
    body = match.group("body").strip()
    if not body:
        raise ValueError(f"{path}: rule body is empty")

    loaded: object = yaml.safe_load(fm_raw)
    if loaded is None:
        fm: dict[str, object] = {}
    elif isinstance(loaded, dict):
        fm = {str(k): v for k, v in loaded.items()}
    else:
        raise ValueError(f"{path}: frontmatter must be a YAML mapping")

    unknown = set(fm) - ALLOWED_FRONTMATTER_KEYS
    if unknown:
        raise ValueError(
            f"{path}: unknown frontmatter key(s) {sorted(unknown)}; "
            f"allowed: {sorted(ALLOWED_FRONTMATTER_KEYS)} "
            f"(did you mean 'platforms' plural?)"
        )

    always_raw: object = fm.get("alwaysApply", False)
    if not isinstance(always_raw, bool):
        raise ValueError(f"{path}: 'alwaysApply' must be a boolean")
    always_apply: bool = always_raw

    desc_raw: object = fm.get("description")
    description: str | None = None
    if desc_raw is not None:
        if not isinstance(desc_raw, str):
            raise ValueError(f"{path}: 'description' must be a string")
        description = desc_raw.strip() or None

    if not always_apply and description is None:
        logger.warning("%s: non-always rule has no 'description'", path)

    platforms = normalize_platforms(fm.get("platforms"), path)
    globs = normalize_globs(fm.get("globs"), path)

    title: str | None = None
    title_match = TITLE_RE.search(body)
    if title_match is not None:
        title = title_match.group("title").strip()

    try:
        source_path = path.resolve()
    except OSError:
        source_path = path.absolute()

    return Rule(
        slug=path.stem,
        source_path=source_path,
        body=body,
        title=title,
        always_apply=always_apply,
        description=description,
        platforms=platforms,
        globs=globs,
    )


def load_rules(sources: Sequence[Source]) -> list[Rule]:
    """Load rules from every source, rejecting duplicate slugs."""
    by_slug: dict[str, Rule] = {}
    for source in sources:
        rules_dir = source.rules_dir
        if not rules_dir.is_dir():
            logger.debug("source has no rules directory: %s", source.path)
            continue
        files = sorted(rules_dir.glob("*.mdc"))
        if not files:
            logger.warning("no *.mdc files found in %s", rules_dir)
        for path in files:
            rule = parse_rule(path)
            existing = by_slug.get(rule.slug)
            if existing is not None:
                raise ValueError(
                    f"duplicate rule {rule.slug!r}: {existing.source_path} and {rule.source_path}"
                )
            by_slug[rule.slug] = rule
    rules = list(by_slug.values())
    # always-apply first, then alphabetical by slug — deterministic output.
    rules.sort(key=lambda r: (not r.always_apply, r.slug))
    return rules


def applies_to(rule: Rule, harness_name: str) -> bool:
    """Return True when a rule should be delivered to ``harness_name``."""
    if rule.platforms is None:
        return True
    return harness_name in rule.platforms


def parse_skill_frontmatter(text: str, path: Path) -> dict[str, object]:
    """Extract the YAML mapping from a `SKILL.md` file."""
    match = FRONTMATTER_RE.match(text)
    if match is None:
        raise ValueError(f"{path}: missing YAML frontmatter (expected --- fences)")
    loaded: object = yaml.safe_load(match.group("fm"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: frontmatter must be a YAML mapping")
    return {str(k): v for k, v in loaded.items()}


def parse_skill(skill_dir: Path) -> Skill:
    """Parse one `skills/<name>/` directory into a :class:`Skill`."""
    path = skill_dir / "SKILL.md"
    if not path.is_file():
        raise ValueError(f"{skill_dir}: missing SKILL.md")
    fm = parse_skill_frontmatter(path.read_text(encoding="utf-8"), path)

    name_raw: object = fm.get("name")
    if not isinstance(name_raw, str) or not name_raw.strip():
        raise ValueError(f"{path}: 'name' must be a non-empty string")
    if name_raw.strip() != skill_dir.name:
        logger.warning(
            "%s: frontmatter name %r != directory name %r; using directory name",
            path,
            name_raw.strip(),
            skill_dir.name,
        )

    desc_raw: object = fm.get("description")
    if not isinstance(desc_raw, str) or not desc_raw.strip():
        raise ValueError(f"{path}: 'description' must be a non-empty string")

    platforms = normalize_platforms(fm.get("platforms"), path)

    try:
        source_dir = skill_dir.resolve()
    except OSError:
        source_dir = skill_dir.absolute()

    return Skill(
        name=skill_dir.name,
        source_dir=source_dir,
        description=desc_raw.strip(),
        platforms=platforms,
    )


def load_skills(sources: Sequence[Source]) -> list[Skill]:
    """Load skills from every source, rejecting duplicate names."""
    by_name: dict[str, Skill] = {}
    seen_skills_dir = False
    for source in sources:
        skills_dir = source.skills_dir
        if not skills_dir.is_dir():
            logger.debug("source has no skills directory: %s", source.path)
            continue
        seen_skills_dir = True
        for entry in sorted(skills_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            if not (entry / "SKILL.md").is_file():
                logger.warning("skipping %s: no SKILL.md", entry)
                continue
            skill = parse_skill(entry)
            existing = by_name.get(skill.name)
            if existing is not None:
                raise ValueError(
                    f"duplicate skill {skill.name!r}: {existing.source_dir} and {skill.source_dir}"
                )
            by_name[skill.name] = skill
    skills = list(by_name.values())
    if not skills and seen_skills_dir:
        logger.warning("no skills found in %s", [str(s.path) for s in sources])
    return skills


def skill_applies_to(skill: Skill, harness_name: str) -> bool:
    """Return True when a skill should be materialized for ``harness_name``."""
    if skill.platforms is None:
        return True
    return harness_name in skill.platforms


def scan_source_names(source: Source) -> tuple[list[str], list[str]]:
    """Return rule slugs and skill names in *source* without parsing them."""
    slugs = (
        sorted(path.stem for path in source.rules_dir.glob("*.mdc"))
        if source.rules_dir.is_dir()
        else []
    )
    names: list[str] = []
    if source.skills_dir.is_dir():
        for entry in sorted(source.skills_dir.iterdir()):
            if (
                entry.is_dir()
                and not entry.name.startswith((".", "_"))
                and (entry / "SKILL.md").is_file()
            ):
                names.append(entry.name)
    return slugs, names


def find_duplicate_names(sources: Sequence[Source]) -> list[str]:
    """Report duplicate rule slugs and skill names across *sources*."""
    rule_owners: dict[str, list[Path]] = {}
    skill_owners: dict[str, list[Path]] = {}
    for source in sources:
        slugs, names = scan_source_names(source)
        for slug in slugs:
            rule_owners.setdefault(slug, []).append(source.path)
        for name in names:
            skill_owners.setdefault(name, []).append(source.path)
    reports: list[str] = []
    for kind, owners in (("rule", rule_owners), ("skill", skill_owners)):
        for name, paths in sorted(owners.items()):
            if len(paths) > 1:
                joined = ", ".join(str(path) for path in paths)
                reports.append(f"duplicate {kind} {name!r} in {joined}")
    return reports


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def demote_headings(body: str, levels: int = 2) -> str:
    """Demote markdown headings by ``levels`` so inlined rules nest correctly.

    The rule's own ``# Title`` becomes ``### Title`` under the generated
    ``## <slug>`` section; deeper headings shift accordingly, clamped at
    ``######``. Non-heading lines pass through untouched.
    """

    def _demote(m: re.Match[str]) -> str:
        depth = min(len(m.group("hashes")) + levels, 6)
        return f"{'#' * depth} {m.group('text')}"

    return "\n".join(HEADING_RE.sub(_demote, line) for line in body.splitlines())


def format_globs(globs: tuple[str, ...]) -> str:
    """Render globs as backticked, comma-separated patterns."""
    return ", ".join(f"`{g}`" for g in globs)


def render_advertising_block(rule: Rule) -> str:
    """Render the pointer block for a non-always rule."""
    lines: list[str] = [f"## {rule.slug}", ""]
    if rule.description is not None:
        if "\n" in rule.description:
            lines.append("```")
            lines.append(rule.description)
            lines.append("```")
        else:
            lines.append(rule.description)
        lines.append("")
    if rule.globs:
        lines.append(f"Globs: {format_globs(rule.globs)}")
        lines.append("")
        lines.append(
            f"When you touch files matching those globs, read "
            f"`{rule.source_path}` before working in those areas."
        )
    else:
        lines.append(
            f"When you work on the relevant areas, read "
            f"`{rule.source_path}` before working in those areas."
        )
    return "\n".join(lines)


def render_always_block(rule: Rule) -> str:
    """Render an always-apply rule with its full body inlined."""
    lines: list[str] = [f"## {rule.slug}", ""]
    if rule.globs:
        lines.append(f"> Applies to: {format_globs(rule.globs)}")
        lines.append("")
    lines.append(demote_headings(rule.body))
    return "\n".join(lines)


def render_agents_md(rules: Sequence[Rule], harness_name: str) -> str:
    """Render the full rule document for one harness."""
    applicable = [r for r in rules if applies_to(r, harness_name)]
    lines: list[str] = ["# Rules", ""]
    if not applicable:
        lines.append("No rules apply to this harness.")
        lines.append("")
        return "\n".join(lines)
    for i, rule in enumerate(applicable):
        if i > 0:
            lines.append("")
            lines.append("---")
            lines.append("")
        if rule.always_apply:
            lines.append(render_always_block(rule))
        else:
            lines.append(render_advertising_block(rule))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _section(cfg: dict[str, object], name: str) -> dict[str, object]:
    raw: object = cfg.get(name, {})
    if not isinstance(raw, dict):
        raise ValueError(f"[{name}] section must be a table")
    return cast("dict[str, object]", raw)


def _pick_path(
    configured: object,
    default: Path,
    *,
    env_value: str | None,
    cli_value: Path | None,
    config_base: Path,
    cli_base: Path,
) -> Path:
    """Resolve a path setting; env/CLI overrides are relative to the cwd."""
    if cli_value is not None:
        return config_mod.resolve_path(cli_value, cli_base)
    if env_value:
        return config_mod.resolve_path(env_value, cli_base)
    return config_mod.resolve_path(default if configured is None else str(configured), config_base)


def resolve_output_dir(
    config_dir: Path,
    *,
    cwd: Path,
    cli_output_dir: Path | None = None,
) -> Path:
    """Resolve ``[build] output_dir`` without validating harness settings."""
    settings = config_mod.read_settings(config_dir)
    build_cfg = _section(settings, "build")
    return _pick_path(
        build_cfg.get("output_dir"),
        config_dir / "build",
        env_value=os.environ.get("DOTAGENTS_OUTPUT_DIR"),
        cli_value=cli_output_dir,
        config_base=config_dir,
        cli_base=cwd,
    )


def resolve_harness(
    spec: harness_mod.HarnessSpec,
    output_dir: Path,
) -> HarnessConfig:
    if spec.target_dir is None:
        raise ValueError(f"harness {spec.name!r} has no target directory")
    return HarnessConfig(
        name=spec.name,
        enabled=spec.enabled,
        target_dir=spec.target_dir,
        rule_style=spec.rule_style,
        rule_file_name=spec.rule_file_name,
        rules_dir=spec.target_dir / "rules" if spec.rule_style == "mdc" else None,
        rule_output=(
            output_dir / f"autogen-{spec.name}.md" if spec.rule_style == "single" else None
        ),
        skills_dir=spec.target_dir / "skills",
    )


def load_config(
    sources: Sequence[Path],
    *,
    config_dir: Path,
    cwd: Path,
    cli_output_dir: Path | None = None,
    cli_no_skills: bool = False,
) -> CompileConfig:
    """Resolve configuration with documented precedence.

    Precedence (low → high): built-in defaults → ``config.toml`` → CLI flags.
    Harness definitions come from ``harnesses.toml`` plus the built-in catalog.
    """
    settings = config_mod.read_settings(config_dir)

    behavior_cfg = _section(settings, "behavior")

    output_dir = resolve_output_dir(config_dir, cwd=cwd, cli_output_dir=cli_output_dir)

    specs = harness_mod.read_harnesses(config_dir)
    harnesses = [resolve_harness(specs[name], output_dir) for name in sorted(specs)]

    prune_raw: object = behavior_cfg.get("prune_stale_symlinks", True)
    if not isinstance(prune_raw, bool):
        raise ValueError("[behavior] prune_stale_symlinks must be a boolean")
    create_raw: object = behavior_cfg.get("create_target_dirs", True)
    if not isinstance(create_raw, bool):
        raise ValueError("[behavior] create_target_dirs must be a boolean")

    return CompileConfig(
        sources=tuple(Source(path) for path in sources),
        harnesses=tuple(harnesses),
        output_dir=output_dir,
        prune_stale=prune_raw,
        create_target_dirs=create_raw,
        enable_skills=not cli_no_skills,
    )


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def write_if_changed(path: Path, content: str, *, dry_run: bool = False) -> bool:
    """Write ``content`` to ``path`` if it differs; return True when changed."""
    existing: str | None = None
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
    if existing == content:
        logger.info("unchanged %s", path)
        return False
    if dry_run:
        logger.info("would write %s (%d bytes)", path, len(content.encode("utf-8")))
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    logger.info("wrote %s", path)
    return True


def sync_link_dir(
    expected: dict[str, Path],
    target_dir: Path,
    *,
    kind: str,
    empty_message: str,
    suffix: str | None = None,
    dry_run: bool = False,
    prune_stale: bool = True,
    create_dir: bool = True,
) -> tuple[int, int, int]:
    """Sync ``target_dir`` to contain symlinks for ``expected``.

    Returns ``(created, kept, pruned)``. Only symlinks are ever removed —
    regular files/directories are left alone and merely logged. The directory
    itself is only created when ``expected`` is non-empty.
    """
    created = kept = pruned = 0

    if not expected:
        logger.info("%s; leaving %s untouched", empty_message, target_dir)
        return (0, 0, 0)

    if not target_dir.exists():
        if dry_run:
            logger.info("would create directory %s", target_dir)
            return (1, 0, 0)
        if not create_dir:
            logger.warning(
                "%s missing and create_target_dirs=false; skipping %s links",
                target_dir,
                kind,
            )
            return (0, 0, 0)
        target_dir.mkdir(parents=True, exist_ok=True)
        logger.info("created directory %s", target_dir)
    elif not target_dir.is_dir():
        raise ValueError(f"target path is not a directory: {target_dir}")

    for name, target in sorted(expected.items()):
        link = target_dir / name
        if link.is_symlink():
            try:
                if link.resolve() == target.resolve():
                    kept += 1
                    logger.debug("kept %s", link)
                    continue
            except OSError:
                pass  # broken or unresolvable — replace below
            if dry_run:
                logger.info("would update symlink %s -> %s", link, target)
            else:
                link.unlink()
                link.symlink_to(target, target_is_directory=target.is_dir())
                logger.info("updated symlink %s -> %s", link, target)
            created += 1
        elif link.exists():
            logger.warning("not touching regular file %s", link)
        else:
            if dry_run:
                logger.info("would link %s -> %s", link, target)
            else:
                link.symlink_to(target, target_is_directory=target.is_dir())
                logger.info("linked %s -> %s", link, target)
            created += 1

    if prune_stale:
        for entry in sorted(target_dir.iterdir()):
            if suffix is not None and not entry.name.endswith(suffix):
                continue
            if entry.name not in expected:
                if entry.is_symlink():
                    if dry_run:
                        logger.info("would prune stale symlink %s", entry)
                    else:
                        entry.unlink()
                        logger.info("pruned stale symlink %s", entry)
                    pruned += 1
                else:
                    logger.warning("not pruning regular file %s", entry)

    return (created, kept, pruned)


def sync_mdc_rule_links(
    rules: Sequence[Rule],
    harness: HarnessConfig,
    *,
    dry_run: bool = False,
    prune_stale: bool = True,
    create_dir: bool = True,
) -> tuple[int, int, int]:
    """Sync `.mdc` rule symlinks; return ``(created, kept, pruned)``."""
    if harness.rules_dir is None:
        raise ValueError(f"harness {harness.name!r} does not use .mdc rules")
    expected: dict[str, Path] = {
        f"{r.slug}.mdc": r.source_path for r in rules if applies_to(r, harness.name)
    }
    return sync_link_dir(
        expected,
        harness.rules_dir,
        kind=f"{harness.name} rule",
        empty_message=f"no {harness.name}-applicable rules",
        suffix=".mdc",
        dry_run=dry_run,
        prune_stale=prune_stale,
        create_dir=create_dir,
    )


def sync_single_link(
    link: Path,
    target: Path,
    *,
    dry_run: bool = False,
    create_dir: bool = True,
) -> bool:
    """Point ``link`` at ``target``; return True when it would change."""
    if link.is_symlink():
        try:
            if link.resolve() == target.resolve():
                logger.debug("kept %s", link)
                return False
        except OSError:
            pass
        if dry_run:
            logger.info("would update symlink %s -> %s", link, target)
        else:
            link.unlink()
            link.symlink_to(target)
            logger.info("updated symlink %s -> %s", link, target)
        return True
    if link.exists():
        logger.warning("not touching regular file %s", link)
        return False
    if not link.parent.exists():
        if dry_run:
            logger.info("would create directory %s", link.parent)
        elif not create_dir:
            logger.warning(
                "%s missing and create_target_dirs=false; skipping rule link",
                link.parent,
            )
            return False
        else:
            link.parent.mkdir(parents=True, exist_ok=True)
            logger.info("created directory %s", link.parent)
    if dry_run:
        logger.info("would link %s -> %s", link, target)
        return True
    link.symlink_to(target)
    logger.info("linked %s -> %s", link, target)
    return True


def sync_single_rule(
    rules: Sequence[Rule],
    harness: HarnessConfig,
    *,
    dry_run: bool = False,
    create_dir: bool = True,
) -> bool:
    """Generate and link a single-file rule document; return True on change."""
    if harness.rule_output is None:
        raise ValueError(f"harness {harness.name!r} does not use a single rule file")
    content = render_agents_md(rules, harness.name)
    changed = write_if_changed(harness.rule_output, content, dry_run=dry_run)
    link = harness.target_dir / harness.rule_file_name
    if sync_single_link(link, harness.rule_output, dry_run=dry_run, create_dir=create_dir):
        changed = True
    return changed


def sync_skill_links(
    skills: Sequence[Skill],
    target_dir: Path,
    *,
    harness_name: str,
    dry_run: bool = False,
    prune_stale: bool = True,
    create_dir: bool = True,
) -> tuple[int, int, int]:
    """Sync skill directory symlinks; return ``(created, kept, pruned)``."""
    selected = [s for s in skills if skill_applies_to(s, harness_name)]
    expected: dict[str, Path] = {s.name: s.source_dir for s in selected}
    return sync_link_dir(
        expected,
        target_dir,
        kind="skill",
        empty_message=f"no {harness_name}-applicable skills",
        dry_run=dry_run,
        prune_stale=prune_stale,
        create_dir=create_dir,
    )


def _resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def _points_into_managed(link: Path, source_dirs: Sequence[Path], output_dir: Path) -> bool:
    try:
        target = link.resolve(strict=False)
    except OSError:
        return False
    for source in source_dirs:
        try:
            target.relative_to(source)
            return True
        except ValueError:
            continue
    try:
        target.relative_to(output_dir)
    except ValueError:
        return False
    return True


def _prune_links_in(
    directory: Path,
    source_dirs: Sequence[Path],
    output_dir: Path,
    *,
    dry_run: bool,
) -> int:
    if not directory.is_dir():
        return 0
    pruned = 0
    for entry in sorted(directory.iterdir()):
        if not entry.is_symlink():
            continue
        if not _points_into_managed(entry, source_dirs, output_dir):
            continue
        if dry_run:
            logger.info("would prune stale symlink %s", entry)
        else:
            entry.unlink()
            logger.info("pruned stale symlink %s", entry)
        pruned += 1
    return pruned


def prune_harness_artifacts(
    harness: HarnessConfig,
    sources: Sequence[Source],
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> int:
    """Remove links and generated files managed for one harness."""
    source_dirs = [_resolve(source.path) for source in sources]
    output = _resolve(output_dir)
    pruned = 0

    if harness.rule_style == "mdc":
        if harness.rules_dir is not None:
            pruned += _prune_links_in(harness.rules_dir, source_dirs, output, dry_run=dry_run)
    else:
        link = harness.target_dir / harness.rule_file_name
        if link.is_symlink() and harness.rule_output is not None:
            try:
                matches = link.resolve(strict=False) == _resolve(harness.rule_output)
            except OSError:
                matches = False
            if matches:
                if dry_run:
                    logger.info("would prune stale symlink %s", link)
                else:
                    link.unlink()
                    logger.info("pruned stale symlink %s", link)
                pruned += 1
        if (
            harness.rule_output is not None
            and harness.rule_output.is_file()
            and not harness.rule_output.is_symlink()
        ):
            if dry_run:
                logger.info("would remove %s", harness.rule_output)
            else:
                harness.rule_output.unlink()
                logger.info("removed %s", harness.rule_output)
            pruned += 1

    pruned += _prune_links_in(harness.skills_dir, source_dirs, output, dry_run=dry_run)
    return pruned


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def existing_sources(sources: Sequence[Source]) -> list[Source]:
    """Filter to sources that exist, warning about missing ones."""
    existing: list[Source] = []
    for source in sources:
        if source.path.is_dir():
            existing.append(source)
        else:
            logger.warning("source not found, skipping: %s", source.path)
    if sources and not existing:
        raise ValueError("none of the configured sources exist")
    return existing


def run(config: CompileConfig, *, check: bool = False, dry_run: bool = False) -> int:
    """Execute generation + symlink sync. Returns process exit code."""
    effective_dry = dry_run or check
    sources = existing_sources(config.sources)
    rules = load_rules(sources)
    skills = load_skills(sources) if config.enable_skills else []
    logger.info(
        "loaded %d rule(s), %d skill(s) from %d source(s)",
        len(rules),
        len(skills),
        len(sources),
    )

    changed = False

    for harness in config.harnesses:
        if not harness.enabled:
            logger.info("skipped %s (disabled)", harness.name)
            if prune_harness_artifacts(harness, sources, config.output_dir, dry_run=effective_dry):
                changed = True
            continue

        if harness.rule_style == "mdc":
            created, _, pruned = sync_mdc_rule_links(
                rules,
                harness,
                dry_run=effective_dry,
                prune_stale=config.prune_stale,
                create_dir=config.create_target_dirs,
            )
            if created or pruned:
                changed = True
        elif sync_single_rule(
            rules,
            harness,
            dry_run=effective_dry,
            create_dir=config.create_target_dirs,
        ):
            changed = True

        if not config.enable_skills:
            logger.info("skipped %s skills (--no-skills)", harness.name)
            continue
        created, _, pruned = sync_skill_links(
            skills,
            harness.skills_dir,
            harness_name=harness.name,
            dry_run=effective_dry,
            prune_stale=config.prune_stale,
            create_dir=config.create_target_dirs,
        )
        if created or pruned:
            changed = True

    if check and changed:
        return 1
    return 0
