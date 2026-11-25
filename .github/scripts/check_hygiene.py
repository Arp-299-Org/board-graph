#!/usr/bin/env python3
"""Repository hygiene guardrails for file/function size, parser/IPC singletons, Python 3.9, comments, shell, and CI YAML."""
from __future__ import annotations

import ast
import json
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCAN_DIRS = ("scripts", "tools/kicad", "dev", "docker")

MAX_FILE_LINES = 1000
MAX_FUNC_LINES = 120

GRANDFATHERED_FILES = {
    "scripts/board_graph_actions.py": 1489,
    "scripts/board_graph_workspace.py": 1906,
    "scripts/editor_step.py": 1074,
    "scripts/kicad_lay.py": 1445,
    "scripts/kicad_route.py": 1995,
}

GRANDFATHERED_FUNCTIONS = {
    "scripts/board_context.py:extract_board_context": 269,
    "scripts/board_context.py:render_board_context": 250,
    "scripts/board_graph_codex.py:supervise": 183,
    "scripts/board_graph_view.py:artifacts": 153,
    "scripts/kicad_finalize_jlc_outputs.py:main": 146,
    "scripts/kicad_lay.py:_check_planned_obstacles": 164,
    "scripts/kicad_lay.py:execute_batch": 248,
    "scripts/kicad_lay.py:create_copper": 134,
    "scripts/kicad_lay_plan.py:_plan_route_geometry": 126,
    "scripts/kicad_lay_plan.py:plan_pour": 140,
    "scripts/kicad_package_production.py:main": 401,
    "scripts/kicad_route.py:run": 187,
    "scripts/routing_codex.py:audit_commands": 131,
    "scripts/routing_evidence.py:adoption_problem": 124,
    "scripts/routing_gemini.py:run": 158,
    "tools/kicad/check_track_craft.py:analyse": 316,
}

RULES_GEOM_DEFS = {
    "local_to_world",
    "world_to_local",
    "pad_signed_distance",
    "parse_dru_rules",
    "dru_min_clearance_mm",
    "load_project_rules",
    "resolve_netclass",
}

IPC_DEFS = {
    "project_identity",
    "verify_live_project",
    "connect_board",
    "extract_pad_geometry",
    "pad_copper_box",
}

OWNER_CHAT_COMMENT_RE = re.compile(r"\(the owner,\s*202\d-\d\d-\d\d")
TODO_COMMENT_RE = re.compile(r"\b(TODO|FIXME|XXX)\b")
PUBLIC_LEAK_PATTERNS = (
    ("legacy project tag", re.compile(r"(?i)\brt-?1\b|\brt1[_.-]|\bRT1_")),
    ("retired private test harness", re.compile(r"test_editor_step\.sh")),
    ("dated owner transcript", OWNER_CHAT_COMMENT_RE),
    ("personal workstation name", re.compile(r"(?i)\bmac\s*mini\b")),
    ("tailnet IP address", re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b")),
    ("personal email address", re.compile(r"(?i)[A-Za-z0-9._%+-]+@gmail\.com")),
    ("personal macOS user path", re.compile(r"/Users/(?!builder\b|kicad\b|x\b)[A-Za-z0-9._-]+")),
)
PUBLIC_SCAN_ROOTS = ("README.md", "UPGRADING.md", "docs", "scripts", "tools", "dev", ".github", "board")
UNQUOTED_STEP_COLON_RE = re.compile(r'^\s*-\s+name:\s+(?!["\']).*:\s')
INLINE_PY_RE = re.compile(r"python3 -c '([^']+)'")
HEREDOC_PY_RE = re.compile(r"python3\s+-\s+<<'PY'\r?\n(.*?)\r?\nPY\b", re.S)


def _has_future_annotations(tree: ast.Module) -> bool:
    for node in tree.body[:8]:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(alias.name == "annotations" for alias in node.names):
                return True
    return False


def _expr_has_bitor(node: ast.AST | None) -> bool:
    if node is None:
        return False
    return any(isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr) for sub in ast.walk(node))


def _check_comments(rel: str, lines: list[str]) -> list[str]:
    violations: list[str] = []
    for idx, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            continue
        if OWNER_CHAT_COMMENT_RE.search(stripped):
            violations.append(f"{rel}:{idx}: dated owner-chat transcript in comment; document the invariant instead")
        if TODO_COMMENT_RE.search(stripped):
            violations.append(f"{rel}:{idx}: unresolved marker in comment")
    return violations


def _check_file(
    path: pathlib.Path,
    seen_files: dict[str, int],
    seen_funcs: dict[str, int],
) -> list[str]:
    violations: list[str] = []
    rel = path.relative_to(REPO_ROOT).as_posix()
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    line_count = len(lines)
    seen_files[rel] = line_count

    file_cap = GRANDFATHERED_FILES.get(rel, MAX_FILE_LINES)
    if line_count > file_cap:
        violations.append(f"{rel}:1: {line_count} lines exceeds cap {file_cap}")

    if rel != "scripts/kicad_rules_geom.py":
        for idx, line in enumerate(lines, 1):
            if "intersectsCourtyard" in line:
                violations.append(f"{rel}:{idx}: '.kicad_dru' intersectsCourtyard parsing belongs in scripts/kicad_rules_geom.py")

    violations.extend(_check_comments(rel, lines))

    tree = ast.parse(src, filename=str(path))
    has_future = _has_future_annotations(tree)
    match_cls = getattr(ast, "Match", None)

    for node in ast.walk(tree):
        if match_cls is not None and isinstance(node, match_cls):
            violations.append(f"{rel}:{node.lineno}: match/case statement is not Python 3.9 compatible")
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        fn_len = (node.end_lineno or node.lineno) - node.lineno + 1
        fn_key = f"{rel}:{node.name}"
        seen_funcs[fn_key] = max(seen_funcs.get(fn_key, 0), fn_len)
        fn_cap = GRANDFATHERED_FUNCTIONS.get(fn_key, MAX_FUNC_LINES)
        if fn_len > fn_cap:
            violations.append(f"{rel}:{node.lineno}: function '{node.name}' is {fn_len} lines, exceeds cap {fn_cap}")
        if rel != "scripts/kicad_rules_geom.py" and node.name in RULES_GEOM_DEFS:
            violations.append(f"{rel}:{node.lineno}: duplicate helper def '{node.name}' belongs in scripts/kicad_rules_geom.py")
        if rel != "scripts/kicad_ipc.py" and node.name in IPC_DEFS:
            violations.append(f"{rel}:{node.lineno}: duplicate IPC helper def '{node.name}' belongs in scripts/kicad_ipc.py")
        if not has_future:
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            if any(_expr_has_bitor(arg.annotation) for arg in args) or _expr_has_bitor(node.returns):
                violations.append(
                    f"{rel}:{node.lineno}: union annotation on '{node.name}' requires 'from __future__ import annotations' on Python 3.9"
                )
    return violations


def _check_shell_and_workflows() -> list[str]:
    violations: list[str] = []
    for rel in ("scripts/mac_screen.sh", "docker/entrypoint.sh"):
        target = REPO_ROOT / rel
        if not target.is_file():
            continue
        raw = target.read_bytes()
        if b"\r\n" in raw:
            violations.append(f"{rel}:1: CRLF line endings forbidden in POSIX shell script")
        text = raw.decode("utf-8")
        pattern = INLINE_PY_RE if rel == "scripts/mac_screen.sh" else HEREDOC_PY_RE
        for idx, snippet in enumerate(pattern.findall(text), 1):
            try:
                compile(snippet, f"{rel}#{idx}", "exec")
            except SyntaxError as exc:
                violations.append(f"{rel}: inline Python #{idx} SyntaxError: {exc.msg}")
    for wf in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        rel = wf.relative_to(REPO_ROOT).as_posix()
        for idx, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
            if UNQUOTED_STEP_COLON_RE.match(line):
                violations.append(f"{rel}:{idx}: unquoted ': ' in YAML step name must be quoted")
    return violations


def _check_stale_grandfathered(seen_files: dict[str, int], seen_funcs: dict[str, int]) -> list[str]:
    violations: list[str] = []
    for rel, cap in sorted(GRANDFATHERED_FILES.items()):
        actual = seen_files.get(rel)
        if actual is None or actual <= MAX_FILE_LINES or cap <= MAX_FILE_LINES:
            violations.append(f".github/scripts/check_hygiene.py: stale GRANDFATHERED_FILES entry '{rel}' ({actual})")
    for fn_key, cap in sorted(GRANDFATHERED_FUNCTIONS.items()):
        actual = seen_funcs.get(fn_key)
        if actual is None or actual <= MAX_FUNC_LINES or cap <= MAX_FUNC_LINES:
            violations.append(f".github/scripts/check_hygiene.py: stale GRANDFATHERED_FUNCTIONS entry '{fn_key}' ({actual})")
    return violations


def _check_manifest_coverage() -> list[str]:
    manifest_path = REPO_ROOT / "tools" / "board_graph_tool.json"
    if not manifest_path.is_file():
        return []
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    listed = set(data.get("files", {})) | set(data.get("starter", []))
    violations: list[str] = []
    for folder in ("scripts", "scripts/hooks", "tools/kicad"):
        base = REPO_ROOT / folder
        if not base.is_dir():
            continue
        for py_file in sorted(base.glob("*.py")):
            rel = py_file.relative_to(REPO_ROOT).as_posix()
            if rel not in listed:
                violations.append(
                    f"{rel}:1: missing from tools/board_graph_tool.json (run board_graph_tool.py manifest --add {rel})"
                )
    return violations


def _check_public_hygiene() -> list[str]:
    violations: list[str] = []
    seen: list[pathlib.Path] = []
    for entry in PUBLIC_SCAN_ROOTS:
        p = REPO_ROOT / entry
        if p.is_file():
            seen.append(p)
        elif p.is_dir():
            seen.extend(
                sorted(
                    c
                    for c in p.rglob("*")
                    if c.is_file() and c.suffix in (".py", ".ps1", ".sh", ".md", ".json", ".yml", ".html", ".plist")
                )
            )
    for path in seen:
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel == ".github/scripts/check_hygiene.py":
            continue
        for idx, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for label, rx in PUBLIC_LEAK_PATTERNS:
                if rx.search(line):
                    violations.append(f"{rel}:{idx}: public hygiene check flagged {label}")
    return violations


def main() -> int:
    files = sorted(
        p
        for folder in SCAN_DIRS
        if (REPO_ROOT / folder).is_dir()
        for p in (REPO_ROOT / folder).glob("*.py")
        if not p.name.startswith("test_")
    )
    seen_files: dict[str, int] = {}
    seen_funcs: dict[str, int] = {}
    all_violations: list[str] = []
    for path in files:
        all_violations.extend(_check_file(path, seen_files, seen_funcs))
    all_violations.extend(_check_stale_grandfathered(seen_files, seen_funcs))
    all_violations.extend(_check_shell_and_workflows())
    all_violations.extend(_check_manifest_coverage())
    all_violations.extend(_check_public_hygiene())
    if all_violations:
        for msg in all_violations:
            print(msg, file=sys.stderr)
        print(f"hygiene check failed: {len(all_violations)} violation(s) across {len(files)} file(s)", file=sys.stderr)
        return 1
    print(
        f"hygiene check ok: {len(files)} scripts checked "
        f"({len(GRANDFATHERED_FILES)} grandfathered files, {len(GRANDFATHERED_FUNCTIONS)} grandfathered functions)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
