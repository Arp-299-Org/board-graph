#!/usr/bin/env python3
"""End-to-end CLI tests walking a freshly initialized board-graph project with zero KiCad or SSH."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
CLI_PATH = SCRIPTS_DIR / "board_graph_cli.py"

CLEAN_ENV_KEYS = (
    "BOARD_GRAPH_PROJECT",
    "BOARD_GRAPH_SITE",
    "BOARD_GRAPH_STATE",
    "BOARD_GRAPH_RECORDS",
    "BOARD_GRAPH_VIEW",
    "BOARD_GRAPH_BOARD",
    "BOARD_GRAPH_BOARDS",
    "BOARD_GRAPH_PACK",
    "BOARD_GRAPH_GRAPH",
)


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in CLEAN_ENV_KEYS:
        env.pop(key, None)
    return env


def _run_cli(
    args: list[str],
    *,
    cwd: pathlib.Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(CLI_PATH), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def _git(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


class BoardGraphCliE2ETests(unittest.TestCase):
    def test_init_check_walk_approvals_and_view_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            env = _clean_env()

            ver = _run_cli(["version"], cwd=tmp, env=env)
            self.assertEqual(ver.returncode, 0, ver.stderr + ver.stdout)
            self.assertIn("board-graph", ver.stdout)

            bare = tmp / "origin.git"
            bare.mkdir()
            _git(bare, "init", "--bare")
            _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")

            proj = tmp / "demo-board"
            proj.mkdir()

            init = _run_cli(["--project", str(proj), "init"], cwd=tmp, env=env)
            self.assertEqual(init.returncode, 0, init.stderr + init.stdout)
            for rel in (
                "tools/board_graph.json",
                "tools/board_graph_site.json",
                "board/board.json",
                "AGENTS.md",
                "docs/PRODUCT_TRUTH.md",
                ".gitignore",
                ".gitattributes",
            ):
                self.assertTrue((proj / rel).is_file(), f"missing starter file {rel}")

            (proj / "tools" / "board_graph_site.json").write_text(
                json.dumps(
                    {
                        "one_machine": True,
                        "state_database": "logs/board-graph/state.sqlite3",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (proj / "board" / "example.kicad_sch").write_text(
                '(kicad_sch (version 20231120) (generator "eeschema"))\n',
                encoding="utf-8",
            )
            (proj / "board" / "example.kicad_pro").write_text("{}\n", encoding="utf-8")

            _git(proj, "init", "-b", "main")
            _git(proj, "config", "user.name", "E2E Tester")
            _git(proj, "config", "user.email", "e2e@example.com")
            _git(proj, "remote", "add", "origin", str(bare))
            _git(proj, "add", ".")
            _git(proj, "commit", "-m", "Initialize demo board")
            _git(proj, "push", "-u", "origin", "main")

            chk = _run_cli(["check"], cwd=proj, env=env)
            self.assertEqual(chk.returncode, 0, chk.stderr + chk.stdout)

            desc = _run_cli(["board-description"], cwd=proj, env=env)
            self.assertEqual(desc.returncode, 0, desc.stderr + desc.stdout)
            self.assertIn("Example", desc.stdout)

            where0 = _run_cli(["where", "--json"], cwd=proj, env=env)
            self.assertEqual(where0.returncode, 0, where0.stderr + where0.stdout)
            self.assertEqual(json.loads(where0.stdout)["tracks"][0]["position"], "inventory")

            inv_next = _run_cli(["next"], cwd=proj, env=env)
            self.assertEqual(inv_next.returncode, 0, inv_next.stderr + inv_next.stdout)
            where1 = json.loads(_run_cli(["where", "--json"], cwd=proj, env=env).stdout)
            self.assertEqual(where1["tracks"][0]["position"], "decisions")

            dec_next = _run_cli(["next", "--by", "owner", "--note", "Start owner decisions"], cwd=proj, env=env)
            self.assertEqual(dec_next.returncode, 2, dec_next.stderr + dec_next.stdout)
            where_open = json.loads(_run_cli(["where", "--json"], cwd=proj, env=env).stdout)
            dec_attempt = where_open["tracks"][0]["node"]["working"][0]["run_id"]
            self.assertEqual(where_open["tracks"][0]["node"]["working"][0]["by"], "owner")

            bad_actor = _run_cli(
                [
                    "record", "decisions", "pass",
                    "--attempt", dec_attempt,
                    "--by", "agent:bot",
                    "--note", "Unauthorized pass",
                    "--evidence", "board/DECISIONS.md",
                ],
                cwd=proj,
                env=env,
            )
            self.assertNotEqual(bad_actor.returncode, 0)

            ok_dec = _run_cli(
                [
                    "record", "decisions", "pass",
                    "--attempt", dec_attempt,
                    "--by", "owner",
                    "--note", "Approved demo spec",
                    "--evidence", "board/DECISIONS.md",
                ],
                cwd=proj,
                env=env,
            )
            self.assertEqual(ok_dec.returncode, 0, ok_dec.stderr + ok_dec.stdout)
            where2 = json.loads(_run_cli(["where", "--json"], cwd=proj, env=env).stdout)
            self.assertEqual(where2["tracks"][0]["position"], "constraints")

            approvals = _run_cli(["approvals"], cwd=proj, env=env)
            self.assertEqual(approvals.returncode, 0, approvals.stderr + approvals.stdout)
            self.assertIn("decisions", approvals.stdout)

            con_next = _run_cli(["next", "--by", "agent:lead", "--note", "Start constraints"], cwd=proj, env=env)
            self.assertEqual(con_next.returncode, 2, con_next.stderr + con_next.stdout)
            where_con = json.loads(_run_cli(["where", "--json"], cwd=proj, env=env).stdout)
            con_attempt = where_con["tracks"][0]["node"]["working"][0]["run_id"]

            decisions_doc = proj / "board" / "DECISIONS.md"
            orig_decisions = decisions_doc.read_bytes()
            decisions_doc.write_bytes(orig_decisions + b"\nUnapproved edit.\n")

            stale = _run_cli(
                [
                    "record", "constraints", "pass",
                    "--attempt", con_attempt,
                    "--by", "agent:lead",
                    "--note", "Should fail on stale decisions",
                    "--evidence", "board/ELECTRICAL_AUDIT_CONTRACT.md",
                ],
                cwd=proj,
                env=env,
            )
            self.assertNotEqual(stale.returncode, 0)

            decisions_doc.write_bytes(orig_decisions)
            ok_con = _run_cli(
                [
                    "record", "constraints", "pass",
                    "--attempt", con_attempt,
                    "--by", "agent:lead",
                    "--note", "Constraints verified",
                    "--evidence", "board/ELECTRICAL_AUDIT_CONTRACT.md",
                ],
                cwd=proj,
                env=env,
            )
            self.assertEqual(ok_con.returncode, 0, ok_con.stderr + ok_con.stdout)
            where3 = json.loads(_run_cli(["where", "--json"], cwd=proj, env=env).stdout)
            self.assertEqual(where3["tracks"][0]["position"], "schematic")

            view_dir = proj / "tmp_view"
            snap = _run_cli(["view", "--out", str(view_dir), "snapshot", "--quick"], cwd=proj, env=env)
            self.assertEqual(snap.returncode, 0, snap.stderr + snap.stdout)
            view_data = json.loads((view_dir / "view.json").read_text(encoding="utf-8"))
            self.assertEqual(view_data["state"]["tracks"]["board"]["position"], "schematic")
            self.assertGreaterEqual(view_data["log"]["total"], 3)

    def test_init_autonomous_walks_owner_card_and_blocks_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            env = _clean_env()

            bad_proj = tmp / "bad-auto"
            bad_proj.mkdir()
            bad_init = _run_cli(["--project", str(bad_proj), "init", "--autonomous", "--until", "order"], cwd=tmp, env=env)
            self.assertNotEqual(bad_init.returncode, 0)
            self.assertIn("order", bad_init.stderr + bad_init.stdout)

            bare = tmp / "origin.git"
            bare.mkdir()
            _git(bare, "init", "--bare")
            _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")

            proj = tmp / "auto-board"
            proj.mkdir()
            ok_init = _run_cli(
                ["--project", str(proj), "init", "--autonomous", "--until", "quote_files", "--reason", "E2E auto test"],
                cwd=tmp,
                env=env,
            )
            self.assertEqual(ok_init.returncode, 0, ok_init.stderr + ok_init.stdout)
            self.assertIn("enabled autonomous mode", ok_init.stdout)

            (proj / "tools" / "board_graph_site.json").write_text(
                json.dumps({"one_machine": True, "state_database": "logs/board-graph/state.sqlite3"}, indent=2) + "\n",
                encoding="utf-8",
            )
            (proj / "board" / "example.kicad_sch").write_text(
                '(kicad_sch (version 20231120) (generator "eeschema"))\n',
                encoding="utf-8",
            )
            (proj / "board" / "example.kicad_pro").write_text("{}\n", encoding="utf-8")

            _git(proj, "init", "-b", "main")
            _git(proj, "config", "user.name", "E2E Tester")
            _git(proj, "config", "user.email", "e2e@example.com")
            _git(proj, "remote", "add", "origin", str(bare))
            _git(proj, "add", ".")
            _git(proj, "commit", "-m", "Initialize autonomous demo board")
            _git(proj, "push", "-u", "origin", "main")

            chk = _run_cli(["check"], cwd=proj, env=env)
            self.assertEqual(chk.returncode, 0, chk.stderr + chk.stdout)

            where_txt = _run_cli(["where"], cwd=proj, env=env)
            self.assertEqual(where_txt.returncode, 0, where_txt.stderr + where_txt.stdout)
            self.assertIn("Mode: AUTONOMOUS (delegated through quote_files; order always requires owner)", where_txt.stdout)

            inv_next = _run_cli(["next"], cwd=proj, env=env)
            self.assertEqual(inv_next.returncode, 0, inv_next.stderr + inv_next.stdout)

            dec_next = _run_cli(["next", "--by", "agent:bot", "--note", "Autonomous decisions"], cwd=proj, env=env)
            self.assertEqual(dec_next.returncode, 2, dec_next.stderr + dec_next.stdout)
            dec_attempt = json.loads(_run_cli(["where", "--json"], cwd=proj, env=env).stdout)["tracks"][0]["node"]["working"][0]["run_id"]

            dec_pass = _run_cli(
                ["record", "decisions", "pass", "--attempt", dec_attempt, "--by", "agent:bot",
                 "--note", "Agent passed decisions in autonomous mode", "--evidence", "board/DECISIONS.md"],
                cwd=proj,
                env=env,
            )
            self.assertEqual(dec_pass.returncode, 0, dec_pass.stderr + dec_pass.stdout)
            where_after = json.loads(_run_cli(["where", "--json"], cwd=proj, env=env).stdout)
            self.assertEqual(where_after["tracks"][0]["position"], "constraints")
            rec_files = sorted((proj / "tools" / "board_graph_records").glob("*-decisions-pass.json"))
            self.assertEqual(len(rec_files), 1)
            self.assertIs(json.loads(rec_files[0].read_text(encoding="utf-8"))["inputs"]["delegated"], True)


if __name__ == "__main__":
    unittest.main()
