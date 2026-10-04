# dotagents

My collection of agent skills and rules

Rules: `rules/*.mdc`
Skills: `skills/*/SKILL.md`


## Compiling

```bash
pip install -r scripts/requirements.txt # install requirements

# Compile and use
./compile.sh
# or
python scripts/compile.py

# additional args
python scripts/compile.py --check
python scripts/compile.py --dry-run -v
```

## Generated files

* `autogen-opencode.md` / `autogen-codex.md`. Your job to symlink there.
* Skills and rules for Cursor are symlinked

Enabled platforms resolve by precedence: `dotagents.toml` <
`dotagents.local.toml` (gitignored, per-machine) < `DOTAGENTS_PLATFORMS`
env var < `--platform` flag. Disabled platforms are skipped entirely —
no files written, no directories created.