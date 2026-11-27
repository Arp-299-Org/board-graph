#!/usr/bin/env python3
"""Prompt templates, tool allow-lists, mode guidance, and routing docs for editor_step."""
from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import tool_paths  # noqa: E402
import validate_board  # noqa: E402

TOOL_ROOT = tool_paths.TOOL_ROOT
SCRIPTS_PREFIX = str(TOOL_ROOT / "scripts") if tool_paths.is_external() else "scripts"
PYTHON = sys.executable

ROUTING = validate_board.DESCRIPTION.routing_agent
RECIPE = validate_board.DESCRIPTION.root / ROUTING["recipe"] if ROUTING.get("recipe") else None

ROUTE_QUALITY_SUMMARY = (
    "Route quality & pad-exit rules: H/V/45-degree segments only (0.002 mm tolerance), no 90-degree corners, "
    "no backtracking or acute bends, min segment 0.15 mm, no track arcs, never graze own-net pads. "
    "When `Pad exits: centered` is set, cross each facing short edge at its center within 0.02 mm offset and 1 deg perpendicular."
)

PROMPT = """{board} is open, idle and verified ready in the real PCB Editor on this Mac (pid {pid}) — do not probe status at startup.

{task}

The board file is locked. The only way to change the board is in that open editor. Do not save it and do not close it. When you stop, a script saves what the editor holds and checks that. Nothing else counts, your last message included. If the check fails and time is left, you will be sent back.

You have no general shell here, whatever the recipe says. You can read files, and run these from the folder you are in, each as its own command with no `cd`:
- date
- {python} {scripts_prefix}/kicad_lay.py (in ipc or hybrid mode)
- {python} {scripts_prefix}/kicad_route.py (in interactive or hybrid mode)
- {python} {scripts_prefix}/kicad_edit.py list, delete or move-via
- {mac_legacy_line}

{copper_tools}

At {deadline} on the Mac, finish or cancel the current gesture, leave KiCad idle and return for inspection. Check the time with date. Do not start another edit after the handoff time.

Before you stop, look at what you did with `mac.py look`, and fix what is plainly wrong, such as a needless detour or corner, or a track that now bends badly. The script then runs the design check, route quality and the electrical audit, and another agent reviews the result.{board_context}{recipe}"""

RECIPE_TAIL = """

The routing recipe{whose} follows. Where it says `py -3`, run {python}.

{recipe}"""

BACK = """{what}

It is {now}. In {seconds} seconds, at {deadline}, finish or cancel the current gesture and return with KiCad idle.

Keep going. Use what you have not tried yet: `--mode shove` pushes other nets' tracks aside, a route may take several vias and `--through` corners, it may leave a pad in the other direction, a via or track can be dragged with `D`, and Command-Z undoes your last step in the editor."""

REVIEW = """A routing agent changed {board} in KiCad. Its task: {task}

The design and route-quality checks allowed this review. {electrical} {read} these pictures of the editor, before and after: {pictures}

What moved, from the board files:
{table}

Look for what those scripts cannot see: a needless detour or corner, tracks bunched or kinked where another route would be plainly better, and a sensitive net{nets} pushed somewhere worse. You read only: never change anything.
{earlier}
Answer one line per new finding, after any lines for earlier findings, and nothing else, at most 5, most serious first:
MUST FIX: what, which net and where, what to do instead. Use this only if the board or the player would really be hurt, or a plainly better route exists.
FINE AS IS: what, and why it is not worth changing.
If you find nothing new, answer: NOTHING.
Your final message is read line by line by a script, not by a person. Every line in it must start with FIXED <n>:, STILL OPEN <n>:, MUST FIX:, FINE AS IS:, PAD <ref.pad>: or be the single word NOTHING. Write no preamble, summary or closing remark (not even "Done checking"), and never "MUST FIX: none": any other line makes the review unreadable and any MUST FIX line is an open finding."""

EARLIER = """
An earlier review of this step found these, word for word, and the agent was sent back to fix them:
{findings}
Check each one in the pictures and the table: it names its places and nets. Answer each first, one line each with its number: FIXED 1: what you see now, or STILL OPEN 1: what you see. One you do not call FIXED stays open. Do not repeat one as a new finding.
"""

CODEX_PROMPT = """{board} is open, idle and verified ready in the real PCB Editor on this Mac (pid {pid}) — do not call kicad_ipc_status.py at startup.

{task}

The starting front and back board views are attached, and `view_image` is enabled so you can inspect PNGs yourself.
The board file is locked. The only way to change the board is in that open editor. Do not save it and do not close it. When you stop, the runner refills, saves what the editor holds, runs DRC, route quality, electrical and pad-exit checks, and sends fresh pictures to an independent reviewer. Nothing else counts, your last message included.

Use the right tool for each job:
{copper_tools}
- Exact copper edits where needed: `{python} {scripts_prefix}/kicad_edit.py --pid {pid}` (`list`, `delete`, `move-via`). It never creates copper.
- Visual inspection (REQUIRED after routing/editing): run `{python} {scripts_prefix}/mac.py look "PCB Editor"` and call `view_image` on the `mac.py look` PNG (and `kicad_route.py`'s `picture` when used). Fix anything that looks wrong (bad pad entry, needless detour, overhang, crowded neighbor) before returning, and briefly state what you saw in the image. A pass without a `mac.py look` after your last edit is sent back.
{mac_gui_line}
- KiCad is already ready on startup, and `kicad_lay.py` leaves KiCad idle; only run `{python} {scripts_prefix}/kicad_ipc_status.py` (or press Escape twice) before handoff if you used interactive/GUI tools or are recovering from a busy error.

At {deadline} on the Mac, finish or cancel the current gesture, leave KiCad idle and return for inspection. Do not start another edit after the handoff time.{board_context}{recipe}"""

GEMINI_PROMPT = """{board} is open, idle and verified ready in the real PCB Editor on this Mac (pid {pid}) — do not call kicad_ipc_status.py at startup.

{task}

The starting front and back board views are saved at: {start_pictures}. Inspect them with `read_file` before making edits.
The board file is locked. The only way to change the board is in that open editor. Do not save it and do not close it. When you stop, the runner refills, saves what the editor holds, runs DRC, route quality, electrical and pad-exit checks, and sends fresh pictures to an independent reviewer. Nothing else counts, your last message included.

Use the right tool for each job:
{copper_tools}
- Exact copper edits where needed: `{python} {scripts_prefix}/kicad_edit.py --pid {pid}` (`list`, `delete`, `move-via`). It never creates copper.
- Visual inspection (REQUIRED after routing/editing): run `{python} {scripts_prefix}/mac.py look "PCB Editor"` and inspect the `mac.py look` PNG (and `kicad_route.py`'s `picture` when used) using `read_file`. Fix anything that looks wrong (bad pad entry, needless detour, overhang, crowded neighbor) before returning, and briefly state what you saw in the image. A pass without a `mac.py look` after your last edit is sent back.
{mac_gui_line}
- KiCad is already ready on startup, and `kicad_lay.py` leaves KiCad idle; only run `{python} {scripts_prefix}/kicad_ipc_status.py` (or press Escape twice) before handoff if you used interactive/GUI tools or are recovering from a busy error.
- Mac KiCad IPC (`kicad_lay.py`, `kicad_route.py`, `kicad_edit.py`, `kicad_ipc_status.py`) and GUI screen capture (`mac.py`) require host IPC/WindowServer access outside the terminal sandbox; run them with `BypassSandbox: true` on the first call rather than retrying from inside the sandbox.

At {deadline} on the Mac, finish or cancel the current gesture, leave KiCad idle and return for inspection. Do not start another edit after the handoff time.{board_context}{recipe}"""

LOOK_ONLY_RETRY_PROMPT = (
    "The editor already holds your completed edits on `{path}` (PID {pid}).\n"
    "{what}\n"
    "Do NOT re-route or make copper changes unless your visual inspection reveals a real defect."
)

REVIEW_SEND_BACKS = 2


def _facade():
    mod = sys.modules.get("editor_step")
    if mod is not None:
        return mod
    main_mod = sys.modules.get("__main__")
    if main_mod is not None and pathlib.Path(getattr(main_mod, "__file__", "") or "").name == "editor_step.py":
        return main_mod
    return sys.modules[__name__]


def allowed_tools(mode: str, *, python: str | None = None, scripts_prefix: str | None = None) -> list:
    py = python if python is not None else getattr(_facade(), "PYTHON", PYTHON)
    sp = scripts_prefix if scripts_prefix is not None else getattr(_facade(), "SCRIPTS_PREFIX", SCRIPTS_PREFIX)
    base = ["Read", "Glob", "Grep", "Skill", "Bash(date:*)", f"Bash({py} {sp}/kicad_edit.py:*)"]
    if mode in ("ipc", "hybrid"):
        base.append(f"Bash({py} {sp}/kicad_lay.py:*)")
    if mode in ("interactive", "hybrid"):
        base.append(f"Bash({py} {sp}/kicad_route.py:*)")
        mac_cmds = ("look", "windows", "menus", "menu", "keys", "type", "mouse")
    else:
        mac_cmds = ("look", "windows")
    return [*base, *(f"Bash({py} {sp}/mac.py {cmd}:*)" for cmd in mac_cmds)]


def _ipc_cheat_sheet(python: str, scripts_prefix: str, pid: int, *, centered: bool = False) -> str:
    centered_prop = '"centered_exits":true,' if centered else ""
    return (
        "  IPC batch syntax (NEVER call write_file or create scratch JSON files; pass specs inline with `--json` or heredoc `--stdin`):\n"
        f"  `{python} {scripts_prefix}/kicad_lay.py --pid {pid} batch --stdin <<'EOF'\n"
        f'  {{"width":0.5,"via_diameter":0.6,"via_drill":0.3,{centered_prop}"routes":[\n'
        '    {"from":"J1.1","to":"J2.1","net":"/N","layer":"F.Cu","steps":[[10.0,20.0],{"via":[15.0,20.0],"layer":"B.Cu"}]},\n'
        '    {"from":"C1.2","to_via":[12.5,18.0],"pour_layer":"In1.Cu","net":"GND","layer":"F.Cu"},\n'
        '    {"from":"U2.6","to_point":[10.0,20.0],"net":"/N","layer":"F.Cu"}\n'
        "  ]}\n"
        "  EOF`\n"
        '  Rules: `"from"` and `"to"` are ALWAYS `"REF.PAD"` strings (never `[x, y]`). '
        'Use `"to_point": [x, y]` to T-tap a pad onto an existing same-net track or via (including a trunk laid earlier in the same `batch`) '
        'instead of putting coordinates in `"from"` or `"to"`. '
        'Use `"to_via": [x, y]` (optional `"pour_layer": "In1.Cu"`) to connect a pad into a filled same-net copper pour without fake inner-layer tie tracks. '
        'Only set `"centered_exits": true` when `Pad exits: centered` is required (pass an initial waypoint in `"steps"` when stacked IC pins share an X or Y column).\n'
        f"  For copper pours (`Pour:` tasks): `{python} {scripts_prefix}/kicad_lay.py --pid {pid} zones` inspects existing zones and "
        "`Edge.Cuts`; `pour --net GND --layer In1.Cu --board-outline --inset 0.3 --connect thermal --thermal-gap 0.25 --thermal-bridge 0.25 --replace` "
        "lays/replaces one plane (`delete-zones ID ...` removes unlocked copper pours; never delete or alter rule areas); "
        "`batch` also accepts `\"pours\":[...]` and standalone stitching `\"vias\":[{\"at\":[x,y],\"net\":\"GND\"}]`.\n"
        f"  (Or single-line `--json '{{...}}'`; for one pad-to-pad wire use `route START TARGET ...`). `steps` entries accept `[x, y]`, "
        "`{\"point\":[x,y],\"width\":W}`, or `{\"via\":[x,y],\"layer\":\"L\",\"width\":W}`. `kicad_lay.py` auto-snaps pad centers, "
        "adds centered short-edge stubs, expands 45-degree corners, merges collinear segments, refills zones, verifies live route quality and "
        "connectivity, diffs `kicad-cli` DRC, and rolls back atomically on any failure.\n"
        "  The board geometry brief below already gives world pad coordinates, facing escapes, courtyards, zones, netclasses, "
        "`.kicad_dru` rules, and existing copper: do NOT spend turns running `kicad_lay.py pads`, `kicad_edit.py list`, or reading "
        "`.kicad_pcb`/`.kicad_pro`/`.kicad_dru`/`kicad_lay.py`/`kicad_route_quality.py` unless re-checking after edits."
    )


def mode_guidance(mode: str, python: str, scripts_prefix: str, pid: int, *, centered: bool = False) -> str:
    if mode == "ipc":
        return (
            f"- Routing mode is `ipc` (checked atomic IPC only). Lay clean 45-degree copper chunks atomically with "
            f"`{python} {scripts_prefix}/kicad_lay.py --pid {pid} batch --json '...'` or `--stdin <<'EOF' ... EOF` (or `route ...`). "
            "NEVER call `write_file` or write scratch JSON files (`--file`), NEVER invoke `kicad_route.py`, `kicad_hover_key.py`, "
            "`mac.py type`, `mac.py mouse`, editing menus, or routing/editing hotkeys in `ipc` mode, never call `create_items` directly, "
            "never write temporary Python wrapper scripts, and never edit `.kicad_pcb` directly. For visual framing you may use "
            "`mac.py look`, `mac.py windows`, `mac.py menu \"PCB Editor\" \"View\" \"Zoom to Fit\"`, or view-only keys (`Home`, `pageup`, `pagedown`). "
            "If a wire cannot pass `kicad_lay.py` cleanly, stop so the runner records `needs_lead`.\n"
            f"{_ipc_cheat_sheet(python, scripts_prefix, pid, centered=centered)}\n"
            f"  {ROUTE_QUALITY_SUMMARY}"
        )
    if mode == "interactive":
        return (
            f"- Routing mode is `interactive` (GUI Push-and-Shove only). Lay copper ONLY through KiCad's real "
            f"interactive router: `{python} {scripts_prefix}/kicad_route.py` directly (pass `--pid {pid}`, and `--width MM`, "
            "`--width-at X,Y,MM` for a mid-route neck-down or width switch, `--through X,Y`, `--via X,Y`, `--layer`, "
            f"`--mode shove` as needed), or GUI `X` via `{scripts_prefix}/mac.py` / `{scripts_prefix}/kicad_hover_key.py`. "
            "NEVER invoke `kicad_lay.py`, raw `create_items`, temporary Python wrapper scripts, or direct `.kicad_pcb` "
            "edits, and never change KiCad's global Preferences (such as grid snap). If "
            f"`{scripts_prefix}/kicad_route.py` cannot lay a connection, stop so the runner records `needs_lead`.\n"
            f"  {ROUTE_QUALITY_SUMMARY}"
        )
    return (
        f"- Routing mode is `hybrid`: use the right checked tool for each region:\n"
        f"  * Clean open channels, parallel buses, and straight pad-to-pad runs: lay atomic checked 45-degree chunks with "
        f"`{python} {scripts_prefix}/kicad_lay.py --pid {pid} batch --json '...'` or `--stdin <<'EOF' ... EOF` (or `route ...`; "
        "never call `write_file` or create scratch files).\n"
        f"  * Dense spots, shoving existing copper, or surgical rework: use KiCad's interactive router "
        f"`{python} {scripts_prefix}/kicad_route.py` directly (pass `--pid {pid}`, and `--width MM`, `--width-at X,Y,MM`, "
        f"`--through X,Y`, `--via X,Y`, `--layer`, `--mode shove` as needed), or GUI `X` via `{scripts_prefix}/mac.py` / "
        f"`{scripts_prefix}/kicad_hover_key.py`.\n"
        "  * NEVER write temporary Python wrapper scripts importing `kipy`/`pcbnew`/`kicad_lay`/`kicad_route`, never call "
        "raw `create_items` outside `kicad_lay.py`, never edit `.kicad_pcb` directly, and never change KiCad Preferences.\n"
        f"{_ipc_cheat_sheet(python, scripts_prefix, pid, centered=centered)}\n"
        f"  {ROUTE_QUALITY_SUMMARY}"
    )


def recipe_text(python: str) -> str:
    """The end of the agent's prompt: the routing recipe with a line saying where it comes from, or nothing when
    the description names none."""
    f = _facade()
    recipe_path = getattr(f, "RECIPE", RECIPE)
    routing = getattr(f, "ROUTING", ROUTING)
    if recipe_path is None:
        return ""
    whose = f" from the {routing['recipe_skill']} skill" if routing.get("recipe_skill") else ""
    return RECIPE_TAIL.format(whose=whose, python=python, recipe=recipe_path.read_text(encoding="utf-8"))


def _routing_doc_paths() -> list[str]:
    routing = getattr(_facade(), "ROUTING", ROUTING)
    docs = list(routing.get("docs", []))
    plan = routing.get("plan")
    if plan and plan not in docs:
        docs.append(plan)
    return docs


def _doc_file(repo: pathlib.Path, name: str) -> pathlib.Path:
    candidate = repo / name
    if candidate.is_file():
        return candidate
    vb = getattr(_facade(), "validate_board", validate_board)
    return vb.DESCRIPTION.root / name


def operator_docs_text(repo: pathlib.Path) -> str:
    """Include the configured board routing plan inline (up to 8,000 chars) and note any reference docs."""
    f = _facade()
    routing = getattr(f, "ROUTING", ROUTING)
    doc_fn = getattr(f, "_doc_file", _doc_file)
    parts: list[str] = []
    plan_rel = routing.get("plan")
    if plan_rel:
        plan_file = doc_fn(repo, plan_rel)
        if plan_file.is_file():
            raw = plan_file.read_text(encoding="utf-8").strip()
            if raw:
                if len(raw) > 8_000:
                    raw = raw[:8_000].rstrip() + f"\n[...truncated; read full file at {plan_rel}]"
                parts.append(f"## Board routing plan ({plan_rel})\n{raw}")
    docs = [str(name) for name in routing.get("docs", []) if str(name) != plan_rel]
    if docs:
        parts.append("Board reference docs (read only if needed): " + ", ".join(docs) + ".")
    return "\n\n".join(parts)


def review_reading() -> dict:
    """The review prompt's parts from the description: what the reviewer reads before the pictures, and the
    sensitive nets it guards."""
    f = _facade()
    routing = getattr(f, "ROUTING", ROUTING)
    docs_fn = getattr(f, "_routing_doc_paths", _routing_doc_paths)
    docs = docs_fn()
    read = "Read"
    if docs:
        named = docs[0] if len(docs) == 1 else ", ".join(docs[:-1]) + " and " + docs[-1]
        read = f"Read {named}, then read"
    nets = f" ({routing['sensitive_nets']})" if routing.get("sensitive_nets") else ""
    return {"read": read, "nets": nets}


def _mac_prompt_lines(mode: str, python: str, scripts_prefix: str) -> tuple[str, str]:
    if mode == "ipc":
        managed = (
            f'- Viewport inspection when needed (`{python} {scripts_prefix}/mac.py look "PCB Editor"` is required after your last edit; '
            f'you may use `{python} {scripts_prefix}/mac.py menu "PCB Editor" "View" "Zoom to Fit"` or view-only keys '
            '`Home` / `pageup` / `pagedown` for framing; NEVER use `kicad_hover_key.py`, `type`, `mouse`, editing hotkeys, or non-View menus in `ipc` mode).'
        )
        legacy = f'{python} {scripts_prefix}/mac.py look, windows, or menu "PCB Editor" View ...'
        return managed, legacy
    managed = (
        f'- GUI dialogs when needed in interactive/hybrid mode (e.g. `E` Track Properties, `D` drag, `Q` dimensions): '
        f'`{python} {scripts_prefix}/kicad_hover_key.py` and `{python} {scripts_prefix}/mac.py` '
        '(`look`, `windows`, `menus`, `menu`, `keys`, `type`, `mouse`).'
    )
    legacy = f'{python} {scripts_prefix}/mac.py look, windows, menus, menu, keys, type or mouse'
    return managed, legacy


def format_operator_prompt(
    provider: str,
    *,
    board: pathlib.Path,
    pid: int,
    python: str,
    scripts_prefix: str,
    copper_tools: str,
    mac_gui_line: str,
    mac_legacy_line: str,
    task: str,
    now_str: str,
    deadline_str: str,
    minutes: int,
    board_ctx_block: str,
    start_pictures: list,
    recipe_str: str,
) -> str:
    """Format the initial operator prompt for codex, gemini, or legacy Claude."""
    if provider == "codex":
        return CODEX_PROMPT.format(
            board=board, pid=pid, python=python, scripts_prefix=scripts_prefix,
            copper_tools=copper_tools, mac_gui_line=mac_gui_line,
            task=task, deadline=deadline_str, board_context=board_ctx_block, recipe=recipe_str,
        )
    if provider == "gemini":
        return GEMINI_PROMPT.format(
            board=board, pid=pid, python=python, scripts_prefix=scripts_prefix,
            copper_tools=copper_tools, mac_gui_line=mac_gui_line,
            task=task, deadline=deadline_str, board_context=board_ctx_block, recipe=recipe_str,
            start_pictures=", ".join(str(p.resolve()) for p in start_pictures),
        )
    return PROMPT.format(
        board=board, pid=pid, python=python, scripts_prefix=scripts_prefix,
        copper_tools=copper_tools, mac_legacy_line=mac_legacy_line,
        task=task, now=now_str, deadline=deadline_str,
        minutes=minutes, board_context=board_ctx_block, recipe=recipe_str,
    )
