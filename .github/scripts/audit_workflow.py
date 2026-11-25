"""Run the same observable workflow claims against any board-graph checkout. Uses disposable local state."""
import argparse
import hashlib
import subprocess
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--json", action="store_true")
    options = parser.parse_args()
    scripts = options.checkout.resolve() / "scripts"
    fixture = scripts / "fixtures"
    os.environ.update(BOARD_GRAPH_BOARD=str(fixture / "board.json"),
                      BOARD_GRAPH_SITE=str(fixture / "board_graph_site.json"),
                      BOARD_GRAPH_BOARDS=str(fixture / "no_boards.json"))
    sys.path.insert(0, str(scripts))
    import board_graph as bg
    cases = {}

    class Project:
        def __init__(self, root):
            self.root = root
            (root / "tools").mkdir()
            self.data = json.loads((fixture / "board_graph.json").read_text())
            self.data.update(approval_mode="enforce", docs=[], open_issues=[], stages=[dict(id="build", label="Build")],
                             tracks=[dict(id="board", label="Board", ordered=True, start="inspect")])
            common = dict(stage="build", track="board", docs=[], task="Inspect evidence",
                          pass_when="matches saved revision", owns=[], action=None)
            self.data["nodes"] = [
                dict(common, id="inspect", label="Inspection", who="agent", approval_files=["part.txt"],
                     outcomes={"pass": "release", "returned": []}),
                dict(common, id="release", label="Release", who="owner", requires=["inspect"],
                     outcomes={"pass": "done", "returned": ["inspect"]})]
            self.path = root / "tools/graph.json"
            self.write_graph()
            self.state = bg.State(root / "database", root / "records")
            for name in ("part.txt", "report.txt", "order.txt", "reading.md"):
                self.write(name, "original")
        def write(self, name, content):
            (self.root / name).write_text(content, encoding="utf-8")
        def write_graph(self):
            self.path.write_text(json.dumps(self.data), encoding="utf-8")
            self.graph = bg.Graph(self.path)
        def record(self, node="inspect", outcome="pass", actor="agent:a", **kwargs):
            return bg.record(self.graph, self.state, node, outcome, actor, "inspected",
                             ["report.txt" if node == "inspect" else "order.txt"], root=self.root, **kwargs)
        def release(self):
            return self.record("release", actor="owner")
        def start(self, actor="agent:a"):
            output = io.StringIO()
            code = bg.next_step(self.graph, self.state, root=self.root, by=actor, out=output, err=output,
                                upkeep=lambda *args: True)
            return code, output.getvalue()

    def case(name):
        def add(fn):
            cases[name] = fn
            return fn
        return add

    def refuses(fn):
        try:
            fn()
        except ValueError:
            return True
        return False

    @case("Fresh review authorizes release")
    def fresh(p):
        p.record()
        p.release()
        return p.state.position(p.graph, "board") == "done"

    @case("Unrelated edit preserves approval")
    def unrelated(p):
        p.record()
        p.write("other.txt", "changed")
        p.release()
        return p.state.position(p.graph, "board") == "done"

    @case("Changed reviewed input blocks release")
    def changed(p):
        p.record()
        p.write("part.txt", "replacement")
        return refuses(p.release) and p.state.position(p.graph, "board") == "release"

    @case("A changed cited report blocks release")
    def report(p):
        p.record()
        p.write("report.txt", "contradicts the review")
        return refuses(p.release)

    @case("A missing cited report blocks release")
    def missing(p):
        p.record()
        (p.root / "report.txt").unlink()
        return refuses(p.release)

    @case("A passing inspection log cannot replace the required drawing")
    def required_output(p):
        p.data["nodes"][0].update(approval_owned=True, owns=["drawing.pdf"])
        p.write_graph()
        if not refuses(p.record):
            return False
        p.write("drawing.pdf", "the drawing")
        p.record()
        (p.root / "drawing.pdf").unlink()
        return refuses(p.release)

    @case("An explicitly optional output may be absent and later changes reopen review")
    def optional_output(p):
        p.data["nodes"][0].update(approval_owned=True, owns=["drawing.pdf", "overlay.pdf"], optional_outputs=["overlay.pdf"])
        p.write_graph()
        p.write("drawing.pdf", "the drawing")
        p.record()
        p.record("release", "more_work", "owner")
        p.write("overlay.pdf", "alternative sheet")
        return refuses(p.release)

    @case("Changed required reading invalidates the review")
    def reading(p):
        p.data["nodes"][0]["docs"] = ["reading.md"]
        p.write_graph()
        p.record()
        p.write("reading.md", "new minimum clearance")
        return refuses(p.release)

    @case("Legacy pass needs a review before enforced release")
    def legacy(p):
        p.state.append(p.graph, "board", "inspect", "pass", "release", "agent:a", "old format", export=True)
        return refuses(p.release)

    @case("Owner authority survives a return")
    def authority(p):
        p.record()
        p.record("release", "returned", "owner", to="inspect")
        p.record()
        return refuses(lambda: p.record("release", actor="agent:a"))

    @case("Changed prerequisites during an open attempt block completion")
    def inflight(p):
        p.record()
        p.start("owner")
        attempt, = p.state.attempts("release", open_only=True)
        p.write("part.txt", "replacement")
        blocked = refuses(lambda: p.record("release", actor="owner", attempt_id=attempt["run_id"]))
        if not blocked:
            return False
        p.record("release", "needs_lead", "owner", attempt_id=attempt["run_id"])
        return blocked and not p.state.attempts("release", open_only=True)

    @case("A stale attempt cannot close its successor")
    def retry(p):
        p.start()
        first, = p.state.attempts("inspect", open_only=True)
        p.record(outcome="needs_lead", attempt_id=first["run_id"])
        p.start()
        second, = p.state.attempts("inspect", open_only=True)
        return refuses(lambda: p.record(attempt_id=first["run_id"])) and (
            p.state.attempts("inspect", open_only=True)[0]["run_id"] == second["run_id"])

    @case("Committed record replay agrees with live position")
    def replay(p):
        p.record()
        p.release()
        rebuilt = bg.State(p.root / "restored", p.root / "records")
        return rebuilt.position(p.graph, "board") == p.state.position(p.graph, "board") == "done"

    @case("Out-of-order arrival agrees with a clean replay")
    def late(p):
        p.record()
        p.release()
        files = sorted((p.root / "records").glob("*.json"))
        inbox = p.root / "inbox"
        inbox.mkdir()
        (inbox / files[1].name).write_bytes(files[1].read_bytes())
        restored = bg.State(p.root / "recovered", inbox)
        (inbox / files[0].name).write_bytes(files[0].read_bytes())
        restored.import_records()
        return restored.position(p.graph, "board") == "done" and [r["node"] for r in restored.transitions()] == ["inspect", "release"]

    @case("Divergent decisions at the same position cannot silently combine")
    def fork(p):
        p.record()
        base = p.state
        p.state = bg.State(p.root / "parallel", p.root / "parallel-records")
        p.record()
        source, = (p.root / "parallel-records").glob("*.json")
        (p.root / "records" / source.name).write_bytes(source.read_bytes())
        return refuses(base.import_records)

    @case("Incomplete history blocks further work until its parent arrives")
    def partial(p):
        p.record()
        p.record("release", "returned", "owner", to="inspect")
        first, second = sorted((p.root / "records").glob("*.json"))
        inbox = p.root / "inbox"
        inbox.mkdir()
        (inbox / second.name).write_bytes(second.read_bytes())
        p.state = bg.State(p.root / "replay", inbox)
        if not refuses(p.record):
            return False
        (inbox / first.name).write_bytes(first.read_bytes())
        p.state.import_records()
        p.record()
        return p.state.position(p.graph, "board") == "release"

    @case("A conflicting copy of the same transition is detected")
    def conflict(p):
        p.record()
        f, = (p.root / "records").glob("*.json")
        row = json.loads(f.read_bytes())
        row["target"] = "done"
        f.write_text(json.dumps(row), encoding="utf-8")
        return refuses(p.state.import_records)

    @case("An open shared product decision blocks issuing dependent work")
    def open_decision(p):
        spec = p.data["nodes"][0]
        spec["requires_decisions"] = ["socket.json"]
        p.write("socket.json", json.dumps(dict(schema="board-graph/product-decision/v1", status="open")))
        p.write_graph()
        p.start()
        return not p.state.attempts("inspect", open_only=True)

    @case("Changing a shared decision invalidates dependent boards")
    def shared(p):
        p.data["nodes"][0]["requires_decisions"] = ["socket.json"]
        approved = dict(schema="board-graph/product-decision/v1", status="accepted", owner="owner",
                        decision="Key faces outward.", acceptance="Inspect both assembled sockets.")
        p.write("socket.json", json.dumps(approved))
        p.write_graph()
        first = p.state
        p.record()
        p.state = bg.State(p.root / "second", p.root / "second-records")
        p.record()
        p.write("socket.json", json.dumps(dict(approved, decision="Key faces inward.")))
        b = refuses(p.release)
        p.state = first
        return b and refuses(p.release)

    def first_article(p, change):
        p.data["tracks"][0]["start"] = "product_judgment"
        p.data["nodes"] = [dict(p.data["nodes"][1], id="product_judgment", requires=[], requires_decisions=["socket.json"],
                                release_check="first_article", integration=dict(boards={"motor":"motor.pcb","base":"base.pcb"},
                                firmware="firmware.bin"), outcomes={"pass":"done","returned":[]})]
        p.write_graph()
        for file in ("motor.pcb", "base.pcb", "firmware.bin"):
            p.write(file, file)
        def sha(file):
            return hashlib.sha256((p.root / file).read_bytes()).hexdigest()
        p.write("socket.json", json.dumps(dict(schema="board-graph/product-decision/v1", status="accepted", owner="owner",
                            decision="Key faces outward.", acceptance="Both sockets mate in the enclosure.")))
        observation = dict(evidence="report.txt", passed=True, observation="Both sockets mated in unit A.",
                           decision="socket.json", decision_sha256=sha("socket.json"))
        receipt = dict(schema="board-graph/product-acceptance/v1", verdict="accepted",
                       board_revisions={n:sha(n+".pcb") for n in ("motor","base")},
                       units=[dict(id="A", firmware=sha("firmware.bin"), checks=[observation])])
        if change == "failed":
            observation["passed"] = False
        if change == "decision":
            observation.pop("decision_sha256")
        p.write("assembly.json", json.dumps(receipt))
        if change in ("motor.pcb", "firmware.bin"):
            p.write(change, "different revision")
        def finish():
            return bg.record(p.graph, p.state, "product_judgment", "pass", "owner", "Assembled unit tested",
                             ["report.txt","assembly.json"], root=p.root)
        if change:
            return refuses(finish) and p.state.position(p.graph, "board") == "product_judgment"
        return finish()["target"] == "done"

    for title, change in [("Tested first article can finish",""), ("Failed bench observation blocks release","failed"),
                          ("Changed motor board blocks assembled acceptance","motor.pcb"),
                          ("Changed firmware blocks assembled acceptance","firmware.bin"),
                          ("Assembly must test the shared owner decision","decision")]:
        case(title)(lambda p, change=change: first_article(p, change))

    def copper(p, change):
        import board_copper_origin
        def git(*args):
            return subprocess.run(["git", *args], cwd=p.root, check=True, capture_output=True).stdout.decode().strip()
        git("init", "-q")
        for key, value in (("user.name","Workflow test"), ("user.email","test@example.test"), ("core.autocrlf","false")):
            git("config", key, value)
        pcb = "motor.kicad_pcb"
        p.write(pcb, "(kicad_pcb)")
        (p.root / "tasks").mkdir()
        p.write("tasks/1.txt", "Connection: SP1.1 J1.1 /MOTOR_A\nPad exits: centered")
        git("add", pcb, "tasks")
        git("commit", "-q", "-m", "Start")
        base = git("rev-parse", "HEAD")
        p.write(pcb, '(kicad_pcb (segment (start 1 1) (end 2 2) (width 0.2) (layer "F.Cu")))')
        digest = hashlib.sha256((p.root / pcb).read_bytes()).hexdigest()
        pads = ["SP1.1","J1.1"]
        proof = dict(schema="board-graph/routing-review/v1", board_sha256=digest, reviewer="independent",
                     answer="PAD SP1.1: centered.\nPAD J1.1: centered.\nNOTHING.", warnings=[], earlier=[])
        step = dict(task="1.txt", kept=True, why="editor_work", provider="codex", review_provider="codex", review_model="independent",
                    reviews=1, review_open=[], review_receipt=proof, reviewed_sha256=digest, pad_exits=[dict(pad=k,issues=[]) for k in pads],
                    connection={"from":pads[0],"to":pads[1],"net":"/MOTOR_A","before":False,"after":True,"saved_sha256":digest})
        summary = dict(summary=True, adopted=True, kept=1, tasks=1, pcb=pcb, outcome="more_work", provider="codex", commit=base, task_folder="tasks")
        if change in ("accepted_warning", "unresolved_warning"):
            warning = "route quality: warning microsegment"
            measured = warning + ", 0.0031 mm within a wider end cap"
            step.update(why="warning_accepted", new_findings=[warning], matching_geometry=[measured])
            proof["warnings"] = [measured]
            if change == "accepted_warning":
                proof["answer"] += "\nFINE AS IS: Warning 1: The short segment lies inside the wider end cap."
        if change == "hash":
            step["reviewed_sha256"] = "0" * 64
        if change == "review":
            step.pop("review_receipt")
        if change == "contradiction":
            proof["answer"] = proof["answer"].replace("PAD J1.1: centered.", "PAD J1.1: MUST FIX: kink under pad.")
        if change == "task":
            step["connection"]["to"] = "J2.1"
        if change == "pad":
            step["pad_exits"].pop()
        p.write("run-example.jsonl", "\n".join(map(json.dumps, (step,summary))))
        git("add", pcb, "run-example.jsonl")
        git("commit", "-q", "-m", "Deliver reviewed board")
        findings = board_copper_origin.unaccounted(p.root, pcb, p.root / "records", base=base)
        return bool(findings) == (change not in ("", "accepted_warning"))

    for title, change in [("Reviewed assigned copper passes the Git provenance gate",""), ("Git rejects mismatched review hash","hash"),
                          ("Git rejects a missing independent verdict","review"), ("Git preserves a pad MUST FIX","contradiction"),
                          ("Git rejects a substituted named connection","task"), ("Git requires both requested pad observations","pad"),
                          ("A reasoned warning acceptance keeps the reviewed board","accepted_warning"),
                          ("A warning without the reviewer disposition blocks adoption","unresolved_warning")]:
        case(title)(lambda p, change=change: copper(p, change))

    results = []
    for name, fn in cases.items():
        try:
            with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
                passed = fn(Project(Path(directory)))
            results.append(dict(case=name, status="pass" if passed else "failed"))
        except Exception as error:
            results.append(dict(case=name, status="error", detail=f"{type(error).__name__}: {error}"))
    if options.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            print(result["status"], result["case"], result.get("detail", ""))
    return int(any(row["status"] != "pass" for row in results))


if __name__ == "__main__":
    sys.exit(main())
