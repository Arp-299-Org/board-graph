"""Workspace views, brief rendering, record/next execution, and track rendering for board_graph."""
from __future__ import annotations

import sys
import json
import pathlib
import subprocess
import time
import traceback
import uuid
from datetime import datetime, timezone

def _bg():
    mod = sys.modules.get("board_graph")
    if mod is None:
        import board_graph as mod
    return mod


def _validate_record_target(graph, state, node, outcome, actor, note, evidence, to, root, views, attempt_id, checked_scope, delivering_copper):
    """Validate card state, pass gates, routing terms, and approval inputs for record()."""
    if actor == _bg().UNCHANGED and not views.get("automatic_reuse"):
        raise ValueError("Automatic approval reuse belongs to next after it verifies an original review")
    if node not in graph.nodes:
        raise ValueError(f"unknown node {node}")
    spec = graph.nodes[node]
    track = spec["track"]
    if graph.tracks[track]["ordered"] and state.position(graph, track) != node:
        position = state.position(graph, track)
        raise ValueError(f"{track} is at {position}, not {node}")
    allowed = graph.allowed(node)
    if outcome not in allowed:
        raise ValueError(f"{node} does not allow {outcome}; it allows {', '.join(allowed)}")
    if not note.strip():
        raise ValueError("a note is required: say why, in plain words")
    if outcome == "returned":
        if to not in allowed["returned"]:
            raise ValueError(f"returned from {node} must name one of {', '.join(allowed['returned'])}")
        target = to
    elif to is not None:
        raise ValueError("--to is only for returned")
    else:
        target = allowed[outcome][0]
    if outcome == "pass":
        blocking = graph.blocking_issues(node)
        if blocking:
            raise ValueError(f"{node} cannot pass while open issues name it: {', '.join(blocking)}")
        unmet = [key for key in spec.get("requires", []) if not state.passed(key)]
        if unmet:
            raise ValueError(f"{node} cannot pass until these pass: {', '.join(unmet)}")
        _bg().board_autonomy.check_target_gate(graph, state, root, views["views"], node, outcome, actor)
        legacy = (node, spec.get("action"))
        release = spec.get("release_check") or _bg().PACK.legacy_release.get(legacy) or _bg().PACK.legacy_release.get((node, "*"))
        if release:
            _bg().PACK.release_checks[release](spec, evidence, root)
        completion_check = spec.get("pass_check") or _bg().PACK.legacy_pass.get(legacy)
        if completion_check:
            if completion_check not in _bg().ACTIONS:
                raise ValueError(f"Cannot pass {spec['label']}: completion check {completion_check} is unavailable")
            findings = _bg().ACTIONS[completion_check].run(root)["findings"]
            if findings:
                raise ValueError(f"Cannot pass {spec['label']}: " + "; ".join(findings[:4]))
    progressing = outcome in _bg().GATED or delivering_copper
    if delivering_copper and (spec.get("action") != "routing_run" or not attempt_id):
        raise ValueError("Copper delivery needs its originating routing attempt")
    view_fns = views["views"]
    approval = (_bg().approval_inputs(graph, state, node, root, view_fns, outcome, actor, attempt_id, checked_scope)
                if progressing else {})
    terms = None
    if spec.get("action") == "routing_run" and progressing:
        if actor in (_bg().OWNER, _bg().UNCHANGED):
            terms = _bg().routing_terms(graph, node, root)
        elif attempt_id or outcome == "pass":
            terms = _bg().check_routing_terms(graph, state, node, root, attempt_id)
    if progressing and actor != _bg().OWNER:
        found, way_out = _bg().gate_findings(graph, node, root)
        if found:
            more = f"; and {len(found) - 6} more" if len(found) > 6 else ""
            raise ValueError(f"{node} cannot record {outcome}: " + "; ".join(found[:6]) + more + f". {way_out}")
    if outcome == _bg().DROPPED and actor != _bg().OWNER:
        raise ValueError(f"dropping {track} is the owner's to record, with the owner's reason as the note")
    if outcome in ("pass", "returned") and not evidence and actor != _bg().UNCHANGED:
        raise ValueError(f"{outcome} needs at least one evidence path")
    return spec, track, allowed, target, approval, terms


def _collect_record_evidence(state, evidence, root, merge, pull_request):
    """Hash evidence paths inside root or at the PR head, normalizing CRLF in published reports."""
    hashed = []
    for item in evidence:
        path = pathlib.Path(item)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            at_head = None
            if merge and not pathlib.Path(item).is_absolute():
                at_head = _bg().hash_at(root, merge["head"], item)
            if at_head is None:
                raise ValueError(f"evidence not found: {item}"
                                 + (f" (here or at pull request {pull_request}'s head)" if merge else ""))
            hashed.append({"path": pathlib.PurePath(item).as_posix().rstrip("/"), "sha256": at_head,
                           "at": merge["head"]})
            continue
        try:
            name = path.resolve().relative_to(pathlib.Path(root).resolve()).as_posix()
        except ValueError:
            raise ValueError(f"evidence must be inside the project: {item}")
        report = _bg().local_report(root, item) if getattr(state, "publish_reports", False) else None
        if report and report.suffix.lower() in _bg().REPORT_SUFFIXES:
            data = report.read_bytes()
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                pass  # not text: leave its bytes alone
            else:
                normalized = data.replace(b"\r\n", b"\n") if b"\0" not in data else data
                if normalized != data:
                    attributes = _bg().run_git(root, "check-attr", "-z", "text", "--", name).split(b"\0")
                    if len(attributes) < 3 or attributes[2] != b"unset":
                        report.write_bytes(normalized)
        hashed.append({"path": name, "sha256": _bg().hash_path(path)})
    return hashed


def record(graph, state, node, outcome, actor, note, evidence=(), to=None, root=_bg().ROOT, pull_request=None, then=None,
           github=None, tokens=None, attempt_id=None, release_launch=False, launch_note=None, views=None, checked_scope=None, merged_review=None, expected_question=None, automatic_reuse=False, delivering_copper=False):
    """Record one outcome at the node where its track stands. Returns the transition written.

    Every attempt still open at the node (work next opened for a person, or a script run that never finished) ends with
    the outcome recorded, in the same write.

    Refuses a node that is not its track's position, an outcome the node does not allow, a
    returned target off the node's list, a pass while an open issue names the node or a
    required node has not passed, a pass or more_work while the card's gate has findings (work that did not come
    through the card's own tool: only the owner's own record accepts it), a pass on an owner node by anyone but the owner, a pass or
    returned without evidence, and an evidence path that does not exist.

    A needs_owner with `pull_request` and `then` asks the owner to merge that pull request: it
    fetches the pull request's head and stores, in the inputs, the number, the outcome that
    follows the merge, the head and the hashes of the node's owned files there, for next's merge
    check. Its evidence may be a path that exists only at that head.

    A node with `unchanged_pass` keeps its view's hash in the inputs of every record, and a pass by
    `script:unchanged` (the view unchanged since the last pass) needs no evidence and may pass an
    owner's card: the owner's pass on the same view stands.
    """
    view_fns = _bg().default_views() if views is None else views
    spec, track, allowed, target, approval, terms = _validate_record_target(
        graph, state, node, outcome, actor, note, evidence, to, root,
        {"views": view_fns, "automatic_reuse": automatic_reuse}, attempt_id, checked_scope, delivering_copper,
    )
    merge = None
    if pull_request is not None or then is not None:
        if outcome != "needs_owner":
            raise ValueError("--pull-request and --then are for needs_owner, asking the owner to merge")
        follows = [key for key in allowed if key not in ("needs_owner", "returned", _bg().DROPPED)]
        if pull_request is None or then not in follows:
            raise ValueError(f"a merge question names the pull request and, with --then, the outcome its merge "
                             f"leads to at {node}: one of {', '.join(follows)}")
        info = (github or (lambda number: _bg().pull_request_view(number, root)))(pull_request)
        if info["state"] != "OPEN":
            raise ValueError(f"pull request {pull_request} is {info['state'].lower()}, not open: there is no merge "
                             f"to ask for")
        head = info["headRefOid"]
        _bg().fetch_pull_request(root, pull_request, head)
        merge = {"number": int(pull_request), "then": then, "head": head,
                 "expected": _bg().hashes_at(root, head, spec["owns"])}
    hashed = _collect_record_evidence(state, evidence, root, merge, pull_request)
    inputs = {**{owned: _bg().hash_path(root / owned) for owned in spec["owns"]}, **approval,
              **(_bg().board_autonomy.delegated_inputs(graph, node, actor) if outcome == "pass" else {})}
    if graph.data.get("approval_mode") == "enforce":
        ordered = graph.tracks[track]["ordered"]
        rows = state.transitions(track, None if ordered else node)
        if not rows or rows[-1]["record_name"]:
            inputs["workflow_parent"] = {"scope": "track" if ordered else "card",
                                         "name": rows[-1]["record_name"] if rows else None}
    if attempt_id:
        inputs["attempt_id"] = attempt_id
    if expected_question is not None:
        inputs["merge_question_at"] = expected_question
    if actor != _bg().UNCHANGED:
        _bg().check_presentation(graph, state, node, root, outcome, hashed, inputs, view_fns, merged_review, actor=actor)
    if merged_review:
        actual = {item["path"]: item["sha256"] for item in hashed}
        if (merged_review["scope"] == inputs.get("approval_v1")
                and all(actual.get(path) == value for path, value in merged_review["evidence"].items())):
            inputs["reviewed_merge"] = {key: merged_review[key] for key in ("head", "actor", "note", "asked_at", "merge_commit")}
        elif graph.data.get("approval_mode") == "enforce":
            raise ValueError("The merged inputs or evidence differ from the original review")
        else:
            inputs.pop("approval_v1", None)
    if terms is not None:
        inputs["routing_terms"] = terms
    view = spec.get("unchanged_pass")
    if view:
        try:
            inputs[f"view:{view}"] = view_fns[view](root)
        except FileNotFoundError:
            pass  # recorded away from the board (a test's root): the view is read back through git when needed
    if merge:
        if graph.data.get("approval_mode", "off") != "off" and then == "pass" and _bg().board_approvals.tracked(spec):
            scope = _bg().review_at_revision(graph, state, node, root, merge["head"])
            cited = {entry["path"]: entry["sha256"] for entry in hashed}
            shown = inputs.get("presentation")
            if (scope and cited and all(scope.values()) and _bg().hashes_at(root, merge["head"], cited) == cited
                    and actor not in (_bg().UNCHANGED, "script:merge_check")
                    and (not _bg().board_approvals.presentation_kind(spec) or (shown and shown["scope"] == scope))
                    and (spec["who"] != _bg().OWNER or actor == _bg().OWNER)
                    and spec["who"] != "auto"):
                merge["review"] = {"head": merge["head"], "scope": scope, "actor": actor, "note": note,
                                   "evidence": cited, "presentation": shown}
        inputs["pull_request"] = dict(merge, evidence=[entry["path"] for entry in hashed])
    return state.append(graph, track, node, outcome, target, actor, note, hashed, inputs, export=True,
                        close_open=True, from_position=True, tokens=tokens, attempt_id=attempt_id,
                        release_launch=release_launch, launch_note=launch_note)


def recorded(graph, state, item):
    """What record prints after writing a transition."""
    track = graph.nodes[item["node"]]["track"]
    closed = "".join(f"closed the open attempt by {attempt['actor']} (since {_bg().stamp(attempt['started_at'])})\n"
                     for attempt in item.get("closed", []))
    position = state.position(graph, track)
    if not graph.tracks[track]["ordered"]:
        standing = f"{track} is a list: {item['node']} recorded {item['outcome']}"
    elif position == _bg().DONE:
        standing = f"{track} is dropped at {item['node']}" if item["outcome"] == _bg().DROPPED else f"{track} is done"
    elif item["outcome"] == "returned":
        standing = f"{track} returned from {item['node']} to {position}"
    elif position != item["node"]:
        standing = f"{track} is between {item['node']} and {position}"
    else:
        standing = f"{track} stays at {position}, nothing on it"
    blockers = state.launch_blocks(graph, track, item["node"])
    if blockers:
        standing += f"; launch {blockers[0]['run_id']} still owns the work"
    return (f"{item['node']} {item['outcome']} -> {item['target']} recorded at {item['at']}\n{closed}"
            f"{standing}\n")


def land(root, paths, message):
    """Carry a record to its branch, the last step of a card's move: the files in `paths` that are inside `root`
    and exist (the record file, its report) are committed in `root` with `message`, and the branch is pushed to
    origin. Returns one line saying what happened. Nothing raises: what could not happen is said, and the files
    stay in the working tree for the next record or the session's next push to carry. On the Mac's main checkout
    nothing is committed, because only a pull request reaches main from there."""
    root = pathlib.Path(root).resolve()
    inside = []
    for path in paths:
        path = pathlib.Path(path)
        try:
            name = path.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        if path.exists():
            inside.append(name)
    if not inside:
        return "not committed: the record is outside this checkout"
    named = ", ".join(inside)
    try:
        branch = _bg().git_text(root, "symbolic-ref", "--short", "-q", "HEAD").strip()
    except ValueError:
        branch = ""
    if not branch:
        return f"not committed: this checkout is on no branch; {named} waits in the working tree"
    if _bg().mac.LOCAL and not _bg().mac.ONE_MACHINE and branch == "main":
        return f"not committed: on the Mac's main a record travels by pull request; {named} waits in the working tree"
    try:
        _bg().run_git(root, "--literal-pathspecs", "add", "--", *inside)
        _bg().run_git(root, "--literal-pathspecs", "commit", "-q", "--only", "-m", message, "--", *inside)
        short = _bg().git_text(root, "rev-parse", "--short", "HEAD").strip()
    except ValueError as error:
        return f"not committed ({error}); {named} waits in the working tree"
    try:
        _bg().run_git(root, "push", "-q", "-u", "origin", branch, timeout=180)
    except ValueError as error:
        return f"committed {short} on {branch}; the push failed ({error}): it goes with the next push"
    return f"committed {short} on {branch} and pushed to origin"


def carry(graph, state, item, where_from, extra=(), root=_bg().ROOT, github=None):
    """land()'s line for transition `item`, with its newline: the record file, individually cited local report
    artifacts and the `extra` paths committed and pushed; on a branch other than main, a line for its pull request
    (keep_pull_request). Empty when the database is not the one on the Mac (--state or BOARD_GRAPH_STATE
    named another: a test or an experiment, whose records stay where they are)."""
    github = _bg().gh if github is None else github
    if where_from["how"] not in ("mac", "ssh"):
        return ""
    root = pathlib.Path(root).resolve()
    cited = []
    for entry in item.get("evidence", []):
        if entry.get("at"):  # evidence from a pull request's head is not a file in this checkout
            continue
        path = _bg().local_report(root, entry["path"])
        if path:
            cited.append(path)
    paths = list(dict.fromkeys([pathlib.Path(state.records) / _bg().record_file_name(item["at"], item["node"], item["outcome"]),
                                *cited, *(root / path for path in extra)]))
    note = " ".join(item["note"].split())
    message = (f"Record: {graph.nodes[item['node']]['label']}, {item['outcome']} by {item['actor']}: "
               f"{note[:160]}{'...' if len(note) > 160 else ''}")
    said = _bg().land(root, paths, message)
    if said.endswith("and pushed to origin"):
        branch = said.split(" on ", 1)[1].split(" and pushed", 1)[0]
        if branch != "main":
            said += "\n" + _bg().keep_pull_request(graph, state, item, root, branch, github)
    return said + "\n"


def carry_report(where_from, name, message, root=_bg().ROOT):
    """land()'s line for a script's report that no record cites yet (a person's card, whose script next ran for
    them): the report committed and pushed on its own, so the numbers are on the branch with the card. Empty when
    the database is not the one on the Mac (a test or an experiment)."""
    if where_from["how"] not in ("mac", "ssh"):
        return ""
    return _bg().land(root, [pathlib.Path(root) / name], message) + "\n"


def work(attempt):
    """An open attempt as where and the view show it."""
    return {"run_id": attempt["run_id"], "by": attempt["actor"], "action": attempt["action"],
            "since": attempt["started_at"], "note": attempt["note"]}


def unresolved(state, track, node, inputs):
    """Why running the node on these inputs would repeat an attempt nobody has dealt with, or None.

    Only script runs count: work next opened for a person runs no script.
    """
    runs = [run for run in state.attempts(node, inputs) if run["action"] != _bg().AGENT]
    if not runs:
        return None
    last = runs[-1]
    if last["finished_at"] is None:
        return (f"{node}: attempt {last['run_id']}, started at {last['started_at']} on these same inputs, never "
                f"finished: it was interrupted or is still running. Inspect it, then pass --again to run anyway.")
    if state.moved_since(track, node, last):
        return None
    ended = {"needs_lead": "ended needs_lead and its findings are unresolved",
             "error": "ended in an error that is unresolved",
             _bg().TAKEN: "never finished and was taken over with next --take"}.get(last["outcome"],
                                                                              "passed, but record refused it")
    return (f"{node}: attempt {last['run_id']}, started at {last['started_at']}, {ended}: nothing it reads has "
            f"changed, the tool is the same version and the position has not moved since. Report: "
            f"{last['report_path']}. Pass --again to rerun.")


def kept_report(report):
    """The parts of a script run's report the view shows: the first FINDINGS_LIMIT findings and their total, and
    the merge check's pull request, merge commit and differences, or the error."""
    findings = report.get("findings") or []
    kept = {"findings": findings[:_bg().FINDINGS_LIMIT], "findings_total": len(findings)}
    for key in ("pull_request", "merge_commit", "differ", "this_checkout_differs", "error"):
        if report.get(key):
            kept[key] = report[key]
    return kept


def checked(found):
    """The script's result as reported, or ValueError when it is not the documented shape."""
    if (not isinstance(found, dict) or found.get("outcome") not in ("pass", "needs_lead")
            or not isinstance(found.get("findings"), list)
            or not all(isinstance(item, str) for item in found["findings"])
            or not isinstance(found.get("detail"), dict)):
        raise ValueError(f"the script returned {str(found)[:300]}, not an outcome of pass or needs_lead with "
                         f"a findings list and a detail object")
    notes = found["detail"].get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(item, str) and item for item in notes):
        raise ValueError(f"the script's detail.notes is {str(notes)[:300]}, not a list of sentences")
    json.dumps(found["detail"])
    return found


def run_tidy(root):
    """tidy's apply, keeping the checkout next runs in."""
    return _bg().tidy.run(apply=True, keep=(root,))


def codex_handoff(graph, state, node, root, minutes, note):
    """The text next --codex adds to the brief: the checkout, this run's stop, and the card's latest record."""
    root = pathlib.Path(root).resolve()
    stop = graph.stop(node)
    if not stop or not isinstance(minutes, int) or isinstance(minutes, bool) or not 0 < minutes <= stop["minutes"]:
        raise ValueError(f"give --minutes from 1 to {stop.get('minutes', '?')} for this single run")
    branch = _bg().git_text(root, "symbolic-ref", "--short", "-q", "HEAD").strip()
    head = _bg().git_text(root, "rev-parse", "HEAD").strip()
    if not branch or not head:
        raise ValueError("the selected checkout needs a named branch and a commit")
    last = _bg().last_transition(graph, state, node)
    _bg().require_answered_wait(last, node)
    lines = ["\nSingle-card Codex handoff:", f"  Checkout: {root}", f"  Branch: {branch}",
             f"  Commit: {head}", f"  Work remaining: {note.strip()}",
             f"  Run stop: {minutes} minutes or {stop['repeat_failures']} failures of the same command on unchanged "
             "source inputs.",
             "  A stop needs the lead; this signal does not detect every kind of loop."]
    if last:
        lines += [f"  Latest graph record: {last.get('record_name') or '(no record filename)'}",
                  f"  Its outcome: {last['node']} {last['outcome']}", f"  Its note: {last['note']}"]
        lines += [f"  Evidence: {entry['path']} sha256 {entry['sha256']}" for entry in last.get("evidence", [])]
        if not last.get("evidence"):
            lines.append("  Evidence: none recorded")
    else:
        lines.append("  Latest graph record: none")
    lines += ["  Use the existing card instructions and stop after this one card.",
              "  Make exactly one final record for this attempt after its work and any review have ended; "
              "do no work after that record."]
    return {"text": "\n".join(lines) + "\n", "head": head, "repeat_failures": stop["repeat_failures"]}


def run_codex(graph, state, node, root, attempt, text, handoff, minutes, out, err, after, launcher, state_path):
    """Launch once, then settle the same attempt; an exit is never a graph outcome."""
    run_id = attempt["run_id"]
    interrupted = None
    try:
        if _bg().git_text(root, "rev-parse", "HEAD").strip() != handoff["head"]:
            result = {"reason": "the checkout moved after the brief; no worker was launched", "tokens": None}
        else:
            out.write(f"Launching one Codex worker for attempt {run_id}; its wall stop is {minutes} minutes.\n")
            out.flush()
            result = launcher(root, text, minutes, run_id, state_path=state_path, records_path=state.records,
                              graph_path=graph.path, repeat_failures=handoff["repeat_failures"])
    except BaseException as error:
        result = {"reason": f"launcher failed: {type(error).__name__}: {error}", "tokens": None,
                  "termination_unconfirmed": True}
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            interrupted = error
    if result.get("thread_id"):
        out.write(f"Codex thread: {result['thread_id']}\n")
    if result.get("provider_usage") is not None:
        out.write("Codex reported usage for the run: " + json.dumps(result["provider_usage"], sort_keys=True) + "\n")
    provenance = {"provider": "Codex CLI", "model": _bg().board_graph_codex.MODEL, "effort": _bg().board_graph_codex.EFFORT,
                  "thread_id": result.get("thread_id"), "reason": result["reason"],
                  "provider_usage": result.get("provider_usage"), "provider_error": result.get("provider_error"),
                  "executable": result.get("executable"), "cli_version": result.get("cli_version"),
                  "termination_unconfirmed": result.get("termination_unconfirmed", False)}
    try:
        state.keep_launcher(run_id, provenance)
    except Exception as error:
        err.write(f"{node}: provider provenance was not kept on attempt {run_id}: {error}\n")
    item, said = _bg().settle(graph, state, node, "agent:codex-launcher", note=result["reason"],
                        tokens=result.get("tokens"), root=root, attempt_id=run_id, launch_ok=result.get("ok", False),
                        termination_unconfirmed=result.get("termination_unconfirmed", False))
    out.write(said)
    if item:
        out.write(_bg().recorded(graph, state, item))
        out.write(after(item, []))
        err.write(f"{node}: {result['reason']}; the attempt needs the lead.\n")
        if interrupted:
            raise interrupted
        return 1, item, "the worker ended without its own record"
    if not result.get("ok", False):
        err.write(f"{node}: {result['reason']}; the original record stands and the launch checkpoint needs the lead.\n")
        if interrupted:
            raise interrupted
        return 1, None, "the worker stopped after recording (above)"
    out.write(f"{result['reason']}; the worker's recorded outcome stands.\n")
    if interrupted:
        raise interrupted
    return 0, None, None


def origin_command(graph, state, root, state_path=None):
    """The selected graph invocation for the shell on this machine."""
    quote = _bg().shell_quote
    root_path = pathlib.Path(root).resolve()
    local_cli = root_path / "scripts/board_graph.py"
    if _bg().tool_paths.is_external() and not local_cli.is_file():
        command = ("& " if _bg().os.name == "nt" else "") + quote(sys.executable) + " -B " + quote(_bg().PROGRAM) + " --project " + quote(root_path)
    else:
        command = ("& " if _bg().os.name == "nt" else "") + quote(sys.executable) + " -B " + quote(local_cli)
    command += _bg().CHOSEN.argument
    command += " --graph " + quote(graph.path.resolve())
    if state_path is None:
        state_path = getattr(state, "selected_state_path", None)
    if state_path is not None:
        command += " --state " + quote(pathlib.Path(state_path).resolve())
    command += " --records " + quote(pathlib.Path(state.records).resolve())
    return command


def origin_record_command(graph, state, node, root, run_id, state_path):
    """A template for the originating shell, with every selected local path explicit."""
    quote = _bg().shell_quote
    command = _bg().origin_command(graph, state, root, state_path)
    command += (f" record {node} " + quote("<outcome>") + f" --attempt {run_id} --by " + quote("<who>")
                + " --note " + quote("<why>") + " --evidence " + quote("<paths>"))
    return command


def origin_settle_command(graph, state, node, root, run_id, state_path=None, ack=False, unconfirmed=False):
    command = _bg().origin_command(graph, state, root, state_path) + f" settle {node} --attempt {run_id}"
    if ack:
        command += " --ack" + (" --verified-stopped" if unconfirmed else "")
        return command + " --by " + _bg().shell_quote("agent:<lead>") + " --note " + _bg().shell_quote("<revised approach>")
    return command + " --by " + _bg().shell_quote("<launcher>")


def next_step(graph, state, track="board", again=False, root=_bg().ROOT, actions=None, out=None, err=None, upkeep=None,
              tidy_run=None, github=None, after_record=None, by=None, note="", take=False, node=None,
              after_report=None, codex=False, minutes=None, launcher=None, state_path=None, views=None):
    """Start the next card in line on `track`.

    Between two cards (nothing is on the card the track stands at), next starts that card: it opens an attempt
    there, so the card is on. For a script's card connected here the script is the attempt: it runs, what it
    reported is recorded with its report as evidence, and the record carries itself to the branch; the track is
    then between this card and the next, or still on this one when the outcome keeps it here. A person's card, or
    a card whose script is not connected here, is started for `by` and left on: do its task and record. A
    person's card with a connected script (Placement) gets its script run first, as its own attempt, and the
    report is carried to the branch by `after_report` (name, message) and is theirs to cite. A list's card (the
    framework) is named with `node`, since a list has no next card in line. A card with `unchanged_pass` is
    first compared: when its view of the board (`views`, by name) is the one at its last pass, next records the
    pass itself, as script:unchanged, and the card never starts. On a card that is already on, next
    says who is on it and since when; `take` ends theirs as taken and starts it anew; `again` reruns a script
    whose earlier attempt on the same inputs is unresolved. When the position waits on the owner's merge, next
    checks the merge instead (merge_check): a merged pull request records the outcome it leads to. The upkeep
    (`upkeep`, bring_current) brings the checkouts current once before any card starts, or after a merge is
    observed and before it is recorded. An already-active card's status query does not run upkeep.

    Returns 0 when something was recorded, 1 when a run was refused or failed, 2 when the card was started for a
    person or is already on, waits on a merge, or the track is done.
    """
    actions = _bg().ACTIONS if actions is None else actions
    views = _bg().default_views() if views is None else views
    out = out or sys.stdout
    err = err or sys.stderr
    after = after_record or (lambda item, extra: "")  # what carries a record to its branch, once written
    root = pathlib.Path(root)
    github = github or (lambda number: _bg().pull_request_view(number, root))
    tidied = []

    class GraphChanged(Exception):
        pass

    def once(*args):
        """The upkeep, the first time it is asked for; after that the checkouts are as it left them."""
        if tidied:
            return True
        before = graph.text
        if not (upkeep or _bg().bring_current)(*args):
            return False
        tidied.append(True)
        if graph.text != before:
            selected = args[3]
            if selected not in graph.nodes or graph.nodes[selected]["track"] != track:
                raise GraphChanged(selected)
            raise GraphChanged
        return True

    while True:
        if track not in graph.tracks:
            err.write(f"unknown track {track}; the tracks are {', '.join(graph.tracks)}\n")
            return 1
        label, cards = graph.tracks[track]["label"], ", ".join(graph.track_nodes(track))
        if not graph.tracks[track]["ordered"]:
            if not node:
                err.write(f"{label} is a list, not a row: name the card to start with --node, one of {cards}\n")
                return 1
            if node not in graph.track_nodes(track):
                err.write(f"{node} is not a card of {label}; its cards are {cards}\n")
                return 1
        elif node is not None and node != state.position(graph, track):
            err.write(f"{label} stands at {state.position(graph, track)}, the card next starts; --node names a list's "
                      f"card\n")
            return 1
        try:
            code, item, why = _bg().run_node(graph, state, track, again, root, actions, out, err, once, tidy_run, github, after,
                                       by=by, note=note, take=take,
                                       card=node if not graph.tracks[track]["ordered"] else None,
                                       after_report=after_report or (lambda name, message: ""), codex=codex,
                                       minutes=minutes, launcher=launcher, state_path=state_path, views=views)
            break
        except ValueError as error:
            err.write(str(error) + "\n")
            return 1
        except GraphChanged as change:
            if change.args:
                err.write(f"{change.args[0]}: the refreshed graph removed this card from {track}; next runs nothing\n")
                out.write("next stops: the selected card is absent from the refreshed graph.\n")
                return 1
            continue  # Select and validate again using the refreshed graph; upkeep itself runs only once.
    if code == 0:
        out.write(_bg().render_track(graph, state, track))  # where the marker is now: between two cards, or on one
    elif code == 1 and why:
        out.write(f"next stops: {why}.\n")
    return code


def _start_person_card(graph, state, node, spec, action, actions, root, out, err, upkeep, tidy_run,
                       after, after_report, by, note, take, codex, minutes, launcher, state_path, views):
    """Open a person's card or non-auto action card for `by` and leave it open (exit code 2 on start)."""
    if not by or not by.strip():
        err.write(f"{spec['label']} is a person's card ({spec['who']}): say who starts it, --by owner or "
                  f"--by agent:<name>\n")
        return 1, None, f"{spec['label']} needs --by"
    if not upkeep(graph, state, root, node, f"starting it for {by.strip()}", out, err, tidy_run):
        return 1, None, "the checkouts are not current"
    handoff = None
    if codex:
        try:
            handoff = _bg().codex_handoff(graph, state, node, root, minutes, note)
        except ValueError as refusal:
            err.write(f"{node}: {refusal}\n")
            return 1, None, "the worker's source or bound is unresolved (above)"
    inputs = {owned: _bg().hash_path(root / owned) for owned in spec["owns"]}
    try:
        inputs.update(_bg().approval_inputs(graph, state, node, root, views))
    except ValueError as error:
        return 1, None, str(error)
    if action == "routing_run":
        inputs["routing_terms"] = _bg().routing_terms(graph, node, root)
    at = _bg().now()
    run_id = uuid.uuid4().hex
    record_command = _bg().origin_record_command(graph, state, node, root, run_id, state_path)
    settle_command = _bg().origin_settle_command(graph, state, node, root, run_id, state_path)
    text = _bg().brief(graph, state, node, root, starting={"by": by.strip(), "note": note.strip(), "at": at,
                                                    "run_id": run_id, "record_command": record_command,
                                                    "settle_command": settle_command})
    if codex:
        text += handoff["text"]
        text += ("  Run the Close it command above from the originating checkout. "
                 + ("It selects the usual Mac state by omitting --state. " if isinstance(state, _bg().Remote) else "")
                 + "Do not assume these selections or the attempt ID cross SSH; if the originating checkout "
                   "cannot be used, stop for the lead.\n")
    try:
        extra = {"run_id": run_id, "launch": True} if codex else {"run_id": run_id}
        attempt, already, taken = state.open_work(graph, node, by.strip(), note.strip(), inputs,
                                                  str(pathlib.Path(root).resolve()), take, at=at, brief=text, **extra)
    except ValueError as refusal:
        err.write(f"{refusal}\n")
        return 1, None, f"{spec['label']} was not started (above)"
    for other in taken:
        out.write(f"took {spec['label']} over from {other['actor']} (since {_bg().stamp(other['started_at'])}); theirs ends as taken\n")
    out.write(text)  # the brief, kept on the attempt open_work opened: what the one on it was given
    out.write(f"{spec['label']} is on: {_bg().held(_bg().work(attempt))}" + (" (already; nothing new started)" if already else "") + "\n")
    if codex and not already:
        return _bg().run_codex(graph, state, node, root, attempt, text, handoff, minutes, out, err, after,
                         launcher or _bg().board_graph_codex.run, state_path)
    if action in actions and not already:
        # A person's card with a connected script: next runs it for them, as its own attempt, so the numbers
        # are on the card as they take it up, and its report is theirs to cite. Findings never keep the card
        # from starting; the upkeep above made the checkout current.
        report, name = _bg().run_script(state, spec, node, action, *actions[action], root, None, graph=graph)
        said = _bg().findings_note(report["findings"], _bg().report_notes(report)) if report["outcome"] != "error" else "error"
        if report["outcome"] == "error":
            out.write(f"Its script {action} failed: {report['detail']['error']}. Report: {name}\n")
        else:
            out.write(f"Its script {action} reports {report['outcome']}: {said}\nReport: {name}\n")
            if report["outcome"] == "needs_lead":
                out.write("These are starting baseline findings to resolve during this card's work (not a broken tool); do the task, recheck, and record pass once resolved.\n")
        out.write(after_report(name, f"Report: {spec['label']}'s script for {by.strip()}: {said}"))
        out.write("Do its task and record the outcome with record, citing the report; that record closes it.\n")
    elif action in _bg().COMMANDS:
        out.write("Use the tool command in the brief. It records the outcome and closes this attempt.\n")
    elif action is None or spec["who"] != "auto":
        out.write("Do its task and record the outcome with record; that record closes it.\n")
    else:
        reason = ("needs KiCad or the Mac" if action in _bg().LATER
                  else f"has no function in {_bg().SCRIPTS_IN}the project's {_bg().PROJECT_ACTIONS}")
        out.write(f"Its script {action} {reason} and is not connected to next: run it where it runs and record "
                  f"the outcome with record; that record closes it.\n")
    return 2, None, None


def run_node(graph, state, track, again, root, actions, out, err, upkeep, tidy_run, github,
             after=lambda item, extra: "", by=None, note="", take=False, card=None,
             after_report=lambda name, message: "", codex=False, minutes=None, launcher=None, state_path=None,
             views=None):
    """One next: (exit code, the transition recorded or None, why it stopped in a few words or None). `card` is
    a list's card, which next_step checked; on a row the card is the track's position.

    Prints what happened: the card started and for whom, who is already on it, what was recorded with the
    findings and the report, or why nothing ran.
    """
    node = card or state.position(graph, track)
    if node == _bg().DONE:
        rows = state.transitions(track)
        ended = "dropped" if rows and rows[-1]["outcome"] == _bg().DROPPED else "done"
        out.write(f"{graph.tracks[track]['label']}: {ended}; there is no next card\n")
        return 2, None, None
    if node not in graph.nodes or graph.nodes[node]["track"] != track:
        err.write(f"{node}: the graph has no such card on {track}; next runs nothing\n")
        return 1, None, "the track's position is absent from the graph"
    blockers = state.launch_blocks(graph, track, node)
    if blockers:
        blocked = blockers[0]
        out.write(_bg().launch_lines([blocked], graph, state, root)[0].strip() + "\n")
        return 2, None, None
    question = state.merge_question(track, card)
    if question:
        if codex:
            return 1, None, "this card waits on the owner's merge; run next without --codex to check it"
        return _bg().merge_check(graph, state, track, question, root, out, err, upkeep, tidy_run, github, after)
    spec = graph.nodes[node]
    action = spec["action"]
    if codex and launcher is None:
        return 1, None, "Codex dispatch has no configured launcher"
    if codex and (spec["who"] != "agent" or (action is not None and action not in _bg().COMMANDS)):
        return 1, None, "--codex is for an agent card without an action"
    if codex and (take or again):
        return 1, None, "--codex cannot take over or repeat an active attempt"
    if codex and (not by or not by.startswith("agent:") or len(by) <= len("agent:") or not note.strip()):
        return 1, None, "--codex needs --by agent:<name> and a note describing the bounded work"
    on = [_bg().work(attempt) for attempt in state.attempts(node) if attempt["finished_at"] is None]
    connected = spec["who"] == "auto" and action is not None and action in actions
    owed = _bg().owed_return(graph, state, node) if spec.get("unchanged_pass") and not on else None
    if owed:
        sender = graph.nodes[owed["node"]]["label"] if owed["node"] in graph.nodes else owed["node"]
        out.write(f"{spec['label']} does not pass by itself: {sender} returned the work to it at "
                  f"{_bg().stamp(owed['at'])}, so it starts as usual\n")
    if (spec.get("unchanged_pass") and not on and not take and not again and not codex and not owed
            and any(row["outcome"] == "pass" for row in state.transitions(node=node))):
        # The card's view of the board against the one at its last pass: unchanged, the pass stands and the card
        # never starts. A card that never passed has nothing to
        # compare with and starts as usual, and so does a card a returned record sent work back to.
        if not upkeep(graph, state, root, node, "before comparing its view", out, err, tidy_run):
            return 1, None, "the checkouts are not current"
        try:
            item = _bg().unchanged_pass(graph, state, node, root, views or _bg().default_views())
        except ValueError as refusal:
            err.write(f"{node}: its view is unchanged, and record refused the pass: {refusal}\n")
            return 1, None, f"record refused {spec['label']}'s unchanged pass (above)"
        if item is not None:
            out.write(f"{spec['label']} passes by itself: {item['note']}\n")
            out.write(_bg().recorded(graph, state, item))
            out.write(after(item, []))
            return 0, item, None
    if on and not take:
        out.write(_bg().render_track(graph, state, track))
        out.write(f"{spec['label']} is on: " + "; ".join(_bg().held(item) for item in on)
                  + (". Its next record closes it; --take starts it anew for someone else.\n" if not connected
                     else ". A run that never finished: inspect and settle its exact attempt before --again.\n"))
        return 2, None, None
    if not connected:
        return _start_person_card(
            graph, state, node, spec, action, actions, root, out, err, upkeep, tidy_run,
            after, after_report, by, note, take, codex, minutes, launcher, state_path, views,
        )
    if not upkeep(graph, state, root, node, "before its script", out, err, tidy_run):
        return 1, None, "the checkouts are not current"
    try:
        report, name = _bg().run_script(state, spec, node, action, *actions[action], root, None if again else track,
                                  graph=graph)
    except ValueError as refusal:  # an earlier attempt on these same inputs is unresolved
        err.write(f"{refusal}\n")
        return 1, None, f"{spec['label']} has an earlier attempt on the same inputs that is unresolved (above)"
    if report["outcome"] == "error":
        err.write(f"{node}: script {action} failed, so nothing is recorded on the graph; attempt "
                  f"{report['run_id']} ends in error. Report: {name}\n{report['detail']['error']}\n")
        return 1, None, f"{spec['label']}'s script {action} failed (above)"
    findings, notes = report["findings"], _bg().report_notes(report)
    try:
        item = _bg().record(graph, state, node, report["outcome"], f"script:{action}", _bg().findings_note(findings, notes),
                      [name], root=root, checked_scope=report.get("checked_scope"))
    except ValueError as error:
        err.write(f"{node}: script {action} reported {report['outcome']}, and record refused it: {error}. "
                  f"Report: {name}\n")
        return 1, None, f"record refused what {spec['label']}'s script reported (above)"
    out.write(_bg().recorded(graph, state, item))
    out.write(after(item, [name]))
    out.write("Findings:\n" + "".join(f"  {finding}\n" for finding in findings) if findings else "No findings.\n")
    out.write("".join(f"Note: {said}\n" for said in notes))
    out.write(f"Report: {name}\n\n")
    return 0, item, None


def findings_note(findings, notes=()):
    """A script's findings in one line: their count and each one, or "no findings"; then what it said that is not
    a finding (its report's detail["notes"]), so the record says it too."""
    said = (f"{len(findings)} finding{'s' if len(findings) != 1 else ''}: " + "; ".join(findings)
            if findings else "no findings")
    return "; ".join([said, *notes])


def report_notes(report):
    """What a script's report says plainly that is not a finding: detail["notes"], or none."""
    return list((report.get("detail") or {}).get("notes") or [])


def run_script(state, spec, node, action, run, reads, root, track, graph=None):
    """Run a card's script as one attempt and write its report: (the report, its path relative to `root`).

    The attempt opens under the write lock with the hashes of the card's owned files, the files the script
    reads and the tool's manifest, so a next in another session finds this run open, not a free card; with
    `track`, the card's track, start_attempt first refuses an earlier attempt on these same inputs that nobody
    has dealt with (ValueError, before anything runs). The manifest names the tool's version and every tool
    file's hash, so after an update of the tool the script that decides the card is another script, the inputs
    differ, and the card's script runs again without --again. The
    script's result in the documented shape, or the error it raised, is the report under REPORTS, kept on the
    attempt with its hash.
    """
    root_path = pathlib.Path(root)
    inputs = {}
    for name in sorted(set(spec["owns"]) | set(reads)):
        target = root_path / name
        if not target.exists() and name not in spec["owns"]:
            shared = _bg().tool_paths.shared_tool_file(name)
            if shared is not None:
                target = shared
        inputs[name] = _bg().hash_path(target)
    inputs[_bg().board_graph_tool.MANIFEST] = _bg().tool_paths.tool_fingerprint(root_path)
    if graph is not None:
        inputs.update(_bg().approval_inputs(graph, state, node, root))
    started = datetime.now(timezone.utc)
    report = {"node": node, "action": action, "started_at": started.isoformat(), "inputs": inputs}
    if graph and graph.data.get("approval_mode", "off") != "off" and spec["who"] == "auto" and _bg().board_approvals.tracked(spec):
        report["checked_scope"] = _bg().approval_review(graph, state, root).scope(node)
    report["run_id"] = state.start_attempt(node, action, inputs, report["started_at"],
                                           checkout=str(pathlib.Path(root).resolve()), track=track, graph=graph)
    try:
        found = _bg().checked(_bg().graph_pack.run_action(run, root, _bg().CHOSEN.name))
        report.update(outcome=found["outcome"], findings=list(found["findings"]), detail=found["detail"])
    except Exception as error:
        text = f"{type(error).__name__}: {error}"
        report.update(outcome="error", findings=[text],
                      detail={"error": text, "traceback": traceback.format_exc().splitlines()})
    report["finished_at"] = _bg().now()
    name = _bg().report_path(node, report["action"])
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    state.finish_attempt(report["run_id"], report["finished_at"], report["outcome"], name, _bg().hash_path(path),
                         _bg().kept_report(report))
    return report, name


def working(state, node, rows):
    """Who is working `node` (its open attempts, oldest first) and, when nobody is, since when it has waited: the
    later of its track's last transition (`rows`) and the last attempt that finished there; None with neither."""
    attempts = state.attempts(node)
    held = [_bg().work(attempt) for attempt in attempts if attempt["finished_at"] is None]
    if held:
        return held, None
    times = [rows[-1]["at"]] if rows else []
    times += [attempt["finished_at"] for attempt in attempts]
    return [], max(times, key=_bg().parse_time) if times else None


def with_tokens(state, rows):
    """Each transition with the tokens kept on the attempt it closed, when known (closing stamps the attempt with the
    record's time); None otherwise."""
    out = []
    for row in rows:
        row = dict(row)
        row["tokens"] = next((attempt["tokens"] for attempt in state.attempts(row["node"])
                              if attempt["finished_at"] == row["at"] and attempt["tokens"] is not None), None)
        out.append(row)
    return out


def where(graph, state):
    """Everything the page or an agent needs to know about the current position, as data."""
    view = {"milestone": graph.data["milestone"], "tracks": []}
    if _bg().board_autonomy.summary(graph): view["autonomy"] = _bg().board_autonomy.summary(graph)
    review = None
    if graph.data.get("approval_mode", "off") != "off":
        review = _bg().approval_review(graph, state)
        view["approvals"] = {"mode": graph.data["approval_mode"], "cards": review.report()}
    for track in graph.data["tracks"]:
        rows = state.transitions(track["id"])
        item = {"id": track["id"], "label": track["label"], "ordered": track["ordered"],
                "transitions": _bg().with_tokens(state, rows[-5:])}
        item["launches"] = (state.launch_blocks(graph, track["id"]) if track["ordered"] else
                            [attempt for key in graph.track_nodes(track["id"])
                             for attempt in state.launch_blocks(graph, track["id"], key)])
        for attempt in item["launches"]:
            unconfirmed = (attempt.get("report") or {}).get("termination_unconfirmed", False)
            attempt["settle_command"] = _bg().origin_settle_command(graph, state, attempt["node"], graph.root, attempt["run_id"])
            attempt["ack_command"] = _bg().origin_settle_command(graph, state, attempt["node"], graph.root, attempt["run_id"],
                                                            ack=True, unconfirmed=unconfirmed)
        if track["ordered"]:
            position = state.position(graph, track["id"])
            item["position"] = position
            node = graph.nodes.get(position)  # None at DONE, and at a node only another checkout's graph has
            if track["id"] == graph.staged:
                stage = node["stage"] if node else None
                item["stages"] = [{"id": s["id"], "label": s["label"], "current": s["id"] == stage,
                                   "nodes": graph.stage_nodes(s["id"])} for s in graph.data["stages"]]
            if position == _bg().DONE and rows and rows[-1]["outcome"] == _bg().DROPPED:
                last = rows[-1]  # the owner ended the track early: where, when and why
                item["dropped"] = {"at": last["node"], "since": last["at"], "note": last["note"],
                                   "label": graph.nodes[last["node"]]["label"] if last["node"] in graph.nodes
                                   else last["node"]}
            if position != _bg().DONE:
                # The record that moved the track here (None at the track's start): the card the marker sits after,
                # and how it got here. Between that card and this one until something is on this card, or returned
                # from it when a finding sent the work back.
                arrived = next((row for row in reversed(rows) if row["target"] == position and row["node"] != position),
                               None)
                moved = {"since": rows[-1]["at"] if rows else None, "moved_by": rows[-1]["actor"] if rows else None,
                         "moved_because": rows[-1]["note"] if rows else None,
                         "after": arrived["node"] if arrived else None, "how": arrived["outcome"] if arrived else None}
                moved["after_label"] = (graph.nodes[moved["after"]]["label"] if moved["after"] in graph.nodes
                                        else moved["after"])
                # A record that kept the track on this card and asks someone for something: the card waits on them
                # until their record (the owner's decision, the lead's help, a skill), said on the card's face.
                last = rows[-1] if rows else None
                moved["waiting_on"] = ({"outcome": last["outcome"], "who": _bg().WAITS_ON[last["outcome"]],
                                        "by": last["actor"], "since": last["at"], "note": last["note"]}
                                       if last and last["node"] == position and last["outcome"] in _bg().WAITS_ON else None)
                if node:
                    item["node"] = {**_bg().card_view(graph, state, position, review), **moved}
                else:
                    # One database for every checkout: a branch's graph can add a node, and a record there moves
                    # the track to it. A checkout whose graph lacks that node reads the same database, so it says
                    # where the track stands instead of stopping with an error.
                    item["node"] = {"id": position, "label": position, "who": None, "stage": None, "unknown": True,
                                    "task": None, "pass_when": None, "owns": [], "action": None, "outcomes": {},
                                    "requires": [], "blocked_by": [], **moved}
                question = state.merge_question(track["id"])
                item["node"]["waits_on"] = ({"pull_request": question["number"], "then": question["then"]}
                                            if question else None)
                item["node"]["working"], item["node"]["waiting_since"] = _bg().working(state, position, rows)
                item["node"]["on"] = bool(item["node"]["working"])
        else:
            item["nodes"] = []
            for key in graph.track_nodes(track["id"]):
                last = state.transitions(node=key)
                item["nodes"].append({"id": key, "label": graph.nodes[key]["label"],
                                      "last_outcome": last[-1]["outcome"] if last else None,
                                      "at": last[-1]["at"] if last else None,
                                      "working": _bg().working(state, key, last)[0]})
        view["tracks"].append(item)
    return view


def stop_shape(value):
    """Whether `value` gives the wall and repeated-failure stops as whole numbers."""
    return (isinstance(value, dict) and all(isinstance(value.get(name), int) and value[name] > 0
                                            for name in ("minutes", "repeat_failures")))


def card_view(graph, state, key, review=None):
    """One card as data, the way where shows the position's: its task, what passing needs, its outcomes, files,
    script, what it requires and the issues that block it, the merge it waits on, who is on it and since when
    nobody has been. Any card, at its track's position or not."""
    node = graph.nodes[key]
    track = graph.tracks[node["track"]]
    rows = state.transitions(track["id"]) if track["ordered"] else state.transitions(node=key)
    item = {"id": key, "label": node["label"], "who": node["who"], "stage": node.get("stage"),
            "task": node["task"], "pass_when": node["pass_when"], "owns": node["owns"],
            "action": node["action"], "outcomes": graph.allowed(key),
            "requires": [{"node": other, "passed": state.passed(other)} for other in node.get("requires", [])],
            "blocked_by": graph.blocking_issues(key), "unchanged_pass": node.get("unchanged_pass")}
    if graph.data.get("approval_mode", "off") != "off":
        review = review or _bg().approval_review(graph, state)
        item["approval_mode"] = graph.data["approval_mode"]
        item["approval_blockers"] = review.blockers(key)
        item["review_returns"] = [other for other in _bg().board_approvals.required(graph, key)
                                  if other in node["outcomes"]["returned"] and review.verdict(other)["status"] != "current"]
        previous = next((r for r in reversed(rows) if r["node"] == key), None)
        if previous and previous["outcome"] in ("needs_lead", "needs_owner", "needs_skill", "more_work"):
            item["retained_evidence"] = previous["evidence"]
        if graph.data["approval_mode"] == "enforce":
            for dep in item["requires"]:
                if _bg().board_approvals.tracked(graph.nodes[dep["node"]]):
                    dep["passed"] = review.verdict(dep["node"])["status"] == "current"
    if node.get("pass_check"):
        item["pass_check"] = node["pass_check"]
    owed = _bg().owed_return(graph, state, key) if node.get("unchanged_pass") else None
    item["owed_return"] = ({"from": owed["node"], "from_label": graph.nodes[owed["node"]]["label"]
                            if owed["node"] in graph.nodes else owed["node"], "at": owed["at"], "by": owed["actor"]}
                           if owed else None)
    if track["ordered"]:  # the merge question is the position's; another card of the row has none
        question = state.merge_question(track["id"]) if state.position(graph, track["id"]) == key else None
    else:
        question = state.merge_question(track["id"], key)
    item["waits_on"] = {"pull_request": question["number"], "then": question["then"]} if question else None
    item["working"], item["waiting_since"] = _bg().working(state, key, rows)
    item["on"] = bool(item["working"])
    return item


def held(work_item, at=None):
    """One open attempt in words: working since when, by whom, how long ago, and its note."""
    return (f"working since {_bg().stamp(work_item['since'])} by {work_item['by']} ({_bg().ago(work_item['since'], at)})"
            + (f": {work_item['note']}" if work_item["note"] else ""))


def launch_lines(attempts, graph=None, state=None, root=_bg().ROOT):
    lines = []
    for attempt in attempts:
        run_id, node = attempt["run_id"], attempt["node"]
        if attempt["launch_state"] == "active":
            command = attempt.get("settle_command") or (_bg().origin_settle_command(graph, state, node, root, run_id)
                      if graph is not None else f"{_bg().COMMAND} settle {node} --attempt {run_id} --by agent:<launcher>")
            lines.append(f"  Launch {run_id} on {node} still owns the work, even if it recorded. "
                         f"After the process has ended: {command}")
        else:
            unconfirmed = (attempt.get("report") or {}).get("termination_unconfirmed", False)
            warning = (" Verify that the worker and all descendants have stopped first." if unconfirmed else "")
            verification = " --verified-stopped" if unconfirmed else ""
            command = attempt.get("ack_command") or (_bg().origin_settle_command(graph, state, node, root, run_id, ack=True,
                      unconfirmed=unconfirmed) if graph is not None else
                      f"{_bg().COMMAND} settle {node} --attempt {run_id} --ack{verification} "
                      "--by agent:<lead> --note \"<revised approach>\"")
            lines.append(f"  Launch {run_id} on {node} needs the lead: {attempt['launch_note']}. "
                         f"{warning} Acknowledge: {command}")
    return lines


def node_lines(node):
    """The lines of `where` that describe the node itself: its task, outcomes, script and files."""
    out = [f"  Task: {node['task']}", f"  Pass when: {node['pass_when']}"]
    moves = [f"pass -> {node['outcomes']['pass'][0]}"]
    if "returned" in node["outcomes"]:
        moves.append("returned -> " + " or ".join(node["outcomes"]["returned"]))
    if _bg().DROPPED in node["outcomes"]:
        moves.append("dropped ends the track, by the owner")
    stays = [key for key, targets in node["outcomes"].items() if key not in ("pass", "returned", _bg().DROPPED)]
    out.append("  Outcomes: " + "; ".join(moves) + f"; {', '.join(stays)} stay here")
    if node["requires"]:
        out.append("  Requires: " + ", ".join(f"{r['node']} ({'passed' if r['passed'] else 'not passed'})"
                                              for r in node["requires"]))
    if node.get("approval_blockers"):
        out.append("  Current approval checks: " + "; ".join(node["approval_blockers"]))
    if node.get("review_returns"):
        out.append("  Available return for review: " + ", ".join(node["review_returns"]))
    if node.get("retained_evidence"):
        out.append("  Evidence from the last attempt: " + ", ".join(e["path"] for e in node["retained_evidence"]))
    if node["blocked_by"]:
        out.append("  Blocked by open issues: " + ", ".join(node["blocked_by"]))
    if node.get("waits_on"):
        out.append(f"  Waits on: the owner merging pull request {node['waits_on']['pull_request']}; then next "
                   f"checks main's files and records {node['waits_on']['then']}")
    elif node["action"] in _bg().ACTIONS and node["who"] == "auto":
        out.append(f"  Script: {node['action']} (next runs it)")
    elif node["action"] in _bg().ACTIONS:
        out.append(f"  Script: {node['action']} (next runs it as it starts the card, then leaves the card to them)")
    elif node["action"] in _bg().COMMANDS:
        out.append(f"  Tool: {_bg().COMMANDS[node['action']]}")
        if node["action"] == "routing_run":
            out.append("  Timing: default 6 minutes to hand off, 60 seconds to finish the gesture, "
                       "and up to 6 minutes per independent review. --minutes sets both windows.")
    elif node["action"] in _bg().LATER:
        out.append(f"  Script: {node['action']} (needs KiCad or the Mac; not connected to next yet)")
    elif node["action"]:
        out.append(f"  Script: {node['action']} (no script of that name in the tool or in the project's "
                   f"{_bg().PROJECT_ACTIONS}; not connected to next)")
    if node.get("pass_check"):
        out.append(f"  Completion check: {node['pass_check']} runs when recording pass.")
    if node.get("unchanged_pass") and node.get("owed_return"):
        owed = node["owed_return"]
        out.append(f"  Does not pass by itself now: {owed['from_label']} returned the work to it at "
                   f"{_bg().stamp(owed['at'])} ({owed['by']}), so it starts as usual until it records a pass of its own")
    elif node.get("unchanged_pass") and node.get("approval_mode") == "enforce":
        out.append("  Automatic reuse needs a current original review and the same saved view.")
    elif node.get("unchanged_pass"):
        out.append(f"  Passes by itself when {_bg().VIEWS[node['unchanged_pass']][1]} is unchanged since its last pass "
                   "(next compares before starting it)")
    if node["owns"]:
        out.append("  Owns: " + ", ".join(node["owns"]))
    return out


def card_lines(track, node, at=None, autonomy=None):
    """The lines of where for an ordered track's position: the head (on, between or before the card, its stage,
    whom it waits on), how the track got here, who is on it or since when nobody is, then node_lines."""
    out = []
    if node.get("unknown"):
        head = f"{track['label']}: at {node['id']}, a node this checkout's graph does not have"
    else:
        if node["working"]:
            head = f"{track['label']}: on {node['label']} ({node['who']})"
        elif node.get("how") == "returned":
            head = f"{track['label']}: returned from {node['after_label']} to {node['label']} ({node['who']})"
        elif node.get("after"):
            head = f"{track['label']}: between {node['after_label']} and {node['label']} ({node['who']})"
        else:
            head = f"{track['label']}: before {node['label']} ({node['who']}), the track's start"
        if node["stage"]:
            head += f", stage {next(s['label'] for s in track['stages'] if s['id'] == node['stage'])}"
        if node.get("waiting_on"):
            head += f", waiting on {node['waiting_on']['who']}"
    out.append(head)
    if node.get("waiting_on"):
        waits = node["waiting_on"]
        out.append(f"  {waits['outcome']} {waits['since']} by {waits['by']}: {waits['note']}")
    elif node["since"]:
        out.append(f"  moved here {node['since']} by {node['moved_by']}: {node['moved_because']}")
    else:
        out.append("  no transition recorded yet")
    out.extend(f"  on it: {_bg().held(item, at)}" for item in node["working"])
    out.extend(f"  attempt ID: {item['run_id']}" for item in node["working"] if item.get("run_id"))
    if node["waiting_since"]:
        moves = ("a launch still owns the track" if track.get("launches") else
                 _bg().board_autonomy.wait_guidance({"autonomy": autonomy}, node["waiting_on"])
                 if node.get("waiting_on") else "next starts it")
        out.append(f"  nothing on it since {_bg().stamp(node['waiting_since'])} ({_bg().ago(node['waiting_since'], at)}): "
                   f"{moves}")
    elif not node["working"]:
        out.append("  nothing on it, and nothing recorded here yet: next starts it")
    if node.get("unknown"):
        out.append("  The record that moved the track here came from a checkout whose graph has this node (a "
                   "branch, or a newer main). Bring this checkout level, or read where there.")
    else:
        out.extend(_bg().node_lines(node))
    out.extend(_bg().launch_lines(track.get("launches", [])))
    return out


def render(view, at=None):
    """`where` as text; `at` is now, for the ages (tests pass one)."""
    out = _bg().board_autonomy.render_header(view, _bg().database_line(view["database"]) if view.get("database") else None)
    for track in view["tracks"]:
        if track.get("stages"):
            row = " > ".join(f"[{s['label']}]" if s["current"] else s["label"] for s in track["stages"])
            out.append(row)
        if track["ordered"] and track["position"] == _bg().DONE:
            dropped = track.get("dropped")
            out.append(f"{track['label']}: dropped at {dropped['label']}: {dropped['note']}" if dropped
                       else f"{track['label']}: done")
            out.append("")
            continue
        if not track["ordered"]:
            out.append(f"{track['label']}:")
            for node in track["nodes"]:
                state = f"{node['last_outcome']} at {node['at']}" if node["last_outcome"] else "no outcome recorded"
                out.append(f"  {node['label']}: {state}")
                out.extend(f"    {_bg().held(item, at)}" for item in node["working"])
            out.extend(_bg().launch_lines(track.get("launches", [])))
            out.append("")
            continue
        node = track["node"]
        out.extend(_bg().card_lines(track, node, at, autonomy=view.get("autonomy")))
        if track["transitions"]:
            out.append("  Last transitions:")
            for row in track["transitions"]:
                out.append(f"    {row['at']} {row['node']} {row['outcome']} -> {row['target']} ({row['actor']}): {row['note']}"
                           + (f" [{row['tokens']:,} tokens]" if row.get("tokens") is not None else ""))
        out.append("")
    if view.get("approvals"):
        out.append(f"Revision approvals ({view['approvals']['mode']}):")
        out.extend(_bg().board_approvals.lines(view["approvals"]["cards"]))
    if view.get("workspace"):
        out.extend(_bg().render_workspace(view["workspace"]))
    return "\n".join(out).rstrip() + "\n"


def _brief_docs_and_history_lines(graph, state, node, key, root):
    """Build issue, docs, owned files, newest reports, and last-on-it lines for brief()."""
    out = []
    issues = [issue for issue in graph.data["open_issues"] if key in issue["close_before"]]
    if issues:
        out.append("  Open issues to close before it passes, in full:")
        for issue in issues:
            nets = f" (nets {', '.join(issue['nets'])})" if issue.get("nets") else ""
            closer = (f" [closed by the work of {graph.nodes[issue['closed_by']]['label']}]"
                      if issue.get("closed_by") else "")
            out.append(f"    {issue['id']}{nets}{closer}: {issue['issue']}")
    docs = graph.docs(key)
    if docs:
        out.append("  Read whole, in this order, before anything else (AGENTS.md is loaded already):")
        for doc in docs:
            path = root / doc
            label = doc
            if not path.is_file() and doc not in _bg().LOADED:
                shared = _bg().tool_paths.shared_tool_file(doc)
                if shared is not None:
                    path = shared
                    label = str(shared)
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                out.append(f"    {label} ({len(text)} characters, {len(text.splitlines())} lines)")
            else:
                out.append(f"    {doc} (missing here)")
    else:
        out.append("  Docs: none named; a script's card reads its report")
    if node["owns"]:
        out.append("  Its files go " + ("on a branch and a pull request (board files; the owner merges)" if _bg().on_a_branch(node)
                                       else "straight to main, in small commits") + ": " + ", ".join(node["owns"]))
    folder = root / _bg().REPORTS / key
    reports = sorted(path for path in folder.iterdir() if path.is_file()) if folder.is_dir() else []
    if reports:
        out.append(f"  Newest reports, {_bg().REPORTS}/{key}/: "
                   + ", ".join(f"{path.name} ({path.stat().st_size} bytes)" for path in reports[:12])
                   + (f", and {len(reports) - 12} more" if len(reports) > 12 else ""))
    else:
        out.append(f"  Newest reports: none under {_bg().REPORTS}/{key}/")
    finished = [attempt for attempt in state.attempts(key) if attempt["finished_at"]]
    if finished:
        last = finished[-1]
        out.append(f"  Last on it: {last['actor']} from {_bg().stamp(last['started_at'])} to {_bg().stamp(last['finished_at'])}, "
                   f"{last['outcome'] or 'no outcome'}" + (f": {last['note']}" if last["note"] else ""))
        if last.get("report") and last["report"].get("provider"):
            out.append("  Last launch: " + json.dumps(last["report"], sort_keys=True))
    else:
        out.append("  Last on it: nobody yet")
    return out


def brief(graph, state, key, root=_bg().ROOT, at=None, starting=None):
    """A card's brief: the predetermined context whoever starts the card is given, as text. The card as
    where shows it, the open issues that name it in full, the docs to read whole in order with their sizes, the
    files it owns and where they go, who was on it last and its newest reports, CI on this checkout's commit,
    its stop, the command that starts it (or that it is on), and the command that closes it. `starting`, the
    attempt next is about to open ({"by", "note", "at"}), is shown as on it, so the text next keeps on that
    attempt reads as the card is once started."""
    root = pathlib.Path(root)
    node = graph.nodes[key]
    track = graph.tracks[node["track"]]
    view = next(item for item in _bg().where(graph, state)["tracks"] if item["id"] == node["track"])
    at_position = track["ordered"] and view["position"] == key
    card = view["node"] if at_position else _bg().card_view(graph, state, key)
    if starting:
        card = dict(card, working=card["working"] + [{"run_id": starting.get("run_id"), "by": starting["by"], "action": _bg().AGENT,
                                                      "since": starting["at"], "note": starting["note"]}],
                    waiting_since=None, on=True)
    out = _bg().board_autonomy.brief_header(graph, key, node["label"], node["who"], track["label"])
    if at_position:
        out += _bg().card_lines(view, card, at, autonomy=_bg().board_autonomy.summary(graph))
        rows = view["transitions"]
    else:
        if track["ordered"]:
            out.append(f"  not where {track['label']} stands ({view['position']}): a preview, until the track reaches it")
            rows = view["transitions"]
        else:
            entry = next(item for item in view["nodes"] if item["id"] == key)
            out.append(f"  on {track['label']}, a list: "
                       + (f"{entry['last_outcome']} at {entry['at']}" if entry["last_outcome"] else "no outcome recorded yet"))
            rows = state.transitions(node=key)[-5:]
        out.extend(f"  on it: {_bg().held(item, at)}" for item in card["working"])
        out.extend(_bg().node_lines(card))
        out.extend(_bg().launch_lines(state.launch_blocks(graph, node["track"], key), graph, state, root))
    ack = _bg().acknowledged_approach(graph, state, key)
    if ack:
        out.append(f"  Lead's acknowledged approach ({ack['by']}): {ack['note']}")
    out.extend(_brief_docs_and_history_lines(graph, state, node, key, root))
    out.extend("  " + line for line in _bg().ci_lines(_bg().ci_section(state, _bg().head_here(root))))
    gate = _bg().PACK.gates.get(node.get("action"))
    if gate and gate.warning:
        out.append(f"  {gate.warning}")
    found, way_out = _bg().gate_findings(graph, key, root)
    if found:
        out.append("  Not through this card's tool, so pass and more_work are refused:")
        out.extend(f"    {line}" for line in found[:12])
        if len(found) > 12:
            out.append(f"    and {len(found) - 12} more")
        out.append(f"    {way_out}")
    out.append(f"  {_bg().board_autonomy.broken_card_note(graph, _bg().BROKEN_CARD)}")
    stop = graph.stop(key)
    if stop:
        out.append(f"  Stop: {stop['minutes']} minutes or {stop['repeat_failures']} failures of the same command "
                   "on unchanged source inputs, a loop guard ("
                   + ("this card's own" if stop["own"] else "the graph's for every card")
                   + "); past it, stop and record where you are")
    else:
        out.append("  Stop: no guard set; a run longer than one step needs the owner's go with a stop")
    if starting:
        out.append("  Started: now, by next, for the one reading this; this brief is kept in the start row")
    elif card["working"]:
        out.append("  Started: it is on (above); the brief next printed at that start is in the start row")
    elif state.launch_blocks(graph, node["track"], key):
        out.append("  Start it: blocked by the launch ownership or checkpoint above")
    else:
        out.append(f"  Start it: {_bg().COMMAND} next --track {node['track']}"
                   + ("" if track["ordered"] else f" --node {key}") + " --by <owner | agent:name> --note \"<what the work is>\"")
    attempt_arg = f" --attempt {card['working'][0]['run_id']}" if len(card["working"]) == 1 and card["working"][0]["run_id"] else ""
    if node["action"] in _bg().COMMANDS and attempt_arg:
        work = card["working"][0]
        template = _bg().COMMANDS[node["action"]]
        command = template.replace("<id>", work["run_id"]).replace("<who>", _bg().shell_quote(work["by"]))
        if node["action"] == "routing_run":
            if graph.path is not None:
                command += " --graph " + _bg().shell_quote(graph.path.resolve())
            scratch = getattr(state, "selected_state_path", None)
            if scratch is not None:
                command += " --state " + _bg().shell_quote(pathlib.Path(scratch).resolve())
            command += " --records " + _bg().shell_quote(pathlib.Path(state.records).resolve())
        out = [line.replace(template, command) if line.startswith("  Tool: ") else line for line in out]
    if graph.data.get("approval_mode", "off") != "off" and _bg().board_approvals.presentation_kind(node):
        images = ("board-render.json and each image shown" if _bg().board_approvals.presentation_kind(node) == "board_render"
                  else "the assembler preview images for the sealed archive")
        out.append(f"  For revision approval, record needs_owner with {images}. "
                   "At sign-off, cite those files again and record the owner's words.")
    record_command = starting.get("record_command") if starting else None
    outcomes = "; ".join(f"{name} -> {' or '.join(targets)}" for name, targets in card["outcomes"].items())
    if record_command:
        out.append("  Close it: " + record_command)
        out.append("  Outcomes: " + outcomes)
    else:
        out.append(f"  Close it: {_bg().COMMAND} record {key} <outcome>{attempt_arg} --by <who> "
                   f"--note \"<why; the owner's words for a decision>\" --evidence <paths>; {outcomes}")
    if attempt_arg:
        settle_command = starting.get("settle_command") if starting else None
        out.append("  Once its thread ends: " + (settle_command or
                   f"{_bg().COMMAND} settle {key}{attempt_arg} --by <launcher> --tokens <n-if-known>"))
    if rows:
        out.append("  Last transitions:")
        out.extend(f"    {row['at']} {row['node']} {row['outcome']} -> {row['target']} ({row['actor']}): {row['note']}"
                   for row in rows)
    return "\n".join(out) + "\n"


def settle(graph, state, key, actor, note="", evidence=(), tokens=None, root=_bg().ROOT, at=None, attempt_id=None,
           launch_ok=None, acknowledge=False, termination_unconfirmed=False, verified_stopped=False):
    """Settle a run: the launcher's move once the thread it started on card `key` has ended, every time. The thread's
    `tokens` of context go on the attempt the thread was, for the Token use card to sum. A thread can also
    die before it records (a crash, a context that ran out, a hung tool), and the card would stay on with nobody
    on it: with an attempt still open, the launcher records needs_lead for `actor`, saying who was on it and how
    long and how many tokens against the stop, with `evidence` paths inside the project if it has any; with
    nothing open, nothing is recorded and the answer says how the card last closed. Returns (the record or None,
    what to print)."""
    if isinstance(state, _bg().Remote):
        state.call("read")  # the worker can have recorded through a different process
    label = graph.nodes[key]["label"]
    attempts = state.attempts(key)
    if attempt_id is not None:
        attempts = [attempt for attempt in attempts if attempt["run_id"] == attempt_id]
        if not attempts:
            raise ValueError(f"attempt {attempt_id} is not an attempt at {key}")
    held = [attempt for attempt in attempts if attempt["finished_at"] is None]
    if attempt_id is None:
        if held or any(attempt.get("launch_state") in ("active", "needs_lead") for attempt in attempts):
            raise ValueError("name the exact run with --attempt before settling it")
        if tokens is not None:
            raise ValueError("name --attempt to keep token usage; unscoped settle only reports status")
    if acknowledge:
        if attempt_id is None:
            raise ValueError("--ack needs the exact --attempt")
        state.ack_launch(attempt_id, actor, note, verified_stopped)
        return None, f"{label}: launch checkpoint {attempt_id} acknowledged by {actor}: {note}\n"
    if verified_stopped:
        raise ValueError("use --verified-stopped with --ack after inspecting the launch checkpoint")
    if launch_ok is None and any(attempt.get("launch_state") == "active" for attempt in attempts):
        termination_unconfirmed = True  # a missing launcher result does not prove the process tree ended
    if not held:
        finished = [attempt for attempt in attempts if attempt["finished_at"]]
        if finished:
            last = finished[-1]
            if last.get("launch_state") == "active":
                reason = note or "the launcher ended without reporting its process result"
                status = state.finish_launch(last["run_id"], reason, abnormal=launch_ok is not True,
                                             termination_unconfirmed=termination_unconfirmed)
                if status == "needs_lead":
                    current = next(attempt for attempt in state.attempts(key) if attempt["run_id"] == last["run_id"])
                    return None, (f"{label}: launch attempt {last['run_id']} stopped after its record: {reason}. "
                                  "The lead must acknowledge this checkpoint with a revised approach.\n"
                                  + _bg().launch_lines([current], graph, state, root)[0].strip() + "\n")
            said = (f"{label}: nothing open; the last attempt, {last['actor']} since {_bg().stamp(last['started_at'])}, ended "
                    f"{_bg().stamp(last['finished_at'])} with {last['outcome'] or 'no outcome'}")
            if tokens is not None:
                state.keep_tokens(last["run_id"], tokens)
                said += f"; {tokens:,} tokens kept on it"
        else:
            said = f"{label}: nothing open, and nothing was ever on it"
        return None, said + "\n"
    at = at or _bg().now()
    stop = graph.stop(key)
    ran = []
    for attempt in held:
        minutes = (_bg().parse_time(at) - _bg().parse_time(attempt["started_at"])).total_seconds() / 60
        ran.append(f"{attempt['actor']} since {_bg().stamp(attempt['started_at'])}, {minutes:.0f} min"
                   + (f" of the stop's {stop['minutes']}" if stop else ""))
    words = "The thread ended without a record: " + "; ".join(ran)
    if tokens is not None:
        words += f"; {tokens:,} tokens used"
    words += ". The lead looks at what it left before anyone starts the card again." + (f" {note}" if note else "")
    try:
        release = ("checkpoint" if termination_unconfirmed else True) if held[0].get("launch_state") == "active" else False
        item = _bg().record(graph, state, key, "needs_lead", actor, words, list(evidence), root=root, tokens=tokens,
                      attempt_id=attempt_id, release_launch=release, launch_note=note if termination_unconfirmed else None)
    except ValueError:
        # Another process may have closed or taken this exact attempt after the read above.
        # Inspect it again; never let its late return close the new worker's attempt.
        if attempt_id is None:
            raise
        if isinstance(state, _bg().Remote):
            state.call("read")
        current = [attempt for attempt in state.attempts(key) if attempt["run_id"] == attempt_id]
        if not current or current[0]["finished_at"] is None:
            raise
        return _bg().settle(graph, state, key, actor, note, evidence, tokens, root, at, attempt_id, launch_ok, acknowledge,
                      termination_unconfirmed, verified_stopped)
    said = (f"{label}: settled by the launcher, needs_lead; " + "; ".join(ran)
            + (f"; {tokens:,} tokens kept on it" if tokens is not None else "") + "\n")
    if termination_unconfirmed and held[0].get("launch_state") == "active":
        current = next(attempt for attempt in state.attempts(key) if attempt["run_id"] == attempt_id)
        said += _bg().launch_lines([current], graph, state, root)[0].strip() + "\n"
    return item, said


def last_upkeep(state):
    """The last tidy run next made, from the database, or None."""
    upkeeps = state.upkeeps()
    return ({key: upkeeps[-1][key] for key in ("started_at", "finished_at", "node", "why", "outcome", "mac", "problems")}
            if upkeeps else None)


def keep_checks(state, commit, run_id, event, summary_path, at=None):
    """A CI run's board checks into the database: scripts/validate_board.py's summary.json at `summary_path`
    (pass or fail, each check, the open count), or a row that says the checks did not finish when there is
    none. The validate workflow runs this as its last step, on the Mac. Returns the row written."""
    try:
        data = json.loads(pathlib.Path(summary_path).read_text(encoding="utf-8-sig"))
        checks = list(data.get("checks") or [])
    except (OSError, ValueError):
        checks = []
    passed = bool(data.get("pass")) if checks else None
    note = None if checks else "no summary.json: the board checks did not finish"
    open_count = next((item.get("open") for item in checks if "open" in item), None)
    return state.record_checks(at or _bg().now(), commit, run_id, event, passed, open_count, checks, note)


def ci_section(state, commit):
    """CI's board checks as the database holds them: the newest row, and the row on `commit` (this checkout's
    head) when one is named and found."""
    return {"commit": commit, "this": state.checks_for(commit) if commit else None, "latest": state.latest_checks()}


def checks_words(item):
    """One CI row in a few words: pass or FAIL with the open count and the failing checks, or why there is no
    result; then the run, its event and when."""
    if item["passed"] is None:
        state = item.get("note") or "no result"
    else:
        state = "pass" if item["passed"] else "FAIL"
        if item["open"] is not None:
            state += f", {item['open']} open"
        failing = [check["check"] for check in item["checks"] if not check.get("ok")]
        inherited = [check["check"] for check in item["checks"] if check.get("inherited")]
        if failing:
            state += "; failing: " + ", ".join(failing)
        if inherited:
            state += "; inherited: " + ", ".join(inherited)
    return f"{state}; run {item['run_id']} ({item['event']}) at {_bg().stamp(item['at'])}"


def ci_lines(section):
    """`where`'s lines on CI's board checks: the row on this checkout's commit, else that there is none yet and
    the newest row there is."""
    if not section:
        return []
    this, latest, commit = section.get("this"), section.get("latest"), section.get("commit")
    if this:
        return [f"CI, this commit ({this['commit'][:8]}): {_bg().checks_words(this)}"]
    lines = [f"CI, this commit ({commit[:8]}): no result on the graph yet" if commit else "CI, this commit: not read"]
    if latest:
        lines.append(f"CI, newest: {latest['commit'][:8]}, {_bg().checks_words(latest)}")
    else:
        lines.append("CI: no run has written to the graph yet")
    return lines


def merge_questions(graph, state):
    """Each merge question a track's position waits on, as its record holds it. GitHub is not asked."""
    found = []
    for track in graph.data["tracks"]:
        question = state.merge_question(track["id"]) if track["ordered"] else None
        if question:
            found.append({"track": track["id"], "node": question["node"], "pull_request": question["number"],
                          "then": question["then"], "asked_at": question["asked_at"],
                          "asked_at_head": question["head"]})
    return found


def workspace(graph, state, root=_bg().ROOT, github=None, mac_status=None, list_pull_requests=None):
    """The full read of what `where --full` reports beside the graph, as data. It fetches first, asks the Mac
    and GitHub, and keeps what it read in the database under this machine's name (State.keep_full_read), where a
    quick `where` here and the view find it.

    Here: main and the checkout `where` runs in, each against GitHub's main, the worktrees whose work is all on
    main and that tidy has yet to clear (tidy pending), and the branches and worktrees holding commits main lacks
    (branches_here). The Mac: the same for its checkout, or that it was not reached.
    Then each pull request a position waits on: open, draft, merged, its checks; and GitHub's open pull
    requests, each tied to the node whose record names it and each with where its branch's own records put the
    tracks (branch_progress).
    """
    root = pathlib.Path(root)
    github = github or (lambda number: _bg().pull_request_view(number, root))
    view = {"at": _bg().now(), "fetch_error": None}
    try:
        _bg().run_git(root, "fetch", "--prune", "-q", "origin", timeout=120)
    except ValueError as error:
        view["fetch_error"] = str(error)
    here = root.resolve()
    trees = _bg().tidy.worktrees(_bg().git_text(root, "worktree", "list", "--porcelain"))
    this = _bg().checkout_here(graph, here)
    checkouts = []
    if this["branch"] == "main":
        checkouts.append(this)
    else:
        head = _bg().main_head(root)
        if head:
            checkouts.append(_bg().main_state(graph, root, _bg().main_checkout(root, trees), head))
        checkouts.append(this)
    view["here"] = {"checkouts": checkouts, **_bg().branches_here(root, trees)}
    if mac_status is None and _bg().mac.LOCAL:
        view["mac"] = {"reached": True, "this_machine": True}
    else:
        try:
            view["mac"] = dict((mac_status or _bg().tidy.mac_status)(), reached=True)
        except _bg().mac.MacError as error:
            view["mac"] = {"reached": False, "error": str(error)}
        except (ValueError, IndexError) as error:
            view["mac"] = {"reached": True, "error": f"its status did not read: {error}"}
    view["merge_questions"] = _bg().merge_questions(graph, state)
    for item in view["merge_questions"]:
        try:
            info = github(item["pull_request"])
            item.update(state=info["state"].lower(), draft=bool(info.get("isDraft")),
                        checks=_bg().checks(info.get("statusCheckRollup")),
                        merge_commit=(info.get("mergeCommit") or {}).get("oid"), head=info.get("headRefOid"),
                        head_moved=info.get("headRefOid") != item["asked_at_head"], url=info.get("url"))
        except ValueError as error:
            item["error"] = str(error)
    try:
        found = (list_pull_requests or _bg().open_pull_requests)(root)
        view["pull_requests"] = {"open": _bg().tied_pull_requests(
            state.transitions(), found, lambda branch: _bg().branch_progress(
                graph, *_bg().records_on_branch(graph, state, root, branch))),
            "error": None}
    except ValueError as error:
        view["pull_requests"] = {"open": [], "error": str(error)}
    view["last_upkeep"] = _bg().last_upkeep(state)
    view["ci"] = _bg().ci_section(state, this.get("oid"))
    view["read_on"] = _bg().MACHINE
    state.keep_full_read(_bg().MACHINE, view)
    return view


def branches_here(root, trees=None):
    """What this computer's git alone knows, read with no network against origin/main as last fetched, by tidy's
    rule: `tidy_pending`, the worktrees all of whose work is on main (each with its branch, None for one on no
    branch, and `kept`, why it stays, or None when the next tidy takes it), and `not_on_main`, the branches and
    worktrees holding commits main lacks (tidy.not_on_main). Both None when git did not answer."""
    root = pathlib.Path(root)
    here = root.resolve()
    try:
        if trees is None:
            trees = _bg().tidy.worktrees(_bg().git_text(root, "worktree", "list", "--porcelain"))
        merged = _bg().tidy.merged_branches(cwd=root)
        pending, moment = [], time.time()
        for tree in trees[1:]:  # the first is the repository's own folder, which tidy never removes
            path, branch = pathlib.Path(tree["path"]), tree.get("branch")
            if (branch not in merged) if branch else not _bg().tidy.landed("HEAD", path):
                continue
            inside = path.resolve() == here or path.resolve() in here.parents
            pending.append({"path": str(path), "branch": branch,
                            "kept": "this checkout" if inside else _bg().tidy.held(path, moment)})
        return {"tidy_pending": pending, "not_on_main": _bg().tidy.not_on_main(root, trees, merged)}
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return {"tidy_pending": None, "not_on_main": None}


def _parse_time_or_none(text):
    try:
        dt = _bg().parse_time(text)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _keep_pace_is_newer(live_kp, prev_mac, prev_at):
    if not isinstance(live_kp, dict) or "error" in live_kp:
        return False
    kp_time = _bg()._parse_time_or_none(live_kp.get("at"))
    if kp_time is None:
        return False
    if not isinstance(prev_mac, dict) or not prev_mac.get("reached") or prev_mac.get("error"):
        return True
    prev_time = _bg()._parse_time_or_none(prev_at)
    if prev_time is None:
        return True
    return kp_time >= prev_time.replace(microsecond=0) and live_kp != prev_mac.get("keep_pace")


def _upkeep_synced_mac_after(upkeep, after_at):
    if not isinstance(upkeep, dict) or upkeep.get("outcome") != "current" or upkeep.get("mac") is not None:
        return False
    finished = _bg()._parse_time_or_none(upkeep.get("finished_at"))
    prev = _bg()._parse_time_or_none(after_at)
    return finished is not None and prev is not None and finished > prev


def _is_stale_mac_read(before, now_text):
    prev = _bg()._parse_time_or_none(before)
    current = _bg()._parse_time_or_none(now_text)
    return prev is not None and current is not None and (current - prev).total_seconds() >= _bg().STALE_MAC_READ_SECONDS


def _merge_live_mac(section, state, previous):
    prev_mac = previous.get("mac")
    prev_at = previous.get("at")
    if _bg().mac.ONE_MACHINE or (isinstance(prev_mac, dict) and prev_mac.get("this_machine")):
        return
    live_kp = getattr(state, "mac_keep_pace", None)
    if _bg()._keep_pace_is_newer(live_kp, prev_mac, prev_at):
        if isinstance(live_kp.get("behind"), int) and isinstance(live_kp.get("ahead"), int) and live_kp.get("head"):
            section["mac"] = {
                "reached": True,
                "branch": live_kp.get("branch") or "detached",
                "head": live_kp["head"],
                "behind": live_kp["behind"],
                "ahead": live_kp["ahead"],
                "tidy_pending": [],
                "notes": [],
                "keep_pace": live_kp,
                "as_of": live_kp["at"],
                "from_keep_pace": True,
            }
        else:
            section["mac"] = {
                "reached": True,
                "error": live_kp.get("said") or "keep-pace failed",
                "keep_pace": live_kp,
                "as_of": live_kp["at"],
                "from_keep_pace": True,
            }
        return
    if isinstance(prev_mac, dict) and _bg()._upkeep_synced_mac_after(section.get("last_upkeep"), prev_at):
        section["mac"] = dict(prev_mac, superseded_by_upkeep=section["last_upkeep"]["finished_at"])


def carried_workspace(graph, state, previous=None):
    """The workspace with no git and no network: an earlier full read carried, the database read now.

    `previous` is that full read. Its checkouts, tidy pending, Mac, fetch and pull requests are kept as they
    were, under its `at`. The last tidy run and the merge questions the records hold are read from the database,
    each question keeping GitHub's word on it from before when it is the same question. `quick` says when the
    database was read and from when the rest is carried (None: nothing to carry).
    """
    previous = previous if isinstance(previous, dict) else {}
    section = {key: value for key, value in previous.items()
               if key not in ("quick", "merge_questions", "last_upkeep", "ci")}
    section["quick"] = {"at": _bg().now(), "carried_from": previous.get("at")}
    asked = {(item.get("pull_request"), item.get("asked_at")): item for item in previous.get("merge_questions", [])}
    section["merge_questions"] = []
    for item in _bg().merge_questions(graph, state):
        said = asked.get((item["pull_request"], item["asked_at"]), {})
        item.update({key: said[key] for key in _bg().CARRIED_QUESTION if key in said})
        section["merge_questions"].append(item)
    section["last_upkeep"] = _bg().last_upkeep(state)
    section["ci"] = _bg().ci_section(state, None)  # the newest CI row; a quick where adds this checkout's commit
    _bg()._merge_live_mac(section, state, previous)
    return section


def quick_workspace(graph, state, root=_bg().ROOT):
    """What `where` reports by default: this checkout and the branches and worktrees here read now (checkout_here
    and branches_here, with no fetch), the database read now, and the rest carried from this machine's last full
    read, kept in the database, or not read when none is kept. It asks nothing of the Mac's checkout or of GitHub;
    the database is read as always, over SSH from the PC."""
    section = _bg().carried_workspace(graph, state, state.last_full_read(_bg().MACHINE))
    this = _bg().checkout_here(graph, root)
    section["ci"] = _bg().ci_section(state, this.get("oid"))
    before = section.get("here") or {}
    main = [dict(item, this=False, carried=True) for item in before.get("checkouts", [])
            if item.get("branch") == "main" and this["branch"] != "main"]
    section["here"] = {"checkouts": main + [this], **_bg().branches_here(root)}
    if _bg().mac.LOCAL:
        section["mac"] = {"reached": True, "this_machine": True}
    return section


def stamp(text):
    """An ISO time to the second, in UTC: 2026-11-06T01:34:31Z. A time that does not parse is shown as it is."""
    try:
        return _bg().parse_time(text).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return str(text)


def ago(text, at=None):
    """How long ago an ISO time was, in a few words; `at` is now (tests pass one)."""
    try:
        minutes = ((at or datetime.now(timezone.utc)) - _bg().parse_time(text)).total_seconds() / 60
    except (TypeError, ValueError):
        return "when, it does not say"
    if minutes < 1:
        return "under a minute ago"
    if minutes < 120:
        return f"{minutes:.0f} min ago"
    if minutes < 48 * 60:
        return f"{minutes / 60:.0f} h ago"
    return f"{minutes / 1440:.0f} days ago"


def standing(item):
    """A checkout's place against GitHub's main, in words."""
    if item.get("error"):
        return f"could not be read ({item['error']})"
    behind, ahead = item["behind"], item["ahead"]
    if behind and ahead:
        place = f"{behind} commit(s) behind and {ahead} ahead of GitHub's main"
    elif behind:
        place = f"{behind} commit(s) behind GitHub's main"
    elif ahead:
        place = f"{ahead} commit(s) ahead of GitHub's main"
    else:
        place = "level with GitHub's main"
    if item.get("dirty_owned"):
        place += "; uncommitted changes to owned files: " + ", ".join(item["dirty_owned"])
    return place


def keep_pace_lines(mac_view, read_at):
    """The Mac's last keep-pace run (scripts/mac_keep_pace.py: one run each time main changes, made by the pusher,
    the sync workflow or tidy) as its status says, in words: when, who made it, the result and its words, against
    the time of the read."""
    if "keep_pace" not in mac_view:
        return []  # a read made before any run
    status = mac_view["keep_pace"]
    if status is None:
        return ["Mac, keep-pace: no run recorded (no logs/mac-keep-pace/status.json there: has nothing been pushed "
                "to main since the Mac was set up?)"]
    if status.get("error"):
        return [f"Mac, keep-pace: {status['error']}"]
    try:
        at = _bg().parse_time(read_at)
    except (TypeError, ValueError):
        at = datetime.now(timezone.utc)
    before = lambda text: _bg().ago(text, at).replace(" ago", " before this read")  # noqa: E731
    return [f"Mac, keep-pace: last run {_bg().stamp(status.get('at'))} by {status.get('by', '?')} "
            f"({before(status.get('at'))}): {status.get('result', '?')}, {status.get('said', '')}"]


def page_lines(mac_view):
    """The graph page, served from the Mac by scripts/board_graph_serve.py, as its status says: whether it answers
    at its address, its launchd job, and launchd's copy of the plist when that is not the repository's."""
    if "page" not in mac_view:
        return []  # a read made before the page was served from the Mac
    page = mac_view["page"]
    if page is None:
        return ["Mac, graph page: no status (scripts/board_graph_serve.py --status said nothing there: is the Mac's "
                "main current?)"]
    if page.get("error"):
        return [f"Mac, graph page: {page['error']}"]
    job = page.get("launchd")
    loaded = (f"launchd job {job.get('state')}" + (f", pid {job['pid']}" if job.get("pid") else "") if job
              else "launchd job not loaded")
    said = page.get("answers")
    line = (f"Mac, graph page: {page.get('address')} " + ("answers" if said == "yes" else f"does not answer ({said})")
            + f"; {loaded}")
    if page.get("job") != "installed":
        line += (f"; launchd's copy of its plist: {page.get('job')} (run `python3 scripts/board_graph_serve.py "
                 "--install` there)")
    return [line]


def _render_mac_workspace_lines(view, quick, before, carried):
    """Render the Mac checkout section of workspace() output."""
    out = []
    mac_view = view.get("mac")
    if _bg().mac.ONE_MACHINE:
        return out
    if mac_view is None:
        out.append("  Mac: not read")
    elif mac_view.get("this_machine"):
        out.append("  Mac: this machine, above")
    elif quick and mac_view.get("superseded_by_upkeep"):
        stale_part = (f" (cached full read from {_bg().stamp(before)} omitted; run `where --full` to check now)"
                      if before else "")
        out.append(f"  Mac: synced during upkeep at {_bg().stamp(mac_view['superseded_by_upkeep'])}{stale_part}")
    elif quick and not mac_view.get("from_keep_pace") and _bg()._is_stale_mac_read(before, quick.get("at")):
        out.append(f"  Mac: cached status from {_bg().ago(before, _bg()._parse_time_or_none(quick.get('at')))} "
                   f"({_bg().stamp(before)}) is stale; run `where --full` to check the Mac now")
    elif not mac_view["reached"]:
        out.append(f"  Mac: not reached ({mac_view['error']}){carried}")
    elif mac_view.get("error"):
        if mac_view.get("from_keep_pace") and mac_view.get("as_of"):
            out.append(f"  Mac: reached, but {mac_view['error']} (as of {_bg().stamp(mac_view['as_of'])}, keep-pace)")
            out.extend(f"  {line}" for line in _bg().keep_pace_lines(mac_view, quick["at"] if quick else view.get("at")))
        else:
            out.append(f"  Mac: reached, but {mac_view['error']}{carried}")
    else:
        from_kp = bool(mac_view.get("from_keep_pace"))
        mac_carried = (f" (as of {_bg().stamp(mac_view['as_of'])}, keep-pace)"
                       if from_kp and mac_view.get("as_of") else carried)
        if mac_view["branch"] == "main":
            out.append(f"  Mac, main at {mac_view['head']}: {_bg().standing(mac_view)}{mac_carried}")
        else:
            out.append(f"  Mac: its checkout is on {mac_view['branch']}, not main ({_bg().standing(mac_view)}){mac_carried}")
        kp_read_at = quick["at"] if (quick and from_kp) else view.get("at")
        kp_suffix = "" if from_kp else carried
        out.extend(f"  {line}{kp_suffix}" for line in _bg().keep_pace_lines(mac_view, kp_read_at))
        out.extend(f"  {line}{carried}" for line in _bg().page_lines(mac_view))
        for item in mac_view.get("tidy_pending") or []:
            if not item["branch"]:
                what = f"{item['path']} is a disposable copy nothing has used for an hour"
            elif item["branch"] == _bg().tidy.NO_BRANCH:
                what = f"a worktree on no branch has nothing main lacks and still holds {item['path']}"
            else:
                what = f"{item['branch']} is merged and still holds {item['path']}"
            out.append("  Mac, tidy pending: " + what + carried)
        out.extend(f"  {note}{carried}" for note in mac_view.get("notes") or [])
    return out


def render_workspace(view):
    """The workspace in words. In a quick read every line carried from the last full read ends "(as of <time>)",
    and what no read has reached says "not read"."""
    quick = view.get("quick")
    before = quick["carried_from"] if quick else None
    carried = f" (as of {_bg().stamp(before)})" if before else ""
    if quick:
        out = ["Workspace, quick read (`where --full` reads it all now):",
               "  Read now: this checkout and the branches and worktrees here, against origin/main as this computer "
               "last fetched it (GitHub not fetched), the merge questions and the last upkeep",
               f"  Carried from the last full read, {_bg().ago(before)}: each line ending \"as of\" that time" if before
               else "  No full read is kept here: what only a full read reaches says \"not read\""]
        if before and view.get("fetch_error"):
            out.append(f"  GitHub: the full read could not fetch ({view['fetch_error']}){carried}")
    elif view["fetch_error"]:
        out = ["Workspace:", f"  GitHub: fetch failed ({view['fetch_error']}); counts are against the last fetch"]
    else:
        out = ["Workspace:", f"  GitHub: fetched at {view['at']}"]
    here = view.get("here") or {}
    for item in here.get("checkouts", []):
        label = "main" if item["branch"] == "main" else f"this checkout ({item['branch'] or 'no branch'})"
        out.append(f"  Here, {label} at {item.get('head', '?')} ({item['path'] or 'no checkout is on it'}): "
                   f"{_bg().standing(item)}" + (carried if item.get("carried") else ""))
    if quick and not before and not any(item["branch"] == "main" for item in here.get("checkouts", [])):
        out.append("  Here, main: not read")
    if here.get("tidy_pending") is None:
        out.append("  Here, tidy pending: not read")
    for item in here.get("tidy_pending") or []:
        what = (f"{item['branch']} is merged and" if item["branch"]
                else "a worktree on no branch has nothing main lacks and")
        out.append(f"  Here, tidy pending: {what} still holds {item['path']}"
                   + (f" (kept: {item['kept']})" if item["kept"] else ""))
    for item in here.get("not_on_main") or []:
        github = {"same": ", on GitHub too", "differs": ", and GitHub's branch of that name differs",
                  None: ", only here"}[item.get("github")] if item["branch"] else ""
        out.append(f"  Here, not on main yet: {item['branch'] or 'a worktree on no branch'}, {item['ahead']} "
                   f"commit(s), last {item['last']}{github}" + (f", in {item['path']}" if item.get("path") else ""))
    out.extend(f"  {line}" for line in _bg().ci_lines(view.get("ci")))
    out.extend(_render_mac_workspace_lines(view, quick, before, carried))
    for item in view["merge_questions"]:
        head = f"  Pull request {item['pull_request']} ({item['node']} waits on its merge, then {item['then']}): "
        if item.get("error"):
            out.append(head + f"GitHub did not say ({item['error']}){carried}")
            continue
        if "state" not in item:
            out.append(head + "GitHub not asked since the question (`where --full` asks)")
            continue
        facts = [item["state"] + (", draft" if item["draft"] and item["state"] == "open" else ""),
                 f"checks {item['checks']}"]
        if item["merge_commit"]:
            facts.append(f"merged as {item['merge_commit'][:8]}")
        if item["head_moved"]:
            facts.append(f"its head moved since the question ({item['asked_at_head'][:8]}, now {item['head'][:8]})")
        out.append(head + "; ".join(facts) + carried)
    pulls = view.get("pull_requests")
    if pulls and pulls.get("error"):
        out.append(f"  Open pull requests: GitHub did not say ({pulls['error']}){carried}")
    for item in (pulls or {}).get("open", []):
        named = f"{item['node']} names it" if item.get("node") else "no record names it"
        out.append(f"  Open pull request {item['number']} ({item.get('branch') or '?'}"
                   + (", draft" if item.get("draft") else "") + f"): checks {item['checks']}; {named}{carried}")
        for entry in item.get("on_branch") or []:
            stands = (f"at {entry['position_label']}" if entry["position"] not in (None, _bg().DONE) else
                      "finished" if entry["position"] == _bg().DONE else
                      f"{entry['last']['label']}, {entry['last']['outcome']}")
            out.append(f"    on its branch, not merged: {entry['track_label']} {stands}"
                       + (f" ({len(entry['passed'])} passed there)" if entry["passed"] else ""))
    last = view["last_upkeep"]
    if last:
        out.append(f"  Last upkeep: {last['finished_at'] or last['started_at'] + ' (never finished)'}, "
                   f"{last['why']} at {last['node']}: {last['outcome'] or 'unfinished'}"
                   + (f"; Mac skipped ({last['mac']})" if last["mac"] else ""))
    return out


def render_track(graph, state, track):
    """What where prints, for one track."""
    view = _bg().where(graph, state)
    return _bg().render(dict(view, tracks=[item for item in view["tracks"] if item["id"] == track]))


def follow(graph, state, view, root=_bg().ROOT):
    """Make the view in folder `view` follow the state: after each write, its view.json is rewritten by
    board_graph_view.py's quick snapshot (the database alone: no git, no network). A snapshot that
    fails is one line on stderr and never fails the write or the command."""
    def refresh():
        try:
            import board_graph_view
            board_graph_view.quick_snapshot(graph, state, view, root)
        except Exception as error:  # noqa: BLE001 - whatever stops the view, the write stands
            sys.stderr.write(f"the view was not refreshed ({type(error).__name__}: {error}); the state is written\n")
    state.on_change = refresh


def every_board(argv, names, command, as_json, run=None):
    """`where` or `check` in a repository with several boards and none named: the same command once per board, each
    in a process of its own, since a process works on one board. Each board's output follows a line naming it;
    with `as_json`, one object, {"boards": [{"name", "description", "where"}]}. The exit code is the worst one."""
    run = run or (lambda words: subprocess.run([sys.executable, str(_bg().PROGRAM), *words], capture_output=True,
                                               **_bg().NO_WINDOW_KWARGS))
    listed, worst, views = _bg().boards.read(_bg().ROOT), 0, []
    for name in names:
        done = run([_bg().boards.ARGUMENT, name, *argv])
        out, err = (text.decode("utf-8", "replace").replace("\r\n", "\n") for text in (done.stdout, done.stderr))
        worst = max(worst, done.returncode)
        if as_json and done.returncode == 0:
            views.append({"name": name, "description": listed[name].description, "where": json.loads(out)})
        elif not as_json:
            sys.stdout.write(f"{_bg().ONE.capitalize()} {name}, {listed[name].description}:\n" if command == "where" else "")
            sys.stdout.write(out)
        sys.stdout.flush()  # what the board's own process said on stderr follows its line, in the order it was said
        sys.stderr.write(err)
        sys.stderr.flush()
        if not as_json and command == "where" and name != names[-1]:
            sys.stdout.write("\n")
    if as_json:
        print(json.dumps({"boards": views}, indent=2))
    return worst
