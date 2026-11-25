#!/usr/bin/env python3
"""The graph page's server on the Mac: what it answers and how, its launchd job's plist, and its status line."""
import io
import json
import os
import pathlib
import plistlib
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
# Fixture machine settings and page plist, so every checkout reads the same address and job.
os.environ["BOARD_GRAPH_SITE"] = str(FIXTURES / "board_graph_site.json")
PLIST = FIXTURES / "demo.board-graph-page.plist"
import board_graph_serve  # noqa: E402
import launchd_job  # noqa: E402
import site_settings  # noqa: E402

RUNNING = ("gui/501/demo.board-graph-page = {\n\tactive count = 1\n\tpath = /Users/builder/Library/LaunchAgents/x\n"
           "\tstate = running\n\n\tprogram = /usr/bin/python3\n\tpid = 4242\n\truns = 3\n"
           "\tdomain = {\n\t\tstate = active\n\t}\n}\n")
STOPPED = "gui/501/demo.board-graph-page = {\n\tstate = not running\n\tprogram = /usr/bin/python3\n\truns = 24\n}\n"


class ServerTests(unittest.TestCase):
    """The folder's files come back with no browser cache and no log line; answers() says what it found."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.directory.name)
        (self.folder / "view.json").write_text(json.dumps({"where": {"tracks": []}}), encoding="utf-8")
        (self.folder / "index.html").write_text("<p>page</p>", encoding="utf-8")
        self.server = board_graph_serve.server("127.0.0.1", 0, self.folder)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.directory.cleanup()

    def test_view_json_comes_back_with_no_cache_and_no_log_line(self):
        err = io.StringIO()
        with redirect_stderr(err), urllib.request.urlopen(f"{self.address}/view.json", timeout=5) as reply:
            self.assertEqual(reply.headers["Cache-Control"], "no-cache")
            self.assertEqual(json.loads(reply.read()), {"where": {"tracks": []}})
        with redirect_stderr(err), urllib.request.urlopen(f"{self.address}/", timeout=5) as reply:
            self.assertIn(b"<p>page</p>", reply.read())
        self.assertEqual(err.getvalue(), "")

    def test_answers_says_yes_a_missing_file_and_a_server_that_is_gone(self):
        self.assertEqual(board_graph_serve.answers(self.address), "yes")
        self.remove(self.folder / "view.json")
        self.assertEqual(board_graph_serve.answers(self.address), "HTTP 404")
        (self.folder / "view.json").write_text("{half", encoding="utf-8")
        self.assertTrue(board_graph_serve.answers(self.address).startswith("no ("))
        self.server.shutdown()
        self.server.server_close()
        self.assertTrue(board_graph_serve.answers(self.address, timeout=2).startswith("no ("))

    @staticmethod
    def remove(path: pathlib.Path) -> None:
        """Unlink `path`, waiting up to a second for the server's thread to close it: the reply reaches this side
        before that thread lets go of the file, and Windows will not remove a file another handle holds open."""
        for _ in range(50):
            try:
                path.unlink()
                return
            except PermissionError:
                time.sleep(0.02)
        path.unlink()

    @staticmethod
    def event(stream) -> dict:
        """The next event on an open /events stream: its data line, then the blank line that ends it."""
        line = stream.readline()
        while line.startswith(b":"):  # a keepalive comment
            stream.readline()
            line = stream.readline()
        data = json.loads(line[len(b"data: "):])
        stream.readline()
        return data

    def test_an_open_page_is_told_the_stamps_when_the_mac_says_the_folder_changed(self):
        os.utime(self.folder / "index.html", (1_700_000_000, 1_700_000_000))
        os.utime(self.folder / "view.json", (1_700_000_100, 1_700_000_100))
        stream = urllib.request.urlopen(f"{self.address}/events", timeout=5)
        self.addCleanup(stream.close)
        self.assertEqual(stream.headers["Content-Type"], "text/event-stream")
        self.assertEqual(self.event(stream), {"view": 1_700_000_100, "page": 1_700_000_000})
        self.assertEqual(board_graph_serve.listening(self.address), 1)
        os.utime(self.folder / "view.json", (1_700_000_200, 1_700_000_200))
        self.assertIsNone(board_graph_serve.notify(self.address))
        self.assertEqual(self.event(stream), {"view": 1_700_000_200, "page": 1_700_000_000})
        os.utime(self.folder / "index.html", (1_700_000_300, 1_700_000_300))
        self.assertIsNone(board_graph_serve.notify(self.address))
        self.assertEqual(self.event(stream), {"view": 1_700_000_200, "page": 1_700_000_300})

    def test_the_stamps_are_whole_seconds_and_zero_for_a_missing_file(self):
        os.utime(self.folder / "index.html", (1_700_000_000.7, 1_700_000_000.7))
        (self.folder / "view.json").unlink()
        self.assertEqual(board_graph_serve.stamps(self.folder), {"view": 0, "page": 1_700_000_000})

    def test_the_stamps_carry_the_newest_notes_file_when_the_graphs_keep_notes(self):
        os.utime(self.folder / "index.html", (1_700_000_000, 1_700_000_000))
        os.utime(self.folder / "view.json", (1_700_000_000, 1_700_000_000))
        notes = [self.folder / "one.md", self.folder / "absent.md"]
        notes[0].write_text("an idea\n", encoding="utf-8")
        os.utime(notes[0], (1_700_000_400, 1_700_000_400))
        self.assertEqual(board_graph_serve.stamps(self.folder, notes),
                         {"view": 1_700_000_000, "page": 1_700_000_000, "notes": 1_700_000_400})

    def test_only_the_mac_itself_may_say_the_folder_changed(self):
        self.assertTrue(board_graph_serve.local("127.0.0.1", "198.51.100.10"))
        self.assertTrue(board_graph_serve.local("198.51.100.10", "198.51.100.10"))
        self.assertFalse(board_graph_serve.local("198.51.100.20", "198.51.100.10"))
        with mock.patch.object(board_graph_serve, "local", return_value=False):
            self.assertEqual(board_graph_serve.notify(self.address), "HTTP 403")
        request = urllib.request.Request(f"{self.address}/view.json", data=b"", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(refused.exception.code, 404)

    def test_notify_and_listening_say_when_the_server_is_gone(self):
        self.assertEqual(board_graph_serve.listening(self.address), 0)
        self.server.shutdown()
        self.server.server_close()
        self.assertTrue(board_graph_serve.notify(self.address, timeout=2).startswith("no ("))
        self.assertIsNone(board_graph_serve.listening(self.address, timeout=2))

    def test_files_route_serves_pdfs_rendered_docs_csv_tables_and_folders_and_refuses_private_paths(self):
        repo = self.folder / "repo"
        (repo / "board" / "exports").mkdir(parents=True)
        (repo / "hardware" / "reference").mkdir(parents=True)
        (repo / "docs").mkdir(parents=True)
        (repo / "logs").mkdir(parents=True)
        (repo / ".git").mkdir(parents=True)
        (repo / "board" / "exports" / "sch.pdf").write_bytes(b"%PDF-1.4\n")
        (repo / "board" / "exports" / "bom.csv").write_text("Refs,Qty,Supplier Part\nU1,1,C128666\n", encoding="utf-8")
        (repo / "docs" / "DECISIONS.md").write_text("# Decisions\n\n- Approved `U1`.\n", encoding="utf-8")
        (repo / "hardware" / "reference" / "part.pdf").write_bytes(b"%PDF-1.4\n")
        (repo / "logs" / "secret.txt").write_text("nope", encoding="utf-8")
        (repo / ".git" / "config").write_text("nope", encoding="utf-8")

        srv = board_graph_serve.server("127.0.0.1", 0, self.folder, repo_root=repo)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), t.join(5)))
        addr = f"http://127.0.0.1:{srv.server_address[1]}"

        with urllib.request.urlopen(f"{addr}/files/board/exports/sch.pdf", timeout=5) as r:
            self.assertEqual(r.headers["Content-Type"], "application/pdf")
            self.assertIn("inline;", r.headers["Content-Disposition"])
            self.assertEqual(r.read(), b"%PDF-1.4\n")

        with urllib.request.urlopen(f"{addr}/files/docs/DECISIONS.md", timeout=5) as r:
            self.assertIn("text/html", r.headers["Content-Type"])
            body = r.read().decode("utf-8")
            self.assertIn("<h1>Decisions</h1>", body)
            self.assertIn("<code>U1</code>", body)

        with urllib.request.urlopen(f"{addr}/files/docs/DECISIONS.md?raw=1", timeout=5) as r:
            self.assertIn("text/plain", r.headers["Content-Type"])

        with urllib.request.urlopen(f"{addr}/files/board/exports/bom.csv", timeout=5) as r:
            body = r.read().decode("utf-8")
            self.assertIn("<table>", body)
            self.assertIn("jlcpcb.com/parts/componentSearch?searchTxt=C128666", body)

        with urllib.request.urlopen(f"{addr}/files/hardware/reference", timeout=5) as r:
            body = r.read().decode("utf-8")
            self.assertIn("part.pdf", body)
            self.assertIn("PDF", body)

        # A file's page leads back to the graph's page: a board graph here, and with ?board=<name> the graph the
        # file was opened from, carried on every link between files. A name that is not a name is dropped.
        with urllib.request.urlopen(f"{addr}/files/docs/DECISIONS.md", timeout=5) as r:
            self.assertIn('<a href="/">Board graph</a>', r.read().decode("utf-8"))
        with urllib.request.urlopen(f"{addr}/files/hardware/reference?board=driver", timeout=5) as r:
            body = r.read().decode("utf-8")
            self.assertIn('<a href="/?board=driver">Board graph</a>', body)
            self.assertIn('href="/files/hardware?board=driver"', body)
            self.assertIn('href="/files/hardware/reference/part.pdf?board=driver"', body)
        with urllib.request.urlopen(f"{addr}/files/docs/DECISIONS.md?board=../x", timeout=5) as r:
            self.assertIn('<a href="/">Board graph</a>', r.read().decode("utf-8"))
        # A project that is not a circuit board calls it a graph.
        (repo / "tools").mkdir()
        (repo / "tools" / "board_graph_pack.json").write_text('{"pack": "none", "reports": "reports"}',
                                                              encoding="utf-8")
        with urllib.request.urlopen(f"{addr}/files/docs/DECISIONS.md?board=talk", timeout=5) as r:
            body = r.read().decode("utf-8")
            self.assertIn('<a href="/?board=talk">Graph</a>', body)
            self.assertIn('href="/files/docs/DECISIONS.md?raw=1&board=talk"', body)
            self.assertNotIn("Board", body)

        for bad in ("/files/logs/secret.txt", "/files/.git/config", "/files/../view.json"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(addr + bad, timeout=5)
            ctx.exception.close()
            self.assertEqual(ctx.exception.code, 403)


class JobTests(unittest.TestCase):
    """launchd's copy of the plist and its words are read; a plist for another checkout is refused."""

    def test_launchd_print_is_read_for_state_and_pid(self):
        self.assertEqual(launchd_job.parse_print(RUNNING), {"state": "running", "pid": 4242})
        self.assertEqual(launchd_job.parse_print(STOPPED), {"state": "not running", "pid": None})

    def test_launchds_copy_is_the_repositorys_or_says_it_is_not(self):
        with tempfile.TemporaryDirectory() as home:
            home = pathlib.Path(home)
            name, data = board_graph_serve.job(PLIST)
            self.assertEqual((name, data), (PLIST.name, PLIST.read_bytes()))
            self.assertEqual(launchd_job.installed(name, data, home), "not installed")
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            shutil.copyfile(PLIST, agents / PLIST.name)
            self.assertEqual(launchd_job.installed(name, data, home), "installed")
            (agents / PLIST.name).write_bytes(b"<plist/>")
            self.assertEqual(launchd_job.installed(name, data, home), "differs")

    def test_install_refuses_a_plist_for_another_checkout(self):
        elsewhere = pathlib.Path(tempfile.gettempdir()) / "elsewhere"
        code, said = launchd_job.install(PLIST.name, PLIST.read_bytes(), root=elsewhere)
        self.assertEqual(code, 2)
        self.assertIn(f"runs the job in /Users/builder/demo-board, not this checkout ({elsewhere})", said)
        written = board_graph_serve.written_plist("/Users/builder/demo-board")
        code, said = launchd_job.install(PLIST.name, written, root=elsewhere)
        self.assertEqual(code, 2)
        self.assertIn(f"runs the job in /Users/builder/demo-board, not this checkout ({elsewhere})", said)

    def test_the_job_written_from_the_site_settings_is_the_one_a_working_project_wrote_by_hand(self):
        # Fixture plist compared against the job written from fixture settings for that checkout.
        by_hand = plistlib.loads(PLIST.read_bytes())
        written = board_graph_serve.written_plist("/Users/builder/demo-board")
        self.assertEqual(plistlib.loads(written), by_hand)
        self.assertEqual(list(plistlib.loads(written)), list(by_hand))  # the keys in the same order
        self.assertEqual(written, board_graph_serve.written_plist("/Users/builder/demo-board"))  # the same bytes

    def test_a_project_without_its_own_plist_gets_the_written_one_under_its_label(self):
        other = site_settings.Settings({
            "kicad_machine": {"ssh_host": "kicad@other-mac", "ssh_key": "~/.ssh/id_other", "checkout": "~/other",
                              "steps": "~/other-steps", "editor_lock": "~/steps/BUSY.d", "worktree_prefix": "~/other-",
                              "cache": "~/.cache/other", "disposable_copies": "/tmp/other-"},
            "graph_page": {"address": "http://other-mac:8100", "launchd_label": "other.board-graph-page",
                           "python": "/opt/homebrew/bin/python3"},
            "state_database": "var/graph/state.sqlite3"}, "settings.json")
        job = plistlib.loads(board_graph_serve.written_plist("/Users/kicad/other", other))
        self.assertEqual(job["Label"], "other.board-graph-page")
        self.assertEqual(job["ProgramArguments"],
                         ["/opt/homebrew/bin/python3", "/Users/kicad/other/scripts/board_graph_serve.py"])
        self.assertEqual(job["WorkingDirectory"], "/Users/kicad/other")
        self.assertEqual(job["StandardErrorPath"], "/Users/kicad/other/var/graph/page.err")
        self.assertTrue(job["KeepAlive"] and job["RunAtLoad"])
        with tempfile.TemporaryDirectory() as folder:
            missing = pathlib.Path(folder) / "tools" / "mac" / "demo.board-graph-page.plist"
            name, data = board_graph_serve.job(missing)
            checkout = pathlib.Path(board_graph_serve.mac.CHECKOUT).expanduser().as_posix()
            self.assertEqual((name, data), ("demo.board-graph-page.plist", board_graph_serve.written_plist(checkout)))
            self.assertEqual(plistlib.loads(data)["ProgramArguments"],
                             ["/usr/bin/python3", f"{checkout}/scripts/board_graph_serve.py"])

    def test_status_is_one_json_line_and_serving_refuses_off_the_mac(self):
        with mock.patch.object(launchd_job, "installed", lambda name, data: "installed"), \
                mock.patch.object(launchd_job, "running", lambda label: {"state": "running", "pid": 7}), \
                mock.patch.object(board_graph_serve, "answers", lambda: "yes"), \
                mock.patch.object(board_graph_serve, "listening", lambda: 2), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(board_graph_serve.main(["--status"]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"address": "http://kicad-host:8000", "job": "installed",
                                                       "launchd": {"state": "running", "pid": 7}, "answers": "yes",
                                                       "listening": 2})
        if sys.platform != "darwin":
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(board_graph_serve.main([]), 2)
            self.assertIn("refused: the page is served from the Mac at http://kicad-host:8000", out.getvalue())

    def test_notify_from_the_command_line_exits_0_or_1_with_why(self):
        # The sync run tells the page this way, in a fresh process, after it fast-forwards main.
        with mock.patch.object(board_graph_serve, "notify", lambda: None), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(board_graph_serve.main(["--notify"]), 0)
        self.assertEqual(out.getvalue(), "")
        with mock.patch.object(board_graph_serve, "notify", lambda: "HTTP 403"), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(board_graph_serve.main(["--notify"]), 1)
        self.assertEqual(out.getvalue().strip(), "HTTP 403")


if __name__ == "__main__":
    unittest.main()
