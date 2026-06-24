#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "requests",
#     "PyYAML",
#     "packaging",
#     "tomli",
# ]
# ///
"""Check Python deps and GitHub Actions against PyPI and GitHub."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

try:
    from packaging.requirements import Requirement, InvalidRequirement
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version, InvalidVersion
except ImportError:
    print("Error: 'packaging' not installed. Run: pip install packaging", file=sys.stderr)
    sys.exit(2)

# ── ANSI colors ──────────────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

GREEN  = lambda t: _c("32", t)
YELLOW = lambda t: _c("33", t)
RED    = lambda t: _c("31", t)
BOLD   = lambda t: _c("1", t)

# ── Data structures ───────────────────────────────────────────────────────────

class Status(str, Enum):
    OK        = "OK"
    OUTDATED  = "OUTDATED"
    NOT_FOUND = "NOT_FOUND"
    ERROR     = "ERROR"

class DepKind(str, Enum):
    PYTHON = "python"
    ACTION = "action"

@dataclass
class Dependency:
    name: str
    specifier: str
    kind: DepKind
    source_file: str
    source_line: int

@dataclass
class CheckResult:
    dep: Dependency
    status: Status
    current_version: Optional[str] = None
    latest_version: Optional[str] = None
    detail: Optional[str] = None

# ── File discovery ─────────────────────────────────────────────────────────────

_PEP723_RE = re.compile(r'^# /// script\s*\n((?:#[^\n]*\n)*?)# ///', re.MULTILINE)

def _has_pep723_block(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "# /// script" in f.read(4096)
    except OSError:
        return False

def discover_files(root: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {
        "requirements": [], "pyproject": [], "workflows": [], "scripts": []
    }
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel = os.path.relpath(dirpath, root)
        parts = rel.replace("\\", "/").split("/")
        in_workflows = (
            len(parts) >= 2
            and parts[-2] == ".github"
            and parts[-1] == "workflows"
        ) or (
            len(parts) >= 1
            and ".github" in parts
            and "workflows" in parts
            and parts.index("workflows") == parts.index(".github") + 1
        )

        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            if re.match(r'requirements.*\.txt$', fname, re.IGNORECASE):
                found["requirements"].append(fpath)
            elif fname == "pyproject.toml":
                found["pyproject"].append(fpath)
            elif in_workflows and fname.endswith((".yml", ".yaml")):
                found["workflows"].append(fpath)
            elif fname.endswith(".py") and _has_pep723_block(fpath):
                found["scripts"].append(fpath)

    return found

# ── Parsers ────────────────────────────────────────────────────────────────────

def parse_requirements_txt(path: str) -> list[Dependency]:
    deps: list[Dependency] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        print(f"Warning: cannot read {path}: {e}", file=sys.stderr)
        return []

    for lineno, raw in enumerate(lines, 1):
        line = raw.partition("#")[0].strip()
        if not line:
            continue
        if line.startswith(("-r", "-c", "-e", "--")):
            continue
        try:
            req = Requirement(line)
            deps.append(Dependency(
                name=req.name,
                specifier=str(req.specifier),
                kind=DepKind.PYTHON,
                source_file=path,
                source_line=lineno,
            ))
        except InvalidRequirement:
            deps.append(Dependency(
                name=line,
                specifier="",
                kind=DepKind.PYTHON,
                source_file=path,
                source_line=lineno,
            ))
    return deps

def parse_pyproject_toml(path: str) -> list[Dependency]:
    if tomllib is None:
        print(f"Warning: cannot parse {path} — tomllib/tomli not available. "
              "Install tomli: pip install tomli", file=sys.stderr)
        return []

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        print(f"Warning: cannot parse {path}: {e}", file=sys.stderr)
        return []

    deps: list[Dependency] = []

    # PEP 621
    pep621 = data.get("project", {}).get("dependencies", [])
    for i, entry in enumerate(pep621, 1):
        if not isinstance(entry, str):
            continue
        try:
            req = Requirement(entry)
            deps.append(Dependency(
                name=req.name,
                specifier=str(req.specifier),
                kind=DepKind.PYTHON,
                source_file=path,
                source_line=i,
            ))
        except InvalidRequirement:
            pass

    # Poetry
    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    for name, spec in poetry.items():
        if name.lower() == "python":
            continue
        if isinstance(spec, str):
            specifier = spec
        elif isinstance(spec, dict):
            specifier = spec.get("version", "")
        else:
            specifier = ""
        deps.append(Dependency(
            name=name,
            specifier=specifier,
            kind=DepKind.PYTHON,
            source_file=path,
            source_line=0,
        ))

    return deps

def parse_inline_script_metadata(path: str) -> list[Dependency]:
    """Parse PEP 723 inline script metadata (# /// script blocks) from a .py file."""
    if tomllib is None:
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        print(f"Warning: cannot read {path}: {e}", file=sys.stderr)
        return []

    m = _PEP723_RE.search(content)
    if not m:
        return []

    toml_lines = []
    for line in m.group(1).splitlines():
        toml_lines.append(line[2:] if line.startswith("# ") else ("" if line == "#" else line))

    try:
        data = tomllib.loads("\n".join(toml_lines))
    except Exception as e:
        print(f"Warning: cannot parse PEP 723 block in {path}: {e}", file=sys.stderr)
        return []

    deps: list[Dependency] = []
    start_line = content[: m.start()].count("\n") + 1

    for i, entry in enumerate(data.get("dependencies", []), start_line + 1):
        if not isinstance(entry, str):
            continue
        try:
            req = Requirement(entry)
            deps.append(Dependency(
                name=req.name,
                specifier=str(req.specifier),
                kind=DepKind.PYTHON,
                source_file=path,
                source_line=i,
            ))
        except InvalidRequirement:
            pass

    return deps

def _extract_uses(node: object) -> list[str]:
    results: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "uses" and isinstance(v, str):
                results.append(v)
            else:
                results.extend(_extract_uses(v))
    elif isinstance(node, list):
        for item in node:
            results.extend(_extract_uses(item))
    return results

def parse_workflow_yml(path: str) -> list[Dependency]:
    if yaml is None:
        print(f"Warning: cannot parse {path} — PyYAML not installed. "
              "Install: pip install PyYAML", file=sys.stderr)
        return []

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        doc = yaml.safe_load(content)
    except Exception as e:
        print(f"Warning: cannot parse {path}: {e}", file=sys.stderr)
        return []

    if doc is None:
        return []

    uses_values = _extract_uses(doc)
    lines = content.splitlines()

    deps: list[Dependency] = []
    for uses in uses_values:
        if uses.startswith("./") or uses.startswith("docker://"):
            continue
        if "@" not in uses:
            continue

        action_ref, _, ref = uses.partition("@")
        # Strip subdir: "owner/repo/subdir" → "owner/repo"
        parts = action_ref.split("/")
        if len(parts) < 2:
            continue
        name = f"{parts[0]}/{parts[1]}"

        # Approximate line number
        lineno = 0
        for i, line in enumerate(lines, 1):
            if uses in line:
                lineno = i
                break

        deps.append(Dependency(
            name=name,
            specifier=ref,
            kind=DepKind.ACTION,
            source_file=path,
            source_line=lineno,
        ))

    return deps

# ── HTTP session ───────────────────────────────────────────────────────────────

def make_session(github_token: Optional[str]) -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "check-deps/1.0"
    if github_token:
        session.headers["Authorization"] = f"Bearer {github_token}"
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session

# ── PyPI checker ───────────────────────────────────────────────────────────────

def check_pypi(dep: Dependency, session: requests.Session) -> CheckResult:
    url = f"https://pypi.org/pypi/{dep.name}/json"
    try:
        resp = session.get(url, timeout=10)
    except Exception as e:
        return CheckResult(dep, Status.ERROR, detail=str(e))

    if resp.status_code == 404:
        return CheckResult(dep, Status.NOT_FOUND, detail="Package not found on PyPI")

    if resp.status_code != 200:
        return CheckResult(dep, Status.ERROR, detail=f"HTTP {resp.status_code}")

    data = resp.json()
    latest_str = data["info"]["version"]

    try:
        latest = Version(latest_str)
    except InvalidVersion:
        return CheckResult(dep, Status.ERROR, detail=f"Cannot parse latest version: {latest_str}")

    if not dep.specifier:
        return CheckResult(dep, Status.OK, latest_version=latest_str)

    # Normalize poetry caret/tilde to something packaging can handle
    specifier_str = dep.specifier
    specifier_str = re.sub(r'\^(\d)', r'>=\1', specifier_str)  # ^1.2 → >=1.2
    specifier_str = re.sub(r'~(\d)', r'~=\1', specifier_str)   # ~1.2 → ~=1.2 (compatible release)

    try:
        spec_set = SpecifierSet(specifier_str, prereleases=False)
    except Exception:
        return CheckResult(dep, Status.OK, latest_version=latest_str,
                           detail=f"Cannot parse specifier '{dep.specifier}'")

    available = []
    for v_str in data.get("releases", {}):
        try:
            v = Version(v_str)
            if not v.is_prerelease:
                available.append(v)
        except InvalidVersion:
            pass

    matching = [v for v in available if v in spec_set]

    if not matching:
        return CheckResult(dep, Status.NOT_FOUND,
                           latest_version=latest_str,
                           detail=f"No release matches '{dep.specifier}'")

    current = str(max(matching))
    if latest not in spec_set:
        return CheckResult(dep, Status.OUTDATED,
                           current_version=current,
                           latest_version=latest_str,
                           detail=f"Newer version {latest_str} available")

    return CheckResult(dep, Status.OK, current_version=current, latest_version=latest_str)

# ── GitHub Actions checker ─────────────────────────────────────────────────────

_SHA_RE = re.compile(r'^[0-9a-f]{40}$', re.IGNORECASE)
_TAG_RE = re.compile(r'^v?\d')

def _parse_major(tag: str) -> Optional[int]:
    m = re.match(r'^v?(\d+)', tag)
    return int(m.group(1)) if m else None

def check_github_action(dep: Dependency, session: requests.Session) -> CheckResult:
    owner_repo = dep.name
    ref = dep.specifier

    # 1. Repo existence
    try:
        resp = session.get(f"https://api.github.com/repos/{owner_repo}", timeout=10)
    except Exception as e:
        return CheckResult(dep, Status.ERROR, detail=str(e))

    if resp.status_code == 404:
        return CheckResult(dep, Status.NOT_FOUND, detail=f"Repository {owner_repo} not found")

    if resp.status_code in (403, 429):
        reset = resp.headers.get("X-RateLimit-Reset", "unknown")
        return CheckResult(dep, Status.ERROR,
                           detail=f"GitHub rate limited (reset: {reset})")

    if resp.status_code != 200:
        return CheckResult(dep, Status.ERROR, detail=f"HTTP {resp.status_code}")

    # Full SHA: immutable, no further checks
    if _SHA_RE.match(ref):
        return CheckResult(dep, Status.OK, current_version=ref[:7])

    # Tag-like ref: validate and check for newer major
    if _TAG_RE.match(ref):
        try:
            tag_resp = session.get(
                f"https://api.github.com/repos/{owner_repo}/git/ref/tags/{ref}",
                timeout=10
            )
        except Exception as e:
            return CheckResult(dep, Status.ERROR, detail=str(e))

        if tag_resp.status_code == 404:
            return CheckResult(dep, Status.NOT_FOUND, detail=f"Tag '{ref}' not found")

        if tag_resp.status_code not in (200, 204):
            return CheckResult(dep, Status.ERROR, detail=f"Tag check HTTP {tag_resp.status_code}")

        # Check for newer major version
        current_major = _parse_major(ref)
        if current_major is not None:
            try:
                tags_resp = session.get(
                    f"https://api.github.com/repos/{owner_repo}/tags?per_page=100",
                    timeout=10
                )
                if tags_resp.status_code == 200:
                    tag_names = [t["name"] for t in tags_resp.json()]
                    newer = [m for name in tag_names
                             if (m := _parse_major(name)) is not None and m > current_major]
                    if newer:
                        best = max(newer)
                        return CheckResult(dep, Status.OUTDATED,
                                           current_version=ref,
                                           latest_version=f"v{best}",
                                           detail=f"Newer major version v{best} available")
            except Exception:
                pass  # tag exists, newer-check failed — return OK

        return CheckResult(dep, Status.OK, current_version=ref)

    # Branch or other ref: just confirm repo exists (already done above)
    return CheckResult(dep, Status.OK, current_version=ref,
                       detail="Branch ref — pinning to a tag or SHA recommended")

# ── Deduplication ──────────────────────────────────────────────────────────────

def deduplicate(deps: list[Dependency]) -> tuple[list[Dependency], dict]:
    seen: dict[tuple, Dependency] = {}
    index: dict[tuple, list[Dependency]] = {}
    for dep in deps:
        key = (dep.name.lower(), dep.specifier, dep.kind)
        if key not in seen:
            seen[key] = dep
            index[key] = []
        index[key].append(dep)
    return list(seen.values()), index

# ── Orchestration ──────────────────────────────────────────────────────────────

def run_checks(
    unique_deps: list[Dependency],
    session: requests.Session,
    max_workers: int = 10,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for dep in unique_deps:
            if dep.kind == DepKind.PYTHON:
                f = executor.submit(check_pypi, dep, session)
            else:
                f = executor.submit(check_github_action, dep, session)
            futures[f] = dep
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                dep = futures[future]
                results.append(CheckResult(dep, Status.ERROR, detail=str(e)))
    return results

# ── Output formatters ──────────────────────────────────────────────────────────

def _status_colored(status: Status) -> str:
    if status == Status.OK:
        return GREEN(status.value)
    if status == Status.OUTDATED:
        return YELLOW(status.value)
    return RED(status.value)

def _col_width(values: list[str], minimum: int = 10) -> int:
    return max(minimum, max((len(v) for v in values), default=0))

def format_table(
    file_to_results: dict[str, list[CheckResult]],
    root: str,
) -> str:
    out: list[str] = []
    totals: dict[Status, int] = {s: 0 for s in Status}

    for path, results in file_to_results.items():
        if not results:
            continue
        rel = os.path.relpath(path, root)
        header = f"── {rel} "
        out.append(BOLD(header + "─" * max(0, 60 - len(header))))

        py_results = [r for r in results if r.dep.kind == DepKind.PYTHON]
        act_results = [r for r in results if r.dep.kind == DepKind.ACTION]

        for group, label in ((py_results, "python"), (act_results, "action")):
            if not group:
                continue
            if label == "python":
                names = [r.dep.name for r in group]
                specs = [r.dep.specifier or "-" for r in group]
                currents = [r.current_version or "-" for r in group]
                latests = [r.latest_version or "-" for r in group]
                statuses = [r.status.value for r in group]

                w_name = _col_width(names + ["Package"])
                w_spec = _col_width(specs + ["Specifier"])
                w_curr = _col_width(currents + ["Current"])
                w_lat  = _col_width(latests + ["Latest"])
                w_stat = _col_width(statuses + ["Status"])

                header_row = (
                    f"{'Package':<{w_name}}  {'Specifier':<{w_spec}}  "
                    f"{'Current':<{w_curr}}  {'Latest':<{w_lat}}  Status"
                )
                out.append(header_row)
                out.append("─" * len(header_row))

                for r in group:
                    status_str = _status_colored(r.status)
                    detail = f"  # {r.detail}" if r.detail and r.status != Status.OK else ""
                    out.append(
                        f"{r.dep.name:<{w_name}}  {(r.dep.specifier or '-'):<{w_spec}}  "
                        f"{(r.current_version or '-'):<{w_curr}}  "
                        f"{(r.latest_version or '-'):<{w_lat}}  {status_str}{detail}"
                    )
                    totals[r.status] += 1
            else:
                names = [r.dep.name for r in group]
                refs  = [r.dep.specifier or "-" for r in group]
                latests = [r.latest_version or "-" for r in group]
                statuses = [r.status.value for r in group]

                w_name = _col_width(names + ["Action"])
                w_ref  = _col_width(refs + ["Ref"])
                w_lat  = _col_width(latests + ["Latest"])

                header_row = f"{'Action':<{w_name}}  {'Ref':<{w_ref}}  {'Latest':<{w_lat}}  Status"
                out.append(header_row)
                out.append("─" * len(header_row))

                for r in group:
                    status_str = _status_colored(r.status)
                    detail = f"  # {r.detail}" if r.detail and r.status != Status.OK else ""
                    out.append(
                        f"{r.dep.name:<{w_name}}  {(r.dep.specifier or '-'):<{w_ref}}  "
                        f"{(r.latest_version or '-'):<{w_lat}}  {status_str}{detail}"
                    )
                    totals[r.status] += 1

        out.append("")

    total = sum(totals.values())
    summary = (
        f"Summary: {total} checked — "
        f"{GREEN(str(totals[Status.OK]))} OK, "
        f"{YELLOW(str(totals[Status.OUTDATED]))} OUTDATED, "
        f"{RED(str(totals[Status.NOT_FOUND]))} NOT_FOUND, "
        f"{RED(str(totals[Status.ERROR]))} ERROR"
    )
    out.append(summary)
    return "\n".join(out)

def format_json(file_to_results: dict[str, list[CheckResult]], root: str) -> str:
    totals: dict[str, int] = {s.value: 0 for s in Status}
    files = []
    for path, results in file_to_results.items():
        items = []
        for r in results:
            items.append({
                "name": r.dep.name,
                "specifier": r.dep.specifier,
                "kind": r.dep.kind.value,
                "status": r.status.value,
                "current_version": r.current_version,
                "latest_version": r.latest_version,
                "detail": r.detail,
                "source_line": r.dep.source_line,
            })
            totals[r.status.value] += 1
        files.append({"path": os.path.relpath(path, root), "results": items})

    total = sum(totals.values())
    return json.dumps({"files": files, "summary": {"total": total, **totals}}, indent=2)

# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="check.py",
        description="Check Python deps and GitHub Actions against PyPI and GitHub.",
    )
    p.add_argument("--path", default=".", metavar="DIR",
                   help="Root directory to scan (default: current directory)")
    p.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN"), metavar="TOKEN",
                   help="GitHub token (or set GITHUB_TOKEN env var)")
    p.add_argument("--json", action="store_true", help="Output results as JSON")
    p.add_argument("--workers", type=int, default=10, metavar="N",
                   help="Parallel HTTP workers (default: 10)")
    return p

def main() -> int:
    args = build_parser().parse_args()
    root = os.path.abspath(os.path.expanduser(args.path))

    if not os.path.isdir(root):
        print(f"Error: '{root}' is not a directory", file=sys.stderr)
        return 2

    discovered = discover_files(root)
    file_to_deps: dict[str, list[Dependency]] = {}

    for path in discovered["requirements"]:
        file_to_deps[path] = parse_requirements_txt(path)

    for path in discovered["pyproject"]:
        file_to_deps[path] = parse_pyproject_toml(path)

    for path in discovered["workflows"]:
        file_to_deps[path] = parse_workflow_yml(path)

    for path in discovered["scripts"]:
        file_to_deps[path] = parse_inline_script_metadata(path)

    all_deps = [dep for deps in file_to_deps.values() for dep in deps]

    if not all_deps:
        print("No dependency files found.", file=sys.stderr)
        return 0

    action_count = sum(1 for d in all_deps if d.kind == DepKind.ACTION)
    if action_count > 20 and not args.github_token:
        print(
            f"Warning: {action_count} GitHub Actions found. Unauthenticated API limit is "
            "60 req/hr. Set GITHUB_TOKEN to avoid rate limiting.",
            file=sys.stderr,
        )

    session = make_session(args.github_token)
    unique_deps, _ = deduplicate(all_deps)
    unique_results = run_checks(unique_deps, session, max_workers=args.workers)

    result_map: dict[tuple, CheckResult] = {
        (r.dep.name.lower(), r.dep.specifier, r.dep.kind): r
        for r in unique_results
    }

    file_to_results: dict[str, list[CheckResult]] = {}
    for path, deps in file_to_deps.items():
        results = []
        for dep in deps:
            key = (dep.name.lower(), dep.specifier, dep.kind)
            if key in result_map:
                # Reattach original source info to the result
                orig = result_map[key]
                results.append(CheckResult(
                    dep=dep,
                    status=orig.status,
                    current_version=orig.current_version,
                    latest_version=orig.latest_version,
                    detail=orig.detail,
                ))
        file_to_results[path] = results

    if args.json:
        print(format_json(file_to_results, root))
    else:
        print(format_table(file_to_results, root))

    has_issues = any(
        r.status != Status.OK
        for results in file_to_results.values()
        for r in results
    )
    return 1 if has_issues else 0

if __name__ == "__main__":
    sys.exit(main())
