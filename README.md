# dotagents

My collection of agent skills and rules

## Compiling rules

Source of truth: `rules/*.mdc` (Cursor-format frontmatter: `alwaysApply`,
`description`, `platforms`, `globs` — note `platforms` is plural).

```bash
pip install -r scripts/requirements.txt
./compile.sh                  # all enabled platforms (short wrapper)
python scripts/compile_rules.py            # same, without the wrapper
python scripts/compile_rules.py --check    # CI: fail if outputs differ
python scripts/compile_rules.py --dry-run -v
```

* `autogen-opencode.md` / `autogen-codex.md` are generated (gitignored —
  everyone regenerates locally, then symlinks one to `AGENTS.md` as needed).
  `alwaysApply` rules are inlined; the rest become pointer blocks with an
  absolute path to the source rule. Single-line descriptions stay inline,
  multi-line ones are fenced. `globs` surface as `> Applies to:` / `Globs:`.
* Cursor is served via symlinks in `~/.cursor/rules/` (only symlinks are
  ever pruned; regular files are left alone).

Enabled platforms resolve by precedence: `dotagents.toml` <
`dotagents.local.toml` (gitignored, per-machine) < `DOTAGENTS_PLATFORMS`
env var < `--platform` flag. Disabled platforms are skipped entirely —
no files written, no directories created.