#!/usr/bin/env python3
"""Subscribe to board-graph SSE /events streams and exit immediately when any benchmark target blocks or finishes."""
from __future__ import annotations

import argparse
import datetime
import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.request


def _now_hms() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _fetch_view(base_url: str) -> dict:
    url = f"{base_url.rstrip('/')}/view.json"
    last_err: Exception | None = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, ValueError) as exc:
            last_err = exc
            time.sleep(0.2)
    raise RuntimeError(f"failed to fetch {url}: {last_err}")


def _board_state(view: dict) -> dict:
    tracks = (view.get("where") or {}).get("tracks") or []
    board_track = next((t for t in tracks if t.get("id") == "board"), None) or {}
    transitions = board_track.get("transitions") or []
    last = transitions[-1] if transitions else {}
    return {
        "project": view.get("project"),
        "position": (view.get("where") or {}).get("position"),
        "seq": int(last.get("seq") or 0),
        "node": last.get("node"),
        "outcome": last.get("outcome"),
        "target": last.get("target"),
        "actor": last.get("actor"),
        "note": last.get("note") or "",
    }


def _snapshot_all(targets: dict[str, str]) -> dict[str, dict]:
    out = {}
    for name, url in targets.items():
        try:
            out[name] = _board_state(_fetch_view(url))
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": str(exc)}
    return out


def _watch_target(
    name: str,
    base_url: str,
    until_node: str,
    stop_outcomes: set[str],
    wait_all: bool,
    events_q: queue.Queue,
    state_lock: threading.Lock,
    reached: set[str],
    last_seq: dict[str, int],
) -> None:
    def inspect_once() -> None:
        st = _board_state(_fetch_view(base_url))
        seq = st["seq"]
        emit = False
        with state_lock:
            if seq != last_seq.get(name, -1):
                last_seq[name] = seq
                emit = True
            if st["outcome"] in stop_outcomes:
                events_q.put(("blocked", 10, name, st))
                return
            if st["node"] == until_node and st["outcome"] == "pass":
                reached.add(name)
                if not wait_all or len(reached) >= len(targets_global):
                    events_q.put(("reached", 0, name, st))
                    return
        if emit:
            summary = f"{st['node']} {st['outcome']} -> {st['target']}" if st["node"] else f"pos={st['position']}"
            print(f"[{_now_hms()}] {name} ({st['project']}): seq={seq} {summary} ({st['actor']})", flush=True)

    sse_url = f"{base_url.rstrip('/')}/events"
    for attempt in range(2):
        try:
            req = urllib.request.Request(sse_url, headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=None) as resp:
                inspect_once()
                while True:
                    raw = resp.readline()
                    if not raw:
                        raise ConnectionError(f"SSE stream closed for {name}")
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line.startswith("data:"):
                        inspect_once()
        except Exception as exc:  # noqa: BLE001
            if attempt == 0:
                print(f"[{_now_hms()}] {name}: SSE stream interrupted ({exc}); reconnecting once...", flush=True)
                time.sleep(2)
                continue
            events_q.put(("stream_error", 11, name, {"error": str(exc)}))
            return


targets_global: dict[str, str] = {}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", action="append", required=True, help="NAME=URL pair (repeatable)")
    parser.add_argument("--until", default="quote_files", help="Completion node id (default: quote_files)")
    parser.add_argument("--stop-outcomes", default="needs_owner,needs_lead,needs_skill",
                        help="Comma-separated outcomes that trigger immediate exit 10")
    parser.add_argument("--wait-all", action="store_true", help="Exit 0 only once every target passes --until")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    targets: dict[str, str] = {}
    for item in args.target:
        if "=" not in item:
            print(f"invalid --target {item!r}: expected NAME=URL", file=sys.stderr)
            return 2
        name, url = item.split("=", 1)
        targets[name.strip()] = url.strip()
    targets_global.update(targets)
    stop_outcomes = {s.strip() for s in args.stop_outcomes.split(",") if s.strip()}

    events_q: queue.Queue = queue.Queue()
    state_lock = threading.Lock()
    reached: set[str] = set()
    last_seq: dict[str, int] = {}

    for name, url in targets.items():
        t = threading.Thread(
            target=_watch_target,
            args=(name, url, args.until, stop_outcomes, args.wait_all, events_q, state_lock, reached, last_seq),
            daemon=True,
        )
        t.start()

    kind, code, trigger_name, detail = events_q.get()
    payload = {
        "kind": kind,
        "trigger": trigger_name,
        "detail": detail,
        "targets": _snapshot_all(targets),
    }
    print(f"EVENT_TRIGGER: {json.dumps(payload, ensure_ascii=False)}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
