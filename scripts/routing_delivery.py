"""Keep the reviewed PCB private until its graph transition commits. Resume publication without rerouting."""
from pathlib import Path
import hashlib
import json
import subprocess
import tempfile

from no_window import KWARGS
from workflow_history import payload, publish


class DeliveryError(ValueError):
    pass


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, **KWARGS)
    if result.returncode:
        raise DeliveryError(result.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
    return result.stdout.decode("utf-8").strip()


def journal(root):
    return Path(git(root, "rev-parse", "--absolute-git-dir")) / "board-graph-routing-delivery.json"


def workspace(root):
    stage = Path(tempfile.mkdtemp(prefix="board-graph-delivery-")).resolve()
    git(root, "worktree", "add", "--detach", "--quiet", str(stage), "HEAD")
    return stage


def seal(root, stage, context, attempt, outcome, note, tokens, files, pcb=None):
    base = git(stage, "rev-parse", "HEAD")
    git(stage, "add", "-f", "--", *files, *([pcb] if pcb else []))
    git(stage, "commit", "--allow-empty", "-q", "-m", f"Routing evidence for attempt {attempt}")
    data = dict(context=context, attempt=attempt, outcome=outcome, note=note, tokens=tokens,
                stage=str(stage), base=base, commit=git(stage, "rev-parse", "HEAD"), files=files, pcb=pcb,
                receipt_sha256=hashlib.sha256((stage / files[0]).read_bytes()).hexdigest())
    publish(journal(root), json.dumps(data, sort_keys=True))
    return data


def matches_record(data, item):
    return (item["inputs"].get("attempt_id") == data["attempt"] and item["actor"] == data["context"]["actor"]
            and item["outcome"] == data["outcome"] and any(
                e["path"] == data["files"][0] and e["sha256"] == data["receipt_sha256"] for e in item["evidence"]))


def finish(root, data, context, state, record, record_path=None):
    """Commit the graph decision once, fast-forward the matching branch, then publish and clear the journal."""
    if data["context"] != context or git(root, "branch", "--show-current") != context["branch"]:
        raise DeliveryError("Pending routing delivery belongs to a different actor, board, branch, task folder or database")
    stage, commit, base = Path(data["stage"]), data["commit"], data["base"]
    if (stage.parent != Path(tempfile.gettempdir()).resolve() or not stage.name.startswith("board-graph-delivery-")
            or git(stage, "rev-parse", "HEAD") != commit or git(stage, "status", "--porcelain")
            or git(stage, "rev-parse", "HEAD^") != base
            or hashlib.sha256((stage / data["files"][0]).read_bytes()).hexdigest() != data["receipt_sha256"]):
        raise DeliveryError("The retained private delivery differs from its saved receipt")
    rows = [r for r in state.transitions(node="routing_batch") if r["inputs"].get("attempt_id") == data["attempt"]]
    if rows:
        item = rows[0]
        if len(rows) != 1 or not matches_record(data, item):
            raise DeliveryError("The graph has a different outcome or evidence for this routing attempt")
    else:
        if git(root, "rev-parse", "HEAD") != base or git(root, "status", "--porcelain"):
            raise DeliveryError("The original checkout changed before the graph accepted this delivery")
        item = record(stage)
    head = git(root, "rev-parse", "HEAD")
    if head == base:
        if git(root, "diff", "--name-only") or git(root, "diff", "--cached", "--name-only"):
            raise DeliveryError("The original checkout has local edits. The reviewed delivery is retained.")
        git(root, "merge", "--ff-only", "--quiet", commit)
    elif head != commit:
        # The only legitimate extra commit is the transition export created below.
        path = record_path(item) if record_path else None
        if (not path or git(root, "rev-parse", "HEAD^") != commit
                or git(root, "diff", "--name-only", commit, "HEAD") != path
                or json.loads(git(root, "show", f"HEAD:{path}")) != payload(item)):
            raise DeliveryError("The active branch moved since this delivery. The reviewed candidate is retained.")
    publication = commit
    if record_path:
        name = record_path(item)
        if json.loads((root / name).read_bytes()) != payload(item):
            raise DeliveryError("The exported transition differs from the graph record. The reviewed delivery is retained.")
        git(root, "add", "--", name)
        if git(root, "diff", "--cached", "--name-only", "--", name):
            git(root, "commit", "-q", "--only", "-m", f"Record routing attempt {data['attempt']}", "--", name)
        publication = git(root, "rev-parse", "HEAD")
        if (git(root, "rev-parse", publication + "^") != commit
                or git(root, "diff", "--name-only", commit, publication) != name
                or json.loads(git(root, "show", f"{publication}:{name}")) != payload(item)):
            raise DeliveryError("The publication commit differs from the accepted transition")
    branch = context["branch"]
    git(root, "push", "-q", "origin", f"{publication}:refs/heads/{branch}")
    journal(root).unlink()
    git(root, "worktree", "remove", str(stage))
    return item
