"""Command-line entry point for dotagents."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from dotagents import compile as compile_mod
from dotagents import config as config_mod
from dotagents import harness as harness_mod

logger = logging.getLogger("dotagents")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dotagents",
        description=(
            "Manage agent rules and skills from one or more content sources. "
            "A source is a directory containing rules/ and/or skills/."
        ),
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="override the config directory (default: XDG config/dotagents)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    source = subparsers.add_parser("source", help="manage content sources")
    source_sub = source.add_subparsers(dest="source_command", required=True)

    add = source_sub.add_parser("add", help="register a content source")
    add.add_argument("path", type=Path, help="source directory to add")

    remove = source_sub.add_parser("remove", help="unregister a content source")
    remove.add_argument("path", type=Path, help="source directory to remove")

    source_sub.add_parser("list", help="list registered sources")

    harness = subparsers.add_parser("harness", help="manage harnesses")
    harness_sub = harness.add_subparsers(dest="harness_command", required=True)

    harness_sub.add_parser("list", help="list built-in and custom harnesses")

    enable = harness_sub.add_parser("enable", help="enable a harness")
    enable.add_argument("name", help="harness name")

    disable = harness_sub.add_parser("disable", help="disable a harness")
    disable.add_argument("name", help="harness name")

    add_harness = harness_sub.add_parser("add", help="declare a custom harness")
    add_harness.add_argument("name", nargs="?", default=None)
    add_harness.add_argument("--target-dir", type=Path, default=None)
    add_harness.add_argument("--rule-style", choices=("mdc", "single"), default=None)
    add_harness.add_argument("--rule-file-name", default=None)

    remove_harness = harness_sub.add_parser("remove", help="remove a custom harness")
    remove_harness.add_argument("name", help="harness name")

    build = subparsers.add_parser("build", help="generate rule files and symlinks")
    build.add_argument(
        "--source",
        type=Path,
        action="append",
        default=None,
        help="use this source for this run instead of the registered sources (repeatable)",
    )
    build.add_argument("--output-dir", type=Path, default=None)
    build.add_argument("--no-skills", action="store_true", help="skip skills symlink management")
    build.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if generated files would change (no writes)",
    )
    build.add_argument(
        "--dry-run",
        action="store_true",
        help="log actions without touching the filesystem",
    )
    build.add_argument("-v", "--verbose", action="store_true")
    return parser


def _prompt(label: str, default: str | None = None) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        logger.error("%s is required", label)


def _prompt_bool(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        logger.error("please answer y or n")


def _cmd_source_add(args: argparse.Namespace, config_dir: Path) -> int:
    source = config_mod.add_source(config_dir, args.path)
    if not (source / "rules").is_dir() and not (source / "skills").is_dir():
        logger.warning("source has neither rules/ nor skills/: %s", source)
    sources = [compile_mod.Source(path) for path in config_mod.read_sources(config_dir)]
    for report in compile_mod.find_duplicate_names(sources):
        logger.warning(report)
    logger.info("added source %s", source)
    return 0


def _cmd_source_remove(args: argparse.Namespace, config_dir: Path) -> int:
    source = config_mod.remove_source(config_dir, args.path)
    logger.info("removed source %s; run 'dotagents build' to prune links", source)
    return 0


def _cmd_source_list(args: argparse.Namespace, config_dir: Path) -> int:
    _ = args
    paths = config_mod.read_sources(config_dir)
    if not paths:
        print("No sources registered. Run 'dotagents source add <path>'.")
        return 0
    sources = [compile_mod.Source(path) for path in paths]
    for source in sources:
        status = "ok" if source.path.is_dir() else "missing"
        rules, skills = compile_mod.scan_source_names(source)
        print(f"{source.path} ({status}, {len(rules)} rule(s), {len(skills)} skill(s))")
    duplicates = compile_mod.find_duplicate_names(sources)
    if duplicates:
        print()
        print("Duplicates:")
        for report in duplicates:
            print(f"  {report}")
    return 0


def _cmd_harness_list(args: argparse.Namespace, config_dir: Path) -> int:
    _ = args
    specs = harness_mod.read_harnesses(config_dir)
    if not specs:
        print("No harnesses configured.")
        return 0
    rows: list[tuple[str, ...]] = [
        ("NAME", "STATUS", "SOURCE", "TARGET", "RULE STYLE", "RULE FILE")
    ]
    for name in sorted(specs):
        spec = specs[name]
        rows.append(
            (
                name,
                "enabled" if spec.enabled else "disabled",
                "builtin" if name in harness_mod.BUILTIN_HARNESSES else "custom",
                str(spec.target_dir) if spec.target_dir is not None else "-",
                spec.rule_style,
                spec.rule_file_name if spec.rule_style == "single" else "-",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return 0


def _cmd_harness_enable(args: argparse.Namespace, config_dir: Path) -> int:
    harness_mod.set_enabled(config_dir, args.name, True)
    logger.info("enabled harness %s", args.name)
    return 0


def _cmd_harness_disable(args: argparse.Namespace, config_dir: Path) -> int:
    harness_mod.set_enabled(config_dir, args.name, False)
    _prune_harness(args.name, config_dir)
    logger.info("disabled harness %s", args.name)
    return 0


def _cmd_harness_add(args: argparse.Namespace, config_dir: Path) -> int:
    name = args.name
    while name is None:
        try:
            name = _prompt("Harness name")
            harness_mod.validate_name(name)
        except ValueError as exc:
            logger.error("%s", exc)
            name = None

    target = args.target_dir
    if target is None:
        target = Path(_prompt("Target folder", f"~/.{name}"))
    resolved_target = config_mod.resolve_path(target, Path.cwd())

    if args.rule_style is None:
        rule_style: harness_mod.RuleStyle = (
            "mdc" if _prompt_bool("Supports .mdc rules?", False) else "single"
        )
    else:
        rule_style = cast("harness_mod.RuleStyle", args.rule_style)

    rule_file_name = args.rule_file_name
    if rule_file_name is None:
        if rule_style == "single":
            rule_file_name = _prompt("Rule file name", "AGENTS.md")
        else:
            rule_file_name = "AGENTS.md"

    spec = harness_mod.HarnessSpec(
        name=name,
        target_dir=resolved_target,
        rule_style=rule_style,
        rule_file_name=rule_file_name,
    )
    harness_mod.add_harness(config_dir, spec)
    logger.info("added harness %s", name)
    return 0


def _cmd_harness_remove(args: argparse.Namespace, config_dir: Path) -> int:
    if args.name in harness_mod.BUILTIN_HARNESSES:
        raise config_mod.ConfigError(
            f"{args.name!r} is a built-in harness; disable it instead of removing it"
        )
    _prune_harness(args.name, config_dir)
    harness_mod.remove_harness(config_dir, args.name)
    logger.info("removed harness %s", args.name)
    return 0


def _prune_harness(name: str, config_dir: Path) -> None:
    cwd = Path.cwd()
    spec = harness_mod.read_harnesses(config_dir).get(name)
    if spec is None:
        return
    output_dir = compile_mod.resolve_output_dir(config_dir, cwd=cwd)
    harness = compile_mod.resolve_harness(spec, output_dir)
    sources = config_mod.read_sources(config_dir)
    if not sources and ((cwd / "rules").is_dir() or (cwd / "skills").is_dir()):
        sources = [cwd]
    existing = [compile_mod.Source(path) for path in sources if path.is_dir()]
    compile_mod.prune_harness_artifacts(harness, existing, output_dir)


def _cmd_build(args: argparse.Namespace, config_dir: Path) -> int:
    cwd = Path.cwd()
    if args.source:
        sources = [config_mod.resolve_path(path, cwd) for path in args.source]
    else:
        sources = config_mod.read_sources(config_dir)
        if not sources:
            if (cwd / "rules").is_dir() or (cwd / "skills").is_dir():
                logger.info("no sources registered; using current directory: %s", cwd)
                sources = [cwd]
            else:
                raise config_mod.ConfigError(
                    "no sources registered; run 'dotagents source add <path>'"
                )

    config = compile_mod.load_config(
        sources,
        config_dir=config_dir,
        cwd=cwd,
        cli_output_dir=args.output_dir,
        cli_no_skills=args.no_skills,
    )
    return compile_mod.run(config, check=args.check, dry_run=args.dry_run)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    config_dir = (args.config_dir or config_mod.default_config_dir()).expanduser()
    config_dir = config_dir.resolve(strict=False)
    try:
        if args.command == "source":
            if args.source_command == "add":
                return _cmd_source_add(args, config_dir)
            if args.source_command == "remove":
                return _cmd_source_remove(args, config_dir)
            if args.source_command == "list":
                return _cmd_source_list(args, config_dir)
            raise config_mod.ConfigError(f"unknown source command: {args.source_command}")
        if args.command == "harness":
            if args.harness_command == "list":
                return _cmd_harness_list(args, config_dir)
            if args.harness_command == "enable":
                return _cmd_harness_enable(args, config_dir)
            if args.harness_command == "disable":
                return _cmd_harness_disable(args, config_dir)
            if args.harness_command == "add":
                return _cmd_harness_add(args, config_dir)
            if args.harness_command == "remove":
                return _cmd_harness_remove(args, config_dir)
            raise config_mod.ConfigError(f"unknown harness command: {args.harness_command}")
        if args.command == "build":
            return _cmd_build(args, config_dir)
        raise config_mod.ConfigError(f"unknown command: {args.command}")
    except ValueError as exc:
        logger.error("error: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
