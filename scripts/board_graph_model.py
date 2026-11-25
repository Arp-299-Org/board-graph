"""Graph model, approvals, routing terms, hashes, and report helpers for board_graph."""
from __future__ import annotations

import sys
import hashlib
import json
import pathlib
import subprocess
from datetime import datetime

def _bg():
    mod = sys.modules.get("board_graph")
    if mod is None:
        import board_graph as mod
    return mod


def hash_path(path):
    """One hash for a file, or for a directory's files by relative name and content. Missing: None."""
    path = pathlib.Path(path)
    if path.is_file():
        return _bg().file_sha256(path)
    if path.is_dir():
        digest = hashlib.sha256()
        for member in sorted(p for p in path.rglob("*") if p.is_file()):
            digest.update(member.relative_to(path).as_posix().encode("utf-8") + b"\0")
            digest.update(_bg().file_sha256(member).encode("ascii") + b"\n")
        return digest.hexdigest()
    return None


def mac_keep_pace_path(where_from):
    """The Mac checkout's logs/mac-keep-pace/status.json for `where_from`, or None when unrelated."""
    if not where_from:
        return None
    path = where_from.get("path")
    if isinstance(path, pathlib.Path):
        for parent in path.parents:
            if parent.name == "board-graph" and parent.parent.name == "logs":
                return parent.parent / "mac-keep-pace" / "status.json"
    if where_from.get("how") == "mac":
        return _bg().mac_checkout() / "logs" / "mac-keep-pace" / "status.json"
    return None


def read_mac_keep_pace(where_from):
    """Read logs/mac-keep-pace/status.json locally when available, else None."""
    path = _bg().mac_keep_pace_path(where_from)
    if path is None or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return {"error": f"its status.json did not read ({error})"}
    return _bg().tidy.json_status(text, "its status.json")


def place(state=None, records=None, view=None, root=_bg().ROOT, environ=None):
    """Which database holds the graph's state and `how` it is reached, the records folder it imports from and the
    view folder that follows it.

    By default the one database, on the Mac. On the Mac itself (`mac`, every checkout there alike): the file in
    its main checkout, importing that checkout's records and followed by its view folder. Elsewhere (`ssh`): the
    same database over SSH, named in words; the Mac imports and refreshes the view, so `imports` is None here
    and no view follows unless `view` names a folder. `--state` or BOARD_GRAPH_STATE (or its old name) names another
    database (`how` says which named it, `named` which variable), opened as a file, which imports from `records`
    (this checkout's by default) and is followed only by a `view` named. A `records` or `view` given is used as given.
    """
    environ = _bg().os.environ if environ is None else environ
    named = _bg().site_settings.env_name(_bg().STATE_ENV, environ)
    if state is not None or named:
        found = {"path": pathlib.Path(state if state is not None else environ[named]),
                 "how": "--state" if state is not None else _bg().STATE_ENV,
                 "imports": pathlib.Path(records) if records else pathlib.Path(root) / _bg().RECORDS_IN,
                 "view": pathlib.Path(view) if view else None}
        if state is None:
            found["named"] = named
        return found
    if _bg().mac.LOCAL:
        home = _bg().mac_checkout()
        return {"path": home / _bg().STATE_IN, "how": "mac",
                "imports": pathlib.Path(records) if records else home / _bg().RECORDS_IN,
                "view": pathlib.Path(view) if view else home / _bg().VIEW_IN}
    return {"path": f"{_bg().mac.HOST}:{_bg().mac.CHECKOUT}/{_bg().STATE_IN}", "how": "ssh", "imports": None,
            "view": pathlib.Path(view) if view else None}


def database_line(where_from):
    """Which database `where` reads, in words."""
    path = where_from["path"]
    return {"mac": f"State: {path}, {_bg().DATABASE}, on this " + ("machine" if _bg().mac.ONE_MACHINE else "Mac"),
            "ssh": f"State: {path}, {_bg().DATABASE}, on the Mac over SSH",
            "--state": f"State: {path}, named by --state",
            _bg().STATE_ENV: f"State: {path}, named by {where_from.get('named', _bg().STATE_ENV)}"}[where_from["how"]]


def report_path(node, action):
    """Where a script's report for a card lives, relative to the project: <reports>/<card>/<script>.json. Each
    run replaces the last at the same path, and the attempt keeps the hash of what it wrote; git history keeps
    every earlier one, so the tree holds only the newest at one fixed path per card."""
    return f"{_bg().REPORTS}/{node}/{action}.json"


def record_file_name(at, node, outcome):
    """The name of a transition's record file: its time, node and outcome."""
    return f"{at.replace(':', '-').replace('+00:00', 'Z')}-{node}-{outcome}.json"


def record_text(item):
    """A transition as its record file holds it."""
    return json.dumps({key: item[key] for key in ("at", "track", "node", "outcome", "target", "actor", "note",
                                                  "evidence", "inputs")}, indent=2, sort_keys=True) + "\n"


class Graph:
    def __init__(self, path=_bg().GRAPH, data=None):
        """The graph file at `path`, or `data` already read from one (the graph another machine sent)."""
        self.path = pathlib.Path(path)
        self.root = self.path.resolve().parent.parent  # the checkout: a doc a card names is a path under it
        self.text = None if data is not None else self.path.read_text(encoding="utf-8")
        self.data = data if data is not None else json.loads(self.text)
        # {name} in a path a card owns or a doc it reads is the graph's name, so one file of cards serves every
        # graph made from it (boards.py new copies the file as it is).
        self.unnamed = []
        if isinstance(self.data.get("docs"), list):
            self.data["docs"] = [self.named(path, "docs") for path in self.data["docs"]]
        for node in self.data["nodes"]:
            for key in ("owns", "docs", "approval_files", "requires_decisions", "optional_outputs"):
                if isinstance(node.get(key), list):
                    node[key] = [self.named(path, node.get("id")) for path in node[key]]
        self.nodes = {node["id"]: node for node in self.data["nodes"]}
        self.tracks = {track["id"]: track for track in self.data["tracks"]}
        self.stages = {stage["id"]: stage for stage in self.data["stages"]}
        self.stay = list(self.data["stay_outcomes"])
        # The one track whose cards are grouped in stages: the track named board, else the track of the first card
        # that names a stage; None when no card does.
        self.staged = "board" if "board" in self.tracks else next(
            (node["track"] for node in self.data["nodes"] if node.get("stage") is not None), None)
        # Whether an owner may end a track early at a card that allows it: the graph names the outcome `dropped`.
        self.drops = _bg().DROPPED in self.data["move_outcomes"]
        problems = self.problems()
        if problems:
            raise ValueError("board graph is inconsistent:\n" + "\n".join(problems))

    def named(self, path, where):
        """`path` with {name} replaced by the graph's name; with no name to give, the path as it is, and `where`
        is kept for problems()."""
        if not isinstance(path, str) or "{name}" not in path:
            return path
        if not _bg().CHOSEN.name:
            self.unnamed.append(f"{where}: {path} says {{name}}, a graph's name, and this project lists no graphs")
            return path
        return path.replace("{name}", _bg().CHOSEN.name)

    def refresh(self):
        """Replace this graph with the validated file, retaining the object held by callbacks and state views."""
        refreshed = _bg().Graph(self.path)
        self.__dict__.update(refreshed.__dict__)

    def problems(self):
        found = list(self.unnamed) + _bg().board_approvals.problems(self)
        if len(self.nodes) != len(self.data["nodes"]):
            found.append("duplicate node id")
        if set(self.data["move_outcomes"]) - {_bg().DROPPED} != {"pass", "returned"}:
            found.append("move_outcomes must be pass and returned, and may add dropped")
        if "stop" in self.data and not _bg().stop_shape(self.data["stop"]):
            found.append("stop must give minutes and repeat_failures as whole numbers")
        for node in self.data["nodes"]:
            key = node["id"]
            if node.get("release_check") and node["release_check"] not in _bg().PACK.release_checks:
                found.append(f"{key}: unknown release check {node['release_check']}")
            if node.get("approval_owned") is not None and type(node["approval_owned"]) is not bool:
                found.append(f"{key}: approval_owned must be a boolean")
            integration = node.get("integration")
            if integration is not None and (not isinstance(integration, dict) or node.get("release_check") != "first_article"
                    or not isinstance(integration.get("boards"), dict) or not integration["boards"]
                    or not all(isinstance(n, str) and n.strip() for n in integration["boards"])
                    or not all(_bg().board_approvals.project_path(p) for p in integration["boards"].values())
                    or "firmware" in integration and not _bg().board_approvals.project_path(integration["firmware"])):
                found.append(f"{key}: integration needs board names and project-relative files on a first_article card")
            if node["track"] not in self.tracks:
                found.append(f"{key}: unknown track {node['track']}")
            if node["who"] not in _bg().WHO:
                found.append(f"{key}: who must be one of {', '.join(_bg().WHO)}")
            if node["track"] == self.staged:
                if node.get("stage") not in self.stages:
                    found.append(f"{key}: {self.staged} node needs a stage")
            elif node.get("stage") is not None:
                found.append(f"{key}: only {self.staged} nodes have a stage")
            outcomes = node["outcomes"]
            if set(outcomes) - {_bg().DROPPED} != {"pass", "returned"}:
                found.append(f"{key}: outcomes must name pass and returned")
                continue
            if _bg().DROPPED in outcomes and (outcomes[_bg().DROPPED] is not True or not self.drops):
                found.append(f"{key}: dropped must be true, in a graph whose move_outcomes name dropped")
            if outcomes["pass"] != _bg().DONE and outcomes["pass"] not in self.nodes:
                found.append(f"{key}: pass names unknown node {outcomes['pass']}")
            elif outcomes["pass"] != _bg().DONE and self.nodes[outcomes["pass"]]["track"] != node["track"]:
                found.append(f"{key}: pass leaves its track")
            for target in outcomes["returned"]:
                if target not in self.nodes:
                    found.append(f"{key}: returned names unknown node {target}")
            for target in node.get("requires", []):
                if target not in self.nodes:
                    found.append(f"{key}: requires unknown node {target}")
            if not isinstance(node.get("owns"), list):
                found.append(f"{key}: owns must be a list of paths")
            if not all(isinstance(doc, str) for doc in node.get("docs", [])):
                found.append(f"{key}: docs must be a list of paths")
            if "stop" in node and not _bg().stop_shape(node["stop"]):
                found.append(f"{key}: stop must give minutes and repeat_failures as whole numbers")
            if "pass_check" in node and (not isinstance(node["pass_check"], str) or not node["pass_check"].strip()):
                found.append(f"{key}: pass_check must name a check")
            if "approval_source" in node and node["approval_source"] not in _bg().PACK.approval_sources:
                found.append(f"{key}: unknown approval_source")
            if node.get("presentation") == "assembler_preview" and node.get("approval_source") != "sealed_package":
                found.append(f"{key}: assembler preview needs the sealed_package source")
            if "presentation" in node and (node["presentation"] not in ("board_render", "assembler_preview")
                                            or node["who"] != _bg().OWNER or not _bg().board_approvals.tracked(node)):
                found.append(f"{key}: presentation needs a tracked owner card and a supported preview type")
            if "approval_view" in node and (not isinstance(node["approval_view"], str) or node["approval_view"] not in _bg().VIEWS):
                found.append(f"{key}: approval_view must name an available pack view")
            if "unchanged_pass" in node and node["unchanged_pass"] not in _bg().VIEWS:
                found.append(f"{key}: unchanged_pass must name a view, " + (f"one of {', '.join(_bg().VIEWS)}" if _bg().VIEWS
                             else f"and the {_bg().PACK.name} pack has none"))
        for issue in self.data["open_issues"]:
            for target in issue["close_before"]:
                if target not in self.nodes:
                    found.append(f"open issue {issue['id']}: close_before names unknown node {target}")
            if "closed_by" in issue and issue["closed_by"] not in self.nodes:
                found.append(f"open issue {issue['id']}: closed_by names unknown node {issue['closed_by']}")
        for track in self.data["tracks"]:
            members = self.track_nodes(track["id"])
            if not track["ordered"]:
                if track["start"] is not None:
                    found.append(f"track {track['id']}: an unordered track has no start")
                continue
            if track["start"] not in members:
                found.append(f"track {track['id']}: start must be one of its nodes")
                continue
            seen, node = [], track["start"]
            while node != _bg().DONE and node not in seen:
                seen.append(node)
                node = self.nodes[node]["outcomes"]["pass"]
            if node != _bg().DONE:
                found.append(f"track {track['id']}: pass edges loop at {node}")
            elif seen != members:
                found.append(f"track {track['id']}: pass edges from {track['start']} must visit every node in file order")
        if not found:
            found.extend(_bg().board_autonomy.problems(self))
        return found

    def docs(self, key):
        """The docs a person or agent on this card reads whole, in order: the graph's own for every card, then the card's."""
        return list(self.data.get("docs", [])) + list(self.nodes[key].get("docs", []))

    def stop(self, key):
        """The card's wall and repeated-failure stops: its own, else the graph's; {} when neither is set."""
        if self.nodes[key].get("stop"):
            return dict(self.nodes[key]["stop"], own=True)
        return dict(self.data["stop"], own=False) if self.data.get("stop") else {}

    def reading_problems(self):
        """What an agent could not read whole: a line of this file a Read would cut (none when the graph came as data
        from another machine), and a doc a card names, or every agent loads, that is missing or over a loader's cut."""
        found = []
        if self.text is not None:
            found += [f"line {number} is {len(line)} characters, over {_bg().LINE_CAP}: a Read cuts it"
                      for number, line in enumerate(self.text.splitlines(), 1) if len(line) > _bg().LINE_CAP]
        named = list(self.data.get("docs", []))
        for node in self.data["nodes"]:
            named += [doc for doc in node.get("docs", []) if doc not in named]
        for doc in named + [doc for doc in _bg().LOADED if doc not in named]:
            path = self.root / doc
            if not path.is_file() and doc not in _bg().LOADED:
                shared = _bg().tool_paths.shared_tool_file(doc)
                if shared is not None:
                    path = shared
            if not path.is_file():
                found.append(f"{doc} is missing" + ("" if doc in _bg().LOADED else ", and a card names it"))
                continue
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            if len(text) > _bg().DOC_CHARS:
                found.append(f"{doc} is {len(text)} characters, over {_bg().DOC_CHARS}: a shell's output cut drops its end")
            if len(lines) > _bg().DOC_LINES:
                found.append(f"{doc} is {len(lines)} lines, over {_bg().DOC_LINES}: a Read stops there")
            cut = [number for number, line in enumerate(lines, 1) if len(line) > _bg().LINE_CAP]
            if cut:
                found.append(f"{doc}: line {cut[0]} is over {_bg().LINE_CAP} characters: a Read cuts it")
        # Every report is at most DOC_LINES lines: the result at the top, a long
        # list in a file beside it, so a file an agent could not read whole never lands under the board's reports folder.
        reports = self.root / _bg().REPORTS
        if reports.is_dir():
            for path in sorted(reports.rglob("*")):
                if path.is_file() and path.suffix.lower() in _bg().REPORT_SUFFIXES:
                    data = path.read_bytes()
                    count = data.count(b"\n") + (0 if data.endswith(b"\n") else 1)  # as splitlines counts
                    if count > _bg().DOC_LINES:
                        found.append(f"{path.relative_to(self.root).as_posix()} is {count} lines, over {_bg().DOC_LINES}: a Read "
                                     f"stops there; put the long list in a file beside the report")
        return found

    def order_problems(self):
        """Cards that can never pass because they wait on each other in a loop. One card must pass before another
        when it comes before it on an ordered track, when the other requires it, or when an open issue the other
        cannot pass with (`close_before`) names it as the card whose work closes the issue (`closed_by`, which an
        issue may leave out: then nothing says who closes it and the issue adds no order). The first loop found, in one
        line."""
        before = {key: [] for key in self.nodes}  # card -> the cards that must pass before it, each with why

        def must(first, then, why):
            if first != then and first in self.nodes and then in self.nodes:
                before[then].append((first, why))

        for track in self.data["tracks"]:
            if track["ordered"]:
                members = self.track_nodes(track["id"])
                for first, then in zip(members, members[1:]):
                    must(first, then, f"comes before it on {track.get('label', track['id'])}")
        for key, node in self.nodes.items():
            for target in node.get("requires", []) + node.get("requires_current", []):
                must(target, key, "it requires it")
        for issue in self.data["open_issues"]:
            for target in issue["close_before"]:
                must(issue.get("closed_by"), target, f"closes open issue {issue['id']}, which blocks it")
        found, done = [], set()

        def walk(key, path):
            # path: the cards from the start of this walk down to `key`, each with why it must pass before the
            # card that led here.
            for first, why in before[key]:
                names = [name for name, _ in path]
                if first in names:
                    loop = [name for name, _ in path[names.index(first):]] + [first]
                    reasons = [reason for _, reason in path[names.index(first) + 1:]] + [why]
                    steps = [f"{loop[index]} waits on {loop[index + 1]} ({reason})"
                             for index, reason in enumerate(reasons)]
                    found.append("these cards wait on each other, so none can pass: " + "; ".join(steps))
                    return True
                if first not in done and walk(first, path + [(first, why)]):
                    return True
            done.add(key)
            return False

        for key in self.nodes:
            if key not in done and walk(key, [(key, None)]):
                break
        return found

    def track_nodes(self, track):
        return [node["id"] for node in self.data["nodes"] if node["track"] == track]

    def stage_nodes(self, stage):
        return [node["id"] for node in self.data["nodes"] if node.get("stage") == stage]

    def owned(self):
        """Every path some node owns."""
        return sorted({path for node in self.data["nodes"] for path in node["owns"]})

    def allowed(self, key):
        """The outcomes a person or action may record at this node."""
        node = self.nodes[key]
        allowed = {"pass": [node["outcomes"]["pass"]]}
        if node["outcomes"]["returned"]:
            allowed["returned"] = list(node["outcomes"]["returned"])
        if node["outcomes"].get(_bg().DROPPED):
            allowed[_bg().DROPPED] = [_bg().DONE]
        for outcome in self.stay:
            allowed[outcome] = [key]
        return allowed

    def blocking_issues(self, key):
        return [issue["id"] for issue in self.data["open_issues"] if key in issue["close_before"]]

    def mermaid(self):
        lines = ["flowchart LR"]
        for stage in self.data["stages"]:
            lines.append(f'  subgraph stage_{stage["id"]}["{stage["label"]}"]')
            for key in self.stage_nodes(stage["id"]):
                lines.append(f'    {key}["{self.nodes[key]["label"]}"]')
            lines.append("  end")
        for track in self.data["tracks"]:
            if track["id"] == self.staged:
                continue
            lines.append(f'  subgraph track_{track["id"]}["{track["label"]}"]')
            for key in self.track_nodes(track["id"]):
                lines.append(f'    {key}["{self.nodes[key]["label"]}"]')
            lines.append("  end")
        for node in self.data["nodes"]:
            target = node["outcomes"]["pass"]
            if target != _bg().DONE:
                lines.append(f"  {node['id']} --> {target}")
            for target in node["outcomes"]["returned"]:
                lines.append(f"  {node['id']} -. returned .-> {target}")
            for target in node.get("requires", []):
                lines.append(f"  {target} -. requires .-> {node['id']}")
        return "\n".join(lines) + "\n"


def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def local_report(root, name):
    """An individually named, non-linked report artifact in this checkout, or None."""
    root = pathlib.Path(root).resolve()
    name = pathlib.Path(name)
    if name.is_absolute():
        try:
            name = name.relative_to(root)
        except ValueError:
            return None
    if ".." in name.parts:
        return None
    path = root / name
    if any(root.joinpath(*name.parts[:end]).is_symlink() for end in range(1, len(name.parts) + 1)):
        return None
    try:
        path.resolve().relative_to(root / _bg().REPORTS)
    except ValueError:
        return None
    return path if path.is_file() and path.suffix.lower() in _bg().REPORT_ARTIFACT_SUFFIXES else None


def default_views():
    """Each view's hash of the saved board under a root: what record keeps and next compares."""
    return {name: (lambda root, name=name: _bg().PACK.view_hash(root, name)) for name in _bg().VIEWS}


def approval_review(graph, state, root=None, views=None):
    return _bg().board_approvals.Approvals(graph, state.transitions(), root or graph.root,
                                     _bg().default_views() if views is None else views, _bg().file_sha256, _bg().PACK.approval_sources, evidence_hash=_bg().hash_path)


def approval_inputs(graph, state, node, root, views=None, outcome=None, actor=None, attempt_id=None, checked_scope=None):
    mode = graph.data.get("approval_mode", "off")
    if mode == "off":
        return {}
    managed = _bg().board_approvals.tracked(graph.nodes[node])
    if mode != "enforce" and not managed and not _bg().board_approvals.required(graph, node):
        return {}
    review = _bg().approval_review(graph, state, root, views)
    inputs = {}
    if mode == "enforce":
        blockers = review.blockers(node)
        if blockers:
            raise ValueError(f"{node} needs current approvals: " + "; ".join(blockers))
        inputs["approval_history_seq"] = review.sequence
        if actor == _bg().UNCHANGED and review.verdict(node)["status"] != "current":
            raise ValueError(f"{node}: a current original review is required for automatic reuse")
    contract = {"terms": review.terms(node),
                "requires": {key: review.verdict(key)["reviewed_at"] for key in _bg().board_approvals.required(graph, node)}}
    owned = graph.nodes[node]["owns"]
    def _is_owned(path_name):
        return any(path_name == path or path_name.startswith(path.rstrip("/") + "/") for path in owned)
    for kind, names in (("file", review.readings(node)), ("decision", graph.nodes[node].get("requires_decisions", []))):
        for name in names:
            if kind == "file" and _is_owned(name):
                continue
            contract[kind + ":" + name] = review.source(kind, name)
    for name in graph.nodes[node].get("approval_files", []):
        if not _is_owned(name):
            contract["input:" + name] = review.source("file", name)
    if "routing_constraints" in _bg().board_approvals.approval_sources(graph.nodes[node]):
        contract["routing_constraints"] = review.source("source", "routing_constraints")
    if outcome is None:
        inputs["approval_contract"] = contract
    elif attempt_id:
        opened = next((a for a in state.attempts(node) if a["run_id"] == attempt_id), None)
        if not opened or opened["inputs"].get("approval_contract") != contract:
            if mode == "enforce":
                raise ValueError(f"{node}: the starting terms or prerequisite reviews differ. Record the stop and start a fresh attempt.")
            return inputs  # Observation preserves the historical move without granting a reusable review.
    if managed and mode == "enforce" and outcome == "pass" and actor == "script:merge_check":
        raise ValueError(f"{node} needs a review of the merged files before its pass")
    if managed and outcome == "pass" and actor not in (_bg().UNCHANGED, "script:merge_check"):
        scope = review.scope(node)
        missing = [name for name, value in scope.items() if not value]
        automatic = graph.nodes[node]["who"] == "auto" and graph.nodes[node]["action"] in _bg().ACTIONS
        if automatic and checked_scope != scope:
            if mode == "enforce":
                raise ValueError(f"{node}: run its card check on the current inputs before passing")
        elif not missing:
            inputs["approval_v1"] = scope
        elif mode == "enforce":
            raise ValueError(f"{node} needs evidence for: " + ", ".join(review.errors.get(k, k) for k in missing))
    return inputs


def review_at_revision(graph, state, node, root, revision):
    """Use the cited commit for files and board views, including when the merge is requested from main."""
    from functools import lru_cache

    @lru_cache(None)
    def board_text():
        return _bg().run_git(root, "show", f"{revision}:{_bg().VIEW_FILE}").decode("utf-8")

    views = {key: lambda _, fn=entry[0]: fn(board_text()) for key, entry in _bg().VIEWS.items()}
    def sealed_at_commit(directory):
        from board_graph_actions import PRODUCTION
        value = _bg().PACK.approval_sources["sealed_package"](directory)
        paths = {value["archive"]: value["sha256"].lower(),
                 PRODUCTION + "/package-manifest.json": value["manifest_sha256"]}
        return value if _bg().hashes_at(root, revision, paths) == paths else None

    definition = graph
    if graph.path.resolve().is_relative_to(root.resolve()):
        path = graph.path.resolve().relative_to(root.resolve()).as_posix()
        try:
            definition = _bg().Graph(graph.path, json.loads(_bg().run_git(root, "show", f"{revision}:{path}")))
        except (ValueError, KeyError):
            return None
    if node not in definition.nodes:
        return None
    @lru_cache(None)
    def committed_file(name):
        if not _bg().run_git(root, "ls-tree", "-z", revision, "--", name):
            raise FileNotFoundError(name)
        return _bg().run_git(root, "show", f"{revision}:{name}")

    sources = {"sealed_package": sealed_at_commit} if "sealed_package" in _bg().PACK.approval_sources else {}
    for name in ("render_models", "routing_constraints"):
        if name in _bg().PACK.approval_sources:
            source = _bg().PACK.approval_sources[name]
            sources[name] = lambda directory, source=source: source(directory, read=committed_file)
    review = _bg().board_approvals.Approvals(definition, state.transitions(), root, views, _bg().file_sha256, sources,
                                       file_at=lambda name: _bg().hash_at(root, revision, name), read_at=committed_file)
    return review.scope(node)


def check_presentation(graph, state, node, root, outcome, evidence, inputs, views, merged=None, actor=None):
    kind = _bg().board_approvals.presentation_kind(graph.nodes[node])
    mode = graph.data.get("approval_mode", "off")
    if mode == "off" or not kind or outcome not in ("needs_owner", "pass"):
        return
    review = _bg().approval_review(graph, state, root, views)
    try:
        if outcome == "needs_owner":
            inputs["presentation"] = _bg().board_approvals.presentation(review, node, evidence, _bg().VIEW_FILE)
            if mode == "enforce":
                inputs["approval_history_seq"] = review.sequence
            return
        if actor and _bg().board_autonomy.can_delegate(graph, node, actor):
            shown = _bg().board_approvals.presentation(review, node, evidence, _bg().VIEW_FILE)
        else:
            previous = state.transitions(node=node)
            last = previous[-1] if previous else {}
            shown = (merged or {}).get("presentation") or last.get("inputs", {}).get("presentation")
            if not shown or (not merged and last.get("outcome") != "needs_owner"):
                raise ValueError("Present the exact render or assembler preview with a needs_owner record before sign-off")
            cited = {item["path"]: item["sha256"] for item in evidence}
            if any(cited.get(path) != expected for path, expected in shown["files"].items()):
                raise ValueError("Sign-off must cite the same presented files, with matching hashes")
        if shown["scope"] != inputs.get("approval_v1") or shown["scope"] != review.scope(node) or (kind == "board_render" and
                shown["board_sha256"] != _bg().hash_path(root / _bg().VIEW_FILE)):
            raise ValueError("The saved inputs changed since presentation. Present the current view for review.")
        inputs["approved_presentation"] = shown
    except (ValueError, OSError) as error:
        if mode == "enforce" and outcome == "pass":
            raise ValueError(str(error)) from error
        inputs.pop("approval_v1", None)
        inputs["presentation_finding"] = str(error)


def verify_approval_sequence(db, inputs):
    seq = (inputs or {}).get("approval_history_seq")
    if seq is not None and seq != db.execute("SELECT COALESCE(MAX(seq), 0) FROM transitions").fetchone()[0]:
        raise ValueError("Graph history changed during the approval check. Read the current state and try again.")


def view_at(root, at, name):
    """A view as it stood when a record was made: the board at main's last change to it at or before `at`, or
    None where git cannot say (no history here, or no change before then)."""
    when = at.split(".")[0] + ("Z" if at.endswith("+00:00") or at.endswith("Z") else "")
    try:
        shown = subprocess.run(["git", "log", "-1", "--format=%H", f"--until={when}", "main", "--", _bg().VIEW_FILE],
                               cwd=str(root), capture_output=True, text=True, **_bg().NO_WINDOW_KWARGS)
        revision = shown.stdout.strip()
        if shown.returncode or not revision:
            return None
        text = subprocess.run(["git", "show", f"{revision}:{_bg().VIEW_FILE}"], cwd=str(root), capture_output=True,
                              encoding="utf-8", errors="replace", **_bg().NO_WINDOW_KWARGS)
    except OSError:
        return None
    if text.returncode:
        return None
    return _bg().VIEWS[name][0](text.stdout)


def owed_return(graph, state, node):
    """The `returned` record that sent work back to `node` since the card's own last pass, else None. Until the
    card records a pass after it, the card owes that work and does not pass by itself."""
    owed = None
    for row in state.transitions(track=graph.nodes[node]["track"]):
        if row["node"] == node and row["outcome"] == "pass":
            owed = None
        elif row["outcome"] == "returned" and row["target"] == node:
            owed = row
    return owed


def gate_findings(graph, node, root):
    """What keeps the card from recording its work as done or moved forward: the lines of its action's gate (the
    pack's `gates`), and what to do instead. No gate, or nothing found: ([], "")."""
    gate = _bg().PACK.gates.get(graph.nodes[node].get("action"))
    found = list(gate.run(root)) if gate else []
    return (found, gate.way_out) if found else ([], "")


def routing_terms(graph, node, root):
    """Capture the project instructions this routing attempt is asked to satisfy."""
    spec = graph.nodes[node]
    rules = {key: spec.get(key) for key in ("task", "pass_when", "action", "requires", "docs")}
    docs = {name: _bg().hash_path(pathlib.Path(root) / name) for name in ["AGENTS.md", *graph.docs(node)]}
    terms = {"card": rules, "docs": docs}
    if graph.data.get("approval_mode") == "enforce":
        terms["routing_constraints"] = _bg().PACK.approval_sources["routing_constraints"](pathlib.Path(root))
    return terms


def check_routing_terms(graph, state, node, root, attempt_id):
    attempts = state.attempts(node)
    if attempt_id:
        active = next((row for row in attempts if row["run_id"] == attempt_id), None)
    else:
        active = next((row for row in reversed(attempts) if row["action"] == _bg().AGENT), None)
    if attempt_id and (active is None or active["finished_at"] is not None):
        raise ValueError(f"attempt {attempt_id} is no longer open at {node}; nothing was recorded")
    expected = active["inputs"].get("routing_terms") if active else None
    if expected is None:
        raise ValueError("Routing needs an attempt opened under the current contract. Record needs_lead on a "
                         "legacy attempt and start a fresh one before progressing.")
    current = _bg().routing_terms(graph, node, root)
    if expected != current:
        names = sorted(name for name in set(expected["docs"]) | set(current["docs"])
                       if expected["docs"].get(name) != current["docs"].get(name))
        if current["card"] != expected["card"]:
            names.append("Routing card instructions")
        if current.get("routing_constraints") != expected.get("routing_constraints"):
            names.append("KiCad routing rules or project settings")
        raise ValueError("Routing requirements changed since this attempt started: " + ", ".join(names) +
                         ". Record needs_lead and bring the change to the owner before another attempt.")
    return current


def unchanged_pass(graph, state, node, root, views):
    """The pass next records at `node` when its view is unchanged since the card's last pass, else None.

    The view's hash at that pass is the one its record kept; a record from before the views were kept is read
    back through git, from the board as it stood then. With no earlier pass, or no way to read the old view,
    the card starts as usual, and so does a card a `returned` record sent work back to since its last pass.
    """
    name = graph.nodes[node].get("unchanged_pass")
    if not name or _bg().owed_return(graph, state, node) or _bg().gate_findings(graph, node, root)[0]:
        return None
    if graph.data.get("approval_mode") == "enforce":
        verdict = _bg().approval_review(graph, state, root, views).verdict(node)
        if verdict["status"] != "current":
            return None
        return _bg().record(graph, state, node, "pass", _bg().UNCHANGED, f"Review by {verdict['reviewed_by']} at {verdict['reviewed_at']} still covers the declared inputs", root=root, views=views, automatic_reuse=True)
    passes = [row for row in state.transitions(node=node) if row["outcome"] == "pass"]
    if not passes:
        return None
    last = passes[-1]
    if graph.nodes[node].get("action") == "routing_run" and last["inputs"].get("routing_terms") != _bg().routing_terms(graph, node, root):
        return None
    stored = last["inputs"].get(f"view:{name}") or _bg().view_at(root, last["at"], name)
    if stored is None:
        return None
    current = views[name](root)
    if current != stored:
        return None
    note = (f"{_bg().VIEWS[name][1]} unchanged since the pass of {_bg().stamp(last['at'])} by {last['actor']} "
            f"(view {name} {current[:12]}): nothing this card reads has changed, so that pass stands")
    return _bg().record(graph, state, node, "pass", _bg().UNCHANGED, note, root=root, views=views, automatic_reuse=True)


def counts(root, ref):
    """How many commits `ref` is behind and ahead of origin/main, in one git call."""
    ahead, behind = _bg().git_text(root, "rev-list", "--left-right", "--count", f"{ref}...origin/main").split()
    return int(behind), int(ahead)


def status(path, paths, branch=False):
    """`git status --porcelain=v2` of `paths` in the checkout at `path`: its `# branch.*` lines by name (with
    `branch`) and the changed paths, untracked ones included (a rename names its new path)."""
    info, changed = {}, []
    for line in _bg().git_text(path, "status", "--porcelain=v2", *(["--branch"] if branch else []), "--",
                         *paths).splitlines():
        if line.startswith("# "):
            key, _, value = line[2:].partition(" ")
            info[key] = value
        elif line[:2] in ("1 ", "2 ", "u ", "? ", "! "):
            fields = {"1": 8, "2": 9, "u": 10}.get(line[0], 1)  # the path is the last field, spaces and all
            changed.append(line.split(" ", fields)[fields].split("\t")[0])
    return info, changed


def last_transition(graph, state, node):
    """The latest record on the card's track, or on the card itself when its track is a list; None before any."""
    track = graph.nodes[node]["track"]
    rows = state.transitions(track, None if graph.tracks[track]["ordered"] else node)
    return rows[-1] if rows else None


def acknowledged_approach(graph, state, node):
    """The lead's latest settle --ack on this visit to the card, the revised approach a new start follows, or None.
    On an ordered track an acknowledgment at any of its cards counts, so it follows a pass to the next card."""
    track = graph.nodes[node]["track"]
    rows = state.transitions(track, None if graph.tracks[track]["ordered"] else node)
    visit_since = None
    for row in reversed(rows):
        if row["node"] != node or row["target"] != node:
            visit_since = row["at"]
            break
    relevant_nodes = set(graph.track_nodes(track)) if graph.tracks[track]["ordered"] else {node}
    acknowledgments = [attempt["report"]["acknowledged"] for attempt in state.attempts()
                       if attempt["node"] in relevant_nodes and attempt.get("report") and attempt["report"].get("acknowledged")
                       and (visit_since is None or _bg().parse_time(attempt["report"]["acknowledged"]["at"]) >= _bg().parse_time(visit_since))]
    return max(acknowledgments, key=lambda value: _bg().parse_time(value["at"])) if acknowledgments else None


def require_answered_wait(last, node):
    if last and last["node"] == node and last["outcome"] in _bg().WAITS_ON:
        raise ValueError(f"the last record waits on {_bg().WAITS_ON[last['outcome']]}; it must be answered first")
