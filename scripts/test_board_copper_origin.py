#!/usr/bin/env python3
"""Tests for scripts/board_copper_origin.py, on small git repositories made here: no KiCad and no Mac."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import board_copper_origin as origin  # noqa: E402

PCB = "board/x.kicad_pcb"
RECORDS = "tools/board_graph_records"


def track(x, width="0.2", uuid="u"):
    return (f'(segment (start {x} 0) (end {x} 5) (width {width}) (layer "F.Cu") (net "/A") (uuid "{uuid}-{x}"))')


def via(x):
    return f'(via (at {x} 9) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "/A") (uuid "v-{x}"))'


def pour(x, fill=""):
    return (f'(zone (net "GND") (layer "F.Cu") (uuid "z-{x}") (polygon (pts (xy {x} 0) (xy {x} 9) (xy 9 9))) {fill})')


def board(*items, layers=("F.Cu", "B.Cu")):
    stack = " ".join(f'({index} "{name}" signal)' for index, name in enumerate(layers))
    return (f'(kicad_pcb (version 1) (generator "t") (layers {stack} (25 "Edge.Cuts" user)) '
            f'(footprint "R" (layer "F.Cu") (at 1 1)) {" ".join(items)})\n')


class Repo:
    def __init__(self, folder):
        self.root = pathlib.Path(folder)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))  # write_text has no newline before Python 3.10

    def commit(self, message, **files):
        for name, text in files.items():
            self.write(name, text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def start(self, *items, **options):
        """main with the board, taken as origin/main, and a branch to work on."""
        self.commit("the board", **{PCB: board(*items, **options), "tasks/01.txt": "Repair: cleanup"})
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("checkout", "-q", "-b", "work")

    def run_commit(self, text, adopted=True, name="board/routing/run-12345678.jsonl"):
        """What scripts/routing_run.py commits: the board and its run file together."""
        digest = origin.sha256(text.encode("utf-8"))
        proof = dict(schema="board-graph/routing-review/v1", board_sha256=digest, reviewer="reviewer",
                     answer="NOTHING.", earlier=[], warnings=[])
        step = dict(task="01.txt", kept=adopted, why="editor_work", provider="codex", reviewed_sha256=digest,
                    reviews=1, review_open=[], review_model="reviewer", review_provider="codex", review_receipt=proof)
        lines = [json.dumps(step), json.dumps(dict(summary=True, adopted=adopted, outcome="more_work", pcb=PCB,
                                                   kept=int(adopted), tasks=1, provider="codex", commit=self.git("rev-parse", "HEAD"), task_folder="tasks"))]
        return self.commit("Routing run 12345678: 1 of 1 steps kept", **{PCB: text, name: "\n".join(lines) + "\n"})

    def owner_record(self, actor="owner", node=origin.CARD):
        digest = origin.sha256((self.root / PCB).read_bytes())
        self.write(f"{RECORDS}/r-{actor}-{node}.json".replace(":", "-"),
                   json.dumps({"node": node, "actor": actor, "outcome": "more_work", "inputs": {PCB: digest}}))

    def found(self, **options):
        return origin.lines(origin.unaccounted(self.root, PCB, self.root / RECORDS, **options))


class CopperOrigin(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Repo(folder.name)

    def test_copper_a_script_wrote_is_a_finding_that_names_its_commit(self):
        repo = self.repo
        repo.start(track(1))
        commit = repo.commit("Joiner: its ten motor wires routed", **{PCB: board(track(1), track(2), track(3), via(2))})
        self.assertEqual(repo.found(), [f"2 tracks, 1 via added outside the routing run, by commit {commit[:8]} "
                                        "(Joiner: its ten motor wires routed)"])

    def test_copper_the_routing_run_adopted_is_accounted_for(self):
        repo = self.repo
        repo.start(track(1))
        repo.run_commit(board(track(1), track(2), via(2)))
        self.assertEqual(repo.found(), [])

    def test_a_run_file_that_adopted_nothing_accounts_for_no_copper(self):
        repo = self.repo
        repo.start()
        repo.run_commit(board(track(2)), adopted=False)
        self.assertEqual(len(repo.found()), 1)

    def test_a_file_only_named_like_a_run_file_accounts_for_nothing(self):
        repo = self.repo
        repo.start()
        repo.commit("copper", **{PCB: board(track(2)), "board/routing/run-1.jsonl": "not json\n"})
        self.assertEqual(len(repo.found()), 1)

    def test_removed_copper_and_a_resave_with_new_ids_are_no_finding(self):
        repo = self.repo
        repo.start(track(1), track(2), pour(1))
        repo.commit("resaved", **{PCB: board(track(1, uuid="other"), pour(1, fill="(filled_polygon (pts (xy 0 0)))"))})
        self.assertEqual(repo.found(), [])

    def test_a_pour_and_a_wider_track_count_as_added(self):
        repo = self.repo
        repo.start(track(1))
        repo.commit("pours", **{PCB: board(track(1, width="0.5"), pour(1))})
        self.assertIn("1 track, 1 pour added outside the routing run", repo.found()[0])

    def test_copper_taken_off_again_is_no_longer_a_finding(self):
        repo = self.repo
        repo.start(track(1))
        repo.commit("scripted", **{PCB: board(track(1), track(2))})
        repo.commit("taken off", **{PCB: board(track(1))})
        self.assertEqual(repo.found(), [])

    def test_a_prior_run_cannot_bless_copper_reintroduced_outside_routing(self):
        repo = self.repo
        repo.start(track(1))
        repo.run_commit(board(track(1), track(2)))
        repo.commit("remove", **{PCB: board(track(1))})
        repo.commit("reintroduced without review", **{PCB: board(track(1), track(2))})
        self.assertIn("reintroduced without review", repo.found()[0])

    def test_the_same_copper_laid_again_by_a_routing_run_is_accounted_for(self):
        repo = self.repo
        repo.start(track(1))
        repo.commit("scripted", **{PCB: board(track(1), track(2))})
        repo.commit("taken off", **{PCB: board(track(1))})
        repo.run_commit(board(track(1), track(2)))
        self.assertEqual(repo.found(), [])

    def test_the_owners_own_record_on_the_routing_card_accepts_what_led_up_to_it(self):
        repo = self.repo
        repo.start(track(1))
        repo.commit("scripted", **{PCB: board(track(1), track(2))})
        repo.owner_record()
        repo.commit("the owner's record")
        self.assertEqual(repo.found(), [])
        later = repo.commit("scripted again", **{PCB: board(track(1), track(2), track(3))})
        self.assertEqual(repo.found(), [f"1 track added outside the routing run, by commit {later[:8]} (scripted again)"])

    def test_an_agents_record_and_the_owners_record_on_another_card_accept_nothing(self):
        repo = self.repo
        repo.start(track(1))
        repo.commit("scripted", **{PCB: board(track(1), track(2))})
        repo.owner_record(actor="agent:claude")
        repo.owner_record(node="placement")
        self.assertEqual(len(repo.found()), 1)

    def test_copper_not_committed_yet_is_a_finding_unless_only_the_commit_is_read(self):
        repo = self.repo
        repo.start(track(1))
        repo.write(PCB, board(track(1), via(4)))
        self.assertEqual(repo.found(), ["1 via added outside the routing run, not committed yet"])
        self.assertEqual(repo.found(worktree=False), [])

    def test_a_change_of_copper_layers_is_a_finding_and_a_new_boards_layers_are_not(self):
        repo = self.repo
        repo.start(track(1))
        commit = repo.commit("four layers", **{PCB: board(track(1), layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))})
        self.assertEqual(repo.found(), ["the board's copper layers changed to 4 (F.Cu, In1.Cu, In2.Cu, B.Cu) without "
                                        f"the owner's record, by commit {commit[:8]} (four layers)"])
        repo.owner_record()
        self.assertEqual(repo.found(), [])
        repo.commit("another board", **{"board/y.kicad_pcb": board(layers=("F.Cu", "In1.Cu", "B.Cu"))})
        self.assertEqual(origin.unaccounted(repo.root, "board/y.kicad_pcb", repo.root / RECORDS), [])

    def test_copper_merged_in_from_main_is_not_the_branchs(self):
        repo = self.repo
        repo.start(track(1))
        repo.commit("docs", **{"notes.md": "x\n"})
        repo.git("checkout", "-q", "main")
        repo.commit("main's own copper", **{PCB: board(track(1), track(7))})
        repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
        repo.git("checkout", "-q", "work")
        repo.git("merge", "-q", "--no-edit", "main")
        self.assertEqual(repo.found(), [])

    def test_with_no_base_to_compare_with_there_is_nothing_to_find(self):
        repo = self.repo
        repo.commit("the board", **{PCB: board(track(1))})
        self.assertEqual(repo.found(), [])
        self.assertEqual(origin.unaccounted(repo.root.parent / "nowhere-at-all", PCB), [])

    def test_a_file_that_is_not_a_board_cannot_be_read(self):
        with self.assertRaises(origin.Unreadable):
            origin.copper("(kicad_sch)")

    def test_cross_provider_adopted_run_accounts_for_copper(self):
        repo = self.repo
        repo.start(track(1))
        text = board(track(1), track(2))
        digest = origin.sha256(text.encode("utf-8"))
        proof = dict(schema="board-graph/routing-review/v1", board_sha256=digest, reviewer="gemini-4-neon-high",
                     answer="NOTHING.", earlier=[], warnings=[])
        step = dict(task="01.txt", kept=True, why="editor_work", provider="codex", review_provider="gemini",
                    model="gpt-6.1-terra", review_model="gemini-4-neon-high", reviewed_sha256=digest,
                    reviews=1, review_open=[], review_receipt=proof)
        summary = dict(summary=True, adopted=True, outcome="more_work", pcb=PCB, kept=1, tasks=1,
                       provider="codex", review_provider="gemini", commit=repo.git("rev-parse", "HEAD"), task_folder="tasks")
        repo.commit("Cross-provider routing run", **{
            PCB: text,
            "board/routing/run-cross123.jsonl": "\n".join(map(json.dumps, (step, summary))) + "\n",
        })
        self.assertEqual(repo.found(), [])

    def test_multi_connection_chunk_adopted_run_accounts_for_copper(self):
        repo = self.repo
        repo.start(track(1))
        start_commit = repo.commit("Add chunk task", **{
            "tasks_chunk/01-chunk.txt": "Connection: J1.1 J2.1 /M1_A\nConnection: J1.1 J2.2 /M1_A\nPad exits: centered\n",
        })
        text = board(track(1), track(2), track(3))
        digest = origin.sha256(text.encode("utf-8"))
        proof = dict(schema="board-graph/routing-review/v1", board_sha256=digest, reviewer="gemini-4-neon-high",
                     answer="PAD J1.1: straight.\nPAD J2.1: straight.\nPAD J2.2: straight.\nNOTHING.",
                     warnings=[], earlier=[])
        step = dict(task="01-chunk.txt", kept=True, why="editor_work", provider="codex", review_provider="gemini",
                    model="gpt-6.1-terra", review_model="gemini-4-neon-high", reviewed_sha256=digest,
                    reviews=1, review_open=[], review_receipt=proof,
                    pad_exits=[{"pad": p, "issues": []} for p in ("J1.1", "J2.1", "J2.2")],
                    connections=[
                        {"from": "J1.1", "to": "J2.1", "net": "/M1_A", "before": False, "after": True, "saved_sha256": digest},
                        {"from": "J1.1", "to": "J2.2", "net": "/M1_A", "before": False, "after": True, "saved_sha256": digest},
                    ])
        summary = dict(summary=True, adopted=True, outcome="more_work", pcb=PCB, kept=1, tasks=1,
                       provider="codex", review_provider="gemini", commit=start_commit, task_folder="tasks_chunk")
        repo.commit("Multi-connection chunk run", **{
            PCB: text,
            "board/routing/run-chunk123.jsonl": "\n".join(map(json.dumps, (step, summary))) + "\n",
        })
        self.assertEqual(repo.found(), [])
        text2 = board(track(1), track(2), track(3), track(4))
        digest2 = origin.sha256(text2.encode("utf-8"))
        proof2 = dict(proof, board_sha256=digest2)
        bad_step = dict(step, reviewed_sha256=digest2, review_receipt=proof2,
                        connections=[dict(step["connections"][0], saved_sha256=digest2),
                                     {"from": "J1.1", "to": "J9.9", "net": "/M1_A", "before": False, "after": True, "saved_sha256": digest2}])
        repo.commit("Tamper receipt on new copper", **{
            PCB: text2,
            "board/routing/run-chunk456.jsonl": "\n".join(map(json.dumps, (bad_step, summary))) + "\n",
        })
        self.assertNotEqual(repo.found(), [])

    def test_ipc_mode_adopted_run_enforces_declared_mode(self):
        repo = self.repo
        repo.start(track(1))
        start_commit = repo.commit("Add ipc chunk task", **{
            "tasks_ipc/01-ipc.txt": "Mode: ipc\nConnection: J1.1 J2.1 /M1_A\nPad exits: centered\n",
        })
        text = board(track(1), track(2))
        digest = origin.sha256(text.encode("utf-8"))
        proof = dict(schema="board-graph/routing-review/v1", board_sha256=digest, reviewer="gemini-4-neon-high",
                     answer="PAD J1.1: straight.\nPAD J2.1: straight.\nNOTHING.", warnings=[], earlier=[])
        step = dict(task="01-ipc.txt", kept=True, why="editor_work", routing_mode="ipc",
                    provider="codex", review_provider="gemini", model="gpt-6.1-terra", review_model="gemini-4-neon-high",
                    reviewed_sha256=digest, reviews=1, review_open=[], review_receipt=proof,
                    pad_exits=[{"pad": p, "issues": []} for p in ("J1.1", "J2.1")],
                    connection={"from": "J1.1", "to": "J2.1", "net": "/M1_A", "before": False, "after": True, "saved_sha256": digest})
        summary = dict(summary=True, adopted=True, outcome="more_work", pcb=PCB, kept=1, tasks=1,
                       provider="codex", review_provider="gemini", commit=start_commit, task_folder="tasks_ipc")
        repo.commit("IPC routing run", **{
            PCB: text,
            "board/routing/run-ipc123.jsonl": "\n".join(map(json.dumps, (step, summary))) + "\n",
        })
        self.assertEqual(repo.found(), [])
        text2 = board(track(1), track(2), track(3))
        digest2 = origin.sha256(text2.encode("utf-8"))
        wrong_mode_step = dict(step, routing_mode="interactive", reviewed_sha256=digest2,
                               review_receipt=dict(proof, board_sha256=digest2),
                               connection=dict(step["connection"], saved_sha256=digest2))
        repo.commit("Wrong mode receipt", **{
            PCB: text2,
            "board/routing/run-ipc456.jsonl": "\n".join(map(json.dumps, (wrong_mode_step, summary))) + "\n",
        })
        self.assertNotEqual(repo.found(), [])


if __name__ == "__main__":
    unittest.main()
