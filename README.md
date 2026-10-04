# dotagents

My collection of agent skills and rules

## Compiling rules

Source of truth: `rules/*.mdc` (Cursor-format frontmatter: `alwaysApply`,
`description`, `platforms`, `globs` — note `platforms` is plural).

```bash
pip install -r scripts/requirements.txt
./compile.sh                  # all enabled platforms (short wrapper)
python scripts/compile.py            # same, without the wrapper
python scripts/compile.py --check    # CI: fail if outputs differ
python scripts/compile.py --dry-run -v
```

* `autogen-opencode.md` / `autogen-codex.md` are generated (gitignored —
  everyone regenerates locally, then symlinks one to `AGENTS.md` as needed).
  `alwaysApply` rules are inlined; the rest become pointer blocks with an
  absolute path to the source rule. Single-line descriptions stay inline,
  multi-line ones are fenced. `globs` surface as `> Applies to:` / `Globs:`.
* Cursor is served via symlinks in `~/.cursor/rules/` (only symlinks are
  ever pruned; regular files are left alone).
* Skills in `skills/<name>/SKILL.md` are linked as whole directories:
  untagged (or all-platforms) skills → `~/.agents/skills/` shared by all
  agents; `platforms`-tagged skills → `~/.config/opencode/skills/`,
  `~/.cursor/skills/`, `~/.codex/skills/` respectively. Only `platforms`
  is interpreted (`name`/`description` are validated per the skills spec,
  everything else passes through). Disabled platforms are skipped entirely.

Enabled platforms resolve by precedence: `dotagents.toml` <
`dotagents.local.toml` (gitignored, per-machine) < `DOTAGENTS_PLATFORMS`
env var < `--platform` flag. Disabled platforms are skipped entirely —
no files written, no directories created.