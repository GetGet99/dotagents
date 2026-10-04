"""Compile `rules/*.mdc` into per-platform AGENTS.md files and Cursor symlinks.

Source of truth: `rules/*.mdc` files with YAML frontmatter::

    ---
    alwaysApply: true
    description: "Optional human/agent-facing summary"
    platforms: ["cursor"]          # omit or leave empty => everywhere
    globs: ["*.tsx", "src/**"]     # or comma-separated string; omit => []
    ---

    # Rule body (markdown)

Outputs:

* ``autogen-opencode.md`` — rules where ``platforms`` is empty or contains
  ``"opencode"``. Same for ``autogen-codex.md`` (symlink one of these to
  ``AGENTS.md`` as needed).
* Cursor (native ``.mdc`` support): symlinks in ``~/.cursor/rules/<slug>.mdc``
  pointing at the source rule, but only for rules applying to ``"cursor"``.

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

logger = logging.getLogger("compile_rules")


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
class CompileConfig:
    """Fully resolved configuration (paths absolute; None = disabled)."""

    repo_root: Path
    rules_dir: Path
    enabled: frozenset[str]
    opencode_out: Path | None
    codex_out: Path | None
    cursor_rules_dir: Path | None
    prune_stale: bool = True
    create_cursor_dir: bool = True


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
    lines: list[str] = [
        "# Rules",
        "",
        "<!-- AUTO-GENERATED by scripts/compile_rules.py — do not edit. -->",
        "<!-- Edit rules/*.mdc instead, then re-run the script. -->",
        f"<!-- platform: {platform} -->",
        "",
    ]
    if not applicable:
        lines.append("<!-- No rules apply to this platform. -->")
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
    cli_opencode_out: Path | None = None,
    cli_codex_out: Path | None = None,
    cli_cursor_rules_dir: Path | None = None,
    cli_no_cursor: bool = False,
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

    prune_raw: object = behavior_cfg.get("prune_stale_cursor_symlinks", True)
    if not isinstance(prune_raw, bool):
        raise ValueError("[behavior] prune_stale_cursor_symlinks must be a boolean")
    create_raw: object = behavior_cfg.get("create_cursor_dir", True)
    if not isinstance(create_raw, bool):
        raise ValueError("[behavior] create_cursor_dir must be a boolean")

    return CompileConfig(
        repo_root=repo_root,
        rules_dir=rules_dir,
        enabled=enabled,
        opencode_out=opencode_out,
        codex_out=codex_out,
        cursor_rules_dir=cursor_rules_dir,
        prune_stale=prune_raw,
        create_cursor_dir=create_raw,
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


def sync_cursor_symlinks(
    rules: Sequence[Rule],
    cursor_dir: Path,
    *,
    dry_run: bool = False,
    prune_stale: bool = True,
    create_dir: bool = True,
) -> tuple[int, int, int]:
    """Sync ``cursor_dir`` symlinks; return ``(created, kept, pruned)``.

    Only symlinks are ever removed — regular files are left alone and merely
    logged. The directory itself is only created when at least one rule
    applies to Cursor.
    """
    expected: dict[str, Path] = {
        f"{r.slug}.mdc": r.source_path for r in rules if applies_to(r, "cursor")
    }
    created = kept = pruned = 0

    if not expected:
        logger.info("no cursor-applicable rules; leaving %s untouched", cursor_dir)
        return (0, 0, 0)

    if not cursor_dir.exists():
        if dry_run:
            logger.info("would create directory %s", cursor_dir)
            return (0, 0, 0)
        if not create_dir:
            logger.warning(
                "cursor rules dir %s missing and create_cursor_dir=false; "
                "skipping symlink creation",
                cursor_dir,
            )
            return (0, 0, 0)
        else:
            cursor_dir.mkdir(parents=True, exist_ok=True)
            logger.info("created directory %s", cursor_dir)
    elif not cursor_dir.is_dir():
        raise ValueError(f"cursor rules path is not a directory: {cursor_dir}")

    for name, target in sorted(expected.items()):
        link = cursor_dir / name
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
                link.symlink_to(target)
                logger.info("updated symlink %s -> %s", link, target)
            created += 1
        elif link.exists():
            logger.warning("not touching regular file %s", link)
        else:
            if dry_run:
                logger.info("would link %s -> %s", link, target)
            else:
                link.symlink_to(target)
                logger.info("linked %s -> %s", link, target)
            created += 1

    if prune_stale:
        for entry in sorted(cursor_dir.iterdir()):
            if entry.name.endswith(".mdc") and entry.name not in expected:
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile rules/*.mdc into AGENTS.md files and Cursor symlinks."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="repository root (holds dotagents.toml and default outputs)",
    )
    parser.add_argument("--rules-dir", type=Path, default=None)
    parser.add_argument("--opencode-out", type=Path, default=None)
    parser.add_argument("--codex-out", type=Path, default=None)
    parser.add_argument("--cursor-rules-dir", type=Path, default=None)
    parser.add_argument(
        "--platform",
        default=None,
        help="'all' or comma-separated subset of cursor,opencode,codex",
    )
    parser.add_argument(
        "--no-cursor", action="store_true", help="skip cursor symlink management"
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
        logger.info("skipped cursor (disabled)")
    else:
        sync_cursor_symlinks(
            rules,
            config.cursor_rules_dir,
            dry_run=effective_dry,
            prune_stale=config.prune_stale,
            create_dir=config.create_cursor_dir,
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
            cli_opencode_out=args.opencode_out,
            cli_codex_out=args.codex_out,
            cli_cursor_rules_dir=args.cursor_rules_dir,
            cli_no_cursor=args.no_cursor,
        )
        return run(config, check=args.check, dry_run=args.dry_run)
    except ValueError as exc:
        logger.error("error: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
