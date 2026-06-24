# check-deps

Checks Python requirements and GitHub Actions workflows for outdated or missing dependencies.

Scans recursively for:
- `requirements*.txt` — checked against [PyPI](https://pypi.org)
- `pyproject.toml` — PEP 621 and Poetry dependency tables
- `.github/workflows/*.yml` — `uses:` directives checked against GitHub

Reports `OK` / `OUTDATED` / `NOT_FOUND` / `ERROR` per dependency.

---

## GitHub Action

Use in any repo without copying files:

```yaml
# .github/workflows/check-deps.yml
name: Check Dependencies

on:
  push:
    branches: [main, master]
  pull_request:
  schedule:
    - cron: '0 8 * * 1'  # Weekly

jobs:
  check-deps:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: mbrede/check-deps-action@main
        with:
          path: .                              # directory to scan (default: .)
          github-token: ${{ secrets.GITHUB_TOKEN }}  # avoids rate limiting
```

**Behavior:**
- `OUTDATED` → `::warning::` annotation, job passes
- `NOT_FOUND` / `ERROR` → `::error::` annotation, job fails
- Writes a summary table to the GitHub Actions job summary page

**Inputs:**

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `path` | No | `.` | Directory to scan |
| `github-token` | No | `${{ github.token }}` | Token for GitHub API calls |

---

## Claude Code Skill

Install once, available as `/check-deps` in every Claude Code session.

**1. Copy the plugin files:**

```bash
mkdir -p ~/.claude/plugins/cache/local/check-deps/.claude-plugin
mkdir -p ~/.claude/plugins/cache/local/check-deps/skills/check-deps/scripts
mkdir -p ~/.claude/plugins/cache/local/check-deps/commands

cp check.py ~/.claude/plugins/cache/local/check-deps/skills/check-deps/scripts/
```

**2. Create the skill definition** at `~/.claude/plugins/cache/local/check-deps/skills/check-deps/SKILL.md`:

```markdown
---
name: check-deps
description: >
  Check all Python requirements (requirements*.txt, pyproject.toml) and GitHub Actions
  workflows (.github/workflows/*.yml) in a directory against PyPI and GitHub for existence,
  version validity, and available updates. Reports OK / OUTDATED / NOT_FOUND / ERROR per dep.
  Trigger: /check-deps [PATH], "check deps", "check requirements", "check actions".
---

Run the check script via Bash:
uv run {SKILL_DIR}/scripts/check.py --path "{PATH}" [--github-token "$GITHUB_TOKEN"]

If PATH not provided, use current working directory.
Print output verbatim. Exit 1 = issues found. Exit 2 = bad args.
```

**3. Register the slash command** at `~/.claude/plugins/cache/local/check-deps/commands/check-deps.toml`:

```toml
description = "Check Python deps and GitHub Actions against PyPI and GitHub for updates"
prompt = "Use the check-deps skill to check dependencies. Path to scan: '{{args}}' (if empty, use the current working directory)."
```

**4. Register the plugin** — add to `~/.claude/plugins/installed_plugins.json`:

```json
"local@check-deps": [{
  "scope": "user",
  "installPath": "/Users/YOU/.claude/plugins/cache/local/check-deps",
  "version": "1.0.0",
  "installedAt": "2026-01-01T00:00:00.000Z",
  "lastUpdated": "2026-01-01T00:00:00.000Z"
}]
```

**5. Enable the plugin** — add to `~/.claude/settings.json`:

```json
"enabledPlugins": {
  "local@check-deps": true
}
```

**6. Create the user command** at `~/.claude/commands/check-deps.md`:

```markdown
Check all Python requirements (requirements*.txt, pyproject.toml) and GitHub Actions
workflows (.github/workflows/*.yml) against PyPI and GitHub for existence and available updates.

Run via Bash:
uv run /Users/YOU/.claude/plugins/cache/local/check-deps/skills/check-deps/scripts/check.py \
  --path "$ARGUMENTS"

If $ARGUMENTS is empty, use the current working directory.
If GITHUB_TOKEN is set, pass --github-token "$GITHUB_TOKEN".
Print output verbatim.
```

**Usage:**

```
/check-deps /path/to/project
/check-deps .
/check-deps
```

---

## CLI

Requires [uv](https://docs.astral.sh/uv/). Dependencies are managed automatically via PEP 723 inline metadata — no `pip install` needed.

```bash
# Scan a directory
uv run check.py --path /path/to/project

# With GitHub token (recommended for repos with many workflow files)
uv run check.py --path . --github-token ghp_xxx

# JSON output
uv run check.py --path . --json

# More parallel workers (default: 10)
uv run check.py --path . --workers 20
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--path DIR` | `.` | Directory to scan |
| `--github-token TOKEN` | `$GITHUB_TOKEN` | GitHub API token |
| `--json` | off | Output JSON instead of table |
| `--workers N` | `10` | Parallel HTTP workers |

**Exit codes:** `0` = all OK · `1` = issues found · `2` = bad arguments

---

## Example output

```
── requirements.txt ─────────────────────────────────────
Package      Specifier    Current    Latest     Status
─────────────────────────────────────────────────────────
requests     >=2.28.0     2.28.2     2.32.3     OUTDATED
pydantic     ^2.0         2.6.1      2.6.1      OK
nonexistent  ==1.0        -          -          NOT_FOUND

── .github/workflows/ci.yml ─────────────────────────────
Action                   Ref    Latest    Status
─────────────────────────────────────────────────────────
actions/checkout         v3     v4        OUTDATED
actions/setup-python     v5     v5        OK

Summary: 5 checked — 2 OK, 2 OUTDATED, 1 NOT_FOUND, 0 ERROR
```
