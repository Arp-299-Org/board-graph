"""The site settings: the default file loads, a missing key is refused by name, and another
file named by BOARD_GRAPH_SITE moves what mac.py and the scripts that read it derive."""
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import site_settings  # noqa: E402
from no_window import KWARGS as NO_WINDOW  # noqa: E402

ROOT = HERE.parent

# Another project's machine setup on another machine: every value differs from the default fixture.
OTHER = {
    "kicad_machine": {
        "ssh_host": "kicad@other-mac", "ssh_key": "~/.ssh/id_other", "checkout": "~/other-project",
        "steps": "~/other-steps", "editor_lock": "~/other-steps/BUSY.d", "worktree_prefix": "~/other-",
        "cache": "~/.cache/other-mac", "disposable_copies": "/tmp/other-",
    },
    "graph_page": {"address": "http://other-mac:8100", "launchd_label": "other.board-graph-page"},
    "state_database": "var/graph/state.sqlite3",
}

# What each script derives, printed by a fresh Python so the module-level reads happen under the environment given.
DERIVED = """
import json, sys
sys.path.insert(0, {scripts!r})
import mac, board_graph, board_graph_serve, routing_run, tidy, kicad_route
print(json.dumps({{
    "mac": [mac.HOST, str(mac.KEY), mac.CHECKOUT, mac.OPENED],
    "state": [board_graph.STATE_IN, str(board_graph.place(environ={{}})["path"])],
    "page": [board_graph_serve.ADDRESS, board_graph_serve.HOST, board_graph_serve.PORT, board_graph_serve.LABEL],
    "routing": [routing_run.LOCK, routing_run.STEPS, mac.SITE.worktree("board/route-keys")],
    "tidy": list(tidy.DISPOSABLE), "cache": kicad_route.CACHE,
}}))
"""


def derived(environ):
    done = subprocess.run([sys.executable, "-c", DERIVED.format(scripts=str(HERE))], capture_output=True, text=True,
                          env=environ, cwd=str(ROOT), check=False, **NO_WINDOW)
    if done.returncode:
        raise AssertionError(done.stderr[-2000:])
    return json.loads(done.stdout.strip().splitlines()[-1])


def clean_environment():
    """This process's environment without the site settings' variable."""
    names = {site_settings.ENV}
    return {key: value for key, value in os.environ.items() if key not in names}


class DefaultTests(unittest.TestCase):
    def test_the_default_file_loads(self):
        self.assertEqual(site_settings.load(environ={}).file, ROOT / "tools" / "board_graph_site.json")


class OneMachineTests(unittest.TestCase):
    """A project with no KiCad machine: the settings say so, and this machine holds the database."""

    def load(self, data):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp).resolve()
            (root / "tools").mkdir()
            (root / "tools/board_graph_site.json").write_text(json.dumps(data), encoding="utf-8")
            return root, site_settings.load(root, {})

    def test_the_file_needs_the_database_alone(self):
        root, found = self.load({"one_machine": True, "state_database": "logs/graph/state.sqlite3"})
        self.assertTrue(found.one_machine)
        self.assertEqual((found.checkout, found.state_database, found.ssh_host, found.page_address,
                          found.launchd_label),
                         (root.as_posix(), "logs/graph/state.sqlite3", "", "http://127.0.0.1:8000", ""))

    def test_the_pages_address_and_job_may_be_given(self):
        _, found = self.load({"one_machine": True, "state_database": "s.sqlite3",
                              "graph_page": {"address": "http://127.0.0.1:9000/", "launchd_label": "my.page"}})
        self.assertEqual((found.page_address, found.launchd_label), ("http://127.0.0.1:9000", "my.page"))

    def test_a_project_with_a_kicad_machine_is_not_one_machine(self):
        self.assertFalse(site_settings.load(environ={}).one_machine)

    def test_wrong_shapes_are_refused(self):
        for data, said in (({"one_machine": True}, "missing required key state_database"),
                           ({"one_machine": "yes", "state_database": "s"}, "one_machine must be true or false"),
                           (dict(OTHER, one_machine=True), "kicad_machine cannot be set"),
                           ({"one_machine": True, "state_database": "/abs/s"}, "relative to the repository's main"),
                           ({"one_machine": True, "state_database": "s", "graph_page": {"address": "nowhere"}},
                            "graph_page.address must be a URL"),
                           ({"state_database": "s"}, "missing required key kicad_machine")):
            with self.subTest(data=data), self.assertRaisesRegex(site_settings.SiteError, said):
                site_settings.validate(copy.deepcopy(data))


class RefusalTests(unittest.TestCase):
    def test_a_missing_key_is_refused_by_name(self):
        for section, keys in site_settings.REQUIRED.items():
            data = copy.deepcopy(OTHER)
            del data[section]
            with self.assertRaisesRegex(site_settings.SiteError, rf"missing required key {section}\b"):
                site_settings.validate(data)
            for key in keys:
                data = copy.deepcopy(OTHER)
                del data[section][key]
                with self.assertRaisesRegex(site_settings.SiteError, rf"missing required key {section}\.{key}\b"):
                    site_settings.validate(data)
        for key in site_settings.TOP:
            data = copy.deepcopy(OTHER)
            del data[key]
            with self.assertRaisesRegex(site_settings.SiteError, rf"missing required key {key}\b"):
                site_settings.validate(data)

    def test_an_empty_value_and_a_wrong_address_are_refused(self):
        data = copy.deepcopy(OTHER)
        data["kicad_machine"]["checkout"] = ""
        with self.assertRaisesRegex(site_settings.SiteError, r"kicad_machine\.checkout must be text"):
            site_settings.validate(data)
        data = copy.deepcopy(OTHER)
        data["graph_page"]["address"] = "other-mac:8100"
        with self.assertRaisesRegex(site_settings.SiteError, r"graph_page\.address must be a URL"):
            site_settings.validate(data)

    def test_the_pages_python_is_optional_and_text_when_given(self):
        self.assertEqual(site_settings.Settings(site_settings.validate(copy.deepcopy(OTHER)), "x").page_python,
                         "/usr/bin/python3")
        data = copy.deepcopy(OTHER)
        data["graph_page"]["python"] = "/opt/homebrew/bin/python3"
        self.assertEqual(site_settings.Settings(site_settings.validate(data), "x").page_python,
                         "/opt/homebrew/bin/python3")
        data["graph_page"]["python"] = ""
        with self.assertRaisesRegex(site_settings.SiteError, r"graph_page\.python must be text"):
            site_settings.validate(data)

    def test_a_missing_file_is_a_plain_error(self):
        with self.assertRaisesRegex(site_settings.SiteError, "cannot be read"):
            site_settings.load(environ={site_settings.ENV: "no/such/site.json"})


class OtherSiteTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.file = pathlib.Path(self.folder.name) / "site.json"
        self.file.write_text(json.dumps(OTHER), encoding="utf-8")

    def test_the_environment_variable_names_the_file(self):
        self.assertEqual(site_settings.find(environ={site_settings.ENV: str(self.file)}), self.file)
        self.assertEqual(site_settings.find(environ={}), ROOT / "tools" / "board_graph_site.json")
        self.assertEqual(site_settings.load(environ={site_settings.ENV: str(self.file)}).ssh_host, "kicad@other-mac")

    def test_mac_and_the_scripts_follow_the_file(self):
        got = derived(dict(clean_environment(), **{site_settings.ENV: str(self.file)}))
        self.assertEqual(got["mac"], ["kicad@other-mac", str(pathlib.Path("~/.ssh/id_other").expanduser()),
                                      "~/other-project", "$HOME/.cache/other-mac/opened"])
        self.assertEqual(got["state"][0], "var/graph/state.sqlite3")
        on_the_mac = str(pathlib.Path("~/other-project").expanduser() / "var/graph/state.sqlite3")
        self.assertEqual(got["state"][1], on_the_mac if sys.platform == "darwin"
                         else "kicad@other-mac:~/other-project/var/graph/state.sqlite3")
        self.assertEqual(got["page"], ["http://other-mac:8100", "other-mac", 8100, "other.board-graph-page"])
        self.assertEqual(got["routing"], ["~/other-steps/BUSY.d", "~/other-steps", "~/other-route-keys"])
        self.assertEqual(got["tidy"], ["/private/tmp/other-", "/tmp/other-"])
        self.assertEqual(got["cache"], "~/.cache/other-mac")


class EnvHelperTests(unittest.TestCase):
    def test_neutral_env_helpers_resolve_variables(self):
        self.assertEqual(site_settings.env("BOARD_GRAPH_STATE", environ={"BOARD_GRAPH_STATE": "new.sqlite3"}),
                         "new.sqlite3")
        self.assertEqual(site_settings.env_name("BOARD_GRAPH_TEST_KICAD", environ={"BOARD_GRAPH_TEST_KICAD": "1"}),
                         "BOARD_GRAPH_TEST_KICAD")
        self.assertIsNone(site_settings.env("BOARD_GRAPH_STATE", environ={}))
        self.assertEqual(site_settings.both_names("BOARD_GRAPH_ATTEMPT_ID", "a1"),
                         {"BOARD_GRAPH_ATTEMPT_ID": "a1"})


if __name__ == "__main__":
    unittest.main()
