"""SQLite State, Remote SSH state proxy, and HTTP serve helpers for board_graph."""
from __future__ import annotations

import sys
import atexit
import base64
import hashlib
import json
import pathlib
import sqlite3
import subprocess
import tempfile
import uuid
from contextlib import closing, contextmanager, suppress

def _bg():
    mod = sys.modules.get("board_graph")
    if mod is None:
        import board_graph as mod
    return mod


def open_state(where_from, records):
    """The State at `where_from` (place()): the Mac's over SSH, or a file. Raises mac.MacError when the Mac does
    not answer."""
    state = (_bg().Remote(where_from["path"], records) if where_from["how"] == "ssh" else
             _bg().State(where_from["path"], records, where_from["imports"]))
    state.selected_state_path = where_from["path"] if where_from["how"] in ("--state", _bg().STATE_ENV) else None
    state.publish_reports = where_from["how"] in ("mac", "ssh")
    if where_from["how"] != "ssh":
        state.mac_keep_pace = _bg().read_mac_keep_pace(where_from)
    return state


class UnconfirmedWrite(_bg().mac.MacError):
    """The server may have committed the request before the response was lost."""


def unreached(where_from, error, command):
    """The one line a command prints when the Mac, which holds the database, does not answer."""
    if isinstance(error, _bg().UnconfirmedWrite):
        return f"{command}: {error}. Reopen this checkout to reconcile the receipt with the Mac before retrying."
    return (f"{command}: the graph's database is on the Mac ({where_from['path']}) and the Mac did not answer "
            f"({error}); " + ("it was not read" if command == "where" else "nothing was written, here or there"))


class State:
    """Append-only transitions; the position of a track is its last transition's target.

    The SQLite file is the live state, the one database on the Mac (place()) or one a test names.
    Every transition is also one JSON record, written to `records` and shared through git; the
    database imports the records in `imports` (`records` unless named) it has not seen, in
    their recorded order. Each write is one transaction under SQLite's write lock (writing());
    a writer waits up to BUSY seconds for another's. After every write that changed something,
    `on_change` is called when it is set (follow() points it at the view).
    """

    def __init__(self, path, records=_bg().RECORDS, imports=None):
        self.path = pathlib.Path(path)
        self.records = pathlib.Path(records)
        self.imports = pathlib.Path(imports) if imports is not None else self.records
        self.on_change = None
        self.mac_keep_pace = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            if self.schema_changes(db):
                db.execute("BEGIN IMMEDIATE")
                try:
                    for statement in self.schema_changes(db):  # read again under the lock: another may have done it
                        db.execute(statement)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
        self.flush_exports()
        self.imported = self.import_records()

    @staticmethod
    def schema_changes(db):
        """The statements that bring the database to SCHEMA, in order; none when it is there. A database made
        before a column existed gains it; nothing is dropped or renamed."""
        found = []
        present = [row["name"] for row in db.execute("PRAGMA table_info(transitions)")]
        if present:
            found += [f"ALTER TABLE transitions ADD COLUMN {column} TEXT" for column in _bg().COLUMNS if column not in present]
        names = {row["name"] for row in db.execute("SELECT name FROM sqlite_master")}
        for statement in (part.strip() for part in _bg().SCHEMA.split(";") if part.strip()):
            if statement.split(" IF NOT EXISTS ")[1].split()[0] not in names:
                found.append(statement)
        present = [row["name"] for row in db.execute("PRAGMA table_info(attempts)")]
        if present:
            found += [f"ALTER TABLE attempts ADD COLUMN {column} TEXT" for column in _bg().ATTEMPT_COLUMNS
                      if column not in present]
            found += [f"ALTER TABLE attempts ADD COLUMN {column} INTEGER" for column in _bg().ATTEMPT_NUMBERS
                      if column not in present]
        return found

    def changed(self):
        if self.on_change:
            self.on_change()

    def import_records(self):
        """Import the record files in `imports` the database has not seen, in one write, and take the note of a
        record it has whose file says something else: a record corrected on main corrects the database. Returns
        how many rows changed."""
        if not self.imports.is_dir():
            return 0
        disk = [(path.name, _bg().workflow_history.validate(json.loads(path.read_bytes()), path))
                for path in sorted(self.imports.glob("*.json"))]
        with self.writing() as db:
            known = {row["record_name"]: self.row(row) for row in db.execute("SELECT * FROM transitions")}
            _bg().workflow_history.branch_points([*known.values(), *(dict(item, record_name=name) for name, item in disk if name not in known)])
            for name, item in disk:
                if name in known:
                    _bg().workflow_history.identical_record(known[name], item, name)
                    if item["note"] != known[name]["note"]:
                        db.execute("UPDATE transitions SET note=? WHERE record_name=?", (item["note"], name))
                    continue
                db.execute("INSERT INTO transitions(at,track,node,outcome,target,actor,note,evidence_json,inputs_json,record_name)"
                           " VALUES(?,?,?,?,?,?,?,?,?,?)", (item["at"], item["track"], item["node"], item["outcome"],
                           item["target"], item["actor"], item["note"], json.dumps(item["evidence"], sort_keys=True),
                           json.dumps(item["inputs"], sort_keys=True), name))
            return db.total_changes

    def flush_exports(self):
        """Replay only pending writes committed with the transition. An interrupted append is recoverable."""
        with self.reading() as db:
            pending = db.execute("SELECT destination, content FROM record_exports").fetchall()
        for row in pending:
            _bg().workflow_history.publish(row["destination"], row["content"])
            with closing(self.connect()) as db:
                db.execute("DELETE FROM record_exports WHERE destination=? AND content=?",
                           (row["destination"], row["content"]))

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=_bg().BUSY, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def writing(self):
        """One write: a connection that takes SQLite's write lock at once (BEGIN IMMEDIATE), so no other writer
        comes between what it reads and what it writes, committed when the block ends and rolled back if it
        raises. on_change follows the commit when something changed."""
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            changes = db.total_changes
        if changes:
            self.changed()

    @contextmanager
    def reading(self, db=None):
        """`db` when a write already holds one, so its reads see what it has written; else a connection of its own."""
        if db is not None:
            yield db
            return
        with closing(self.connect()) as own:
            yield own

    def transitions(self, track=None, node=None, db=None):
        query, params = "SELECT * FROM transitions", []
        clauses = []
        if track is not None:
            clauses.append("track=?")
            params.append(track)
        if node is not None:
            clauses.append("node=?")
            params.append(node)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self.reading(db) as conn:
            rows = conn.execute(query, params).fetchall()
        return _bg().workflow_history.replay(self.row(row) for row in rows)

    @staticmethod
    def row(row):
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        item["inputs"] = json.loads(item.pop("inputs_json"))
        return item

    def launch_blocks(self, graph, track, node=None, db=None):
        """Outstanding launch ownership on this ordered track, or on this one list card."""
        nodes = graph.track_nodes(track) if graph.tracks[track]["ordered"] else [node]
        if not nodes:
            return []
        with self.reading(db) as conn:
            rows = conn.execute("SELECT * FROM attempts WHERE launch_state IN ('active','needs_lead') AND node IN ("
                                + ",".join("?" for _ in nodes) + ") ORDER BY rowid", nodes).fetchall()
        return [self.attempt(row) for row in rows]

    def append(self, graph, track, node, outcome, target, actor, note, evidence=None, inputs=None, at=None,
               export=False, close_open=False, from_position=False, record_name=None, tokens=None, attempt_id=None,
               release_launch=False, launch_note=None):
        """Write one transition. Validation of who may record what belongs to record(), not here.

        With `close_open`, every attempt still open at the node ends with this outcome in the same write, and the
        returned item names them under `closed` (the record file does not). With `from_position`, an ordered
        track must still stand at the node under the write lock: another session that moved it since record()
        checked makes this refuse, and nothing is written. With `export`, the record file is written here in the
        same write, named record_file_name(); `record_name` is the file's name when another checkout writes it
        (a Remote's, after this write).
        """
        if graph.nodes[node]["track"] != track:
            raise ValueError(f"{node} is not on track {track}")
        if target != _bg().DONE and target not in graph.nodes:
            raise ValueError(f"unknown target {target}")
        at = at or _bg().now()
        item = {"at": at, "track": track, "node": node, "outcome": outcome, "target": target, "actor": actor,
                "note": note, "evidence": evidence or [], "inputs": inputs or {}}
        name = record_name
        if export:
            name = name or _bg().record_file_name(at, node, outcome)
            self.records.mkdir(parents=True, exist_ok=True)
            path = self.records / name
            if path.exists():
                raise ValueError(f"record already exists: {path}")
        with self.writing() as db:
            _bg().verify_approval_sequence(db, inputs)
            parent = item["inputs"].get("workflow_parent")
            if parent is not None:
                rows = self.transitions(track, None if graph.tracks[track]["ordered"] else node, db)
                if (rows[-1]["record_name"] if rows else None) != parent["name"]:
                    raise ValueError("Another transition has already changed this workflow decision")
                if (rows and name and _bg().workflow_history.order(dict(item, record_name=name)) <= _bg().workflow_history.order(rows[-1])):
                    raise ValueError("The new transition must follow its predecessor in time")
            pending = (inputs or {}).get("merge_question_at")
            if pending:
                rows = self.transitions(track, None if graph.tracks[track]["ordered"] else node, db)
                if not rows or rows[-1]["at"] != pending or not rows[-1]["inputs"].get("pull_request"):
                    raise ValueError("The pending merge review changed; read the current card")
            blockers = self.launch_blocks(graph, track, node, db)
            if blockers and (len(blockers) != 1 or blockers[0]["run_id"] != attempt_id
                             or blockers[0]["launch_state"] != "active"):
                raise ValueError(f"launch attempt {blockers[0]['run_id']} owns this work; settle it or acknowledge "
                                 "its checkpoint before another record")
            if attempt_id is not None:
                attempt = db.execute("SELECT node, finished_at FROM attempts WHERE run_id=?", (attempt_id,)).fetchone()
                if not attempt or attempt["node"] != node or attempt["finished_at"] is not None:
                    raise ValueError(f"attempt {attempt_id} is no longer open at {node}; nothing was recorded")
            if close_open and attempt_id is None:
                held = db.execute("SELECT run_id FROM attempts WHERE node=? AND finished_at IS NULL LIMIT 1", (node,)).fetchone()
                if held:
                    raise ValueError(f"attempt {held['run_id']} is open at {node}; name it with --attempt")
            if from_position and graph.tracks[track]["ordered"]:
                position = self.position(graph, track, db)
                if position != node:
                    raise ValueError(f"{track} moved to {position} while {node} was being recorded; nothing was "
                                     f"written")
                prior = self.transitions(track=track, db=db)
                last = next((row for row in reversed(prior) if row["record_name"]), None)
                if name and last and _bg().workflow_history.order(dict(item, record_name=name)) <= _bg().workflow_history.order(last):
                    raise ValueError("The record clock precedes this track's latest receipt. Refresh the clock before recording.")
            db.execute(
                "INSERT INTO transitions(at,track,node,outcome,target,actor,note,evidence_json,inputs_json,record_name)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (at, track, node, outcome, target, actor, note, json.dumps(item["evidence"], sort_keys=True),
                 json.dumps(item["inputs"], sort_keys=True), name))
            closed = []
            if close_open:
                clause, params = ("run_id=? AND node=?", (attempt_id, node)) if attempt_id is not None else ("node=?", (node,))
                closed = [self.attempt(row) for row in db.execute(
                    f"SELECT * FROM attempts WHERE {clause} AND finished_at IS NULL ORDER BY rowid", params)]
                db.execute(f"UPDATE attempts SET finished_at=?, outcome=?, tokens=COALESCE(?, tokens)"
                           f" WHERE {clause} AND finished_at IS NULL", (at, outcome, tokens, *params))
                if release_launch and attempt_id:
                    state = "needs_lead" if release_launch == "checkpoint" else "returned"
                    if release_launch == "checkpoint":
                        kept = db.execute("SELECT report_json FROM attempts WHERE run_id=?", (attempt_id,)).fetchone()
                        report = json.loads(kept["report_json"]) if kept and kept["report_json"] else {}
                        report["termination_unconfirmed"] = True
                        db.execute("UPDATE attempts SET launch_state=?, launch_note=?, report_json=? "
                                   "WHERE run_id=? AND launch_state='active'",
                                   (state, launch_note, json.dumps(report, sort_keys=True), attempt_id))
                    else:
                        db.execute("UPDATE attempts SET launch_state=?, launch_note=? WHERE run_id=? AND launch_state='active'",
                                   (state, launch_note, attempt_id))
            if export:
                db.execute("INSERT INTO record_exports VALUES(?,?)", (str(path.resolve()), _bg().record_text(item)))
        if export:
            self.flush_exports()
        return dict(item, closed=closed) if close_open else item

    def copy(self):
        """The database's bytes as they are now, taken as one consistent read (SQLite's backup), for a Remote."""
        part = pathlib.Path(tempfile.gettempdir()) / f"board-graph-copy-{uuid.uuid4().hex}.sqlite3"
        try:
            with closing(self.connect()) as db, closing(sqlite3.connect(str(part))) as copy:
                db.backup(copy)
            return part.read_bytes()
        finally:
            with suppress(OSError):
                part.unlink()

    def position(self, graph, track, db=None):
        """The track's current node, DONE, or None for an unordered track."""
        spec = graph.tracks[track]
        if not spec["ordered"]:
            return None
        with self.reading(db) as conn:
            records = conn.execute("SELECT at,record_name,seq,target FROM transitions WHERE track=?", (track,))
            ordered = _bg().workflow_history.replay(dict(row) for row in records)
        return ordered[-1]["target"] if ordered else spec["start"]

    def passed(self, node):
        return any(row["outcome"] == "pass" for row in self.transitions(node=node))

    def moved_since(self, track, node, attempt):
        """Whether the track has stood anywhere but `node` since the attempt ran.

        The attempt's own transition is the one that cites its report; it and every later one count. An
        attempt with no transition (an error, or a result record refused) counts what came after it began.
        """
        rows = self.transitions(track)
        own = next((i for i, row in enumerate(rows)
                    if any(item["path"] == attempt["report_path"] for item in row["evidence"])), None)
        if own is not None:
            later = rows[own:]
        else:
            start = _bg().parse_time(attempt["started_at"])
            later = [row for row in rows if _bg().parse_time(row["at"]) > start]
        return any(row["node"] != node or row["target"] != node for row in later)

    def start_attempt(self, node, action, inputs, started_at, actor=None, note="", checkout=None, track=None,
                      db=None, brief=None, run_id=None, graph=None, launch=False):
        """Open one attempt at `node`: a script next runs (`actor` script:<action>) or, with action AGENT, a person
        or an agent at work there, in the checkout at `checkout`, given `brief` (the card's brief as next printed
        it, kept on the row). With `track`, the node's track, it first asks under the write lock whether a run on
        these inputs would repeat an attempt nobody has dealt with (unresolved()): that reason is raised as
        ValueError and nothing opens. Inside a write, pass its `db`."""
        if db is None:
            with self.writing() as own:
                return self.start_attempt(node, action, inputs, started_at, actor, note, checkout, track, own, brief,
                                          run_id, graph, launch)
        _bg().verify_approval_sequence(db, inputs)
        if "approval_history_seq" in inputs:
            inputs = {name: value for name, value in inputs.items() if name != "approval_history_seq"}
        if graph is not None:
            blockers = self.launch_blocks(graph, graph.nodes[node]["track"], node, db)
            if blockers:
                raise ValueError(f"launch attempt {blockers[0]['run_id']} still owns this work")
        refusal = _bg().unresolved(self, track, node, inputs) if track is not None else None
        if refusal:
            raise ValueError(refusal)
        run_id = run_id or uuid.uuid4().hex
        db.execute("INSERT INTO attempts(run_id,node,action,inputs_json,started_at,actor,note,checkout,brief,launch_state)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (run_id, node, action, json.dumps(inputs, sort_keys=True), started_at, actor or f"script:{action}",
                    note, str(checkout) if checkout else None, brief, "active" if launch else None))
        return run_id

    def take(self, attempts, at, db=None):
        """End open attempts as taken: next --take found someone else's open attempt at the node."""
        if db is None:
            with self.writing() as own:
                return self.take(attempts, at, own)
        for attempt in attempts:
            db.execute("UPDATE attempts SET finished_at=?, outcome=? WHERE run_id=? AND finished_at IS NULL",
                       (at, _bg().TAKEN, attempt["run_id"]))

    def open_work(self, graph, node, actor, note, inputs, checkout, take=False, at=None, brief=None, run_id=None,
                  launch=False):
        """next's write for a person's card: open an attempt at `node`, where its track must stand, for `actor`, with the node's owned
        files hashed in `inputs`, the checkout the work is in, and the card's `brief` as next printed it, kept on the row.

        Returns (the attempt opened or already open, whether it was already open, the attempts taken over). Refuses
        a node that is not its track's position (a list's nodes are each open to work, as for record) and a node
        someone else already holds open (a script next runs, or another next), unless `take`: that closes theirs
        as taken. The same actor starting again opens nothing new. The checks and the write are one transaction
        under the write lock, so of two sessions starting the node at once, the second finds the first.
        """
        if graph.nodes[node].get("action") == "routing_run" and "routing_terms" not in inputs and pathlib.Path(checkout).is_dir():
            inputs = dict(inputs, routing_terms=_bg().routing_terms(graph, node, checkout))
        track = graph.nodes[node]["track"]
        at = at or _bg().now()
        with self.writing() as db:
            _bg().verify_approval_sequence(db, inputs)
            blockers = self.launch_blocks(graph, track, node, db)
            if blockers:
                raise ValueError(f"launch attempt {blockers[0]['run_id']} still owns this work")
            position = self.position(graph, track, db)
            if graph.tracks[track]["ordered"] and position != node:
                raise ValueError(f"{track} is at {position}, not {node}: next starts the position")
            held = self.attempts(node, open_only=True, db=db)
            others = [attempt for attempt in held if attempt["actor"] != actor]
            mine = [attempt for attempt in held if attempt["actor"] == actor]
            if others and not take:
                words = "; ".join(f"{attempt['actor']} since {_bg().stamp(attempt['started_at'])}"
                                  + (f" ({attempt['note']})" if attempt["note"] else "") for attempt in others)
                raise ValueError(f"{node} is on: {words}. Pass --take to take it over.")
            self.take(others, at, db)
            run_id = None if mine else self.start_attempt(node, _bg().AGENT, inputs, at, actor=actor, note=note,
                                                          checkout=checkout, db=db, brief=brief, run_id=run_id,
                                                          launch=launch)
        if mine:
            return mine[-1], True, others
        opened = next(attempt for attempt in self.attempts(node) if attempt["run_id"] == run_id)
        return opened, False, others

    def finish_attempt(self, run_id, finished_at, outcome, report_path, report_sha256, report=None):
        """End an attempt. `report`, the parts of its report the view shows (kept_report), stays in the row."""
        with self.writing() as db:
            db.execute("UPDATE attempts SET finished_at=?, outcome=?, report_path=?, report_sha256=?, report_json=?"
                       " WHERE run_id=?",
                       (finished_at, outcome, report_path, report_sha256,
                        json.dumps(report, sort_keys=True) if report is not None else None, run_id))

    def keep_tokens(self, run_id, tokens):
        """Keep a thread's tokens of context on its attempt: the launcher knows them once the thread has ended."""
        with self.writing() as db:
            db.execute("UPDATE attempts SET tokens=? WHERE run_id=?", (tokens, run_id))

    def keep_launcher(self, run_id, result):
        """Keep compact provider provenance on this exact agent attempt."""
        with self.writing() as db:
            row = db.execute("SELECT action FROM attempts WHERE run_id=?", (run_id,)).fetchone()
            if not row or row["action"] != _bg().AGENT:
                raise ValueError(f"attempt {run_id} is not an agent attempt")
            db.execute("UPDATE attempts SET report_json=? WHERE run_id=?", (json.dumps(result, sort_keys=True), run_id))

    def finish_launch(self, run_id, reason, abnormal, termination_unconfirmed=False):
        """Release a returned process, or leave a visible lead checkpoint after its earlier record."""
        with self.writing() as db:
            row = db.execute("SELECT * FROM attempts WHERE run_id=?", (run_id,)).fetchone()
            if not row or row["launch_state"] != "active" or row["finished_at"] is None:
                raise ValueError(f"launch attempt {run_id} is not an active launch with a completed record")
            state = "needs_lead" if abnormal else "returned"
            report = json.loads(row["report_json"]) if row["report_json"] else {}
            if termination_unconfirmed:
                report["termination_unconfirmed"] = True
            db.execute("UPDATE attempts SET launch_state=?, launch_note=?, report_json=? WHERE run_id=?",
                       (state, reason, json.dumps(report, sort_keys=True), run_id))
            return state

    def ack_launch(self, run_id, actor, note, verified_stopped=False):
        """Keep a lead's revised approach on the exact post-record checkpoint and release it."""
        if not actor.strip() or not note.strip():
            raise ValueError("a lead and revised approach are required to acknowledge a launch checkpoint")
        with self.writing() as db:
            row = db.execute("SELECT * FROM attempts WHERE run_id=?", (run_id,)).fetchone()
            if not row or row["launch_state"] != "needs_lead":
                raise ValueError(f"launch attempt {run_id} has no checkpoint to acknowledge")
            report = json.loads(row["report_json"]) if row["report_json"] else {}
            if report.get("termination_unconfirmed") and not verified_stopped:
                raise ValueError("the worker's process tree was not confirmed stopped; inspect it and use "
                                 "--verified-stopped only after verifying every process has ended")
            report["acknowledged"] = {"by": actor, "note": note, "at": _bg().now(),
                                      "verified_stopped": verified_stopped}
            db.execute("UPDATE attempts SET launch_state='acknowledged', report_json=? WHERE run_id=?",
                       (json.dumps(report, sort_keys=True), run_id))

    def attempts(self, node=None, inputs=None, open_only=False, db=None):
        """Attempts in the order they started: script runs and work next opened for a person. With `inputs`, only those
        on exactly these input hashes; with `open_only`, only those not finished yet."""
        query, params = "SELECT * FROM attempts", []
        clauses = []
        if node is not None:
            clauses.append("node=?")
            params.append(node)
        if inputs is not None:
            clauses.append("inputs_json=?")
            params.append(json.dumps(inputs, sort_keys=True))
        if open_only:
            clauses.append("finished_at IS NULL")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self.reading(db) as conn:
            rows = conn.execute(query + " ORDER BY rowid", params).fetchall()
        return [self.attempt(row) for row in rows]

    @staticmethod
    def attempt(row):
        item = dict(row)
        item["inputs"] = json.loads(item.pop("inputs_json"))
        item["actor"] = item.get("actor") or f"script:{item['action']}"  # a row from before the column
        item["note"] = item.get("note") or ""
        kept = item.pop("report_json", None)
        item["report"] = json.loads(kept) if kept else None
        return item

    def start_upkeep(self, node, why, started_at):
        """One tidy run that next asked for, written before tidy starts."""
        run_id = uuid.uuid4().hex
        with self.writing() as db:
            db.execute("INSERT INTO upkeep(run_id,node,why,started_at) VALUES(?,?,?,?)", (run_id, node, why, started_at))
        return run_id

    def finish_upkeep(self, run_id, finished_at, outcome, said, mac_skipped, problems):
        with self.writing() as db:
            db.execute("UPDATE upkeep SET finished_at=?, outcome=?, mac=?, said=?, problems_json=? WHERE run_id=?",
                       (finished_at, outcome, mac_skipped, said, json.dumps(problems), run_id))

    def upkeeps(self):
        """The tidy runs next made, in the order they started: when, why, the outcome and tidy's own words."""
        with self.reading() as db:
            rows = db.execute("SELECT * FROM upkeep ORDER BY rowid").fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["problems"] = json.loads(item.pop("problems_json") or "[]")
            items.append(item)
        return items

    def keep_full_read(self, machine, view):
        """Keep `machine`'s full read of the workspace (workspace()) in place of its last: a quick `where` there
        carries it, and the view carries the newest machine's."""
        with self.writing() as db:
            db.execute("INSERT OR REPLACE INTO full_reads(machine, at, view_json) VALUES(?,?,?)",
                       (machine, view["at"], json.dumps(view)))

    def last_full_read(self, machine=None):
        """The workspace as `machine`'s last full read found it, or as the newest machine's did with None; None
        when none is kept."""
        with self.reading() as db:
            if machine is None:
                row = db.execute("SELECT view_json FROM full_reads ORDER BY at DESC LIMIT 1").fetchone()
            else:
                row = db.execute("SELECT view_json FROM full_reads WHERE machine=?", (machine,)).fetchone()
        return json.loads(row["view_json"]) if row else None

    def record_checks(self, at, commit, run_id, event, passed, open_count, checks, note=None):
        """One CI run's board checks on `commit`: the validate workflow's last step writes it (keep_checks), on
        the Mac, where the database is. `checks` is summary.json's list; `passed` is None, with a `note`, when
        the run left no checks. Returns the row as checks_for gives it."""
        with self.writing() as db:
            db.execute("INSERT INTO checks(at,commit_sha,run_id,event,passed,open,checks_json,note)"
                       " VALUES(?,?,?,?,?,?,?,?)",
                       (at, commit, str(run_id), event, None if passed is None else int(passed), open_count,
                        json.dumps(checks, sort_keys=True), note))
        return self.checks_for(commit)

    def checks_for(self, commit):
        """The newest CI row on `commit` (a full sha, or its first characters), or None."""
        if not commit:
            return None
        return self.one_check("SELECT * FROM checks WHERE commit_sha LIKE ? ORDER BY seq DESC LIMIT 1", (commit + "%",))

    def latest_checks(self):
        """The newest CI row on any commit, or None."""
        return self.one_check("SELECT * FROM checks ORDER BY seq DESC LIMIT 1", ())

    def one_check(self, query, params):
        """One row of the checks table, or None: also when the table is not there yet (a copy sent by a Mac whose
        code is older than this), which reads as no run having written to the graph."""
        with self.reading() as db:
            try:
                row = db.execute(query, params).fetchone()
            except sqlite3.OperationalError as error:
                if "no such table" in str(error):
                    return None
                raise
        return self.check_row(row) if row else None

    @staticmethod
    def check_row(row):
        item = dict(row)
        item["commit"] = item.pop("commit_sha")
        item["checks"] = json.loads(item.pop("checks_json"))
        item["passed"] = None if item["passed"] is None else bool(item["passed"])
        return item

    def merge_question(self, track, node=None):
        """The pull request the track's position waits on, or None: its last transition asked the owner to merge
        it. With `node`, a list's card, that card's own last transition."""
        rows = self.transitions(track, node)
        if not rows:
            return None
        last = rows[-1]
        question = last["inputs"].get("pull_request")
        if last["outcome"] != "needs_owner" or not isinstance(question, dict):
            return None
        return dict(question, node=last["node"], asked_at=last["at"])


class Remote(State):
    """The Mac's database from another machine, over the SSH path scripts/mac.py uses.

    Opening it fetches a copy of the database (one round trip; the Mac imports its main checkout's record files
    first), and every read answers from that copy, read-only. Every write is one round trip: `serve` on the Mac
    runs the same State method there, one transaction under SQLite's write lock with the check that decides it
    under the same lock, refreshes the page's view.json there, and sends the database back, so the copy is current
    after each write. A record file this checkout exports is written here after the Mac's write. A Mac that does
    not acknowledge a write leaves its result uncertain. Reopening reads committed state and recovers the local export.
    """

    def __init__(self, path, records=_bg().RECORDS):
        self.path = path  # the database on the Mac, in words
        self.records = pathlib.Path(records)
        self.imports = None
        self.on_change = None
        self.mac_keep_pace = None
        self.imported = 0
        self.copy_path = pathlib.Path(tempfile.gettempdir()) / f"board-graph-{uuid.uuid4().hex}.sqlite3"
        atexit.register(self.close)
        self.call("read")
        self.recover_deliveries()

    def pending_folder(self):
        parent = self.records.parent
        if parent.is_dir():
            directory = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=parent, capture_output=True,
                                       text=True, **_bg().NO_WINDOW_KWARGS)
            if directory.returncode == 0:
                namespace = hashlib.sha256(str(self.records.resolve()).encode()).hexdigest()
                return pathlib.Path(directory.stdout.strip()) / "board-graph-pending" / namespace
        return self.records.with_name("." + self.records.name + ".pending")

    def recover_deliveries(self):
        pending = self.pending_folder()
        if not pending.is_dir():
            return
        rows = {r["record_name"]: r for r in self.transitions()}
        for marker in pending.glob("*.json"):
            committed = rows.get(marker.name)
            if committed:
                intended = json.loads(marker.read_bytes())
                _bg().workflow_history.identical_record(committed, intended, marker.name)
                _bg().workflow_history.publish(self.records / marker.name, _bg().record_text(committed))
                marker.unlink(missing_ok=True)

    def close(self):
        with suppress(OSError):
            self.copy_path.unlink()

    def connect(self):
        db = sqlite3.connect(f"{self.copy_path.as_uri()}?mode=ro", uri=True, timeout=_bg().BUSY, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def writing(self):
        raise RuntimeError("a Remote writes through call(), one round trip to the Mac per write")
        yield  # noqa: unreachable, makes this a generator as the base's is

    def call(self, op, graph=None, **args):
        """One round trip: `op` (OPS) with `args` on the Mac's database, with this checkout's `graph` when the op
        needs one. Returns what it returned there and keeps the copy it sent back; raises what it raised there (a
        ValueError as ValueError, a busy database as sqlite3.OperationalError, else RuntimeError), and
        mac.MacError when the Mac did not answer."""
        wire_op = "approval_" + op if "approval_history_seq" in (args.get("inputs") or {}) else op
        request = {"op": wire_op, "args": args, "graph": graph.data if graph is not None else None}
        try:
            answer = _bg().remote_call(json.dumps(request))
        except _bg().mac.MacError as error:
            if op != "read":
                raise _bg().UnconfirmedWrite(f"Mac write acknowledgment missing for {op}: {error}") from error
            raise
        if "error" in answer:
            kind = answer["error"].get("type")
            raise {"ValueError": ValueError, "OperationalError": sqlite3.OperationalError}.get(kind, RuntimeError)(
                answer["error"].get("message", "serve on the Mac failed"))
        self.copy_path.write_bytes(base64.b64decode(answer["copy"]))
        if "mac_keep_pace" in answer:
            self.mac_keep_pace = answer["mac_keep_pace"]
        elif op == "read":
            self.mac_keep_pace = None
        if op != "read":
            self.changed()
        return answer["result"]

    def append(self, graph, track, node, outcome, target, actor, note, evidence=None, inputs=None, at=None,
               export=False, close_open=False, from_position=False, record_name=None, tokens=None, attempt_id=None,
               release_launch=False, launch_note=None):
        at = at or _bg().now()
        path = None
        if export:
            record_name = record_name or _bg().record_file_name(at, node, outcome)
            self.records.mkdir(parents=True, exist_ok=True)
            path = self.records / record_name
            if path.exists():
                raise ValueError(f"record already exists: {path}")
        # tokens travels only when set: the Mac's serve may still run the code before this argument for the minute
        # after a push to main, until its keep-pace sync, and must not refuse every record meanwhile.
        extra = {"tokens": tokens} if tokens is not None else {}
        if attempt_id is not None:
            extra["attempt_id"] = attempt_id
        if release_launch:
            extra["release_launch"] = release_launch
        if launch_note is not None:
            extra["launch_note"] = launch_note
        marker = None
        if path is not None:
            marker = self.pending_folder() / record_name
            intended = dict(at=at, track=track, node=node, outcome=outcome, target=target, actor=actor, note=note,
                            evidence=evidence or [], inputs=inputs or {})
            _bg().workflow_history.publish(marker, _bg().record_text(intended))
        try:
            item = self.call("append", graph, track=track, node=node, outcome=outcome, target=target, actor=actor,
                             note=note, evidence=evidence, inputs=inputs, at=at, export=False, close_open=close_open,
                             from_position=from_position, record_name=record_name, **extra)
        except ValueError:
            if marker:
                marker.unlink(missing_ok=True)
            raise
        if path is not None:
            _bg().workflow_history.publish(path, _bg().record_text(item))
            marker.unlink(missing_ok=True)
        return item

    def start_attempt(self, node, action, inputs, started_at, actor=None, note="", checkout=None, track=None,
                      db=None, brief=None, run_id=None, graph=None, launch=False):
        extra = {"run_id": run_id} if run_id is not None else {}
        if launch:
            extra["launch"] = True
        return self.call("start_attempt", graph, node=node, action=action, inputs=inputs, started_at=started_at,
                         actor=actor, note=note, checkout=checkout, track=track, brief=brief, **extra)

    def open_work(self, graph, node, actor, note, inputs, checkout, take=False, at=None, brief=None, run_id=None,
                  launch=False):
        extra = {"run_id": run_id} if run_id is not None else {}
        if launch:
            extra["launch"] = True
        attempt, already, others = self.call("open_work", graph, node=node, actor=actor, note=note, inputs=inputs,
                                             checkout=checkout, take=take, at=at or _bg().now(), brief=brief, **extra)
        return attempt, already, others

    def finish_attempt(self, run_id, finished_at, outcome, report_path, report_sha256, report=None):
        return self.call("finish_attempt", run_id=run_id, finished_at=finished_at, outcome=outcome,
                         report_path=report_path, report_sha256=report_sha256, report=report)

    def keep_tokens(self, run_id, tokens):
        return self.call("keep_tokens", run_id=run_id, tokens=tokens)

    def keep_launcher(self, run_id, result):
        return self.call("keep_launcher", run_id=run_id, result=result)

    def finish_launch(self, run_id, reason, abnormal, termination_unconfirmed=False):
        return self.call("finish_launch", run_id=run_id, reason=reason, abnormal=abnormal,
                         termination_unconfirmed=termination_unconfirmed)

    def ack_launch(self, run_id, actor, note, verified_stopped=False):
        return self.call("ack_launch", run_id=run_id, actor=actor, note=note, verified_stopped=verified_stopped)

    def start_upkeep(self, node, why, started_at):
        return self.call("start_upkeep", node=node, why=why, started_at=started_at)

    def finish_upkeep(self, run_id, finished_at, outcome, said, mac_skipped, problems):
        return self.call("finish_upkeep", run_id=run_id, finished_at=finished_at, outcome=outcome, said=said,
                         mac_skipped=mac_skipped, problems=problems)

    def keep_full_read(self, machine, view):
        return self.call("keep_full_read", machine=machine, view=view)

    def record_checks(self, at, commit, run_id, event, passed, open_count, checks, note=None):
        return self.call("record_checks", at=at, commit=commit, run_id=run_id, event=event, passed=passed,
                         open_count=open_count, checks=checks, note=note)


def remote_call(request_text):
    """One request to `serve` on the Mac, run in its main checkout over SSH; the answer, parsed. Raises
    mac.MacError when the Mac does not answer, or serve there did not answer in JSON."""
    result = _bg().mac.ssh(_bg().SERVE, request_text, _bg().SERVE_TIMEOUT)
    out = _bg().mac.text(result.stdout).strip()
    try:
        answer = json.loads(out.splitlines()[-1]) if out else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        said = _bg().mac.text(result.stderr).strip() or out or f"exit {result.returncode}"
        raise _bg().mac.MacError(f"serve on the Mac did not answer: {' '.join(said.split())[:300]}")
    return answer


def serve(request, where_from, records=_bg().RECORDS, graph_path=_bg().GRAPH):
    """One request from a Remote, on this machine's database (`where_from`, place()): {"op", "args", "graph"}.
    The answer: {"result", "copy"} (the database's bytes after the op, base64) or {"error": {"type", "message"}}.

    Opening the database imports the record files in where_from["imports"] it has not seen, and the view in
    where_from["view"] follows every write, drawn with this checkout's graph (the Mac's main); the op itself
    uses the graph the request sent, the caller's, which can hold a node this checkout's does not.
    """
    try:
        op = request.get("op")
        if op in ("approval_append", "approval_open_work", "approval_start_attempt"):
            op = op[len("approval_"):]
        if op not in _bg().OPS:
            raise ValueError(f"serve does not know the operation {op!r}")
        state = _bg().State(where_from["path"], records, where_from["imports"])
        state.selected_state_path = where_from["path"] if where_from["how"] in ("--state", _bg().STATE_ENV) else None
        keep_pace = _bg().read_mac_keep_pace(where_from)
        state.mac_keep_pace = keep_pace
        if where_from["view"]:
            try:
                _bg().follow(_bg().Graph(graph_path), state, where_from["view"])
            except (OSError, ValueError) as error:
                sys.stderr.write(f"the view does not follow this write ({error}); the state is written\n")
            else:
                if state.imported:
                    state.changed()
        args = dict(request.get("args") or {})
        if op == "read":
            result = None
        elif op in ("append", "open_work"):
            result = getattr(state, op)(_bg().Graph(data=request["graph"]), **args)
        elif op == "start_attempt" and request.get("graph") is not None:
            result = state.start_attempt(graph=_bg().Graph(data=request["graph"]), **args)
        else:
            result = getattr(state, op)(**args)
        answer = {"result": result, "copy": base64.b64encode(state.copy()).decode("ascii")}
        if keep_pace is not None:
            answer["mac_keep_pace"] = keep_pace
        return answer
    except Exception as error:  # noqa: BLE001 - the error travels back as data, typed, and is raised there
        return {"error": {"type": type(error).__name__, "message": str(error)}}
