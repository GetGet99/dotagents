# dotagents

Rules and skills manager

Declare rules and skills once. Works for multiple harnesses


## Install

Requires Python ≥ 3.11.

```sh
uv tool install --editable .
# or: pipx install .
```

This provides the `dotagents` command.

## Use

Create your folders of skills and rules:

```
my-agent-content/
  rules/*.mdc
  skills/<name>/SKILL.md
```

Then run

```sh
dotagents harness enable cursor
dotagents harness enable codex
dotagents harness enable opencode
dotagents source add path/to/my-agent-content
dotagents build
```

All your coding agents get the same skills and rules. `*.mdc` files are converted to AGENTS.md format for harness not supporting `*.mdc` files.

## Commands

```sh
dotagents source add path/to/my-agent-content # register a source
dotagents source add path/to/agent-content-2  # multiple sources OK
dotagents source list
dotagents harness enable opencode             # built-ins start disabled
dotagents build                               # generate files + symlinks
dotagents build --check                       # CI: fail if outputs differ
dotagents source remove ~/Codes/agent-content-2
```

`dotagents build --source DIR` uses a one-off source instead of the registered
ones. With no registered sources, `build` falls back to the current directory
when it contains `rules/` or `skills/`.

## Harnesses

Built-in harnesses are `cursor`, `opencode`, and `codex`. All built-ins start
disabled, so each machine enables only the harnesses it uses:

```sh
dotagents harness list
dotagents harness enable codex
dotagents harness disable cursor
```

Declare a custom harness interactively:

```sh
dotagents harness add
```

Or non-interactively:

```sh
dotagents harness add claude \
  --target-dir ~/.claude \
  --rule-style single \
  --rule-file-name CLAUDE.md
```

### Built-in catalog

| name | target | rule style | rule file |
|---|---|---|---|
| cursor | `~/.cursor` | `mdc` | `AGENTS.md` (unused) |
| opencode | `~/.config/opencode` | `single` | `AGENTS.md` |
| codex | `~/.codex` | `single` | `AGENTS.md` |

Built-ins cannot be removed; disable them instead.

## Outputs

For each enabled harness, `build` writes:

* `rule_style = "mdc"`: symlinks in `<target>/rules/<slug>.mdc`.
* `rule_style = "single"`: `<output_dir>/autogen-<name>.md`, symlinked as
  `<target>/<rule_file_name>`.
* Skills: symlinks in `<target>/skills/<name>`. Universal skills (no
  `platforms`) are materialized for every harness; tagged skills only for the
  harnesses they name.

`platforms` in rule and skill frontmatter accepts harness names, including
custom ones. Omit `platforms` for content that applies everywhere.

Disabled harnesses are skipped, and their managed symlinks and generated files
are pruned on the next `build` (regular files are never touched).

### Source registry

Sources are stored per machine at
`${XDG_CONFIG_HOME:-~/.config}/dotagents/sources` (one absolute path per line).
`DOTAGENTS_CONFIG_DIR` overrides the whole config directory.

Missing sources are skipped with a warning. Duplicate rule slugs or skill names across
sources are a hard error: shared content must live in exactly one source.

### Settings

Optional settings live at `~/.config/dotagents/config.toml`. See
`config.toml.example` for all keys. Precedence is:

```
built-in defaults < config.toml < DOTAGENTS_* env vars < CLI flags
```

## Development

```sh
./compile.sh --check
PYTHONPATH=src python -m dotagents.cli --help
pytest
ruff check src tests
mypy src tests
```
