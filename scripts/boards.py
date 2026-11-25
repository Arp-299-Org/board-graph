#!/usr/bin/env python3
"""The boards of one repository: which boards the graph runs, and the files each board's graph lives in.

A repository with one board has no list, and nothing here changes it: its board is the one description
(scripts/board_description.py: BOARD_GRAPH_BOARD, else board/board.json) and its graph lives where it always has.
A repository with several boards lists them in tools/board_graph_boards.json:

  {"first": "strip",
   "boards": {"strip": "board/board.json", "driver": "driver/board.json"}}

`boards` gives each board a name and its description, a path relative to the repository root. `first` names the
board that keeps the files a one-board repository has, so a board that was alone keeps its cards, its records and
its position when a second one is listed. Every other board has files of its own, named after it:

  what                      the first board                  another board, <name>
  cards                     tools/board_graph.json           tools/board_graph.<name>.json
  records                   tools/board_graph_records/       tools/board_graph_records.<name>/
  state, on the KiCad Mac   the site's state_database        beside it, <name>/<the same file name>
  view                      tools/board_graph_view/          tools/board_graph_view/boards/<name>/

A board's reports, decisions file, footprint library and every other file of its own come from its description,
as before. Two boards may name the same parts data and the same datasheets folder.

One process works on one board, chosen before any script reads a description: by `--board <name>` on the
command line of a script that takes it (take_argument, called before that script's other imports), or by
BOARD_GRAPH_BOARD naming the board's description, which every process started from there inherits. With a list
and neither, the board is the first one; scripts/board_graph.py then prints every board for `where` and `check`
and refuses a command that writes until a board is named. A description the list does not name runs on the first
board's files, as in a one-board repository: a test's scratch board does that with a database of its own.
BOARD_GRAPH_BOARDS names another list file; one that is not there means no list, which is how the tool's own
tests read every checkout as a repository with one board.

Standard library only, and it imports under Python 3.9, since KiCad's Python on the Mac runs scripts that read it.

  python3 scripts/boards.py     prints each board, its description and its files
  python3 scripts/boards.py new <name> [--like <name>] [--title <words>]
                                adds a graph that is not a circuit board, with the cards of one already listed
                                (the first when --like is left out) and a position, records and page of its own
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

import tool_paths

if __name__ == "__main__":
    tool_paths.take_argument()

import board_description  # noqa: E402

ROOT = tool_paths.project_root()
LIST = "tools/board_graph_boards.json"
LIST_ENV = "BOARD_GRAPH_BOARDS"  # another list file than LIST, relative to the repository root or absolute
ENV = board_description.ENV  # BOARD_GRAPH_BOARD: the chosen board's description
ARGUMENT = "--board"
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")  # a board's name is part of file names and of the page's address
GRAPH = "tools/board_graph.json"
RECORDS = "tools/board_graph_records"
VIEW = "tools/board_graph_view"


class BoardsError(ValueError):
    """The list of boards cannot be read or used, or a name is not on it; the message says which."""


class Board:
    """One board: `name` (None for a board no list names), `description` (as the list or BOARD_GRAPH_BOARD gives
    it) and whether it is `first`, the board on the files a one-board repository has."""

    def __init__(self, name, description, first=True):
        self.name = name
        self.description = description
        self.first = first

    def __repr__(self):
        return f"Board({self.name!r}, {self.description!r}, first={self.first})"

    @property
    def graph(self):
        """The board's cards, relative to the repository root."""
        return GRAPH if self.first else f"tools/board_graph.{self.name}.json"

    @property
    def records(self):
        """The folder of the board's record files, relative to the repository root."""
        return RECORDS if self.first else f"{RECORDS}.{self.name}"

    @property
    def view(self):
        """The folder of the board's view.json and snapshot.js, relative to the repository root: the page's own
        folder for the first board, a folder inside it for another."""
        return VIEW if self.first else f"{VIEW}/boards/{self.name}"

    @property
    def data(self):
        """Where the page reads the board's view from, relative to the page: "" or boards/<name>/."""
        return "" if self.first else f"boards/{self.name}/"

    def state(self, database):
        """The board's state database, given the site settings' `database` (relative to the Mac's main
        checkout): that file for the first board, <its folder>/<name>/<its file name> for another."""
        if self.first:
            return database
        path = pathlib.PurePosixPath(database)
        return (path.parent / self.name / path.name).as_posix()

    @property
    def argument(self):
        """What a command printed for a person says to reach this board: " --board <name>", or "" for a board
        no list names."""
        return f" {ARGUMENT} {self.name}" if self.name else ""

    @property
    def remote_argument(self):
        """What the same command on another machine is given: " --board <name>" for a board with files of its
        own, "" for the first, which every checkout reaches with no name, as a one-board repository does."""
        return "" if self.first else self.argument


def read(root=ROOT, environ=None):
    """The listed boards by name, in the list's order; {} for a repository with no list. BoardsError names what
    is wrong with a list that cannot be used."""
    file = pathlib.Path(root) / ((os.environ if environ is None else environ).get(LIST_ENV) or LIST)
    if not file.exists():
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8-sig"))
    except OSError as error:
        raise BoardsError(f"{LIST} cannot be read: {error}") from None
    except ValueError as error:
        raise BoardsError(f"{LIST} is not JSON: {error}") from None
    named = data.get("boards") if isinstance(data, dict) else None
    if not isinstance(named, dict) or not named:
        raise BoardsError(f"{LIST}: boards must give each board's name its description, and name at least one")
    first = data.get("first")
    if first not in named:
        raise BoardsError(f"{LIST}: first must name the board that keeps the files a one-board repository has, "
                          f"one of {', '.join(named)}")
    found, seen = {}, {}
    for name, description in named.items():
        if not isinstance(name, str) or not NAME.match(name):
            raise BoardsError(f"{LIST}: a board's name is lower-case letters, digits, - and _, starting with a "
                              f"letter or digit, not {name!r}")
        if not isinstance(description, str) or not description:
            raise BoardsError(f"{LIST}: {name} must name its description, a path relative to the repository root")
        if pathlib.PurePosixPath(description).is_absolute() or pathlib.PureWindowsPath(description).is_absolute():
            raise BoardsError(f"{LIST}: {name}'s description must be relative to the repository root, not "
                              f"{description}")
        description = pathlib.PurePosixPath(description.replace("\\", "/")).as_posix()
        if description in seen:
            raise BoardsError(f"{LIST}: {seen[description]} and {name} name the same description, {description}")
        seen[description] = name
        found[name] = Board(name, description, first=name == first)
    return found


def relative(given, root=ROOT):
    """A description as BOARD_GRAPH_BOARD names it, as the list would write it: relative to `root` with forward
    slashes when it is inside it, else as given."""
    path = pathlib.Path(given)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(pathlib.Path(root).resolve()).as_posix()
        except ValueError:
            return str(path)
    return pathlib.PurePosixPath(str(given).replace("\\", "/")).as_posix()


def chosen(root=ROOT, environ=None):
    """The board this process works on: the listed board whose description BOARD_GRAPH_BOARD names, else the
    first listed; with no list, or a description the list does not name, a board with no name on the files a
    one-board repository has."""
    environ = os.environ if environ is None else environ
    given = environ.get(ENV)
    listed = read(root)
    if not listed:
        return Board(None, given or board_description.DEFAULT)
    if not given:
        return next(board for board in listed.values() if board.first)
    wanted = relative(given, root)
    for board in listed.values():
        if board.description == wanted:
            return board
    return Board(None, given)


def every(root=ROOT, environ=None):
    """Every board of the repository, in the list's order; the one board when there is no list."""
    return list(read(root).values()) or [chosen(root, environ)]


def named(environ=None):
    """Whether a board was chosen: BOARD_GRAPH_BOARD is set, by --board or by whoever started this process."""
    return bool((os.environ if environ is None else environ).get(ENV))


def unnamed(root=ROOT, environ=None):
    """The listed boards' names when the repository has several and none was chosen, else []: what a command
    that works on one board refuses on."""
    listed = read(root)
    return list(listed) if len(listed) > 1 and not named(environ) else []


def find(name, root=ROOT):
    """The listed board called `name`; BoardsError says which boards there are."""
    listed = read(root)
    if name not in listed:
        raise BoardsError(f"no board {name!r} here: " + (f"{LIST} lists {', '.join(listed)}" if listed else
                                                          f"this repository lists no boards (there is no {LIST})"))
    return listed[name]


def argument_value(argv):
    """The name `--board <name>` or `--board=<name>` gives on a command line, the last one; None without one."""
    name = None
    for index, word in enumerate(argv):
        if word == ARGUMENT and index + 1 < len(argv):
            name = argv[index + 1]
        elif word.startswith(ARGUMENT + "="):
            name = word.split("=", 1)[1]
    return name


def take_argument(argv=None, environ=None, root=ROOT):
    """`--board <name>` on the command line, taken before any module reads a description: BOARD_GRAPH_BOARD
    becomes that board's description, for this process and every process it starts. Returns the board, or None
    when the command line names none. An unknown name ends the program with exit 2 and the boards there are."""
    name = argument_value(sys.argv[1:] if argv is None else argv)
    if name is None:
        return None
    try:
        board = find(name, root)
    except BoardsError as error:
        print(error, file=sys.stderr)
        raise SystemExit(2) from None
    (os.environ if environ is None else environ)[ENV] = board.description
    return board


def add_argument(parser, **options):
    """The --board argument, for a script whose first lines call take_argument()."""
    parser.add_argument(ARGUMENT, metavar="NAME", help=f"the board to work on, by its name in {LIST}; a repository "
                                                       "with one board needs none", **options)
    return parser


def new(name, like=None, title=None, root=ROOT, environ=None):
    """Add a graph called `name` to the list, with the cards of the listed graph `like` (the first one when
    left out) and a position, records and page of its own: for work that repeats, one graph each time. Its
    description is written beside the model's, <name>.json, with `title` (else `name`) as its name. Returns the
    new Board. BoardsError when there is no list, the name is taken or not a name, or the model is a circuit
    board, whose description names files of its own and is written by hand."""
    root = pathlib.Path(root)
    listed = read(root, environ)
    if not listed:
        raise BoardsError(f"there is no {LIST} here: list the first graph there, then add others with new")
    if not isinstance(name, str) or not NAME.match(name):
        raise BoardsError(f"a name is lower-case letters, digits, - and _, starting with a letter or digit, "
                          f"not {name!r}")
    if name in listed:
        raise BoardsError(f"{name} is already listed in {LIST}")
    model = find(like, root) if like else next(board for board in listed.values() if board.first)
    try:
        description = json.loads((root / model.description).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise BoardsError(f"{model.name}'s description, {model.description}, cannot be read: {error}") from None
    if not isinstance(description, dict) or set(description) - {"name", "project"}:
        raise BoardsError(f"{model.name} is a board with files of its own ({model.description}): write the new "
                          f"board's description by hand and list it in {LIST}")
    board = Board(name, (pathlib.PurePosixPath(model.description).parent / f"{name}.json").as_posix(), first=False)
    for path in (board.graph, board.description, board.records):
        if (root / path).exists():
            raise BoardsError(f"{path} is already there: choose another name, or list it by hand")
    file = root / ((os.environ if environ is None else environ).get(LIST_ENV) or LIST)
    data = json.loads(file.read_text(encoding="utf-8-sig"))
    data["boards"][name] = board.description
    (root / board.description).parent.mkdir(parents=True, exist_ok=True)
    (root / board.description).write_text(json.dumps(dict(description, name=title or name), indent=2,
                                                     ensure_ascii=False) + "\n", encoding="utf-8")
    (root / board.graph).write_bytes((root / model.graph).read_bytes())
    file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return board


def copied_paths(board, root=ROOT):
    """The paths the cards of `board` own that do not say {name}: a graph made by new has them as its model does,
    so both graphs' cards name the same files until they are changed."""
    data = json.loads((pathlib.Path(root) / board.graph).read_text(encoding="utf-8-sig"))
    return sorted({path for node in data.get("nodes", []) for path in node.get("owns", [])
                   if isinstance(path, str) and "{name}" not in path})


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "new":
        import argparse
        parser = argparse.ArgumentParser(prog="boards.py new", description="Add a graph with the cards of one "
                                         "already listed, and a position, records and page of its own.")
        parser.add_argument("name", help="the new graph's name: lower-case letters, digits, - and _")
        parser.add_argument("--like", metavar="NAME", help="the listed graph whose cards it gets; the first one "
                            "when left out")
        parser.add_argument("--title", help="its name on the page; the name when left out")
        args = parser.parse_args(argv[1:])
        try:
            board = new(args.name, args.like, args.title)
        except BoardsError as error:
            print(error, file=sys.stderr)
            return 1
        print(f"{board.name}: cards {board.graph}, description {board.description}, listed in {LIST}")
        same = copied_paths(board)
        if same:
            print(f"Its cards name the same files as the graph it was copied from: {', '.join(same)}. Change them "
                  f"in {board.graph}, or write {{name}} in a path where each graph has its own: it stands for the "
                  f"graph's name.")
        print(f"Commit these, then: python3 scripts/board_graph.py{board.argument} next --by <who>")
        return 0
    try:
        boards = every()
    except BoardsError as error:
        print(error, file=sys.stderr)
        return 1
    for board in boards:
        print(f"{board.name or 'the one board'}: description {board.description}, cards {board.graph}, "
              f"records {board.records}/, view {board.view}/"
              + ("" if board.first else f", state database in {board.name}/ beside the first board's"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
