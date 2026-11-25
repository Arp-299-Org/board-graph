"""Portable transition records shared by SQLite, Git and the remote writer."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile


FIELDS = ("at", "track", "node", "outcome", "target", "actor", "note", "evidence", "inputs")


def order(row):
    """Receipt timestamps determine replay order. SQLite sequence is a local commit counter."""
    when = datetime.fromisoformat(row["at"].replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when, row.get("record_name") or "", row.get("seq", 0)


def replay(rows):
    # Database-only legacy transitions have no portable identity. Keep their original slots.
    local = sorted(rows, key=lambda row: row["seq"])
    named = iter(sorted((row for row in local if row["record_name"]), key=order))
    return [next(named) if row["record_name"] else row for row in local]


def payload(row):
    return {key: row[key] for key in FIELDS}


def validate(row, source):
    if (not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in FIELDS[:-2])
            or not isinstance(row.get("inputs"), dict) or not isinstance(row.get("evidence"), list)):
        raise ValueError(f"Invalid transition in {source}")
    for entry in row["evidence"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str):
            raise ValueError(f"Invalid cited evidence in {source}")
    order(row)
    return payload(row)


def identical_record(saved, incoming, name):
    changed = [key for key in FIELDS if key != "note" and saved[key] != incoming[key]]
    if changed:
        raise ValueError(f"Conflicting transition {name}: {', '.join(changed)}")


def branch_points(rows):
    """Detect two successors to the same workflow decision. Return missing parents for partial imports."""
    rows = list(rows)
    names = {r["record_name"]: r for r in rows if r.get("record_name")}
    children, missing = {}, {}
    for row in rows:
        link = row.get("inputs", {}).get("workflow_parent")
        if link is None:
            continue
        if (not isinstance(link, dict) or link.get("scope") not in ("track", "card")
                or "name" not in link or link["name"] is not None and not isinstance(link["name"], str)):
            raise ValueError("Invalid parent in workflow record")
        scope = (row["track"], row["node"] if link["scope"] == "card" else None)
        key = (*scope, link["name"])
        if key in children and children[key] != row["record_name"]:
            raise ValueError(f"Conflicting workflow successors {children[key]} and {row['record_name']}")
        children[key] = row["record_name"]
        if link["name"]:
            parent = names.get(link["name"])
            if parent is None:
                missing.setdefault(scope, []).append(link["name"])
            elif (parent["track"] != row["track"] or scope[1] and parent["node"] != scope[1]
                  or order(parent) >= order(row)):
                raise ValueError(f"Invalid workflow predecessor {link['name']} for {row['record_name']}")
    return missing


def publish(path, content):
    """Install a whole immutable record, or recognize the same completed delivery on retry."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".graph-", suffix=".tmp", delete=False) as staged:
        temp = Path(staged.name)
        try:
            staged.write(data)
            staged.flush()
            os.fsync(staged.fileno())
        except BaseException:
            staged.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        try:
            if os.name == "nt":
                os.rename(temp, path)  # Windows refuses to replace an existing destination.
            else:
                os.link(temp, path)
        except FileExistsError:
            if json.loads(path.read_bytes()) != json.loads(data):
                raise ValueError(f"Conflicting delivery at {path}")
    finally:
        temp.unlink(missing_ok=True)
