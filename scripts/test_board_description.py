"""The board description: the default loads, a missing key is refused by name, and a second board's description,
named by --board-description or by BOARD_GRAPH_BOARD, moves every path the graph's scripts derive from it."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import board_description  # noqa: E402

ROOT = HERE.parent

# A second board: another name, folder and file names, and no electrical audit of its own.
OTHER = {
    "name": "x1", "project": "DemoBoard", "folder": "boards/x1",
    "schematic": "boards/x1/main.kicad_sch", "pcb": "boards/x1/main.kicad_pcb",
    "kicad_project": "boards/x1/main.kicad_pro", "rules": "boards/x1/main.kicad_dru",
    "footprint_table": "boards/x1/fp-lib-table", "footprint_library": "boards/x1/xlib.pretty",
    "model_folders": ["boards/x1/3d"], "bom_export": "boards/x1/out/x1-bom.csv",
    "schematic_export": "boards/x1/out/x1-schematic.pdf", "owner_placement": "boards/x1/placement.json",
    "reports": "boards/x1/checks", "production": "boards/x1/fab",
    "drc_exclusions": "boards/x1/checks/drc-accepted.json", "package_name": "DEMO_X1",
    "parts_data": "parts/x1.json", "datasheets": "parts/sheets", "decisions": "boards/x1/WHY.md",
    "board_audit": {"script": "scripts/x1_audit.py", "args": [], "check": "Full board audit (G6)"},
    "hand_solder_notes": {"manual": {}, "factory_parts": [], "when_unpopulated": [], "when_factory": []},
}

# What each script derives, printed by a fresh Python so the module-level reads happen under the environment given.
DERIVED = """
import json, sys
sys.path.insert(0, {scripts!r})
import board_graph, board_graph_actions as a, board_manual_joints, kicad_package_production, routing_run
import validate_board, editor_step
import mac_keep_pace, board_copper_diff, kicad_step_model, board_graph_codex
print(json.dumps({{
    "keep_pace": [mac_keep_pace.BOARD, mac_keep_pace.SCHEMATIC, mac_keep_pace.PROJECT, mac_keep_pace.SCHEMATIC_TITLE],
    "copper_diff": board_copper_diff.BOARD,
    "step_model": [str(kicad_step_model.OUT), kicad_step_model.Step("M").text().splitlines()[2],
                   sorted(kicad_step_model.parts())],
    "joint_names": board_manual_joints.NAMES,
    "review": editor_step.review_reading(),
    "recipe": str(editor_step.RECIPE),
    "codex_excludes": list(board_graph_codex.SOURCE_EXCLUDES[:3]),
    "actions": [a.SCHEMATIC, a.BOARD, a.BOM, a.PLACEMENT, a.PARTS, a.EXCLUSIONS, a.LIBRARY, a.PROJECT_FILE,
                a.TABLE, a.LIBRARY_DIR, list(a.MODEL_DIRS), list(a.DESIGN)],
    "reports": board_graph.REPORTS, "design_folders": list(board_graph.DESIGN_FOLDERS),
    "validate": [str(validate_board.BOARD), str(validate_board.SCHEMATIC), validate_board.PROJECT,
                 validate_board.ELECTRICAL, list(validate_board.BOARD_INPUTS)],
    "joints": [str(board_manual_joints.BOARD), str(board_manual_joints.PARTS)],
    "package": str(kicad_package_production.EXCLUSIONS_DEFAULT),
    "routing": [routing_run.BOARD, routing_run.FOLDER, list(routing_run.LEFT_OUT)],
    "denied": editor_step.DENIED,
}}))
"""


def derived(environ):
    done = subprocess.run([sys.executable, "-c", DERIVED.format(scripts=str(HERE))], capture_output=True, text=True,
                          env=environ, cwd=str(ROOT), check=False)
    if done.returncode:
        raise AssertionError(done.stderr[-2000:])
    return json.loads(done.stdout.strip().splitlines()[-1])


class DefaultTests(unittest.TestCase):
    def test_the_default_description_is_the_projects_board_json(self):
        description = board_description.load(environ={})
        self.assertEqual(description.file, ROOT / "board" / "board.json")
        self.assertEqual(description.name, json.loads(description.file.read_text(encoding="utf-8"))["name"])


class RefusalTests(unittest.TestCase):
    def test_a_missing_key_is_refused_by_name(self):
        for key in board_description.REQUIRED:
            data = dict(OTHER)
            del data[key]
            with self.assertRaisesRegex(board_description.DescriptionError, rf"missing required key {key}\b"):
                board_description.validate(data)

    def test_a_missing_audit_field_is_refused_by_name(self):
        data = dict(OTHER, board_audit={"script": "scripts/x1_audit.py", "args": []})
        with self.assertRaisesRegex(board_description.DescriptionError, r"missing required key board_audit\.check"):
            board_description.validate(data)

    def test_an_absolute_path_is_refused(self):
        with self.assertRaisesRegex(board_description.DescriptionError, "pcb must be relative"):
            board_description.validate(dict(OTHER, pcb="/boards/x1/main.kicad_pcb"))

    def test_a_missing_file_is_a_plain_error(self):
        with self.assertRaisesRegex(board_description.DescriptionError, "cannot be read"):
            board_description.load("no/such/board.json", environ={})

    def test_no_electrical_audit_is_allowed(self):
        self.assertIsNone(board_description.Description(board_description.validate(dict(OTHER)), "x").electrical_audit)

    def test_the_other_optional_keys_may_be_left_out(self):
        description = board_description.Description(board_description.validate(dict(OTHER)), "x", ROOT)
        self.assertEqual((description.part_models, description.manual_joints, description.routing_agent),
                         (None, {}, {}))

    def test_the_other_optional_keys_are_checked(self):
        for data, said in ((dict(OTHER, part_models="/abs/models.py"), "part_models must be relative"),
                           (dict(OTHER, manual_joints=[]), "manual_joints must be an object"),
                           (dict(OTHER, manual_joints={"names": {"U1": ""}}), "manual_joints.names must give"),
                           (dict(OTHER, manual_joints={"clearance_note": 3}), r"manual_joints\.clearance_note must be text"),
                           (dict(OTHER, routing_agent={"docs": "AGENTS.md"}), r"routing_agent\.docs must be a list"),
                           (dict(OTHER, routing_agent={"docs": ["/AGENTS.md"]}), "routing_agent.docs must be relative"),
                           (dict(OTHER, routing_agent={"plan": "/abs/plan.md"}), "routing_agent.plan must be relative"),
                           (dict(OTHER, routing_agent={"recipe": "C:/r.md"}), "routing_agent.recipe must be relative")):
            with self.assertRaisesRegex(board_description.DescriptionError, said):
                board_description.validate(data)

    def test_the_other_optional_keys_are_given_with_absolute_paths(self):
        data = dict(OTHER, part_models="boards/x1/models.py",
                    manual_joints={"names": {"U1": "the brain"}, "clearance_note": "None on record"},
                    routing_agent={"docs": ["README.md"], "plan": "board/ROUTING_PLAN.md", "recipe": "skills/route.md",
                                   "recipe_skill": "route", "sensitive_nets": "the clock"})
        description = board_description.Description(board_description.validate(data), "x", ROOT)
        self.assertEqual(description.part_models, ROOT / "boards/x1/models.py")
        self.assertEqual(description.manual_joints["names"], {"U1": "the brain"})
        printed = description.as_absolute()
        self.assertEqual(printed["part_models"], str(ROOT / "boards/x1/models.py"))
        self.assertEqual(printed["routing_agent"]["docs"], [str(ROOT / "README.md")])
        self.assertEqual(printed["routing_agent"]["plan"], str(ROOT / "board/ROUTING_PLAN.md"))
        self.assertEqual(printed["routing_agent"]["recipe"], str(ROOT / "skills/route.md"))
        self.assertEqual(description.routing_agent["recipe"], "skills/route.md")

    def test_starter_template_defaults_gemini_and_placement_ergonomics(self):
        starter = board_description.load(str(ROOT / "board" / "board.json"), environ={})
        self.assertEqual(starter.routing_agent.get("provider"), "gemini")
        space_text = (ROOT / "board" / "SPACE.md").read_text(encoding="utf-8")
        self.assertIn("tactile control", space_text)
        graph_data = json.loads((ROOT / "tools" / "board_graph.json").read_text(encoding="utf-8"))
        placement = next(n for n in graph_data["nodes"] if n["id"] == "placement")
        self.assertIn("finger/thumb touch clearance", placement["task"])

    def test_no_board_audit_is_allowed(self):
        bare = {key: value for key, value in OTHER.items() if key != "board_audit"}
        for data in (bare, dict(OTHER, board_audit=None)):
            description = board_description.Description(board_description.validate(data), "x")
            self.assertIsNone(description.board_audit)
            self.assertIsNone(description.as_absolute().get("board_audit"))

    def test_gemini_provider_and_review_provider_validation(self):
        for ok in ({"provider": "gemini"}, {"provider": "codex", "review_provider": "gemini"},
                   {"provider": "gemini", "review_provider": "codex"}):
            validated = board_description.validate(dict(OTHER, routing_agent=ok))
            self.assertEqual(validated["routing_agent"], ok)
        for bad in ({"provider": "unknown"}, {"review_provider": "unknown"}, {"review_provider": 3}):
            with self.assertRaises(board_description.DescriptionError):
                board_description.validate(dict(OTHER, routing_agent=bad))


class SecondBoardTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.file = pathlib.Path(self.folder.name) / "x1.json"
        self.file.write_text(json.dumps(OTHER), encoding="utf-8")

    def test_the_argument_comes_before_the_environment_and_the_default(self):
        environ = {board_description.ENV: str(ROOT / "board" / "board.json")}
        self.assertEqual(board_description.find(str(self.file), environ=environ), self.file)
        self.assertEqual(board_description.find(None, environ={board_description.ENV: str(self.file)}), self.file)
        self.assertEqual(board_description.find(None, environ={}), ROOT / "board" / "board.json")

    def test_the_argument_picks_it_up(self):
        parser = board_description.add_argument(__import__("argparse").ArgumentParser())
        args = parser.parse_args(["--board-description", str(self.file)])
        description = board_description.load(args.board_description, environ={})
        self.assertEqual(description.path("pcb"), ROOT / "boards/x1/main.kicad_pcb")
        done = subprocess.run([sys.executable, str(HERE / "board_description.py"), "--board-description",
                               str(self.file)], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["pcb"], str(ROOT / "boards/x1/main.kicad_pcb"))

    def test_validate_board_follows_the_argument_and_skips_the_electrical_audit(self):
        import validate_board

        original = validate_board.DESCRIPTION
        self.addCleanup(validate_board.use, original)
        validate_board.use(board_description.load(str(self.file), environ={}))
        self.assertEqual(validate_board.BOARD, ROOT / "boards/x1/main.kicad_pcb")
        self.assertEqual(validate_board.PROJECT, "boards/x1")
        self.assertIn("boards/x1/", validate_board.BOARD_INPUTS)
        self.assertNotIn("board/", validate_board.BOARD_INPUTS)
        with tempfile.TemporaryDirectory() as out, mock.patch.object(
                validate_board, "kicad_offline",
                return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="")) as offline:
            made = validate_board.check_electrical(pathlib.Path(out), 0)
            self.assertTrue(made["ok"])
            self.assertIn("skipped", made["detail"])
            offline.assert_not_called()
            validate_board.check_exhaustive(pathlib.Path(out), 0)
            self.assertEqual(offline.call_args[0][0], "scripts/x1_audit.py")
        self.assertIn("skipped", validate_board.check_waivers("abc")["detail"])

    def test_a_board_without_its_own_board_audit_skips_that_row_and_that_report(self):
        import kicad_package_production as packager
        import validate_board

        original = validate_board.DESCRIPTION
        self.addCleanup(validate_board.use, original)
        self.file.write_text(json.dumps({k: v for k, v in OTHER.items() if k != "board_audit"}), encoding="utf-8")
        description = board_description.load(str(self.file), environ={})
        validate_board.use(description)
        self.assertNotIn("scripts/x1_audit.py", validate_board.BOARD_INPUTS)
        with tempfile.TemporaryDirectory() as out, mock.patch.object(validate_board, "kicad_offline") as offline:
            made = validate_board.check_exhaustive(pathlib.Path(out), 0)
            offline.assert_not_called()
        self.assertEqual((made["check"], made["ok"]), ("Full board audit (G6)", True))
        self.assertEqual(made["detail"], "skipped: this board's description names no full board audit")
        self.assertNotIn("exhaustive", packager.gate_reports(description))
        self.assertNotIn("electrical", packager.gate_reports(description))

    def test_the_scripts_paths_follow_the_environment(self):
        environ = dict(os.environ)
        environ.pop(board_description.ENV, None)
        default = derived(environ)
        environ[board_description.ENV] = str(self.file)
        other = derived(environ)
        self.assertEqual(default["actions"][1], board_description.load(environ={})["pcb"])
        self.assertEqual(other["actions"], [
            "boards/x1/main.kicad_sch", "boards/x1/main.kicad_pcb", "boards/x1/out/x1-bom.csv",
            "boards/x1/placement.json", "parts/x1.json", "boards/x1/checks/drc-accepted.json", "xlib",
            "boards/x1/main.kicad_pro", "boards/x1/fp-lib-table", "boards/x1/xlib.pretty", ["boards/x1/3d"],
            ["boards/x1/main.kicad_sch", "boards/x1/main.kicad_pcb", "boards/x1/main.kicad_pro",
             "boards/x1/main.kicad_dru", "boards/x1/out/x1-bom.csv"]])
        self.assertEqual(other["reports"], "boards/x1/checks")
        self.assertEqual(other["design_folders"], ["boards/x1", "parts/sheets", "boards/x1/3d"])
        self.assertEqual(other["validate"][:4], [str(ROOT / "boards/x1/main.kicad_pcb"),
                                                 str(ROOT / "boards/x1/main.kicad_sch"), "boards/x1", None])
        self.assertEqual(other["joints"], [str(ROOT / "boards/x1/main.kicad_pcb"), str(ROOT / "parts/x1.json")])
        self.assertEqual(other["package"], str(ROOT / "boards/x1/checks/drc-accepted.json"))
        self.assertEqual(other["routing"], ["boards/x1/main.kicad_pcb", "boards/x1", ["checks", "fab", "out"]])
        self.assertIn("Read(./boards/x1/**)", other["denied"])
        # The Mac's keep-pace run, the copper diff, the STEP writer and the Codex launcher: the second board's.
        self.assertEqual(other["keep_pace"], ["boards/x1/main.kicad_pcb", "boards/x1/main.kicad_sch", "boards/x1",
                                              "main — Schematic Editor"])
        self.assertEqual(other["copper_diff"], "boards/x1/main.kicad_pcb")
        self.assertEqual(other["step_model"], [str(ROOT / "boards" / "x1" / "3d"),
                                               "FILE_DESCRIPTION(('DemoBoard part model from datasheet dimensions'),'2;1');",
                                               []])
        # The part names, the review's reading and the routing recipe: none for the second board, which gives none.
        self.assertEqual(other["joint_names"], {})
        self.assertEqual(other["review"], {"read": "Read", "nets": ""})
        self.assertEqual(other["recipe"], "None")
        self.assertEqual(other["codex_excludes"], ["boards/x1/checks/", "boards/x1/fab/", "boards/x1/out/"])


if __name__ == "__main__":
    unittest.main()
