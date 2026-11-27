#!/usr/bin/env python3
"""Unified CLI entrypoint for the shared board-graph checkout.

Run `board-graph ...` from inside any project directory (or pass `--project <dir>` /
`BOARD_GRAPH_PROJECT`). Subcommands invoke the scripts inside TOOL_ROOT while targeting
that project's graph, boards, site settings, records, and reports.

Standard library only; Python 3.9+.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import boards  # noqa: E402
import tool_paths  # noqa: E402

TOOL_ROOT = tool_paths.TOOL_ROOT

SUBCOMMANDS = {
    "route": "scripts/routing_run.py",
    "validate": "scripts/validate_board.py",
    "run-checks": "scripts/run_board_checks.py",
    "track-craft": "scripts/board_track_craft.py",
    "view": "scripts/board_graph_view.py",
    "serve": "scripts/board_graph_serve.py",
    "mac": "scripts/mac.py",
    "tidy": "scripts/tidy.py",
    "boards": "scripts/boards.py",
    "tool": "scripts/board_graph_tool.py",
    "render": "tools/kicad/render_board_views.py",
    "render-schematic": "tools/kicad/render_schematic_pages.py",
    "kicad-cli": "tools/kicad/invoke_kicad_cli.py",
    "site-settings": "scripts/site_settings.py",
    "board-description": "scripts/board_description.py",
}
TOOL_ROOT_COMMANDS = frozenset({"check", "update", "starter", "unvendor"})
BOARD_HOIST_COMMANDS = frozenset({"route", "run-checks", "track-craft", "view"})
BOARD_ENV_COMMANDS = frozenset({"validate", "board-description"})
VALUE_FLAGS = frozenset({
    tool_paths.ARGUMENT,
    boards.ARGUMENT,
    "--note", "--by", "--evidence", "--attempt", "--adopt-from", "--target", "--graph", "--state",
    "--records", "--view", "--out", "--base", "--from", "--to", "--minutes", "--model",
    "--review-model", "--provider", "--review-provider", "--effort", "--agent",
    "--branch", "--tokens", "--reason", "--prompt", "--after", "--board-description",
    "--board-name", "--project-title",
    "--mac-tool-checkout", "--mac-tree", "--mac-steps", "--ref", "--version", "--root", "--skip",
    "--track", "--node", "--output", "--json", "--timeout", "--exclusions", "--production", "--base-name", "--name",
})


def usage(out=None) -> None:
    stream = out or sys.stdout
    stream.write(
        "usage: board-graph [--project DIR] [--board NAME] <command> [args...]\n\n"
        "For multi-board repos, --board NAME works before or after the command\n"
        "(where, next, record, check, route, validate, run-checks, view, board-description).\n\n"
        "Commands (run against the active project):\n"
        "  init [--skip PATH ...] [--board-name SLUG] [--project-title TITLE] [--autonomous [--until CARD] [--reason TEXT]]\n"
        "                         copy starter files, .gitattributes, and .gitignore into this folder\n"
        "  version                print installed board-graph version and location\n"
        "  where, next, record, brief, settle, check, graph, approvals, checks\n"
        "                         pass directly through to scripts/board_graph.py\n"
        "  route ...              scripts/routing_run.py\n"
        "  validate ...           scripts/validate_board.py\n"
        "  run-checks ...         scripts/run_board_checks.py\n"
        "  track-craft ...        scripts/board_track_craft.py (uses configured KiCad machine)\n"
        "  view ...               scripts/board_graph_view.py\n"
        "  serve ...              scripts/board_graph_serve.py\n"
        "  mac ...                scripts/mac.py\n"
        "  tidy ...               scripts/tidy.py\n"
        "  boards ...             scripts/boards.py\n"
        "  tool ...               scripts/board_graph_tool.py (auto-adds --root DIR)\n"
        "  render ...             tools/kicad/render_board_views.py\n"
        "  render-schematic ...   tools/kicad/render_schematic_pages.py\n"
        "  kicad-cli ...          tools/kicad/invoke_kicad_cli.py\n"
        "  kicad-run ...          scripts/kicad_run.ps1 (via pwsh)\n"
        "  kicad-preflight ...    scripts/kicad_preflight.ps1 (via pwsh)\n"
        "  site-settings ...      scripts/site_settings.py\n"
        "  board-description ...  scripts/board_description.py\n"
        "  help                   show this message\n"
    )


def _extract_flag(argv: list[str], flag: str) -> tuple[str | None, list[str]]:
    selected = None
    kept: list[str] = []
    i = 0
    while i < len(argv):
        word = argv[i]
        if word == "--":
            kept.extend(argv[i:])
            break
        if word == flag:
            if i + 1 >= len(argv) or not argv[i + 1].strip():
                raise ValueError(f"{flag} requires a value")
            selected = argv[i + 1]
            i += 2
            continue
        if word.startswith(flag + "="):
            raw = word.split("=", 1)[1]
            if not raw.strip():
                raise ValueError(f"{flag} requires a value")
            selected = raw
            i += 1
            continue
        if word in VALUE_FLAGS and i + 1 < len(argv):
            kept.extend((word, argv[i + 1]))
            i += 2
            continue
        kept.append(word)
        i += 1
    return selected, kept


def _extract_project_flag(argv: list[str]) -> tuple[pathlib.Path | None, list[str]]:
    raw, kept = _extract_flag(argv, tool_paths.ARGUMENT)
    if raw is None:
        return None, kept
    project = pathlib.Path(raw).expanduser().resolve()
    if not project.is_dir():
        raise ValueError(f"project directory does not exist: {project}")
    return project, kept


def _extract_leading_board(argv: list[str]) -> tuple[str | None, list[str]]:
    selected = None
    i = 0
    while i < len(argv):
        word = argv[i]
        if word == boards.ARGUMENT:
            if i + 1 >= len(argv) or not argv[i + 1].strip():
                raise ValueError(f"{boards.ARGUMENT} requires a board name")
            selected = argv[i + 1]
            i += 2
            continue
        if word.startswith(boards.ARGUMENT + "="):
            raw = word.split("=", 1)[1]
            if not raw.strip():
                raise ValueError(f"{boards.ARGUMENT} requires a board name")
            selected = raw
            i += 1
            continue
        break
    return selected, argv[i:]


def _find_pwsh() -> str | None:
    pwsh = shutil.which("pwsh")
    if pwsh:
        return pwsh
    fallback = pathlib.Path.home() / ".local" / "bin" / "pwsh"
    if fallback.is_file():
        return str(fallback)
    return None


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _kicad_ps_cmd(pwsh: str, ps_path: pathlib.Path, command: str, tail: list[str]) -> list[str]:
    """Build pwsh invocation, converting '-- <args...>' into a native PowerShell -ScriptArgs array."""
    if command != "kicad-run" or "--" not in tail:
        return [pwsh, "-NoProfile", "-File", str(ps_path), *tail]
    split_at = tail.index("--")
    head, post = tail[:split_at], tail[split_at + 1:]
    head_parts = [tok if (tok.startswith("-") and tok[1:2].isalpha()) else _ps_quote(tok) for tok in head]
    post_parts = ", ".join(_ps_quote(tok) for tok in post)
    joined_head = " ".join(head_parts)
    stmt = f"& {_ps_quote(str(ps_path))} {joined_head} -ScriptArgs @({post_parts}); exit $LASTEXITCODE"
    return [pwsh, "-NoProfile", "-NonInteractive", "-Command", stmt]


def _has_root_arg(args: list[str]) -> bool:
    return any(word == "--root" or word.startswith("--root=") for word in args)


def _first_positional(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        word = args[i]
        if word == "--root":
            i += 2
            continue
        if word.startswith("-"):
            i += 1
            continue
        return word
    return None


def _run_version() -> int:
    """Print the installed board-graph version and tool root directory."""
    manifest = TOOL_ROOT / "tools" / "board_graph_tool.json"
    version = "unknown"
    if manifest.is_file():
        import json
        try:
            version = str(json.loads(manifest.read_text(encoding="utf-8")).get("version") or "unknown")
        except (OSError, ValueError):
            pass
    print(f"board-graph {version} ({TOOL_ROOT})")
    return 0


def _split_init_args(tail: list[str]) -> tuple[bool, str, str, str | None, str | None, list[str]]:
    """Extract init options (--autonomous/--until/--reason/--board-name/--project-title) and forward the rest."""
    autonomous = False
    until = "quote_files"
    reason = "Autonomous run authorized via board-graph init --autonomous"
    board_name: str | None = None
    project_title: str | None = None
    forwarded: list[str] = []
    i = 0
    while i < len(tail):
        word = tail[i]
        if word == "--autonomous":
            autonomous = True
            i += 1
        elif word in ("--until", "--reason", "--board-name", "--project-title"):
            if i + 1 >= len(tail) or not tail[i + 1].strip():
                raise ValueError(f"{word} requires a value")
            val = tail[i + 1]
            if word == "--until":
                until = val
            elif word == "--reason":
                reason = val
            elif word == "--board-name":
                board_name = val
            else:
                project_title = val
            i += 2
        elif word.startswith(("--until=", "--reason=", "--board-name=", "--project-title=")):
            flag, val = word.split("=", 1)
            if not val.strip():
                raise ValueError(f"{flag} requires a value")
            if flag == "--until":
                until = val
            elif flag == "--reason":
                reason = val
            elif flag == "--board-name":
                board_name = val
            else:
                project_title = val
            i += 1
        else:
            forwarded.append(word)
            i += 1
    return autonomous, until, reason, board_name, project_title, forwarded


def _rewrite_example_value(value, board_name: str):
    if isinstance(value, str):
        return (value
                .replace("board/example.", f"board/{board_name}.")
                .replace("board/exports/example-", f"board/exports/{board_name}-"))
    if isinstance(value, list):
        return [_rewrite_example_value(item, board_name) for item in value]
    if isinstance(value, dict):
        return {k: _rewrite_example_value(v, board_name) for k, v in value.items()}
    return value


def _apply_board_identity(target: pathlib.Path, board_name: str | None, project_title: str | None) -> str | None:
    """Update starter board/board.json and tools/board_graph.json paths to match board_name and project_title."""
    import json
    import re

    if not board_name and not project_title:
        return None
    if board_name and not re.fullmatch(r"[A-Za-z0-9_-]+", board_name):
        return f"--board-name must match [A-Za-z0-9_-]+, got {board_name!r}"

    board_file = target / "board" / "board.json"
    if board_file.is_file():
        bdata = json.loads(board_file.read_text(encoding="utf-8"))
        if board_name:
            bdata = _rewrite_example_value(bdata, board_name)
            bdata["name"] = board_name
            bdata["package_name"] = board_name.upper()
        if project_title:
            bdata["project"] = project_title
        board_file.write_text(json.dumps(bdata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    graph_file = target / "tools" / "board_graph.json"
    if board_name and graph_file.is_file():
        gdata = json.loads(graph_file.read_text(encoding="utf-8"))
        gdata = _rewrite_example_value(gdata, board_name)
        for node in gdata.get("nodes", []):
            integ = node.get("integration")
            if isinstance(integ, dict) and isinstance(integ.get("boards"), dict) and "example" in integ["boards"]:
                val = integ["boards"].pop("example")
                integ["boards"][board_name] = val
        graph_file.write_text(json.dumps(gdata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return None


def _enable_autonomous(target: pathlib.Path, until: str, reason: str) -> str | None:
    """Validate and persist autonomous delegated config in starter tools/board_graph.json."""
    import json
    import types
    import board_autonomy

    graph_file = target / "tools" / "board_graph.json"
    if not graph_file.is_file():
        return f"cannot enable autonomous mode: {graph_file} does not exist"
    data = json.loads(graph_file.read_text(encoding="utf-8"))
    data["autonomy"] = {
        "mode": "delegated",
        "until": until,
        "authorized_by": "owner",
        "reason": reason,
    }
    nodes = {n["id"]: n for n in data.get("nodes", [])}
    tracks = {t["id"]: t for t in data.get("tracks", [])}
    stub = types.SimpleNamespace(
        data=data,
        nodes=nodes,
        tracks=tracks,
        track_nodes=lambda tid: [n["id"] for n in data.get("nodes", []) if n.get("track") == tid],
    )
    errs = board_autonomy.problems(stub)
    if errs:
        return "; ".join(errs)
    graph_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return None


def _run_init(explicit_project: pathlib.Path | None, tail: list[str]) -> int:
    """Scaffold starter project files, .gitattributes, and .gitignore into target directory."""
    try:
        autonomous, until, reason, board_name, project_title, starter_args = _split_init_args(tail)
    except ValueError as error:
        print(f"board-graph: {error}", file=sys.stderr)
        return 2
    if autonomous:
        import board_autonomy
        if until in board_autonomy.NEVER_DELEGATED:
            print(f"board-graph: autonomy.until cannot delegate {until}; order always requires owner", file=sys.stderr)
            return 2
    target = explicit_project or pathlib.Path.cwd().resolve()
    cmd = [sys.executable, "-B", str(TOOL_ROOT / SUBCOMMANDS["tool"]), "--root", str(target), "starter", *starter_args]
    done = subprocess.run(cmd, cwd=str(target), check=False)
    if done.returncode != 0:
        return done.returncode
    identity_err = _apply_board_identity(target, board_name, project_title)
    if identity_err:
        print(f"board-graph: {identity_err}", file=sys.stderr)
        return 2
    if autonomous:
        err = _enable_autonomous(target, until, reason)
        if err:
            print(f"board-graph: {err}", file=sys.stderr)
            return 2
        print(f"  enabled autonomous mode (delegated through {until}; order always requires owner)")
    gitattributes = target / ".gitattributes"
    if not gitattributes.exists():
        gitattributes.write_bytes(b"* text=auto eol=lf\n")
        print("  wrote .gitattributes")
    gitignore = target / ".gitignore"
    if not gitignore.exists():
        gitignore.write_bytes(
            b"logs/board-graph/\n*.kicad_prl\nfp-info-cache\n~*.lck\n__pycache__/\n"
            b"tools/board_graph_view/view.json\ntools/board_graph_view/view.json.*.tmp\n"
        )
        print("  wrote .gitignore")
    print(
        f"\nInitialized board-graph project in {target}.\n"
        "Next steps:\n"
        "  1. Put your KiCad files in board/ and edit board/board.json\n"
        "  2. Edit tools/board_graph_site.json (set one_machine: true or kicad_machine settings)\n"
        "  3. Run board-graph check and board-graph where"
    )
    return 0


def _build_tool_cmd(explicit_project: pathlib.Path | None, tail: list[str]) -> tuple[pathlib.Path, list[str]]:
    """Build subprocess project root and argv for `board-graph tool ...`."""
    tool_args = tail
    explicit_root = _has_root_arg(tool_args)
    first_pos = _first_positional(tool_args)
    try:
        project = explicit_project or tool_paths.find_project_root()
    except ValueError:
        if not explicit_root and first_pos != "starter":
            raise
        project = pathlib.Path.cwd().resolve()
    if not explicit_root and (first_pos in TOOL_ROOT_COMMANDS):
        tool_args = ["--root", str(project), *tool_args]
    return project, [sys.executable, "-B", str(TOOL_ROOT / SUBCOMMANDS["tool"]), *tool_args]


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        explicit_project, rest = _extract_project_flag(raw)
        leading_board, after_leading = _extract_leading_board(rest)
    except ValueError as error:
        print(f"board-graph: {error}", file=sys.stderr)
        return 2
    if not after_leading:
        usage(sys.stderr)
        return 2
    if after_leading[0] in ("help", "-h", "--help"):
        usage(sys.stdout)
        return 0

    command = after_leading[0]
    tail = after_leading[1:]
    if command == "version":
        return _run_version()
    if command == "init":
        return _run_init(explicit_project, tail)

    extra_env: dict[str, str] = {}
    if command == "tool":
        try:
            project, cmd = _build_tool_cmd(explicit_project, tail)
        except ValueError:
            print("board-graph: no project found here; pass --project DIR or --root DIR", file=sys.stderr)
            return 2
    elif command in ("kicad-run", "kicad-preflight"):
        try:
            project = explicit_project or tool_paths.find_project_root()
            if leading_board:
                extra_env[boards.ENV] = boards.find(leading_board, project).description
        except (ValueError, boards.BoardsError) as error:
            print(f"board-graph: {error}", file=sys.stderr)
            return 2
        pwsh = _find_pwsh()
        if not pwsh:
            print("board-graph: pwsh was not found on PATH", file=sys.stderr)
            return 1
        ps_script = "kicad_run.ps1" if command == "kicad-run" else "kicad_preflight.ps1"
        cmd = _kicad_ps_cmd(pwsh, TOOL_ROOT / "scripts" / ps_script, command, tail)
    elif command in SUBCOMMANDS:
        try:
            project = explicit_project or tool_paths.find_project_root()
        except ValueError as error:
            print(f"board-graph: {error}", file=sys.stderr)
            return 2
        if command == "render":
            try:
                tail_board, _ = _extract_flag(tail, boards.ARGUMENT)
            except ValueError as error:
                print(f"board-graph: {error}", file=sys.stderr)
                return 2
            if leading_board and tail_board:
                print(
                    "board-graph: pass --board either before or after 'render', not both",
                    file=sys.stderr,
                )
                return 2
            sub_args = ([boards.ARGUMENT, leading_board, *tail] if leading_board else tail)
        elif command in BOARD_ENV_COMMANDS:
            try:
                tail_board, clean_tail = _extract_flag(tail, boards.ARGUMENT)
                chosen_board = tail_board or leading_board
                if chosen_board:
                    extra_env[boards.ENV] = boards.find(chosen_board, project).description
            except (ValueError, boards.BoardsError) as error:
                print(f"board-graph: {error}", file=sys.stderr)
                return 2
            sub_args = clean_tail
        elif command in BOARD_HOIST_COMMANDS:
            try:
                tail_board, clean_tail = _extract_flag(tail, boards.ARGUMENT)
            except ValueError as error:
                print(f"board-graph: {error}", file=sys.stderr)
                return 2
            chosen_board = tail_board or leading_board
            sub_args = ([boards.ARGUMENT, chosen_board, *clean_tail] if chosen_board else clean_tail)
        else:
            if leading_board:
                try:
                    extra_env[boards.ENV] = boards.find(leading_board, project).description
                except boards.BoardsError as error:
                    print(f"board-graph: {error}", file=sys.stderr)
                    return 2
            sub_args = tail
        cmd = [sys.executable, "-B", str(TOOL_ROOT / SUBCOMMANDS[command]), *sub_args]
    else:
        try:
            project = explicit_project or tool_paths.find_project_root()
        except ValueError as error:
            print(f"board-graph: {error}", file=sys.stderr)
            return 2
        cmd = [sys.executable, "-B", str(TOOL_ROOT / "scripts" / "board_graph.py"), *rest]

    env = dict(os.environ)
    env[tool_paths.ENV] = str(project)
    env.update(extra_env)
    done = subprocess.run(cmd, cwd=str(project), env=env, check=False)
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
