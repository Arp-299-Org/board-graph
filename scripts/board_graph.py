#!/usr/bin/env python3
"""Where the board work is: the graph in tools/board_graph.json and its position in SQLite.

`where` prints the position on every track, the node's task and outcomes, who is working it
or how long it has waited, and the last transitions, then the workspace: each checkout against
GitHub's main, what tidy has left, and the pull requests a position waits on. By default it
reads only this checkout (no fetch) and the database, and carries the rest from the last full
read, each such line saying "as of" when; `where --full` fetches, asks the Mac and GitHub, and
keeps what it read for the quick reads after. A track is between two cards until something is on
the next one: `next` starts the next card in line, the one verb. For a person's card it opens an attempt for `--by`, so the card is on, and first
runs the card's script when one is connected, its report theirs to cite; for a script's card
connected here it brings the checkouts current with tidy, runs the script as that attempt and
records what it reported, with its report as evidence; at a card waiting on a merge it checks the
merge instead; on a card that is already on it says who is on it; a list's card (the framework)
is named with `--node`. `record` writes
one outcome at the current card, with its evidence and the card's owned inputs hashed, into the
SQLite state and as one JSON record in git, which it commits and pushes itself; on a branch it
also keeps the branch's pull request: opened as a draft at the first record, its description the
branch's records and CI's checks, ready only at the record that asks the owner to merge once CI's checks have
passed on the commit it asks about (a pass alone leaves it a draft). It closes what
was on the card, and a `needs_owner` that asks for a merge names the pull request and the
outcome that follows it. `checks` keeps a validate run's board checks in the database: a validate workflow's last
step, on the Mac, or scripts/run_board_checks.py in a project without one, so `where`, the page and the Board checks
card read the result there and never from GitHub.
`brief <card>` prints a card's brief, the predetermined context whoever starts it is given, without starting it;
`next` prints the same brief as it starts a person's card and keeps it on the attempt it opened. `settle <card>`
settles a run once its thread has ended: the thread's tokens on its attempt, and needs_lead when no record closed
the card. `graph` prints
the graph in Mermaid form. `check` validates the JSON and what the cards name.

The state is one SQLite database, on the KiCad machine (the agents' machine, always on):
the site settings' state_database in its main checkout, mac.CHECKOUT. On the Mac every checkout opens
it as a file (place()); from the owner's PC every checkout reaches it over the SSH path scripts/mac.py
uses (Remote): a read fetches a copy of the database and answers from it, and each write is one
round trip that runs the same State method on the Mac (`serve`), one transaction under SQLite's write
lock there, with the check that decides it (the position, who holds the node) under the same lock,
and brings a fresh copy back. When the Mac does not answer, the command refuses in one line and
writes nothing, here or there: there is no other live database. `--state` or BOARD_GRAPH_STATE
names another database (a test's, a scratch one), opened as a file. Record files are written in this
checkout, to be committed from its branch; the Mac's database imports, under its lock, the record
files its main checkout holds. Every write on the Mac rewrites view.json in its main checkout's
tools/board_graph_view, the folder scripts/board_graph_serve.py serves the page from, at once from
the database with no git and no network (board_graph_view.py's quick snapshot); with another
database, nothing follows unless `--view` names a folder.

A repository may list several boards (scripts/boards.py). Each has its own cards file, records folder, database
and view, and one process works on one board: `--board <name>`, before or after the command's name, or
BOARD_GRAPH_BOARD naming its description. With several boards and none named, `where` and `check` answer for every
board, each in a process of its own, and every other command is refused with the boards' names; the Mac is told
the board with each round trip, except for the first board, which is reached as a one-board repository's is.
"""
from __future__ import annotations

import argparse
import atexit
import base64
import hashlib
import json
import os
import pathlib
import platform
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from contextlib import closing, contextmanager, suppress
from datetime import datetime, timezone

import tool_paths

if __name__ == "__main__":
    tool_paths.take_argument()

import boards  # noqa: E402

if __name__ == "__main__":
    boards.take_argument()  # --board <name> chooses the board before the modules below read its description

import site_settings  # noqa: E402

try:
    import mac  # noqa: E402
except site_settings.SiteError as error:  # the settings file is missing or wrong: one line, not a traceback
    if __name__ != "__main__":
        raise
    sys.exit(str(error))
import tidy  # noqa: E402
import board_graph_codex  # noqa: E402
import board_graph_tool  # noqa: E402
import workflow_history
import board_approvals  # noqa: E402
import board_autonomy  # noqa: E402
import graph_pack  # noqa: E402
from graph_pack import PROJECT_ACTIONS, file_sha256, sha256  # noqa: E402
from no_window import KWARGS as NO_WINDOW_KWARGS  # noqa: E402

TOOL_ROOT = tool_paths.TOOL_ROOT
ROOT = tool_paths.project_root()
# The board this process works on (scripts/boards.py) and the files its graph lives in: in a repository with one
# board, the files below as they have always been named; for another listed board, files named after it.
try:
    CHOSEN = boards.chosen(ROOT)
except boards.BoardsError as error:
    if __name__ != "__main__":
        raise
    sys.exit(str(error))
GRAPH = ROOT / CHOSEN.graph  # tools/board_graph.json
STATE_IN = CHOSEN.state(mac.SITE.state_database)  # under the Mac's main checkout (place()), from the site settings
RECORDS_IN = CHOSEN.records  # tools/board_graph_records
VIEW_IN = CHOSEN.view  # tools/board_graph_view
COMMAND = ("board-graph" if tool_paths.is_external() else "py -3 scripts/board_graph.py") + CHOSEN.argument
PROGRAM = pathlib.Path(__file__).resolve()  # what every_board runs once per board
# What a Remote runs on the Mac, in its main checkout: this script's serve, told the board unless it is the first.
SERVE = (f'cd {mac.CHECKOUT} && BOARD_GRAPH_PROJECT="$PWD" exec python3 {mac.TOOL_CHECKOUT}/scripts/board_graph.py'
         f'{CHOSEN.remote_argument} serve' if mac.TOOL_CHECKOUT else
         f"cd {mac.CHECKOUT} && exec python3 scripts/board_graph.py{CHOSEN.remote_argument} serve")
DATABASE = "the one database"  # with several listed, "the board <name>'s database", set below with the pack
RECORDS = ROOT / RECORDS_IN
VIEW = ROOT / VIEW_IN
STATE_ENV = "BOARD_GRAPH_STATE"
BUSY = 5  # seconds a write waits for another writer's lock before it gives up
SERVE_TIMEOUT = 60  # seconds one round trip to the Mac may take
# What a Remote may ask serve to run on the Mac's database: read (a copy) and the State methods that write.
OPS = ("read", "append", "start_attempt", "open_work", "finish_attempt", "keep_tokens", "keep_launcher",
       "finish_launch", "ack_launch", "start_upkeep", "finish_upkeep",
       "keep_full_read", "record_checks")
# What the engine takes from the kind of work the graph runs (scripts/graph_pack.py): the card scripts, the views a
# card compares with its last pass, the reports folder and the folders that go on a branch. Nothing below this
# names a circuit board's files.
try:
    PACK = graph_pack.load(ROOT)
except graph_pack.PackError as error:
    if __name__ != "__main__":
        raise
    sys.exit(str(error))
try:
    ACTIONS = PACK.actions_for(CHOSEN.name)  # {name} in a path a script reads is this graph's name
    # One folder per card, <reports>/<card>/, holding its newest reports, none older; each graph has its own.
    REPORTS = PACK.reports_for(CHOSEN.name, CHOSEN.first)
except ValueError as error:
    if __name__ != "__main__":
        raise
    sys.exit(str(error))
LATER, COMMANDS, VIEWS = PACK.later, PACK.commands, PACK.views
VIEW_FILE = PACK.view_file  # the one file the views read
SCRIPTS_IN = (f"{pathlib.PurePosixPath(PACK.module).name} or " if PACK.module else "")  # for a message
# The folders whose files go on a branch and a pull request (on_a_branch): for a board, its design folders.
DESIGN_FOLDERS = PACK.branch_folders
# What one listed graph is called in a message: a board with the electronics pack, a graph otherwise.
ONE = "board" if PACK.name == graph_pack.ELECTRONICS else "graph"
if CHOSEN.name:
    DATABASE = f"the {ONE} {CHOSEN.name}'s database"
DONE = "done"
# What every brief says, so whoever is on a card reads it at the start.
BROKEN_CARD = ("When this card's script or tool crashes or breaks: stop. Record needs_lead with what it said, "
               "word for word. Never do the card's work another way, with another tool or by hand, and never "
               "record it as done: another method is the owner's choice, asked for in plain words. Starting "
               "baseline findings on an agent card are work to resolve on this card (not a broken tool); stale "
               "upstream approvals (recheck) are refreshed by walking `returned` through the graph. In "
               "Mode: AUTONOMOUS (delegated), do not exit on needs_lead: self-heal genuine tool bugs via "
               "board-graph heal and iterate routine routing retries as the delegated lead.")
# The outcome that ends a track early, by the owner alone, at a card that allows it: the track is then at DONE, and
# its last transition says it was dropped, where and why.
DROPPED = "dropped"
LINE_CAP = 2000  # characters: a Read cuts a longer line, and an agent that edits the graph reads this file
DOC_CHARS = 30_000  # a doc a card names is read whole by a fresh agent: the smallest cut among the loaders is a
DOC_LINES = 2000  # shell's 30,000 characters of output, then a Read's 2000 lines (docs only)
LOADED = ("AGENTS.md",)
REPORT_SUFFIXES = (".json", ".md", ".txt", ".csv", ".rpt", ".log", ".html", ".diff")  # a report an agent reads as text
REPORT_ARTIFACT_SUFFIXES = REPORT_SUFFIXES + (".jsonl", ".pdf", ".png", ".jpg", ".jpeg")
WHO = ("auto", "agent", "owner", "owner_and_agent")
OWNER = "owner"
MACHINE = platform.node()  # this computer's name: the full read of the workspace is kept per machine
FINDINGS_LIMIT = 30  # findings an attempt keeps of its report, for the view
PULL_REQUEST_LIST = "number,title,headRefName,isDraft,url,updatedAt,statusCheckRollup"
NAMES_PULL_REQUEST = re.compile(r"\bpull request (\d+)\b", re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS transitions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    track TEXT NOT NULL,
    node TEXT NOT NULL,
    outcome TEXT NOT NULL,
    target TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    record_name TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS transitions_record_name ON transitions(record_name);
CREATE TABLE IF NOT EXISTS attempts (
    run_id TEXT PRIMARY KEY,
    node TEXT NOT NULL,
    action TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    report_path TEXT,
    report_sha256 TEXT,
    actor TEXT,
    note TEXT,
    checkout TEXT,
    report_json TEXT,
    brief TEXT,
    tokens INTEGER,
    launch_state TEXT,
    launch_note TEXT
);
CREATE TABLE IF NOT EXISTS record_exports (
    destination TEXT PRIMARY KEY,
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS upkeep (
    run_id TEXT PRIMARY KEY,
    node TEXT NOT NULL,
    why TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    mac TEXT,
    said TEXT,
    problems_json TEXT
);
CREATE TABLE IF NOT EXISTS full_reads (
    machine TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    view_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    run_id TEXT NOT NULL,
    event TEXT NOT NULL,
    passed INTEGER,
    open INTEGER,
    checks_json TEXT NOT NULL,
    note TEXT
);
"""
COLUMNS = ("seq", "at", "track", "node", "outcome", "target", "actor", "note", "evidence_json", "inputs_json",
           "record_name")
# Added after the attempts table first shipped; older rows hold NULL. checkout: where the attempt ran. report_json:
# the parts of its report the view shows (kept_report), so a view written on another machine has them without
# the file. brief: the card's brief as next printed it when it started the card, recording what context was fed in.
ATTEMPT_COLUMNS = ("actor", "note", "checkout", "report_json", "brief", "launch_state", "launch_note")
ATTEMPT_NUMBERS = ("tokens",)  # the thread's tokens used, the provider's input and output totals for the run: the Codex
# launcher settles them, a manual thread gives them with record --tokens, and the Token use card sums them.
AGENT = "agent"  # the action of an attempt next opened for a person or an agent; no script runs for it
TAKEN = "taken"  # the outcome of an open attempt that next --take closed
PULL_REQUEST_FIELDS = "state,isDraft,mergeCommit,mergedAt,headRefOid,statusCheckRollup,url"


def now():
    return datetime.now(timezone.utc).isoformat()


UNCHANGED = "script:unchanged"  # the actor of a pass next records because the card's view is unchanged
GATED = ("pass", "more_work")  # the outcomes that say the card's work moved forward
WAITS_ON = {"needs_owner": "the owner", "needs_lead": "the lead", "needs_skill": "a skill"}
# What GitHub said of a merge question, which a quick read carries while the record still asks it.
CARRIED_QUESTION = ("state", "draft", "checks", "merge_commit", "head", "head_moved", "url", "error")
STALE_MAC_READ_SECONDS = 3600

sys.modules.setdefault("board_graph", sys.modules[__name__])

from board_graph_git import (  # noqa: E402
    branch_progress, branch_records, bring_current, checkout_here, checkout_problems,
    checks, dirty_owned, fetch_pull_request, gh, git_text, hash_at, hashes_at,
    head_here, keep_pull_request, lfs_content, local_upkeep, mac_checkout, main_checkout, main_head,
    main_state, merge_ask_readiness, merge_check, naming_record, on_a_branch,
    open_pull_requests, pull_request_body, pull_request_view, records_on_branch,
    run_git, shell_quote, short, tied_pull_requests,
)
from board_graph_model import (  # noqa: E402
    Graph, acknowledged_approach, approval_inputs, approval_review, check_presentation,
    check_routing_terms, counts, database_line, default_views, gate_findings, hash_path,
    last_transition, local_report, mac_keep_pace_path, owed_return, parse_time, place,
    read_mac_keep_pace, record_file_name, record_text, report_path,
    require_answered_wait, review_at_revision, routing_terms, status, unchanged_pass,
    view_at, verify_approval_sequence,
)
from board_graph_state import (  # noqa: E402
    Remote, State, UnconfirmedWrite, open_state, remote_call, serve, unreached,
)
import board_heal  # noqa: E402
from board_graph_workspace import (  # noqa: E402
    _is_stale_mac_read, _keep_pace_is_newer, _merge_live_mac, _parse_time_or_none,
    _upkeep_synced_mac_after, ago, branches_here, brief, card_lines, card_view,
    carried_workspace, carry, carry_report, checked, checks_words, ci_lines,
    ci_section, codex_handoff, every_board, findings_note, follow, held, keep_checks,
    keep_pace_lines, kept_report, land, last_upkeep, launch_lines, merge_questions,
    next_step, node_lines, origin_command, origin_record_command,
    origin_settle_command, page_lines, quick_workspace, record, recorded, render,
    render_track, render_workspace, report_notes, run_codex, run_node, run_script,
    run_tidy, settle, stamp, standing, stop_shape, unresolved, where, with_tokens,
    work, working, workspace,
)


def _build_cli_parser():
    """Build the board_graph CLI argument parser and its subcommands."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    boards.add_argument(parser)
    parser.add_argument("--graph", type=pathlib.Path, default=site_settings.env(board_graph_codex.GRAPH_ENV, GRAPH))
    parser.add_argument("--state", type=pathlib.Path,
                        help=f"another database than the one on the Mac ({mac.CHECKOUT}/{STATE_IN} there), for a "
                             f"test or a scratch run; {STATE_ENV} names one too")
    parser.add_argument("--records", type=pathlib.Path, default=site_settings.env(board_graph_codex.RECORDS_ENV),
                        help=f"the folder record files are written to, and imported from with --state or on the Mac; "
                             f"by default this checkout's {RECORDS_IN}, and the Mac's database imports its main "
                             f"checkout's")
    parser.add_argument("--view", type=pathlib.Path,
                        help=f"the view folder whose view.json follows every write to the state; by default the Mac's "
                             f"main checkout's {VIEW_IN} on the Mac, and none here")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("where", help="print the position on every track, then the workspace: this checkout "
                                             "and the database read now, the rest as of the last full read")
    show.add_argument("--json", action="store_true")
    show.add_argument("--full", action="store_true",
                      help="read the whole workspace now: fetch, main here, tidy pending, the Mac and GitHub, and "
                           "keep it for the quick reads after")
    compare = commands.add_parser("approvals", help="compare historical passes with current inputs from local records (read-only)")
    compare.add_argument("--json", action="store_true")
    preview = commands.add_parser("brief", help="print a card's brief without starting it: the context next gives "
                                                "whoever starts the card")
    preview.add_argument("node")
    shut = commands.add_parser("settle", help="settle a run once the thread next started on a card has ended: keep its "
                                              "tokens on the attempt, and record needs_lead if no record closed the card")
    shut.add_argument("node")
    shut.add_argument("--by", required=True, help="the launcher: owner, or agent:<name>")
    shut.add_argument("--note", default="", help="what the launcher saw, in plain words")
    shut.add_argument("--evidence", nargs="*", default=[], help="paths inside the project, hashed as recorded")
    shut.add_argument("--tokens", type=int,
                      help="the thread's tokens used, when known: the provider's input and output totals for the run")
    shut.add_argument("--attempt", help="settle this exact attempt, never a later one on the same card")
    shut.add_argument("--ack", action="store_true", help="acknowledge this launch's post-record checkpoint with a revised --note")
    shut.add_argument("--verified-stopped", action="store_true", help="attest that the worker and every descendant have stopped")
    write = commands.add_parser("record", help="record one outcome at the node where its track stands")
    write.add_argument("node")
    write.add_argument("outcome")
    write.add_argument("--to", help="for returned: the earlier node the work goes back to")
    write.add_argument("--by", required=True, help="who records it: owner, or agent:<name>")
    write.add_argument("--note", required=True, help="why, in plain words; the owner's words for an owner node")
    write.add_argument("--evidence", nargs="*", default=[], help="paths inside the project, hashed as recorded")
    write.add_argument("--pull-request", type=int,
                       help="for needs_owner: the pull request the owner is asked to merge; next checks the merge")
    write.add_argument("--then", help="with --pull-request: the outcome next records once the merge checks out")
    write.add_argument("--attempt", help="record for this exact open attempt")
    write.add_argument("--tokens", type=int, help="the thread's tokens used, when known (the provider's input and "
                                                  "output totals), kept on the attempt this record closes")
    step = commands.add_parser("next", help="start the next card in line: open it for a person (--by), or run its "
                                            "script and record what it reports; check a merge the position waits on")
    step.add_argument("--track", default=None, help="the track to step: the staged track (a board's is named "
                      "board) when left out, else the first track in order")
    step.add_argument("--by", default=None, help="who starts a person's card: owner, or agent:<name>")
    step.add_argument("--note", default="", help="what the work is, in plain words")
    step.add_argument("--take", action="store_true",
                      help="take the card over from someone else's open attempt, which ends as taken")
    step.add_argument("--again", action="store_true",
                      help="run the card's script even though an interrupted or unresolved attempt on the same inputs "
                           "exists")
    step.add_argument("--node", default=None, help="on a list (the framework), the card to start")
    step.add_argument("--codex", action="store_true", help="launch one agent card with no action, on Windows")
    step.add_argument("--minutes", type=int, help="wall-clock stop for one --codex run")
    ci = commands.add_parser("checks", help="keep a validate run's board checks on the graph: a validate workflow's "
                                            "last step on the Mac, where the database is, or "
                                            "scripts/run_board_checks.py without a workflow")
    ci.add_argument("--commit", required=True, help="the commit the run checked (a pull request's head)")
    ci.add_argument("--run", required=True, help="GitHub's run id, or the name of a run made by hand")
    ci.add_argument("--event", default="push", help="push or pull_request; local for a run made by hand")
    ci.add_argument("--summary", type=pathlib.Path, required=True,
                    help="scripts/validate_board.py's summary.json; a missing one is kept as a run with no checks")
    commands.add_parser("graph", help="print the graph in Mermaid form")
    commands.add_parser("check", help="validate the graph file, and refuse a line a Read would cut or a doc a card names "
                                      "that is missing or over a loader's cut")
    commands.add_parser("serve", help="answer one request from another machine's board_graph.py (stdin to stdout, "
                                      "JSON) on this machine's database: how the PC reaches the Mac's")
    for command in commands.choices.values():  # --board is taken after the command's name too
        boards.add_argument(command, default=argparse.SUPPRESS)
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = _build_cli_parser()
    args = parser.parse_args(argv)
    if args.board and args.board != CHOSEN.name:
        # The board is read when the script starts (boards.take_argument); a caller that imported this module
        # chose its board then, with BOARD_GRAPH_BOARD.
        print(f"--board {args.board}: this process already works on {CHOSEN.name or CHOSEN.description}; run "
              f"scripts/board_graph.py as a program, or set {boards.ENV} before it is imported", file=sys.stderr)
        return 2
    try:
        several = boards.unnamed(ROOT)
    except boards.BoardsError as error:
        print(error, file=sys.stderr)
        return 1
    # Several boards and none named: where and check answer for every board; serve is the first board's, as every
    # checkout reaches it; anything else works on one board and is refused until it is named.
    own_files = any(word.split("=", 1)[0] in ("--graph", "--state", "--records", "--view") for word in argv)
    if several and args.command in ("where", "check") and not own_files:
        return every_board(argv, several, args.command, getattr(args, "json", False))
    if several and args.command != "serve":
        print(f"{args.command}: this repository has {len(several)} {ONE}s ({', '.join(several)}); name the one "
              f"this is for, e.g. py -3 scripts/board_graph.py {boards.ARGUMENT} {several[0]} "
              f"{args.command}", file=sys.stderr)
        return 2
    try:
        graph = Graph(args.graph)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    if args.command == "check":
        problems = graph.reading_problems() + graph.order_problems()
        for problem in problems:
            print(f"{args.graph}: {problem}", file=sys.stderr)
        if problems:
            return 1
        named = {doc for node in graph.nodes for doc in graph.docs(node)}
        print(f"{args.graph}: {len(graph.nodes)} nodes on {len(graph.tracks)} tracks, consistent, every line under "
              f"{LINE_CAP} characters; {len(named)} docs the cards name, each present and under the cuts; every report "
              f"under {REPORTS} at most {DOC_LINES} lines")
        return 0
    if args.command == "approvals":
        folder = pathlib.Path(args.records or graph.root / RECORDS_IN)
        if args.state or args.view:
            parser.error("approvals reads checkout records only; use --records to select a copy")
        try:
            rows = board_approvals.read_records(folder)
            report = board_approvals.Approvals(graph, rows, graph.root, default_views(), file_sha256, PACK.approval_sources, evidence_hash=hash_path).report()
        except (OSError, ValueError) as error:
            print(error, file=sys.stderr)
            return 1
        data = {"source": str(folder), "records": len(rows), "mode": graph.data.get("approval_mode", "off"), "cards": report}
        print(json.dumps(data, indent=2) if args.json else "\n".join(
            [f"Checkout comparison: {len(rows)} records in {folder}", *board_approvals.lines(report)]))
        return 0
    if args.command == "graph":
        sys.stdout.write(graph.mermaid())
        return 0
    where_from = place(args.state, args.records, args.view, root=graph.root)
    if CHOSEN.name is None and where_from["how"] in ("mac", "ssh") and boards.read(ROOT):
        # A description the list does not name would read and write the first board's position with another
        # board's files. A scratch database (--state) is the test's own, so that stays allowed.
        print(f"{args.command}: {CHOSEN.description} is not a board {boards.LIST} lists "
              f"({', '.join(boards.read(ROOT))}); list it there, or name a scratch database with --state",
              file=sys.stderr)
        return 2
    if args.command == "serve":
        sys.stdout.write(json.dumps(serve(json.load(sys.stdin), where_from, args.records or RECORDS, args.graph)) + "\n")
        return 0
    try:
        return run(args, graph, where_from)
    except sqlite3.OperationalError as error:
        print(f"the state database {where_from['path']} did not answer ({error}); a write waits {BUSY} s for "
              f"another session's, so another may have held it longer", file=sys.stderr)
        return 1
    except mac.MacError as error:
        print(unreached(where_from, error, args.command), file=sys.stderr)
        return 1


def run(args, graph, where_from):
    """main() once the graph has loaded: the commands that open the state."""
    if args.command in ("next", "record"):
        heal_err = board_heal.check_tool_heal_gate(
            graph_path=graph.path,
            state_path=where_from.get("path") if where_from.get("how") != "ssh" else None,
            records_path=getattr(args, "records", None) or (graph.root / RECORDS_IN),
            command=args.command,
            outcome=getattr(args, "outcome", None),
        )
        if heal_err:
            print(heal_err, file=sys.stderr)
            return 1
    state = open_state(where_from, args.records or graph.root / RECORDS_IN)
    if where_from["view"]:
        follow(graph, state, where_from["view"])
        if state.imported:
            state.changed()  # records that came through git moved the graph too
    if args.command == "checks":
        item = keep_checks(state, args.commit, args.run, args.event, args.summary)
        sys.stdout.write(f"CI on {item['commit'][:8]}: {checks_words(item)}\n")
        return 0
    if args.command == "record":
        try:
            inherited = site_settings.env(board_graph_codex.ATTEMPT_ENV)
            if inherited and args.attempt and inherited != args.attempt:
                raise ValueError("the record's attempt does not match this launched worker")
            if args.tokens is not None and not state.attempts(args.node, open_only=True):
                raise ValueError("--tokens needs an open attempt on the card to keep them on")
            item = record(graph, state, args.node, args.outcome, args.by, args.note, args.evidence, args.to,
                          root=graph.root, pull_request=args.pull_request, then=args.then,
                          attempt_id=inherited or args.attempt, tokens=args.tokens)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
        sys.stdout.write(recorded(graph, state, item))
        sys.stdout.write(carry(graph, state, item, where_from, root=graph.root))
        return 0
    if args.command == "next":
        state_path = where_from["path"] if where_from["how"] in ("--state", STATE_ENV) else None
        launcher = None
        if args.codex:
            if os.name != "nt":
                print("Codex dispatch is verified on Windows only; manual graph starts remain available here",
                      file=sys.stderr)
                return 1
            try:
                executable, _ = board_graph_codex.codex_cli()
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                print(f"Codex dispatch preflight failed: {error}; no attempt was opened", file=sys.stderr)
                return 1

            def launcher(*values, **options):
                return board_graph_codex.run(*values, executable=executable, **options)

        track = args.track or graph.staged or next(
            (item["id"] for item in graph.data["tracks"] if item["ordered"]), "board")
        upkeep = local_upkeep if getattr(PACK, "upkeep", None) == "local" else None
        return next_step(graph, state, track, args.again, root=graph.root, by=args.by, note=args.note, take=args.take,
                         node=args.node, codex=args.codex, minutes=args.minutes, upkeep=upkeep,
                         state_path=state_path, launcher=launcher,
                         after_record=lambda item, extra: carry(graph, state, item, where_from, extra, root=graph.root),
                         after_report=lambda name, message: carry_report(where_from, name, message, root=graph.root))
    if args.command == "brief":
        if args.node not in graph.nodes:
            print(f"unknown card {args.node}; the cards are {', '.join(graph.nodes)}", file=sys.stderr)
            return 1
        sys.stdout.write(brief(graph, state, args.node, root=graph.root))
        return 0
    if args.command == "settle":
        if args.node not in graph.nodes:
            print(f"unknown card {args.node}; the cards are {', '.join(graph.nodes)}", file=sys.stderr)
            return 1
        try:
            item, said = settle(graph, state, args.node, args.by, args.note, args.evidence, args.tokens, root=graph.root,
                                attempt_id=args.attempt, acknowledge=args.ack, verified_stopped=args.verified_stopped)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
        sys.stdout.write(said)
        if item:
            sys.stdout.write(recorded(graph, state, item))
            sys.stdout.write(carry(graph, state, item, where_from, root=graph.root))
        return 0
    view = where(graph, state)
    view["database"] = {"path": str(where_from["path"]), "how": where_from["how"]}
    view["workspace"] = workspace(graph, state) if args.full else quick_workspace(graph, state)
    if args.json:
        print(json.dumps(view, indent=2))
    else:
        sys.stdout.write(render(view))
        hint = board_heal.needs_lead_hint(view)
        if hint:
            sys.stdout.write(hint + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
