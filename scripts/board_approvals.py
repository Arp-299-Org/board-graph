"""Evaluate revision-scoped approvals using recorded outcomes and this checkout's files."""
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import board_autonomy
import tool_paths
import workflow_history


def tracked(node):
    return bool(node.get("unchanged_pass") or node.get("approval_view") or node.get("approval_files") or node.get("approval_source")
                or node.get("requires_decisions") or node.get("approval_owned") or node.get("release_check"))


def presentation_kind(node):
    if node.get("presentation"):
        return node["presentation"]
    if node["who"] == "owner" and "placement" in (node.get("approval_view"), node.get("unchanged_pass")):
        return "board_render"
    return None


def approval_sources(node):
    names = [node["approval_source"]] if node.get("approval_source") else []
    if presentation_kind(node) == "board_render":
        names.append("render_models")
    if node.get("approval_view") == "copper" or node.get("action") == "routing_run":
        names.append("routing_constraints")
    return list(dict.fromkeys(names))


def changed_keys(previous, current):
    return sorted(key for key in previous.keys() | current.keys() if key not in previous or key not in current
                  or previous[key] != current[key])


def presentation(review, key, evidence, board_path):
    """Freeze the visual files supplied for the owner's decision and their source at presentation."""
    kind = presentation_kind(review.graph.nodes[key])
    scope = review.scope(key)
    if not all(scope.values()):
        missing = [review.errors.get(name, name) for name, value in scope.items() if not value]
        raise ValueError("The presentation needs all approval inputs: " + ", ".join(missing))
    images = {e["path"]: e["sha256"] for e in evidence if Path(e["path"]).suffix.lower() in (".png", ".jpg", ".jpeg", ".pdf")}
    if not images:
        raise ValueError("Cite the preview images presented to the owner")
    shown = dict(images)
    source = None
    if kind == "board_render":
        receipts = [e for e in evidence if Path(e["path"]).name == "board-render.json"]
        if len(receipts) != 1:
            raise ValueError("Cite board-render.json and the images shown to the owner")
        receipt = receipts[0]
        path = review.root / receipt["path"]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            source = review.hash_file(review.root / board_path)
        except (OSError, ValueError) as error:
            raise ValueError(f"Cannot read the board render receipt: {error}") from error
        if not isinstance(data, dict) or data.get("schema") != "board-graph/board-render/v2":
            raise ValueError("Present a fresh board render with model hashes")
        if data.get("board_sha256") != source or not isinstance(data.get("images"), dict):
            raise ValueError("The presented render receipt does not match the saved board")
        recorded = data.get("models")
        if not isinstance(recorded, dict):
            raise ValueError("Present a fresh render that identifies its model files")
        changes = changed_keys(recorded, scope["source:render_models"]["models"])
        if changes or any(not value for value in recorded.values()):
            raise ValueError("The rendered models differ from the current models. Present a fresh render: " + ", ".join(changes))
        for name, fingerprint in images.items():
            image = Path(name)
            if image.parent != Path(receipt["path"]).parent or data["images"].get(image.name) != fingerprint:
                raise ValueError(f"The presented image differs from the render receipt: {name}")
        shown[receipt["path"]] = receipt["sha256"]
    return {"kind": kind, "scope": scope, "board_sha256": source, "files": shown}


def required(graph, key):
    node = graph.nodes[key]
    existing = [name for name in node.get("requires", []) if name in graph.nodes and tracked(graph.nodes[name])]
    return list(dict.fromkeys(existing + node.get("requires_current", [])))


def project_path(value):
    return (isinstance(value, str) and not PureWindowsPath(value).anchor and "\\" not in value and ":" not in value
            and all(part not in ("", ".", "..") for part in value.split("/")))


def problems(graph):
    bad = []
    if graph.data.get("approval_mode", "off") not in ("off", "observe", "enforce"):
        bad.append("approval_mode must be off, observe or enforce")
    for key, node in graph.nodes.items():
        for field in ("approval_files", "requires_current", "requires_decisions", "requires", "optional_outputs"):
            values = node.get(field, [])
            if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
                bad.append(f"{key}: {field} must be a list of names")
                continue
            if len(values) != len(set(values)):
                bad.append(f"{key}: duplicate {field} entry")
            for value in values:
                if field == "optional_outputs" and (not node.get("approval_owned") or value not in node.get("owns", [])):
                    bad.append(f"{key}: optional output {value} must belong to owns with approval_owned enabled")
                if field == "requires":
                    if value not in graph.nodes:
                        bad.append(f"{key}: requires unknown card {value}")
                elif field == "requires_current":
                    if value == key or value not in graph.nodes or not tracked(graph.nodes[value]):
                        bad.append(f"{key}: {value} must be a different card with declared approval inputs")
                elif (PureWindowsPath(value).anchor or "\\" in value or ":" in value
                      or any(part in ("", ".", "..") for part in value.split("/"))):
                    bad.append(f"{key}: {field} needs project-relative file paths: {value}")
    if not bad:
        visiting, done = set(), set()
        def visit(key):
            if key in visiting:
                raise ValueError(f"Approval dependency loop at {key}")
            if key not in done:
                visiting.add(key)
                for other in required(graph, key):
                    visit(other)
                visiting.remove(key)
                done.add(key)
        try:
            for key in graph.nodes:
                visit(key)
        except ValueError as error:
            bad.append(str(error))
    return bad


def read_records(folder):
    if not folder.is_dir():
        raise ValueError(f"Cannot compare records: directory missing: {folder}")
    rows = []
    for path in sorted(folder.glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in
                                               ("at", "track", "node", "outcome", "target", "actor", "note")):
            raise ValueError(f"Invalid transition in {path}")
        if not isinstance(row.get("inputs"), dict) or not isinstance(row.get("evidence"), list):
            raise ValueError(f"Missing transition inputs or evidence in {path}")
        row["record_name"] = path.name
        rows.append((workflow_history.order(row), row))
    return [row for _, row in sorted(rows, key=lambda item: item[0])]


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class Approvals:
    """One history snapshot. Each verdict names the original review and its first outstanding conditions."""

    def __init__(self, graph, rows, root, views, hash_file, sources=None, file_at=None, read_at=None, evidence_hash=None):
        self.graph = graph
        self.rows = rows
        self.missing = workflow_history.branch_points(rows)
        self.root = Path(root).resolve()
        self.views = views
        self.sources = sources or {}
        self.errors = {}
        self.hash_file = hash_file
        self.file_at = file_at
        self.read_at = read_at
        self.evidence_hash = evidence_hash or hash_file
        self.cache = {}
        self.verdicts = {}
        self.visiting = set()
        self.sequence = max((r.get("seq", 0) for r in rows), default=0)

    def source(self, kind, name):
        key = (kind, name)
        if key not in self.cache:
            try:
                if kind == "source":
                    value = self.sources[name](self.root)
                elif kind == "view":
                    value = self.views[name](self.root)
                elif kind == "decision":
                    path = (self.root / name).resolve()
                    if not path.is_relative_to(self.root):
                        raise ValueError(f"Decision outside project: {name}")
                    data = self.read_at(name) if self.read_at else path.read_bytes()
                    record = json.loads(data)
                    if (not isinstance(record, dict) or record.get("schema") != "board-graph/product-decision/v1"
                            or record.get("status") != "accepted" or record.get("owner") != "owner"
                            or not all(isinstance(record.get(k), str) and record[k].strip()
                                       for k in ("decision", "acceptance"))):
                        raise ValueError(f"Owner decision needed in {name}: record the decision and its acceptance test")
                    value = hashlib.sha256(data).hexdigest()
                elif self.file_at is not None:
                    value = self.file_at(name)
                else:
                    path = (self.root / PurePosixPath(name)).resolve()
                    if not path.is_relative_to(self.root):
                        raise ValueError(f"Path outside the project: {name}")
                    elif kind == "owned" and path.is_dir():
                        members = list(path.rglob("*"))
                        if not any(p.is_file() for p in members) or any(p.is_symlink() for p in members):
                            raise ValueError(f"Owned output directory needs verifiable project files: {name}")
                        value = self.evidence_hash(path)
                    elif kind == "evidence" or kind == "owned":
                        value = self.evidence_hash(path) if path.exists() else None
                    elif not path.is_file():
                        shared = tool_paths.shared_tool_file(name)
                        value = self.hash_file(shared) if shared is not None else None
                    else:
                        value = self.hash_file(path) if path.is_file() else None
            except (OSError, ValueError, IndexError, KeyError, TypeError, RuntimeError) as error:
                self.errors[kind + ":" + name] = str(error)
                value = None
            self.cache[key] = value
        return self.cache[key]

    def terms(self, key):
        node = self.graph.nodes[key]
        rules = {name: node.get(name) for name in ("who", "task", "pass_when", "action", "pass_check",
                                                   "unchanged_pass", "approval_view", "approval_files", "requires", "requires_current",
                                                   "approved_targets", "approval_source", "presentation")}
        for field in ("requires_decisions", "approval_owned", "optional_outputs", "release_check", "integration"):
            if node.get(field):
                rules[field] = node[field]
        rules["reading"] = self.graph.docs(key)
        return digest(rules)

    def readings(self, key):
        docs = self.graph.docs(key)
        return list(dict.fromkeys(["AGENTS.md", *docs])) if self.graph.nodes[key].get("action") == "routing_run" else docs

    def scope(self, key):
        node = self.graph.nodes[key]
        values = {"card terms": self.terms(key)}
        name = node.get("approval_view") or node.get("unchanged_pass")
        if name:
            values["view:" + name] = self.source("view", name)
        for name in approval_sources(node):
            values["source:" + name] = self.source("source", name)
        for name in node.get("approval_files", []):
            values["file:" + name] = self.source("file", name)
        for name in self.readings(key):
            values["file:" + name] = self.source("file", name)
        for name in node.get("requires_decisions", []):
            values["decision:" + name] = self.source("decision", name)
        for name in node.get("integration", {}).get("boards", {}).values():
            values["file:" + name] = self.source("file", name)
        firmware = node.get("integration", {}).get("firmware")
        if firmware:
            values["file:" + firmware] = self.source("file", firmware)
        if node.get("approval_owned"):
            for name in node["owns"]:
                hashed = self.source("owned", name)
                optional = name in node.get("optional_outputs", [])
                values["owned:" + name] = {"sha256": hashed} if hashed or (optional and "owned:" + name not in self.errors) else None
        for other in required(self.graph, key):
            approval = self.verdict(other)
            values["review:" + other] = approval["reviewed_at"] if approval["status"] == "current" else None
        return values

    def verdict(self, key):
        if key in self.verdicts:
            return self.verdicts[key]
        if key in self.visiting:
            raise ValueError(f"Approval dependency loop at {key}")
        self.visiting.add(key)
        try:
            result = self.evaluate(key)
            self.verdicts[key] = result
            return result
        finally:
            self.visiting.remove(key)

    def missing_history(self, key):
        track = self.graph.nodes[key]["track"]
        return self.missing.get((track, None if self.graph.tracks[track]["ordered"] else key), [])

    def evaluate(self, key):
        history = [r for r in self.rows if r["node"] == key or (r["outcome"] == "returned" and r["target"] == key)]
        passes = [r for r in history if r["node"] == key and r["outcome"] == "pass"]
        substantive = [r for r in history if not (r["outcome"] == "pass" and r["actor"] == "script:unchanged")]
        row = substantive[-1] if substantive else {}
        reviews = [r for r in substantive if r["node"] == key and r["outcome"] == "pass"]
        review = reviews[-1] if reviews else {}
        result = {"card": key, "ever_passed": bool(passes), "reviewed_at": review.get("at"),
                  "reviewed_by": review.get("actor"), "status": "unproven", "reasons": []}
        reasons = result["reasons"]
        if row:
            result["last_record"] = {name: row.get(name) for name in ("at", "node", "outcome", "note", "evidence")}
        if self.missing_history(key):
            reasons.append("Incomplete history. Import the missing predecessor: " + ", ".join(self.missing_history(key)))
        elif row and row["outcome"] != "pass":
            result["status"] = "open"
            reasons.append(f"{row['node']} recorded {row['outcome']}: {row['note']}")
        elif not review:
            reasons.append("No original review is recorded")
        elif self.graph.blocking_issues(key):
            result["status"] = "open"
            reasons.append("Open issues: " + ", ".join(self.graph.blocking_issues(key)))
        else:
            saved = review["inputs"].get("approval_v1")
            if not isinstance(saved, dict) or not saved:
                scope = self.scope(key)
                reasons.append(review["inputs"].get("presentation_finding") or (
                    "Historical pass lacks a reusable scope. Review " + ", ".join(
                    name for name in scope if name != "card terms") + " once."))
                if presentation_kind(self.graph.nodes[key]) and not review["inputs"].get("presentation_finding"):
                    reasons.append("The historical record does not bind the presented images to the reviewed inputs")
                for name, value in scope.items():
                    # Legacy views have no algorithm version. Only raw file hashes are comparable.
                    if name.startswith("file:"):
                        legacy = review["inputs"].get(name.removeprefix("file:"))
                        if legacy and value and legacy != value:
                            reasons.append("The recorded file fingerprint differs: " + name)
            else:
                delegated = (
                    self.graph.nodes[key]["who"] == "owner"
                    and bool(review["inputs"].get("delegated"))
                    and board_autonomy.can_delegate(self.graph, key, review["actor"])
                )
                if not review.get("evidence") or (self.graph.nodes[key]["who"] == "owner" and review["actor"] != "owner" and not delegated):
                    reasons.append("The pass lacks the required reviewer or cited evidence")
                else:
                    if delegated:
                        result["delegated"] = True
                    current = self.scope(key)
                    for entry in review["evidence"]:
                        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                            reasons.append("Malformed evidence in the original review")
                            continue
                        name, expected = entry.get("path"), entry.get("sha256")
                        if not name or not expected or self.source("evidence", name) != expected:
                            reasons.append("Cited evidence changed or is missing: " + str(name))
                    if presentation_kind(self.graph.nodes[key]):
                        shown = review["inputs"].get("approved_presentation") or {}
                        if not shown.get("files") or shown.get("scope") != saved:
                            reasons.append("Missing approval of the presented view")
                        for name, value in shown.get("files", {}).items():
                            if self.source("file", name) != value:
                                reasons.append("Presented evidence changed: " + name)
                    for name in sorted(saved.keys() | current.keys()):
                        if name not in current or name not in saved:
                            reasons.append("Scope changed: " + name)
                        elif not current[name] or not saved[name]:
                            reasons.append("Evidence needed: " + name + (": " + self.errors[name] if name in self.errors else ""))
                        elif saved[name] != current[name]:
                            if name == "source:routing_constraints" and isinstance(saved[name], dict):
                                reasons.extend("Changed routing input: " + path for path in changed_keys(saved[name], current[name]))
                            elif (name == "source:render_models" and isinstance(saved[name], dict)
                                  and isinstance(saved[name].get("models"), dict) and saved[name]["models"] != current[name]["models"]):
                                reasons.extend("Changed rendered model: " + path for path in
                                               changed_keys(saved[name]["models"], current[name]["models"]))
                            else:
                                reasons.append("Changed: " + name)
                    result["status"] = "recheck" if reasons else "current"
                    if not reasons:
                        if delegated:
                            reasons.append(f"Delegated to {review['actor']} under autonomous mode; all declared inputs match")
                        else:
                            reasons.append("All declared inputs match the reviewed revision")
        return result

    def blockers(self, key):
        blocked = ["Incomplete workflow history. Import the missing predecessor: " + name for name in self.missing_history(key)]
        for name in self.graph.nodes[key].get("requires_decisions", []):
            if not self.source("decision", name):
                blocked.append(self.errors.get("decision:" + name, "Decision needed: " + name))
        for other in required(self.graph, key):
            item = self.verdict(other)
            if item["status"] != "current":
                blocked.append(f"{other} ({item['status']}): " + "; ".join(item["reasons"]))
        blocked.extend(board_autonomy.delegated_upstream_blockers(self.graph, self, key))
        return blocked

    def report(self):
        return [self.verdict(key) for key, node in self.graph.nodes.items() if tracked(node)]


def lines(items):
    return [f"  {v['card']} | historical pass: {'yes' if v['ever_passed'] else 'no'} | {v['status']}: "
            + "; ".join(v["reasons"]) for v in items]
