#!/usr/bin/env python3
"""Where the board's copper came from: every track, arc, via and pour a branch adds must come through the Routing
card's routing run, or be accepted by the owner's own record on that card.

  python3 scripts/board_copper_origin.py [--base origin/main] [--board <name>]

The Routing card's tool (scripts/routing_run.py) commits the board it adopts together with its run file,
run-<attempt>.jsonl, whose last line says `"adopted": true`. This reads the branch's commits since --base that
touch the board and takes each commit's added copper: what the board holds there that none of the commit's parents
held. Copper a commit adds without such a run file beside it was laid some other way: a script that writes the board
file, the route tool run by hand, a hand edit, another router. That copper is a finding while the board still holds
it, unless a routing run laid the same copper again, or the owner accepted it.

The owner accepts copper laid another way with the owner's own record on the Routing card: the record keeps the
board file's hash, and every commit up to the one whose board has that hash is accepted.

A change to the board's copper layers (two to four, say) outside a routing run is a finding the same way: how many
layers a board has is the owner's decision. A new board's first layers are not a change.

Copper is compared by what it is, not by its id: a track's ends, width and layer, a via's place, size, hole and
layers, a pour's net, layers and outline. A pour's fill is redrawn by every refill and does not count; a rule area
is not copper. Removing copper is never a finding.

What it cannot see: who typed a record or wrote a run file. A hand-written run file or a record typed as the owner
passes it; it stops copper that goes around the card, not a forged record.

Exit 0 when all the copper is accounted for, 1 when some is not, 2 when it could not be read.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import re
import json
import pathlib
import subprocess
import sys

SCRIPTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import tool_paths  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

ROOT = tool_paths.project_root()
from routing_evidence import adoption_problem  # noqa: E402
import kicad_rules_geom as rg  # noqa: E402
import kicad_sexp  # noqa: E402
from kicad_sexp import child, copper_layers as _board_copper_layers, parse  # noqa: E402
from no_window import KWARGS as NO_WINDOW  # noqa: E402

CARD = "routing_batch"  # the Routing card: the owner's record there accepts copper
OWNER = "owner"
BASE = "origin/main"
TRACKS = ("segment", "arc", "via")
LAYERS = "layers"  # the board's copper layers, kept beside its copper: how many is the owner's decision
IDS = ("uuid", "tstamp", "locked", "net", "net_name")  # which item it is, never what copper it is
WORDS = {"segment": "track", "arc": "arc", "via": "via", "zone": "pour"}
# What to do about a finding, said wherever one is shown.
WAY_OUT = ("A card whose tool does not work is not worked around: record needs_lead on the Routing card and say what "
           "the tool refused, take this copper off, and bring the choice to the owner. No other router and no script "
           "that writes the board lays copper; only the owner's own record on the Routing card accepts copper laid "
           "another way, and so does a change of copper layers.")


# What the Routing card's brief says every time, to whoever is on it.
WARNING = ("WARNING, read before anything else. Opus models are banned from routing: an Opus model does not route, "
           "start the routing run, or record on this card; it says so and stops. The routing run refuses one. "
           "it says so and stops. The routing run refuses one. Whatever the model: agents on this card have avoided "
           "the KiCad editor by every means, writing tracks with scripts, pitching other routers, and calling scripted "
           "copper \"routed by hand\". All of it is forbidden. Copper is laid in the editor by this card's routing "
           "run and no other way. If the run fails, stop and report it; do not find another way.")


class Unreadable(Exception):
    """git or the board could not be read; the message says which."""


def frozen(item):
    return tuple(frozen(part) for part in item) if isinstance(item, list) else item


def _canonical_copper_atom(part):
    if isinstance(part, list):
        head = part[0] if part else None
        if head in ("start", "end", "mid", "at", "width", "size", "drill"):
            return (head, *(rg.canonical_float_atom(x) for x in part[1:]))
        if head == "xy":
            return ("xy", *(rg.canonical_float_atom(x) for x in part[1:]))
        return tuple(_canonical_copper_atom(x) for x in part)
    return part


def copper(text):
    """The board's copper as a Counter of (kind, what it is): tracks, arcs and vias without their ids and nets,
    and each pour by its net, layers and outline. An empty or missing board has none."""
    found = collections.Counter()
    if not text or not text.strip():
        return found
    root = parse(text)
    if not isinstance(root, list) or not root or root[0] != "kicad_pcb":
        raise Unreadable("not a KiCad board")
    net_map = rg.board_net_map(root)
    board_cu = _board_copper_layers(root) or ["F.Cu", "B.Cu"]
    for item in root[1:]:
        if not isinstance(item, list) or not item:
            continue
        kind = item[0]
        if kind in TRACKS:
            norm = kicad_sexp.normalize_angles(item)
            kept = [part for part in norm[1:]
                    if not (isinstance(part, list) and part and part[0] in IDS) and part != "locked"]
            if kind == "segment":
                p0 = rg.canonical_point(norm, "start")
                p1 = rg.canonical_point(norm, "end")
                if p0 is not None and p1 is not None:
                    a, b = (p0, p1) if p0 <= p1 else (p1, p0)
                    kept = [p for p in kept if not (isinstance(p, list) and p and p[0] in ("start", "end"))]
                    kept.extend((["start", a[0], a[1]], ["end", b[0], b[1]]))
            parts_key = tuple(sorted((_canonical_copper_atom(p) for p in kept), key=repr))
            found[(kind, parts_key)] += 1
        elif kind == "layers":
            names = tuple(part[1] for part in item[1:] if isinstance(part, list) and len(part) > 1
                          and isinstance(part[1], str) and part[1].endswith(".Cu"))
            if names:
                found[(LAYERS, names)] += 1
        elif rg.is_copper_pour_node(item):
            znet = rg.resolve_item_net(item, net_map)
            lnode = child(item, "layers") or child(item, "layer")
            zlayers = rg.expand_copper_layers([str(tok) for tok in (lnode[1:] if lnode else [])], board_cu)
            polys = tuple(
                tuple(
                    (rg.canonical_float_atom(xy[1]), rg.canonical_float_atom(xy[2]))
                    for xy in kicad_sexp.children(child(poly, "pts") or [], "xy")
                    if len(xy) >= 3
                )
                for poly in kicad_sexp.children(item, "polygon")
            )
            found[(kind, (znet, zlayers, polys))] += 1
    return found


def counted(items):
    """A Counter of copper in words: "70 tracks, 20 vias, 2 pours"."""
    kinds = collections.Counter()
    for (kind, _), count in items.items():
        kinds[kind] += count
    return ", ".join(f"{kinds[kind]} {WORDS[kind]}{'' if kinds[kind] == 1 else 's'}"
                     for kind in ("segment", "arc", "via", "zone") if kinds[kind])


def said(items):
    """What a commit did outside the routing run, in words."""
    parts = []
    if counted(items):
        parts.append(f"{counted(items)} added outside the routing run")
    for (kind, names), _ in items.items():
        if kind == LAYERS:
            parts.append(f"the board's copper layers changed to {len(names)} ({', '.join(names)}) without the "
                         "owner's record")
    return " and ".join(parts)


def git(root, *args, check=True):
    done = subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=False, **NO_WINDOW)
    if done.returncode and check:
        raise Unreadable(f"git {' '.join(args)}: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout if done.returncode == 0 else None


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class Board:
    """The board file at any commit, each distinct version read once."""

    def __init__(self, root, pcb):
        self.root, self.pcb, self.seen = root, pcb, {}

    def at(self, commit):
        """(the file's hash, its copper) at `commit`; a commit without the board has no copper."""
        data = git(self.root, "show", f"{commit}:{self.pcb}", check=False)
        return self.read(data)

    def read(self, data):
        if data is None:
            return None, collections.Counter()
        digest = sha256(data)
        if digest not in self.seen:
            self.seen[digest] = copper(data.decode("utf-8"))
        return digest, self.seen[digest]


def run_file(root, commit, pcb, digest):
    """The adopted routing run a commit carries: the name of a run-*.jsonl the commit adds or changes whose
    summary line says the run's board was adopted, else None."""
    names = git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--root", "-z", commit)
    for name in dict.fromkeys(names.decode("utf-8").split("\0")):
        leaf = name.rsplit("/", 1)[-1]
        if not (leaf.startswith("run-") and leaf.endswith(".jsonl")):
            continue
        text = git(root, "show", f"{commit}:{name}", check=False)
        try:
            rows = [json.loads(line) for line in (text or b"").decode("utf-8").splitlines() if line.strip()]
            if not rows or not all(isinstance(row, dict) for row in rows):
                continue
            summary = rows[-1]
            if (summary.get("summary") is True and summary.get("adopted") is True and summary.get("pcb") == pcb
                    and isinstance(summary.get("commit"), str) and isinstance(summary.get("task_folder"), str)):
                start, folder = summary["commit"], summary["task_folder"]
                if not re.fullmatch("[0-9a-f]{40,64}", start) or git(root, "merge-base", "--is-ancestor", start, commit, check=False) is None:
                    continue
                entries = git(root, "ls-tree", "-z", "--name-only", f"{start}:{folder}", check=False)
                if not entries:
                    continue
                tasks = {n: git(root, "show", f"{start}:{folder}/{n}").decode("utf-8")
                         for n in entries.decode("utf-8").rstrip("\0").split("\0")}
                if not adoption_problem(rows[:-1], summary, digest, summary.get("provider"), tasks):
                    return name
        except (UnicodeError, ValueError, TypeError):
            continue
    return None


def accepted_hashes(records, pcb):
    """The board file hashes the owner's own records on the Routing card keep: each accepts the copper the board
    held then."""
    found = set()
    folder = pathlib.Path(records)
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
        try:
            row = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(row, dict) and row.get("node") == CARD and row.get("actor") == OWNER and row.get("outcome") in ("pass", "more_work"):
            digest = (row.get("inputs") or {}).get(pcb)
            if digest:
                found.add(digest)
    return found


def unaccounted(root, pcb, records=None, base=BASE, worktree=True, strict=False):
    """The copper the branch added outside a routing run and still holds, as a list of
    {"commit", "subject", "added" (a Counter), "words"}; `commit` is None for copper not committed yet.
    Empty when every piece is accounted for, and when `base` is not a commit here (nothing to compare with)."""
    root = pathlib.Path(root)
    if not root.is_dir() or git(root, "rev-parse", "-q", "--verify", f"{base}^{{commit}}", check=False) is None:
        if strict and (root / pcb).is_file() and any(k[0] != LAYERS for k in copper((root / pcb).read_text(encoding="utf-8"))):
            raise Unreadable(f"Cannot establish copper origin without the comparison ref {base}")
        return []
    board = Board(root, pcb)
    accepted = accepted_hashes(records, pcb) if records else set()
    head_hash, head = board.at("HEAD")
    file = root / pcb
    here_hash, here = board.read(file.read_bytes()) if worktree and file.is_file() else (head_hash, head)
    if here_hash in accepted:
        return []
    additions, blessed = [], []
    listed = git(root, "rev-list", "--reverse", "--topo-order", f"{base}..HEAD", "--", pcb).decode("ascii").split()
    for commit in listed:
        digest, now = board.at(commit)
        if digest in accepted:
            blessed.append(commit)
        parents = git(root, "rev-parse", f"{commit}^@", check=False)
        before = collections.Counter()
        for parent in (parents or b"").decode("ascii").split():
            before |= board.at(parent)[1]
        added = now - before
        if not any(kind == LAYERS for kind, _ in before):  # a board's first layers are no change
            added = collections.Counter({key: count for key, count in added.items() if key[0] != LAYERS})
        if not added:
            continue
        additions.append((commit, added, bool(run_file(root, commit, pcb, digest))))
    if blessed:  # the owner accepted the board at these commits: what led up to each is accepted with it
        additions = [(commit, added, reviewed or any(
            git(root, "merge-base", "--is-ancestor", commit, tip, check=False) is not None for tip in blessed))
                     for commit, added, reviewed in additions]
    if here_hash != head_hash and here - head:
        loose = here - head
        if not any(kind == LAYERS for kind, _ in head):
            loose = collections.Counter({key: count for key, count in loose.items() if key[0] != LAYERS})
        additions.append((None, loose, False))
    findings, remaining = [], here.copy()
    for commit, added, reviewed in reversed(additions):  # the newest first: copper laid twice is the later commit's
        still = added & remaining  # copper taken off again is no finding
        remaining -= still
        if reviewed or not still:
            continue
        subject = (git(root, "show", "-s", "--format=%s", commit).decode("utf-8", "replace").strip()
                   if commit else "not committed yet")
        findings.append({"commit": commit, "subject": subject, "added": still,
                         "words": said(still) + (f", by commit {commit[:8]} ({subject})" if commit
                                                 else ", not committed yet")})
    findings.reverse()
    return findings


def lines(found):
    """The findings as the lines a card, a check or a refusal shows; empty when there are none."""
    return [item["words"] for item in found]


def main(argv=None):
    import board_description
    import boards
    boards.take_argument()
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    boards.add_argument(parser)
    parser.add_argument("--base", default=BASE, help="the branch this one merges into")
    args = parser.parse_args(argv)
    description = board_description.load()
    try:
        found = unaccounted(ROOT, description.rel("pcb"), ROOT / boards.chosen(ROOT).records, args.base, strict=True)
    except (Unreadable, ValueError) as error:
        print(f"board_copper_origin: {error}", file=sys.stderr)
        return 2
    if not found:
        print(f"Copper origin: every track, via and pour added since {args.base} came through the routing run "
              "or was accepted by the owner")
        return 0
    print("Copper origin: FAIL")
    for line in lines(found):
        print(f"  {line}")
    print(WAY_OUT)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
