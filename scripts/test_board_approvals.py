"""Revision-scoped approval comparisons and the ordinary card commands on a disposable graph."""
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
os.environ["BOARD_GRAPH_BOARDS"] = str(SCRIPTS / "fixtures" / "no_boards.json")
os.environ["BOARD_GRAPH_BOARD"] = str(SCRIPTS / "fixtures" / "board.json")
os.environ["BOARD_GRAPH_SITE"] = str(SCRIPTS / "fixtures" / "board_graph_site.json")
import board_graph as bg
import board_graph_actions as actions
from kicad_sexp import children, parse
sys.path.insert(0, str(SCRIPTS.parent / "tools" / "kicad"))
import render_board_views


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.path = self.root / "tools" / "graph.json"
        self.path.parent.mkdir()
        board = self.root / bg.VIEW_FILE
        board.parent.mkdir(parents=True, exist_ok=True)
        self.board = board
        self.design = ('(kicad_pcb (gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts")) '
                       '(footprint "Socket" (layer "F.Cu") (at 2 3) (property "Reference" "J1") '
                       '(property "Supplier Part" "A") (attr smd) (pad "1" smd rect (at 0 0))))')
        board.write_text(self.design)
        (self.root / actions.PROJECT_FILE).write_text('{}')
        (self.root / "mating.txt").write_text("socket faces outward")
        (self.root / "inspection.md").write_text("Inspected the saved render")
        self.data = json.loads((SCRIPTS / "fixtures" / "board_graph.json").read_text())
        self.data.update(approval_mode="observe", docs=[], open_issues=[],
                         tracks=[dict(id="board", label="Board", ordered=True, start="placement")],
                         stages=[dict(id="layout", label="Layout")])
        common = dict(track="board", stage="layout", task="Review this revision", pass_when="reviewed", owns=[], action=None)
        self.data["nodes"] = [
            dict(common, id="placement", label="Placement", who="owner", unchanged_pass="placement",
                 approval_files=["mating.txt"], outcomes=dict(returned=[], **{"pass": "purchasing"})),
            dict(common, id="purchasing", label="Parts", who="agent", approval_view="purchasing",
                 outcomes=dict(returned=["placement"], **{"pass": "routing"})),
            dict(common, id="routing", label="Routing", who="agent", requires_current=["placement"],
                 unchanged_pass="copper_and_silk", approval_view="copper", outcomes=dict(returned=["placement"], **{"pass": "release"})),
            dict(common, id="release", label="Release", who="owner", requires_current=["purchasing", "routing"],
                 outcomes=dict(returned=["placement", "routing", "purchasing"], **{"pass": "done"}))]
        self.save_graph()
        self.state = bg.State(self.root / "db.sqlite3", self.root / "records")

    def save_graph(self):
        self.path.write_text(json.dumps(self.data, indent=2))
        self.graph = bg.Graph(self.path)

    def mode(self, mode):
        self.data["approval_mode"] = mode
        self.save_graph()

    def test_optional_outputs_must_be_owned_and_explicitly_reviewed(self):
        node = self.data["nodes"][1]
        for owned, enabled in (([], True), (["extra.dwg"], False)):
            node.update(owns=owned, approval_owned=enabled, optional_outputs=["extra.dwg"])
            with self.assertRaisesRegex(ValueError, "optional output"):
                self.save_graph()

    def test_required_output_directory_must_contain_a_file(self):
        self.data["nodes"][1].update(approval_owned=True, owns=["drawings"])
        self.mode("enforce")
        self.record("placement", actor="owner")
        folder = self.root / "drawings"
        folder.mkdir()
        with self.assertRaisesRegex(ValueError, "drawings"):
            self.record("purchasing")
        (folder / "drawing.txt").write_text("saved drawing")
        self.record("purchasing")
        self.assertEqual(self.check("purchasing")["status"], "current")

    def render_files(self):
        folder = self.root / "review"
        folder.mkdir(exist_ok=True)
        def render(saved, stage):
            (stage / "top.png").write_bytes(b"test image of " + saved.read_bytes())
        with mock.patch.object(render_board_views, "render_views", side_effect=render):
            render_board_views.render_snapshot(self.board, folder)
        return ["review/board-render.json", "review/top.png"]

    def present(self, state=None):
        files = self.render_files()
        bg.record(self.graph, state or self.state, "placement", "needs_owner", "agent:test",
                  "These are the views presented to the owner", files, root=self.root)
        return files

    def record(self, node, result="pass", actor="agent:test", **kw):
        evidence = ["inspection.md"]
        if (node == "placement" and result == "pass" and not kw.get("attempt_id")
                and self.data["approval_mode"] != "off"):
            evidence = self.present()
        return bg.record(self.graph, self.state, node, result, actor, "inspected", evidence, root=self.root, **kw)

    def check(self, key):
        return bg.approval_review(self.graph, self.state).verdict(key)

    def start(self):
        output = io.StringIO()
        code = bg.next_step(self.graph, self.state, root=self.root, by="agent:test", out=output, err=output,
                            upkeep=lambda *args: True)
        return code, output.getvalue()

    def test_supplier_and_edge_have_distinct_effects(self):
        self.record("placement", actor="owner")
        self.record("purchasing")
        self.mode("enforce")
        self.board.write_text(self.design.replace('"A"', '"B"'))
        self.assertEqual(self.check("placement")["status"], "current")
        self.assertEqual(self.check("purchasing")["reasons"], ["Changed: view:purchasing"])
        self.record("routing")
        with self.assertRaisesRegex(ValueError, "purchasing"):
            self.record("release", actor="owner")
        self.board.write_text(self.design.replace("end 10", "end 9"))
        self.assertIn("Changed: view:placement", self.check("placement")["reasons"])
        self.assertEqual(self.check("purchasing")["status"], "current")
        self.assertIn("Evidence needed: review:placement", self.check("routing")["reasons"])

    def test_shared_decision_blocks_start_and_pass_but_allows_disposition(self):
        self.record("placement", actor="owner")
        self.record("purchasing")
        self.mode("enforce")
        (self.root / "mating.txt").write_text("revised connector requirement")
        self.assertIn("Changed: file:mating.txt", bg.render(bg.where(self.graph, self.state)))
        code, text = self.start()
        self.assertEqual(code, 1)
        self.assertIn("placement", text)
        self.assertFalse(self.state.attempts("routing"))
        with self.assertRaisesRegex(ValueError, "current approvals"):
            self.record("routing", "more_work")
        self.record("routing", "returned", to="placement")
        self.assertEqual(self.check("placement")["status"], "open")
        self.record("placement", actor="owner")
        self.assertEqual(self.check("placement")["status"], "current")

    def test_failed_candidate_and_legacy_automatic_pass_never_grant_review(self):
        self.mode("off")
        self.record("placement", actor="owner")
        self.record("purchasing")
        self.record("routing", "needs_lead")
        self.mode("observe")
        self.assertTrue(self.check("placement")["ever_passed"])
        self.assertEqual(self.check("placement")["status"], "unproven")
        self.assertEqual(self.check("routing")["status"], "open")
        self.assertEqual(self.check("routing")["last_record"]["evidence"][0]["path"], "inspection.md")
        self.state.append(self.graph, "board", "placement", "pass", "purchasing", bg.UNCHANGED, "old reuse")
        self.assertEqual(self.check("placement")["status"], "unproven")
        self.mode("enforce")
        with self.assertRaisesRegex(ValueError, "current original review"):
            bg.approval_inputs(self.graph, self.state, "placement", self.root, outcome="pass", actor=bg.UNCHANGED)

    def test_historical_view_hashes_do_not_imply_physical_changes(self):
        self.state.append(self.graph, "board", "placement", "pass", "purchasing", "owner", "Approved pictures",
                          inputs={"view:placement": "older-algorithm", "mating.txt": "older-file-hash"})
        verdict = self.check("placement")
        self.assertEqual(verdict["status"], "unproven")
        self.assertTrue(any("presented images" in reason for reason in verdict["reasons"]))
        self.assertEqual([r for r in verdict["reasons"] if "differs" in r],
                         ["The recorded file fingerprint differs: file:mating.txt"])

    def test_observe_off_and_missing_file(self):
        (self.root / "mating.txt").unlink()
        self.record("placement", actor="owner")
        self.record("purchasing")
        self.assertEqual(self.check("placement")["status"], "unproven")
        self.mode("enforce")
        self.assertEqual(self.start()[0], 1)
        self.mode("off")
        self.assertEqual(self.start()[0], 2)

    def test_changed_terms_and_new_review_change_a_dependent_receipt(self):
        self.record("placement", actor="owner")
        self.record("purchasing")
        self.record("routing")
        self.record("release", "returned", to="placement", actor="owner")
        self.record("placement", actor="owner")
        self.assertIn("Changed: review:placement", self.check("routing")["reasons"])
        self.data["nodes"][0]["pass_when"] = "reviewed against revised criteria"
        self.save_graph()
        self.assertIn("Changed: card terms", self.check("placement")["reasons"])

    def test_stale_history_cannot_open_an_attempt_or_append_a_pass(self):
        self.mode("enforce")
        saved = bg.approval_inputs(self.graph, self.state, "placement", self.root)
        self.record("placement", "needs_owner")
        with self.assertRaisesRegex(ValueError, "history changed"):
            self.state.open_work(self.graph, "placement", "owner", "", saved, str(self.root))
        with self.assertRaisesRegex(ValueError, "history changed"):
            self.state.append(self.graph, "board", "placement", "pass", "purchasing", "owner", "", inputs=saved)
        self.assertFalse(self.state.attempts("placement"))
        self.assertEqual(len(self.state.transitions()), 1)

    def test_read_only_cli_matches_live_evaluation_without_a_database(self):
        self.record("placement", actor="owner")
        expected = self.check("placement")
        output = io.StringIO()
        args = ["--graph", str(self.path), "--records", str(self.state.records), "approvals", "--json"]
        before = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        with contextlib.redirect_stdout(output):
            self.assertEqual(bg.main(args), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["cards"][0], expected)
        self.assertEqual(report["records"], 2)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()})

    def test_receipt_from_an_attempt_with_changed_terms_cannot_be_adopted(self):
        self.mode("enforce")
        self.assertEqual(self.start()[0], 2)
        attempt = self.state.attempts("placement", open_only=True)[0]["run_id"]
        self.data["nodes"][0]["pass_when"] = "a new requirement"
        self.save_graph()
        with self.assertRaisesRegex(ValueError, "starting terms"):
            self.record("placement", actor="owner", attempt_id=attempt)
        self.record("placement", "needs_lead", attempt_id=attempt)
        self.assertFalse(self.state.attempts("placement", open_only=True))

    def test_automatic_card_requires_the_check_to_have_seen_current_inputs(self):
        self.mode("enforce")
        self.record("placement", actor="owner")
        self.data["nodes"][1].update(who="auto", action="bom")
        self.save_graph()
        with self.assertRaisesRegex(ValueError, "run its card check"):
            self.record("purchasing", actor="script:bom")
        def check(root):
            self.board.write_text(self.design.replace('"A"', '"B"'))
            return {"outcome": "pass", "findings": [], "detail": {}}
        args = dict(root=self.root, actions={"bom": (check, [])}, out=io.StringIO(), err=io.StringIO(),
                    upkeep=lambda *args: True)
        self.assertEqual(bg.next_step(self.graph, self.state, **args), 1)
        self.assertFalse(self.state.passed("purchasing"))
        self.assertEqual(bg.next_step(self.graph, self.state, again=True, **args), 0)
        self.assertEqual(self.check("purchasing")["status"], "current")

    def test_two_boards_can_share_one_decision_and_keep_separate_approvals(self):
        other = bg.State(self.root / "other.sqlite3", self.root / "other-records")
        self.record("placement", actor="owner")
        bg.record(self.graph, other, "placement", "pass", "owner", "second board reviewed",
                  self.present(other), root=self.root)
        self.assertEqual(bg.approval_review(self.graph, other).verdict("placement")["status"], "current")
        (self.root / "mating.txt").write_text("changed for both boards")
        self.assertEqual(self.check("placement")["status"], "recheck")
        self.assertEqual(bg.approval_review(self.graph, other).verdict("placement")["status"], "recheck")
        self.assertNotEqual(self.state.transitions()[0]["at"], other.transitions()[0]["at"])

    def test_bom_reads_the_selected_boards_approved_targets(self):
        # The module's board selection is per process, just as on a real multi-board project.
        listing = self.root / "boards.json"
        listing.write_text(json.dumps({"first": "first", "boards": {
            "first": "board/board.json", "second": "scripts/fixtures/board.json"}}))
        (self.path.parent / "board_graph.second.json").write_text(json.dumps({
            "nodes": [{"id": "bom", "approved_targets": ["second-board-only"]}]}))
        env = dict(os.environ, BOARD_GRAPH_BOARDS=str(listing), BOARD_GRAPH_BOARD="scripts/fixtures/board.json")
        program = ("import sys; from board_graph_actions import Project; "
                   "print(Project(sys.argv[1]).approved_targets())")
        output = subprocess.check_output([sys.executable, "-B", "-c", program, str(self.root)],
                                         env=env, cwd=SCRIPTS, text=True)
        self.assertIn("second-board-only", output)

    def test_owner_signs_only_the_geometry_and_images_presented(self):
        self.mode("enforce")
        def sign(files):
            bg.record(self.graph, self.state, "placement", "pass", "owner", "Approved the top view",
                      files, root=self.root)
        with self.assertRaisesRegex(ValueError, "needs_owner"):
            sign(["inspection.md"])
        files = self.present()
        self.board.write_text(self.design.replace("end 10", "end 9"))
        with self.assertRaisesRegex(ValueError, "changed since presentation"):
            sign(files)
        stale = bg.record(self.graph, self.state, "placement", "needs_owner", "agent:test", "Old render",
                          files, root=self.root)
        self.assertIn("does not match", stale["inputs"]["presentation_finding"])
        self.assertFalse(self.state.passed("placement"))
        files = self.present()
        image = self.root / files[-1]
        original = image.read_bytes()
        image.write_bytes(b"different view")
        with self.assertRaisesRegex(ValueError, "same presented files"):
            sign(files)
        image.write_bytes(original)
        sign(files)
        self.assertEqual(self.check("placement")["status"], "current")
        image.write_bytes(b"replacement")
        self.assertEqual(self.check("placement")["status"], "recheck")

    def test_observe_records_the_move_and_exposes_a_stale_presentation(self):
        files = self.present()
        self.board.write_text(self.design.replace("end 10", "end 9"))
        bg.record(self.graph, self.state, "placement", "pass", "owner", "Sign-off",
                  files, root=self.root)
        self.assertEqual(self.state.position(self.graph, "board"), "purchasing")
        self.assertEqual(self.check("placement")["status"], "unproven")
        self.assertIn("changed since presentation", self.check("placement")["reasons"][0])

    def add_socket_model(self):
        ref = "${KIPRJMOD}/models/socket.step"
        self.design = self.design.replace('(attr smd)', f'(attr smd) (model "{ref}")')
        self.board.write_text(self.design)
        socket = self.board.parent / "models/socket.step"
        socket.parent.mkdir()
        socket.write_bytes(b"eight circuits")
        return ref, socket

    def sign(self, files):
        return bg.record(self.graph, self.state, "placement", "pass", "owner", "Approved the presented view",
                         files, root=self.root)

    def test_model_replacement_between_render_and_presentation_with_explicit_input(self):
        self.mode("enforce")
        ref, socket = self.add_socket_model()
        self.data["nodes"][0]["approval_files"].append(socket.relative_to(self.root).as_posix())
        self.save_graph()
        files = self.render_files()
        receipt = json.loads((self.root / files[0]).read_text())
        self.assertEqual(receipt["models"], {ref: bg.hash_path(socket)})
        socket.write_bytes(b"ten circuits")
        row = bg.record(self.graph, self.state, "placement", "needs_owner", "agent:test", "Old picture",
                        files, root=self.root)
        self.assertIn("rendered models differ", row["inputs"]["presentation_finding"])
        with self.assertRaisesRegex(ValueError, "before sign-off"):
            self.sign(files)
        self.sign(self.present())
        self.assertEqual(self.check("placement")["status"], "current")

    def test_models_are_checked_at_sign_off_and_afterward_without_listing_them(self):
        self.mode("enforce")
        ref, socket = self.add_socket_model()
        files = self.present()
        socket.write_bytes(b"ten circuits")
        with self.assertRaisesRegex(ValueError, "changed since presentation"):
            self.sign(files)
        self.sign(self.present())
        socket.write_bytes(b"eight circuits")
        self.assertIn("Changed rendered model: " + ref, self.check("placement")["reasons"])
        socket.unlink()
        self.assertTrue(any(ref in why for why in self.check("placement")["reasons"]))

    def test_renderer_uses_private_copies_of_every_model_on_a_footprint(self):
        ref, socket = self.add_socket_model()
        other = socket.with_name("shell.step")
        other.write_bytes(b"shell")
        second = ref.replace("socket", "shell")
        self.board.write_text(self.design.replace(f'(model "{ref}")', f'(model "{ref}") (model "{second}")'))
        folder = self.root / "review"
        folder.mkdir()
        def render(saved, stage):
            socket.write_bytes(b"ten circuits")
            fp = children(parse(saved.read_text()), "footprint")[0]
            used = [Path(model[1]) for model in children(fp, "model")]
            self.assertEqual([p.read_bytes() for p in used], [b"eight circuits", b"shell"])
            self.assertTrue(all(p.parent != socket.parent for p in used))
            (stage / "top.png").write_bytes(b"frozen image")
        with mock.patch.object(render_board_views, "render_views", side_effect=render):
            render_board_views.render_snapshot(self.board, folder)
        receipt = json.loads((folder / "board-render.json").read_text())
        self.assertEqual(set(receipt["models"]), {ref, second})
        self.assertNotEqual(receipt["models"][ref], bg.hash_path(socket))

    def test_legacy_render_and_missing_or_unhydrated_models_need_a_fresh_presentation(self):
        self.mode("enforce")
        ref, socket = self.add_socket_model()
        files = self.render_files()
        path = self.root / files[0]
        old = json.loads(path.read_text())
        old.update(schema="board-graph/board-render/v1")
        old.pop("models")
        path.write_text(json.dumps(old))
        row = bg.record(self.graph, self.state, "placement", "needs_owner", "agent:test", "Old picture", files, root=self.root)
        self.assertIn("fresh board render", row["inputs"]["presentation_finding"])
        socket.write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"0" * 64 + b"\nsize 50\n")
        row = bg.record(self.graph, self.state, "placement", "needs_owner", "agent:test", "Unavailable model", files, root=self.root)
        self.assertIn(ref, row["inputs"]["presentation_finding"])

    def test_merge_scopes_read_models_rules_and_settings_from_the_reviewed_revision(self):
        ref, socket = self.add_socket_model()
        socket_path = socket.relative_to(self.root).as_posix()
        inputs = {bg.VIEW_FILE: self.board.read_bytes(), socket_path: socket.read_bytes(),
                  actions.PROJECT_FILE: b'{"net_settings":{"classes":[]}}', actions.RULES: b"reviewed rules"}
        saved_models = actions.render_models(self.root, read=inputs.__getitem__)
        saved_rules = actions.routing_constraints(self.root, read=inputs.__getitem__)
        socket.write_bytes(b"newer model")
        (self.root / actions.RULES).write_bytes(b"newer rules")
        self.assertNotEqual(saved_models, actions.render_models(self.root))
        self.assertNotEqual(saved_rules, actions.routing_constraints(self.root))
        oid = saved_models["models"][ref]
        inputs[socket_path] = f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize 14\n".encode()
        self.assertEqual(saved_models, actions.render_models(self.root, read=inputs.__getitem__))

    def test_changed_rule_during_an_open_attempt_needs_another_check(self):
        self.mode("enforce")
        self.record("placement", actor="owner")
        self.record("purchasing")
        self.assertEqual(self.start()[0], 2)
        attempt = self.state.attempts("routing", open_only=True)[0]["run_id"]
        (self.root / actions.RULES).write_text('(version 1)')
        with self.assertRaisesRegex(ValueError, "starting terms"):
            self.record("routing", attempt_id=attempt)
        self.assertFalse(self.state.passed("routing"))

    def test_rules_and_netclasses_reopen_existing_copper_without_cosmetic_false_alarms(self):
        self.data["nodes"][-1]["id"] = "visual_review"
        self.data["nodes"][2]["outcomes"]["pass"] = "visual_review"
        self.mode("enforce")
        self.board.write_text(self.design[:-1] + ' (segment (start 1 1) (end 2 2) (width 0.20) (layer "F.Cu")))')
        rules = self.root / actions.RULES
        template = '(version 1)\n(rule "Minimum width" (constraint track_width (min {}mm)))'
        rules.write_text(template.format("0.15"))
        project = self.root / actions.PROJECT_FILE
        settings = {"board": {"design_settings": {"rules": {"min_clearance": 0.1}}},
                    "net_settings": {"classes": [{"name": "Default", "track_width": 0.2}]}}
        project.write_text(json.dumps(settings))
        self.record("placement", actor="owner")
        self.record("purchasing")
        self.record("routing")
        rules.write_text(template.format("0.40"))
        self.assertIn("Changed routing input: " + actions.RULES, self.check("routing")["reasons"])
        compare = bg.approval_review(self.graph, self.state)
        self.assertTrue(any("routing (recheck)" in reason for reason in compare.blockers("visual_review")))
        with self.assertRaisesRegex(ValueError, "routing"):
            self.record("visual_review", actor="owner")
        self.assertEqual(self.check("placement")["status"], "current")
        rules.write_text(template.format("0.15"))
        settings["board"]["3dviewports"] = [{"name": "inspection"}]
        settings["net_settings"]["classes"][0]["pcb_color"] = "blue"
        project.write_text(json.dumps(settings, indent=4))
        self.assertEqual(self.check("routing")["status"], "current")
        settings["net_settings"]["classes"][0]["track_width"] = 0.4
        project.write_text(json.dumps(settings))
        self.assertEqual(self.check("routing")["status"], "recheck")
        settings["net_settings"]["classes"][0]["track_width"] = 0.2
        settings["board"]["design_settings"]["rules"]["min_clearance"] = 0.5
        project.write_text(json.dumps(settings))
        self.assertEqual(self.check("routing")["status"], "recheck")
        project.write_text("{broken")
        self.assertEqual(self.check("routing")["status"], "recheck")

    def test_renderer_hashes_the_private_snapshot_and_only_new_images(self):
        folder = self.root / "render"
        folder.mkdir()
        (folder / "stale.png").write_bytes(b"left over")
        before = bg.hash_path(self.board)
        def render(saved, stage):
            self.board.write_text(self.design.replace("end 10", "end 9"))
            self.assertEqual(bg.hash_path(saved), before)
            (stage / "top.png").write_bytes(b"fresh")
        with mock.patch.object(render_board_views, "render_views", side_effect=render):
            render_board_views.render_snapshot(self.board, folder)
        receipt = json.loads((folder / "board-render.json").read_text())
        self.assertEqual(receipt["board_sha256"], before)
        self.assertNotEqual(receipt["board_sha256"], bg.hash_path(self.board))
        self.assertEqual(receipt["images"], {"top.png": bg.hash_path(folder / "top.png")})

    def test_field_id_and_silk_edits_preserve_routing_but_copper_changes_do_not(self):
        self.design = self.design.replace('(property "Reference" "J1")',
            '(property "Reference" "J1" (at 0 1) (layer "F.SilkS") (uuid old-id))')
        self.design = self.design[:-1] + ' (segment (start 1 1) (end 2 2) (width 0.2) (layer "F.Cu")))'
        self.board.write_text(self.design)
        self.record("placement", actor="owner")
        self.record("purchasing")
        self.record("routing")
        self.mode("enforce")
        silk = bg.VIEWS["silkscreen"][0]
        changed_id = self.design.replace("old-id", "new-id")
        self.board.write_text(changed_id)
        self.assertEqual(self.check("routing")["status"], "current")
        self.assertEqual(self.check("placement")["status"], "current")
        self.assertEqual(self.check("purchasing")["status"], "current")
        self.assertEqual(silk(self.design), silk(changed_id))
        moved_label = changed_id.replace("at 0 1", "at 0 2")
        self.board.write_text(moved_label)
        self.assertEqual(self.check("routing")["status"], "current")
        self.assertNotEqual(silk(self.design), silk(moved_label))
        wider_trace = moved_label.replace("width 0.2", "width 0.3")
        self.board.write_text(wider_trace)
        self.assertIn("Changed: view:copper", self.check("routing")["reasons"])
        self.assertEqual(silk(moved_label), silk(wider_trace))

    def test_invalid_scope_is_refused(self):
        self.data["nodes"][0]["approval_files"] = ["../outside"]
        with self.assertRaisesRegex(ValueError, "project-relative"):
            self.save_graph()

    def test_owned_reading_doc_can_be_edited_during_open_attempt_and_still_tracked_after_pass(self):
        decisions = self.root / "decisions.md"
        truth = self.root / "truth.md"
        decisions.write_text("initial decisions\n", encoding="utf-8")
        truth.write_text("product truth\n", encoding="utf-8")
        self.record("placement", actor="owner")
        self.data["nodes"][1].update(approval_owned=True, docs=["decisions.md", "truth.md"], owns=["decisions.md"])
        self.mode("enforce")

        self.assertEqual(self.start()[0], 2)
        attempt1 = self.state.attempts("purchasing", open_only=True)[0]["run_id"]
        truth.write_text("edited non-owned truth mid-attempt\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "starting terms"):
            self.record("purchasing", actor="agent:test", attempt_id=attempt1)
        self.record("purchasing", "needs_lead", actor="agent:test", attempt_id=attempt1)

        self.assertEqual(self.start()[0], 2)
        attempt2 = self.state.attempts("purchasing", open_only=True)[0]["run_id"]
        decisions.write_text("updated decisions during open attempt\n", encoding="utf-8")
        self.record("purchasing", actor="agent:test", attempt_id=attempt2)
        self.assertEqual(self.check("purchasing")["status"], "current")

        decisions.write_text("changed decisions after pass\n", encoding="utf-8")
        verdict = self.check("purchasing")
        self.assertNotEqual(verdict["status"], "current")
        self.assertIn("Changed: owned:decisions.md", verdict["reasons"])

    def test_board_views_normalize_270_vs_minus_90_and_omitted_zero_angles(self):
        import board_graph_actions as bga

        canonical = (
            '(kicad_pcb\n'
            '  (footprint "lib:C" (layer "F.Cu") (at 69 44.5 270)\n'
            '    (property "Reference" "C8" (at 0 1.68 270) (layer "F.SilkS"))\n'
            '    (property "Value" "4.7uF" (at 0 -1.43 270) (layer "F.Fab"))\n'
            '    (pad "1" smd roundrect (at -0.775 0 270) (size 0.8 0.9) (layers "F.Cu"))\n'
            '  )\n'
            '  (footprint "lib:U" (layer "F.Cu") (at 10 20 0)\n'
            '    (property "Reference" "U1" (at 0 -2 0) (layer "F.SilkS"))\n'
            '    (pad "1" smd rect (at 0 0 0) (size 0.5 0.5) (layers "F.Cu"))\n'
            '  )\n'
            ')\n'
        )
        kicad10_saved = (
            '(kicad_pcb\n'
            '  (footprint "lib:C" (layer "F.Cu") (at 69 44.5 -90)\n'
            '    (property "Reference" "C8" (at 0 1.68 -90) (layer "F.SilkS"))\n'
            '    (property "Value" "4.7uF" (at 0 -1.43 -90) (layer "F.Fab"))\n'
            '    (pad "1" smd roundrect (at -0.775 0 -90) (size 0.8 0.9) (layers "F.Cu"))\n'
            '  )\n'
            '  (footprint "lib:U" (layer "F.Cu") (at 10 20)\n'
            '    (property "Reference" "U1" (at 0 -2) (layer "F.SilkS"))\n'
            '    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu"))\n'
            '  )\n'
            ')\n'
        )
        rotated = canonical.replace("(at 69 44.5 270)", "(at 69 44.5 90)")
        for name in ("placement", "silkscreen", "copper", "copper_and_silk"):
            fn = bga.VIEWS[name][0]
            self.assertEqual(fn(canonical), fn(kicad10_saved), name)
            self.assertNotEqual(fn(canonical), fn(rotated), name)


if __name__ == "__main__":
    unittest.main()
