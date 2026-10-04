"""Compile `rules/*.mdc` and `skills/*/SKILL.md` for the enabled platforms.

Source of truth for rules: `rules/*.mdc` files with YAML frontmatter::

    ---
    alwaysApply: true
    description: "Optional human/agent-facing summary"
    platforms: ["cursor"]          # omit or leave empty => everywhere
    globs: ["*.tsx", "src/**"]     # or comma-separated string; omit => []
    ---

    # Rule body (markdown)

Source of truth for skills: `skills/<name>/SKILL.md` (Agent Skills format).
Only ``platforms`` is interpreted (same semantics as rules: omit/empty/all
platforms => universal); ``name`` and ``description`` are required by the
skills spec and validated. All other frontmatter keys pass through untouched.

Outputs:

* ``autogen-opencode.md`` — rules where ``platforms`` is empty or contains
  ``"opencode"``. Same for ``autogen-codex.md`` (symlink one of these to
  ``AGENTS.md`` as needed).
* Cursor (native ``.mdc`` support): symlinks in ``~/.cursor/rules/<slug>.mdc``
  pointing at the source rule, but only for rules applying to ``"cursor"``.
* Skills (whole-directory symlinks, so nested files ride along):
  universal skills → ``~/.agents/skills/<name>`` (shared by all agents);
  platform-tagged skills → ``~/.config/opencode/skills/<name>``,
  ``~/.cursor/skills/<name>``, ``~/.codex/skills/<name>`` respectively.

Which platforms run is resolved by :func:`load_config` with precedence
(low to high): built-in defaults → ``dotagents.toml`` → ``dotagents.local.toml``
→ ``DOTAGENTS_*`` env vars → CLI flags. Disabled platforms are skipped
entirely: no files written, no directories created, no symlinks pruned.

Rendering contract for ``AGENTS.*.md``:

* ``alwaysApply: true`` → full body inlined (headings demoted by 2 so the
  rule's ``# Title`` nests under ``## <slug>`` as ``### Title``), plus a
  ``> Applies to: ...`` hint line when ``globs`` is non-empty.
* ``alwaysApply: false`` → advertising block: ``Description:`` (inline when
  single-line, fenced block when multi-line), ``Globs:`` line when globs are
  present, and a pointer sentence with the rule's absolute path.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence
from typing import Literal, cast

import yaml

Platform = Literal["cursor", "opencode", "codex"]

SUPPORTED_PLATFORMS: frozenset[str] = frozenset({"cursor", "opencode", "codex"})
CONFIG_FILENAME = "dotagents.toml"
LOCAL_CONFIG_FILENAME = "dotagents.local.toml"

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
    platforms: tuple[str, ...] | None  # None = universal (shared skills dir)


@dataclass(frozen=True, slots=True)
class CompileConfig:
    """Fully resolved configuration (paths absolute; None = disabled)."""

    repo_root: Path
    rules_dir: Path
    skills_dir: Path
    enabled: frozenset[str]
    opencode_out: Path | None
    codex_out: Path | None
    cursor_rules_dir: Path | None
    agents_skills_dir: Path  # shared; managed when universal skills exist
    opencode_skills_dir: Path | None
    cursor_skills_dir: Path | None
    codex_skills_dir: Path | None
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
        name = item.strip().lower()
        if name not in SUPPORTED_PLATFORMS:
            raise ValueError(
                f"{path}: unknown platform {item!r}; "
                f"supported: {sorted(SUPPORTED_PLATFORMS)}"
            )
        normalized.append(name)
    return tuple(normalized)


def parse_rule(path: Path, repo_root: Path) -> Rule:
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

    _ = repo_root  # reserved for future repo-relative pointer rendering
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


def load_rules(rules_dir: Path, repo_root: Path) -> list[Rule]:
    """Load and deterministically sort all ``*.mdc`` rules."""
    if not rules_dir.is_dir():
        raise ValueError(f"rules directory not found: {rules_dir}")
    files = sorted(rules_dir.glob("*.mdc"))
    if not files:
        logger.warning("no *.mdc files found in %s", rules_dir)
    rules = [parse_rule(p, repo_root) for p in files]
    # always-apply first, then alphabetical by slug — deterministic output.
    rules.sort(key=lambda r: (not r.always_apply, r.slug))
    return rules


def applies_to(rule: Rule, platform: Platform) -> bool:
    """Return True when a rule should be delivered to ``platform``."""
    if rule.platforms is None:
        return True
    return platform in rule.platforms


def parse_skill_frontmatter(text: str, path: Path) -> dict[str, object]:
    """Extract the YAML mapping from a `SKILL.md` file."""
    match = FRONTMATTER_RE.match(text)
    if match is None:
        raise ValueError(f"{path}: missing YAML frontmatter (expected --- fences)")
    loaded: object = yaml.safe_load(match.group("fm"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: frontmatter must be a YAML mapping")
    return {str(k): v for k, v in loaded.items()}


def parse_skill(skill_dir: Path, repo_root: Path) -> Skill:
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
    if platforms is not None and frozenset(platforms) >= SUPPORTED_PLATFORMS:
        platforms = None  # explicitly-all == universal (shared skills dir)

    try:
        source_dir = skill_dir.resolve()
    except OSError:
        source_dir = skill_dir.absolute()

    _ = repo_root  # reserved for future repo-relative pointer rendering
    return Skill(
        name=skill_dir.name,
        source_dir=source_dir,
        description=desc_raw.strip(),
        platforms=platforms,
    )


def load_skills(skills_dir: Path, repo_root: Path) -> list[Skill]:
    """Load and alphabetically sort all `skills/*/SKILL.md` skills."""
    if not skills_dir.is_dir():
        logger.warning("skills directory not found, skipping skills: %s", skills_dir)
        return []
    skills: list[Skill] = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        if not (entry / "SKILL.md").is_file():
            logger.warning("skipping %s: no SKILL.md", entry)
            continue
        skills.append(parse_skill(entry, repo_root))
    if not skills:
        logger.warning("no skills found in %s", skills_dir)
    return skills


def skill_applies_to(skill: Skill, platform: Platform) -> bool:
    """Return True when a skill should be linked into ``platform``'s dir."""
    if skill.platforms is None:
        return False  # universal skills live in the shared dir, not here
    return platform in skill.platforms


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


def render_agents_md(rules: Sequence[Rule], platform: Platform) -> str:
    """Render the full per-platform document (autogen-<platform>.md)."""
    applicable = [r for r in rules if applies_to(rule=r, platform=platform)]
    lines: list[str] = ["# Rules", ""]
    if not applicable:
        lines.append("No rules apply to this platform.")
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


def _read_toml_file(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            data: object = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid TOML in {path}: top level must be a table")
    return cast("dict[str, object]", data)


def _deep_merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    merged: dict[str, object] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(
                cast("dict[str, object]", merged[key]),
                cast("dict[str, object]", value),
            )
        else:
            merged[key] = value
    return merged


def _section(cfg: dict[str, object], name: str) -> dict[str, object]:
    raw: object = cfg.get(name, {})
    if not isinstance(raw, dict):
        raise ValueError(f"[${name}] section must be a table")
    return cast("dict[str, object]", raw)


def _parse_enabled_list(raw: object, origin: str) -> frozenset[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        items = [p.strip().lower() for p in raw.split(",") if p.strip()]
    elif isinstance(raw, list):
        items = []
        for entry in raw:
            if not isinstance(entry, str):
                raise ValueError(f"{origin}: platform entries must be strings")
            items.append(entry.strip().lower())
    else:
        raise ValueError(f"{origin}: expected a list of platform names or string")
    if not items or items == ["all"]:
        return None  # None = all supported
    unknown = [p for p in items if p not in SUPPORTED_PLATFORMS]
    if unknown:
        raise ValueError(
            f"{origin}: unknown platform(s) {unknown}; "
            f"supported: {sorted(SUPPORTED_PLATFORMS)}"
        )
    return frozenset(items)


def load_config(
    repo_root: Path,
    *,
    cli_platforms: str | None = None,
    cli_rules_dir: Path | None = None,
    cli_skills_dir: Path | None = None,
    cli_opencode_out: Path | None = None,
    cli_codex_out: Path | None = None,
    cli_cursor_rules_dir: Path | None = None,
    cli_agents_skills_dir: Path | None = None,
    cli_opencode_skills_dir: Path | None = None,
    cli_cursor_skills_dir: Path | None = None,
    cli_codex_skills_dir: Path | None = None,
    cli_no_cursor: bool = False,
    cli_no_skills: bool = False,
) -> CompileConfig:
    """Resolve configuration with documented precedence.

    Precedence (low → high): built-in defaults → ``dotagents.toml`` →
    ``dotagents.local.toml`` → ``DOTAGENTS_*`` env vars → CLI flags.
    """
    cfg: dict[str, object] = {}
    cfg = _deep_merge(cfg, _read_toml_file(repo_root / CONFIG_FILENAME))
    cfg = _deep_merge(cfg, _read_toml_file(repo_root / LOCAL_CONFIG_FILENAME))

    platforms_cfg = _section(cfg, "platforms")
    paths_cfg = _section(cfg, "paths")
    behavior_cfg = _section(cfg, "behavior")

    enabled: frozenset[str] | None = _parse_enabled_list(
        platforms_cfg.get("enabled"), CONFIG_FILENAME
    )

    env_platforms = os.environ.get("DOTAGENTS_PLATFORMS")
    if env_platforms is not None and env_platforms.strip() != "":
        parsed = _parse_enabled_list(env_platforms, "DOTAGENTS_PLATFORMS")
        enabled = parsed
    if cli_platforms is not None and cli_platforms.strip() != "":
        if cli_platforms.strip().lower() == "all":
            enabled = None
        else:
            parsed = _parse_enabled_list(cli_platforms, "--platform")
            enabled = parsed
    if enabled is None:
        enabled = frozenset(SUPPORTED_PLATFORMS)

    def _resolve_path(value: object, default: str) -> Path:
        raw = default if value is None else str(value)
        p = Path(raw).expanduser()
        return p if p.is_absolute() else repo_root / p

    rules_dir = _resolve_path(paths_cfg.get("rules_dir", "rules"), "rules")
    env_rules = os.environ.get("DOTAGENTS_RULES_DIR")
    if env_rules:
        rules_dir = _resolve_path(env_rules, env_rules)
    if cli_rules_dir is not None:
        p = cli_rules_dir.expanduser()
        rules_dir = p if p.is_absolute() else repo_root / p

    opencode_default: object = paths_cfg.get("opencode_out", "autogen-opencode.md")
    codex_default: object = paths_cfg.get("codex_out", "autogen-codex.md")
    cursor_default: object = paths_cfg.get("cursor_rules_dir", "~/.cursor/rules")

    env_opencode = os.environ.get("DOTAGENTS_OPENCODE_OUT")
    env_codex = os.environ.get("DOTAGENTS_CODEX_OUT")
    env_cursor = os.environ.get("DOTAGENTS_CURSOR_RULES_DIR")

    opencode_out: Path | None = None
    codex_out: Path | None = None
    cursor_rules_dir: Path | None = None

    if "opencode" in enabled:
        raw: object = env_opencode if env_opencode else opencode_default
        if cli_opencode_out is not None:
            raw = str(cli_opencode_out)
        opencode_out = _resolve_path(raw, str(raw))

    if "codex" in enabled:
        raw = env_codex if env_codex else codex_default
        if cli_codex_out is not None:
            raw = str(cli_codex_out)
        codex_out = _resolve_path(raw, str(raw))

    if "cursor" in enabled and not cli_no_cursor:
        raw = env_cursor if env_cursor else cursor_default
        if cli_cursor_rules_dir is not None:
            raw = str(cli_cursor_rules_dir)
        cursor_rules_dir = _resolve_path(raw, str(raw))

    skills_dir = _resolve_path(paths_cfg.get("skills_dir", "skills"), "skills")
    env_skills = os.environ.get("DOTAGENTS_SKILLS_DIR")
    if env_skills:
        skills_dir = _resolve_path(env_skills, env_skills)
    if cli_skills_dir is not None:
        p = cli_skills_dir.expanduser()
        skills_dir = p if p.is_absolute() else repo_root / p

    agents_default: object = paths_cfg.get("agents_skills_dir", "~/.agents/skills")
    env_agents_skills = os.environ.get("DOTAGENTS_AGENTS_SKILLS_DIR")
    agents_raw: object = env_agents_skills if env_agents_skills else agents_default
    if cli_agents_skills_dir is not None:
        agents_raw = str(cli_agents_skills_dir)
    agents_skills_dir = _resolve_path(agents_raw, str(agents_raw))

    skill_dir_defaults: dict[str, str] = {
        "opencode": "~/.config/opencode/skills",
        "cursor": "~/.cursor/skills",
        "codex": "~/.codex/skills",
    }
    skill_dir_env: dict[str, str | None] = {
        "opencode": os.environ.get("DOTAGENTS_OPENCODE_SKILLS_DIR"),
        "cursor": os.environ.get("DOTAGENTS_CURSOR_SKILLS_DIR"),
        "codex": os.environ.get("DOTAGENTS_CODEX_SKILLS_DIR"),
    }
    skill_dir_cli: dict[str, Path | None] = {
        "opencode": cli_opencode_skills_dir,
        "cursor": cli_cursor_skills_dir,
        "codex": cli_codex_skills_dir,
    }
    platform_skill_dirs: dict[str, Path | None] = {}
    for plat, default in skill_dir_defaults.items():
        if plat not in enabled:
            platform_skill_dirs[plat] = None
            continue
        cfg_key = f"{plat}_skills_dir"
        plat_raw: object = (
            skill_dir_env[plat] if skill_dir_env[plat] else paths_cfg.get(cfg_key, default)
        )
        if skill_dir_cli[plat] is not None:
            plat_raw = str(skill_dir_cli[plat])
        platform_skill_dirs[plat] = _resolve_path(plat_raw, str(plat_raw))

    prune_raw: object = behavior_cfg.get("prune_stale_symlinks", True)
    if not isinstance(prune_raw, bool):
        raise ValueError("[behavior] prune_stale_symlinks must be a boolean")
    create_raw: object = behavior_cfg.get("create_target_dirs", True)
    if not isinstance(create_raw, bool):
        raise ValueError("[behavior] create_target_dirs must be a boolean")

    return CompileConfig(
        repo_root=repo_root,
        rules_dir=rules_dir,
        skills_dir=skills_dir,
        enabled=enabled,
        opencode_out=opencode_out,
        codex_out=codex_out,
        cursor_rules_dir=cursor_rules_dir,
        agents_skills_dir=agents_skills_dir,
        opencode_skills_dir=platform_skill_dirs["opencode"],
        cursor_skills_dir=platform_skill_dirs["cursor"],
        codex_skills_dir=platform_skill_dirs["codex"],
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
            return (0, 0, 0)
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


def sync_cursor_symlinks(
    rules: Sequence[Rule],
    cursor_dir: Path,
    *,
    dry_run: bool = False,
    prune_stale: bool = True,
    create_dir: bool = True,
) -> tuple[int, int, int]:
    """Sync ``cursor_dir`` rule symlinks; return ``(created, kept, pruned)``."""
    expected: dict[str, Path] = {
        f"{r.slug}.mdc": r.source_path for r in rules if applies_to(r, "cursor")
    }
    return sync_link_dir(
        expected,
        cursor_dir,
        kind="cursor rule",
        empty_message="no cursor-applicable rules",
        suffix=".mdc",
        dry_run=dry_run,
        prune_stale=prune_stale,
        create_dir=create_dir,
    )


def sync_skill_links(
    skills: Sequence[Skill],
    target_dir: Path,
    *,
    platform: Platform | None,
    dry_run: bool = False,
    prune_stale: bool = True,
    create_dir: bool = True,
) -> tuple[int, int, int]:
    """Sync skill directory symlinks; return ``(created, kept, pruned)``.

    ``platform=None`` selects universal skills (shared dir); otherwise only
    skills explicitly tagged with that platform.
    """
    if platform is None:
        selected = [s for s in skills if s.platforms is None]
        empty_message = "no universal skills"
    else:
        selected = [s for s in skills if skill_applies_to(s, platform)]
        empty_message = f"no {platform}-tagged skills"
    expected: dict[str, Path] = {s.name: s.source_dir for s in selected}
    return sync_link_dir(
        expected,
        target_dir,
        kind="skill",
        empty_message=empty_message,
        dry_run=dry_run,
        prune_stale=prune_stale,
        create_dir=create_dir,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compile rules/*.mdc into autogen files + Cursor symlinks, "
            "and link skills/*/ into agent skills dirs."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="repository root (holds dotagents.toml and default outputs)",
    )
    parser.add_argument("--rules-dir", type=Path, default=None)
    parser.add_argument("--skills-dir", type=Path, default=None)
    parser.add_argument("--opencode-out", type=Path, default=None)
    parser.add_argument("--codex-out", type=Path, default=None)
    parser.add_argument("--cursor-rules-dir", type=Path, default=None)
    parser.add_argument("--agents-skills-dir", type=Path, default=None)
    parser.add_argument("--opencode-skills-dir", type=Path, default=None)
    parser.add_argument("--cursor-skills-dir", type=Path, default=None)
    parser.add_argument("--codex-skills-dir", type=Path, default=None)
    parser.add_argument(
        "--platform",
        default=None,
        help="'all' or comma-separated subset of cursor,opencode,codex",
    )
    parser.add_argument(
        "--no-cursor", action="store_true", help="skip cursor symlink management"
    )
    parser.add_argument(
        "--no-skills", action="store_true", help="skip skills symlink management"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if generated files would change (no writes)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log actions without touching the filesystem",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def run(config: CompileConfig, *, check: bool = False, dry_run: bool = False) -> int:
    """Execute generation + symlink sync. Returns process exit code."""
    effective_dry = dry_run or check
    rules = load_rules(config.rules_dir, config.repo_root)
    logger.info("loaded %d rule(s) from %s", len(rules), config.rules_dir)

    changed = False

    targets: list[tuple[str, Path | None, Platform]] = [
        ("opencode", config.opencode_out, "opencode"),
        ("codex", config.codex_out, "codex"),
    ]
    for name, out, platform in targets:
        if out is None:
            logger.info("skipped %s (disabled)", name)
            continue
        content = render_agents_md(rules, platform)
        if check:
            existing = out.read_text(encoding="utf-8") if out.is_file() else None
            if existing != content:
                logger.warning("check: %s would change", out)
                changed = True
            else:
                logger.info("check: %s up to date", out)
        elif write_if_changed(out, content, dry_run=dry_run):
            changed = True

    if config.cursor_rules_dir is None:
        logger.info("skipped cursor rules (disabled)")
    else:
        sync_cursor_symlinks(
            rules,
            config.cursor_rules_dir,
            dry_run=effective_dry,
            prune_stale=config.prune_stale,
            create_dir=config.create_target_dirs,
        )

    if not config.enable_skills:
        logger.info("skipped skills (--no-skills)")
    else:
        skills = load_skills(config.skills_dir, config.repo_root)
        logger.info("loaded %d skill(s) from %s", len(skills), config.skills_dir)
        sync_skill_links(
            skills,
            config.agents_skills_dir,
            platform=None,
            dry_run=effective_dry,
            prune_stale=config.prune_stale,
            create_dir=config.create_target_dirs,
        )
        skill_targets: list[tuple[str, Path | None, Platform]] = [
            ("opencode", config.opencode_skills_dir, "opencode"),
            ("cursor", config.cursor_skills_dir, "cursor"),
            ("codex", config.codex_skills_dir, "codex"),
        ]
        for name, target_dir, platform in skill_targets:
            if target_dir is None:
                logger.info("skipped %s skills (disabled)", name)
                continue
            sync_skill_links(
                skills,
                target_dir,
                platform=platform,
                dry_run=effective_dry,
                prune_stale=config.prune_stale,
                create_dir=config.create_target_dirs,
            )

    if check and changed:
        return 1
    _ = changed
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    repo_root: Path = args.repo_root.expanduser().resolve()
    try:
        config = load_config(
            repo_root,
            cli_platforms=args.platform,
            cli_rules_dir=args.rules_dir,
            cli_skills_dir=args.skills_dir,
            cli_opencode_out=args.opencode_out,
            cli_codex_out=args.codex_out,
            cli_cursor_rules_dir=args.cursor_rules_dir,
            cli_agents_skills_dir=args.agents_skills_dir,
            cli_opencode_skills_dir=args.opencode_skills_dir,
            cli_cursor_skills_dir=args.cursor_skills_dir,
            cli_codex_skills_dir=args.codex_skills_dir,
            cli_no_cursor=args.no_cursor,
            cli_no_skills=args.no_skills,
        )
        return run(config, check=args.check, dry_run=args.dry_run)
    except ValueError as exc:
        logger.error("error: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
