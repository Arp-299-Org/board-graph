#!/usr/bin/env python3
"""Project-specific actions and approval sources for board-graph's own dev workflow."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
import uuid

MANIFEST_REL = "tools/board_graph_tool.json"
SLOW_CHECK_WARN_SECONDS = 90.0
EXCLUDED_APPROVAL_FILES = {
    MANIFEST_REL,
    "pyproject.toml",
    "UPGRADING.md",
}
EXTRA_APPROVAL_FILES = (
    ".github/scripts/check_hygiene.py",
    ".github/scripts/audit_workflow.py",
    ".github/workflows/linux-kicad-smoke.yml",
    ".github/workflows/test.yml",
    "dev/tool_graph.json",
    "dev/tool_pack.json",
    "dev/tool_actions.py",
    "dev/tool_dev.py",
    "dev/test_tool_dev.py",
    "docs/KICAD_10_INVARIANTS.md",
)
IPC_ROUTING_PREFIXES = (
    "scripts/kicad_",
    "scripts/editor_step",
    "scripts/routing_",
)
IPC_ROUTING_EXACT = {
    "scripts/board_context.py",
    "scripts/pad_exits.py",
    "scripts/validate_board.py",
}


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "BOARD_GRAPH_PACK",
        "BOARD_GRAPH_SITE",
        "BOARD_GRAPH_STATE",
        "BOARD_GRAPH_RECORDS",
        "BOARD_GRAPH_VIEW",
        "BOARD_GRAPH_BOARD",
        "BOARD_GRAPH_PROJECT",
        "BOARD_GRAPH_TEST_KICAD",
    ):
        env.pop(key, None)
    return env


def _git(root: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=check,
    )


def _lf_sha256(path: pathlib.Path) -> str:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def tool_code_fingerprint(root: pathlib.Path) -> dict:
    """Hash all tool code, dev workflow files, CI scripts, and starter template while excluding release metadata."""
    root = pathlib.Path(root)
    manifest_path = root / MANIFEST_REL
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidates = set(data.get("files", {})) | set(data.get("starter", [])) | set(EXTRA_APPROVAL_FILES)
    scripts_dir = root / "scripts"
    if scripts_dir.is_dir():
        for py_file in sorted(scripts_dir.glob("*.py")):
            candidates.add(f"scripts/{py_file.name}")

    files: dict[str, str | None] = {}
    agg = hashlib.sha256()
    for rel in sorted(candidates - EXCLUDED_APPROVAL_FILES):
        target = root / rel
        digest = _lf_sha256(target) if target.is_file() else None
        files[rel] = digest
        agg.update(f"{rel}\0{digest or 'MISSING'}\n".encode("utf-8"))
    return {
        "schema": "board-graph/tool-code/v1",
        "files": files,
        "digest": agg.hexdigest(),
    }


def _tail(text: str, limit: int = 8) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-limit:]) if lines else "no output"


def _worker_count(total: int) -> int:
    raw = os.environ.get("BOARD_GRAPH_DEV_WORKERS", "").strip()
    if raw:
        try:
            workers = int(raw)
        except ValueError:
            workers = 0
    else:
        workers = min(8, os.cpu_count() or 4)
    if workers < 1:
        workers = min(8, os.cpu_count() or 4)
    return max(1, min(workers, max(1, total)))


def _discover_test_suites(root: pathlib.Path) -> list[tuple[str, list[str]]]:
    manifest_path = root / MANIFEST_REL
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    test_rels: set[str] = set()
    for folder in ("scripts", "scripts/hooks", "tools/kicad"):
        base = root / folder
        if base.is_dir():
            for path in base.glob("test_*.py"):
                test_rels.add(path.relative_to(root).as_posix())
    for rel in manifest.get("files", {}):
        if rel.rsplit("/", 1)[-1].startswith("test_") and rel.endswith(".py") and (root / rel).is_file():
            test_rels.add(rel)

    suites: list[tuple[str, list[str]]] = [
        ("audit_workflow", [sys.executable, ".github/scripts/audit_workflow.py", "."]),
    ]
    if os.environ.get("BOARD_GRAPH_DEV_IN_TOOL_CHECKS") != "1":
        suites.append(("dev_tests", [sys.executable, "dev/test_tool_dev.py"]))
    for rel in sorted(test_rels):
        if rel == "tools/kicad/test_kicad_semantic_snapshot.py" and sys.version_info < (3, 10):
            continue
        suites.append((rel, [sys.executable, rel]))
    return suites


def _run_one_check(root: pathlib.Path, env: dict[str, str], label: str, cmd: list[str]) -> tuple[str, int, str, float]:
    t0 = time.monotonic()
    done = subprocess.run(cmd, cwd=str(root), env=env, capture_output=True, text=True)
    elapsed = time.monotonic() - t0
    out = (done.stdout or "") + "\n" + (done.stderr or "")
    return label, done.returncode, out, elapsed


def run_tool_checks(root: pathlib.Path) -> dict:
    """Run fail-fast hygiene preflight, then run workflow audit, dev tests, and unit/integration suites in parallel."""
    root = pathlib.Path(root)
    started = time.monotonic()
    env = _clean_env()
    env["BOARD_GRAPH_DEV_IN_TOOL_CHECKS"] = "1"
    preflight: list[tuple[str, list[str]]] = [
        ("hygiene", [sys.executable, ".github/scripts/check_hygiene.py"]),
    ]
    suites = _discover_test_suites(root)
    findings: list[str] = []
    passed: list[str] = []
    failed: list[str] = []
    timings: dict[str, float] = {}

    for idx, (label, cmd) in enumerate(preflight, 1):
        _, rc, out, sec = _run_one_check(root, env, label, cmd)
        timings[label] = round(sec, 2)
        status = "ok  " if rc == 0 else "FAIL"
        print(f"[preflight {idx}/{len(preflight)}] {status} {label} ({sec:.1f}s)", file=sys.stderr, flush=True)
        if rc == 0:
            passed.append(label)
        else:
            failed.append(label)
            findings.append(f"{label} failed (exit {rc}): {_tail(out)}")

    if failed:
        elapsed_s = round(time.monotonic() - started, 2)
        fp = tool_code_fingerprint(root)
        slowest = [{"check": k, "elapsed_s": v} for k, v in sorted(timings.items(), key=lambda item: -item[1])[:5]]
        return {
            "outcome": "needs_lead",
            "findings": findings,
            "detail": {
                "checked": len(preflight),
                "passed": len(passed),
                "failed": failed,
                "code_sha": fp["digest"],
                "elapsed_s": elapsed_s,
                "timings": timings,
                "slowest": slowest,
                "preflight_failed": True,
                "skipped_suites": len(suites),
            },
        }

    results: dict[str, tuple[int, str, float]] = {}
    total_suites = len(suites)
    with concurrent.futures.ThreadPoolExecutor(max_workers=_worker_count(total_suites)) as pool:
        future_map = {pool.submit(_run_one_check, root, env, label, cmd): label for label, cmd in suites}
        for done_idx, future in enumerate(concurrent.futures.as_completed(future_map), 1):
            label, rc, out, sec = future.result()
            results[label] = (rc, out, sec)
            timings[label] = round(sec, 2)
            status = "ok  " if rc == 0 else "FAIL"
            print(f"[tool_checks {done_idx}/{total_suites}] {status} {label} ({sec:.1f}s)", file=sys.stderr, flush=True)

    for label, _ in suites:
        rc, out, _ = results[label]
        if rc == 0:
            passed.append(label)
        else:
            failed.append(label)
            findings.append(f"{label} failed (exit {rc}): {_tail(out)}")

    elapsed_s = round(time.monotonic() - started, 2)
    slowest = [{"check": k, "elapsed_s": v} for k, v in sorted(timings.items(), key=lambda item: -item[1])[:5]]
    detail: dict[str, object] = {
        "checked": len(preflight) + total_suites,
        "passed": len(passed),
        "failed": failed,
        "code_sha": tool_code_fingerprint(root)["digest"],
        "elapsed_s": elapsed_s,
        "timings": timings,
        "slowest": slowest,
        "preflight_failed": False,
        "skipped_suites": 0,
    }
    if elapsed_s > SLOW_CHECK_WARN_SECONDS:
        top = ", ".join(f"{s['check']} ({s['elapsed_s']:.1f}s)" for s in slowest[:3])
        warn = f"tool_checks took {elapsed_s:.1f}s (> {SLOW_CHECK_WARN_SECONDS:.0f}s budget); slowest: {top}"
        detail["slow_warning"] = warn
        print(f"WARNING: {warn}", file=sys.stderr, flush=True)

    return {
        "outcome": "pass" if not findings else "needs_lead",
        "findings": findings,
        "detail": detail,
    }


def _is_ipc_or_routing(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    if name.startswith("test_"):
        return False
    return rel in IPC_ROUTING_EXACT or rel.startswith(IPC_ROUTING_PREFIXES)


def changed_ipc_routing_files(root: pathlib.Path, base_ref: str | None = None) -> tuple[str, list[str]]:
    """Return (base_ref, sorted changed production IPC/routing file paths since base_ref + working tree)."""
    root = pathlib.Path(root)
    if not base_ref:
        tag = _git(root, "describe", "--tags", "--abbrev=0", check=False)
        if tag.returncode == 0 and tag.stdout.strip():
            base_ref = tag.stdout.strip()
        else:
            remote = _git(root, "rev-parse", "--verify", "origin/main", check=False)
            base_ref = "origin/main" if remote.returncode == 0 else "HEAD~1"
    changed: set[str] = set()
    committed = _git(root, "diff", "--name-only", base_ref, "HEAD", check=False)
    if committed.returncode == 0:
        changed.update(line.strip() for line in committed.stdout.splitlines() if line.strip())
    dirty = _git(root, "diff", "--name-only", "HEAD", check=False)
    if dirty.returncode == 0:
        changed.update(line.strip() for line in dirty.stdout.splitlines() if line.strip())
    matched = sorted(rel for rel in changed if _is_ipc_or_routing(rel))
    return base_ref, matched


def check_mac_idle(host: str, key: pathlib.Path) -> tuple[bool, str]:
    """Verify the configured KiCad machine has zero active pcbnew, editor_step, or routing_run processes."""
    if not host:
        return False, "BOARD_GRAPH_DEV_MAC_HOST is not set"
    cmd = ["ssh", "-i", str(key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
           "pgrep -fl 'pcbnew|editor_step|routing_run' || echo NONE_RUNNING"]
    done = subprocess.run(cmd, capture_output=True, text=True)
    text = (done.stdout or done.stderr or "").strip()
    return (done.returncode == 0 and text == "NONE_RUNNING"), text or f"exit {done.returncode}"


_MAC_SMOKE_SCRIPT = """
import json, pathlib, shutil, sys, tempfile, traceback

smoke_dir = pathlib.Path(sys.argv[1])
scripts_dir = smoke_dir / "scripts"
sys.path.insert(0, str(scripts_dir))
sys.path.insert(0, str(smoke_dir / "tools" / "kicad"))

import editor_step_kicad
import kicad_ipc
import kicad_lay
import kicad_route
import test_dense_board_integration as dense
from invoke_kicad_cli import run_kicad_cli

proj_dir = smoke_dir / "proj"
bundle = dense.build_dense_board_bundle(proj_dir)
pcb_path = bundle["pcb_path"].resolve()
pro_path = bundle["pro_path"].resolve()

drc_out = proj_dir / "drc-smoke.json"
drc = run_kicad_cli(
    ["pcb", "drc", "--refill-zones", "--severity-all", "--format", "json", "--output", str(drc_out), str(pcb_path)],
    capture_output=True, text=True,
)
if drc.returncode != 0 or not drc_out.is_file():
    raise RuntimeError(f"kicad-cli drc failed ({drc.returncode}): {drc.stderr or drc.stdout}")
drc_data = json.loads(drc_out.read_text(encoding="utf-8"))

editor_step_kicad.refuse_if_editor_running()
proc, socket = editor_step_kicad.open_editor(pcb_path)
try:
    connected = kicad_ipc.connect_board(socket_path=socket, expected_project=pro_path)
    route = kicad_route.KipyBoard(str(proc.pid))
    board = kicad_lay.KipyLayBoard.__new__(kicad_lay.KipyLayBoard)
    board.route = route
    board.board = route.board
    board._init_caches()
    board.wait_ready()

    pad_u2 = board.pad("U2.2")
    if pad_u2.copper.shape != "oval":
        raise RuntimeError(f"unexpected U2.2 shape: {pad_u2.copper.shape}")
    obstacles = board.pad_obstacles()
    layers = board.enabled_layers()
    gnd_nc = board.netclass_defaults("GND")
    routes = [
        {"from": "Q1.1", "to": "Q1.2", "layer": "F.Cu", "steps": []},
        {"from": "Q2.1", "to": "Q2.2", "layer": "F.Cu", "steps": []},
        {"from": "Q3.1", "to": "Q3.2", "layer": "F.Cu", "steps": []},
    ]
    batch = kicad_lay.execute_batch(board, {"routes": routes}, run_drc=True)
    board.board.save()
    result = {
        "ok": True,
        "socket": socket,
        "board_name": connected.document.board_filename,
        "drc_violations": len(drc_data.get("violations", [])),
        "pad_obstacles": len(obstacles),
        "enabled_layers": layers,
        "gnd_track_width_mm": gnd_nc["track_width_mm"],
        "tracks_created": batch["tracks_created"],
    }
    print(json.dumps(result))
finally:
    editor_step_kicad.close_editor(proc)
""".strip()


def run_live_smoke(root: pathlib.Path) -> dict:
    """Check KiCad machine idle state and run a disposable KiCad 10 CLI + live IPC smoke in /tmp if IPC/routing changed."""
    root = pathlib.Path(root)
    host = os.environ.get("BOARD_GRAPH_DEV_MAC_HOST", "").strip()
    key = pathlib.Path(os.environ.get("BOARD_GRAPH_DEV_MAC_KEY", "~/.ssh/id_ed25519")).expanduser()
    idle, pgrep_out = check_mac_idle(host, key)
    if not idle:
        return {
            "outcome": "needs_lead",
            "findings": [f"Mac {host} is not idle or unreachable ({pgrep_out}); never interrupt an active run"],
            "detail": {"mac_idle": False, "pgrep": pgrep_out},
        }
    base_ref, changed_ipc = changed_ipc_routing_files(root)
    if not changed_ipc:
        return {
            "outcome": "pass",
            "findings": [],
            "detail": {
                "mac_idle": True,
                "ipc_changed": False,
                "base_ref": base_ref,
                "skipped": "no production IPC or routing files changed",
            },
        }
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD", "scripts", "tools", "board"],
        cwd=str(root),
        capture_output=True,
    )
    if archive.returncode != 0:
        return {
            "outcome": "needs_lead",
            "findings": [f"git archive failed: {archive.stderr.decode('utf-8', errors='replace').strip()}"],
            "detail": {"mac_idle": True, "ipc_changed": True, "changed_files": changed_ipc},
        }
    smoke_dir = f"/tmp/board-graph-smoke-{uuid.uuid4().hex[:10]}"
    # Stream archive separately from inline python script so binary tar never collides with bash stdin
    unpack = subprocess.run(
        ["ssh", "-i", str(key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
         f"mkdir -p {shlex.quote(smoke_dir)} && tar -xf - -C {shlex.quote(smoke_dir)}"],
        input=archive.stdout,
        capture_output=True,
    )
    if unpack.returncode != 0:
        err = unpack.stderr.decode("utf-8", errors="replace").strip()
        return {
            "outcome": "needs_lead",
            "findings": [f"Failed to stage disposable smoke bundle on {host}: {err}"],
            "detail": {"mac_idle": True, "ipc_changed": True, "changed_files": changed_ipc},
        }
    smoke_cmd = (
        f"trap 'rm -rf {shlex.quote(smoke_dir)}' EXIT; "
        f"~/board-graph/.venv-kicad-ipc/bin/python - {shlex.quote(smoke_dir)}"
    )
    done = subprocess.run(
        ["ssh", "-i", str(key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, smoke_cmd],
        input=_MAC_SMOKE_SCRIPT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if done.returncode != 0:
        err_text = ((done.stdout or "") + "\n" + (done.stderr or "")).strip()
        return {
            "outcome": "needs_lead",
            "findings": [f"Mac live KiCad 10 smoke failed (exit {done.returncode}): {_tail(err_text)}"],
            "detail": {"mac_idle": True, "ipc_changed": True, "changed_files": changed_ipc, "raw_output": err_text[-2000:]},
        }
    last_json = next((json.loads(line) for line in reversed(done.stdout.splitlines()) if line.strip().startswith("{")), {})
    return {
        "outcome": "pass",
        "findings": [],
        "detail": {
            "mac_idle": True,
            "ipc_changed": True,
            "base_ref": base_ref,
            "changed_files": changed_ipc,
            "smoke": last_json,
        },
    }


def _pyproject_version(root: pathlib.Path) -> str | None:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


def run_release_gate(root: pathlib.Path) -> dict:
    """Verify release metadata, clean tree, annotated tag at HEAD, manifest check, and dev/ exclusion."""
    root = pathlib.Path(root)
    findings: list[str] = []
    manifest_data = json.loads((root / MANIFEST_REL).read_text(encoding="utf-8"))
    manifest_ver = manifest_data.get("version", "")
    py_ver = _pyproject_version(root) or ""

    if not re.fullmatch(r"\d+\.\d+\.\d+", manifest_ver):
        findings.append(f"manifest version {manifest_ver!r} is not X.Y.Z semver")
    if py_ver != manifest_ver:
        findings.append(f"pyproject.toml version ({py_ver!r}) disagrees with manifest ({manifest_ver!r})")

    leaked = [p for p in (*manifest_data.get("files", {}), *manifest_data.get("starter", [])) if p.startswith("dev/")]
    if leaked:
        findings.append(f"dev workflow files leaked into {MANIFEST_REL}: {', '.join(leaked)}")

    env = _clean_env()
    for label, cmd in (
        ("board_graph_tool.py check", [sys.executable, "scripts/board_graph_tool.py", "check"]),
        ("board_graph_tool.py notes", [sys.executable, "scripts/board_graph_tool.py", "notes"]),
    ):
        done = subprocess.run(cmd, cwd=str(root), env=env, capture_output=True, text=True)
        if done.returncode != 0:
            findings.append(f"{label} failed: {_tail((done.stdout or '') + (done.stderr or ''))}")

    status = _git(root, "status", "--porcelain", check=False)
    if status.returncode != 0 or status.stdout.strip():
        findings.append("working tree is not clean before closing release")

    tag = f"v{manifest_ver}" if manifest_ver else ""
    if tag:
        obj_type = _git(root, "cat-file", "-t", tag, check=False)
        tag_commit = _git(root, "rev-parse", f"{tag}^{{commit}}", check=False)
        head_commit = _git(root, "rev-parse", "HEAD", check=False)
        if obj_type.returncode != 0 or obj_type.stdout.strip() != "tag":
            findings.append(f"annotated tag {tag} is missing")
        elif tag_commit.stdout.strip() != head_commit.stdout.strip():
            findings.append(f"tag {tag} points to {tag_commit.stdout.strip()[:8]}, not HEAD {head_commit.stdout.strip()[:8]}")

    return {
        "outcome": "pass" if not findings else "needs_lead",
        "findings": findings,
        "detail": {"version": manifest_ver, "tag": tag},
    }


ACTIONS = {
    "tool_checks": (run_tool_checks, ["tools/board_graph_tool.json", "dev/tool_graph.json"]),
    "live_smoke": (run_live_smoke, ["dev/tool_graph.json"]),
    "release_gate": (run_release_gate, ["pyproject.toml", "UPGRADING.md", "tools/board_graph_tool.json"]),
}

APPROVAL_SOURCES = {
    "tool_code": tool_code_fingerprint,
}
