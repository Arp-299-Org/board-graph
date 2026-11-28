#!/usr/bin/env python3
"""Multi-board / multi-run flat 2D board-graph Hub with live SSE updates."""
from __future__ import annotations

import argparse
import concurrent.futures
import http.server
import json
import mimetypes
import pathlib
import queue
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_heal  # noqa: E402
import tool_paths  # noqa: E402
from board_graph_hub_page import HUB_HTML  # noqa: E402

KEEPALIVE = 15


class HubListeners:
    """Thread-safe set of SSE client queues."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: set[queue.Queue] = set()

    def add(self, q: queue.Queue) -> None:
        with self._lock:
            self._queues.add(q)

    def drop(self, q: queue.Queue) -> None:
        with self._lock:
            self._queues.discard(q)

    def wake(self) -> int:
        with self._lock:
            targets = list(self._queues)
        for q in targets:
            q.put(True)
        return len(targets)


def parse_source_spec(raw: str, *, is_project: bool = False) -> dict:
    """Parse NAME=URL or NAME=DIR into a normalized source record."""
    if "=" in raw:
        name, val = raw.split("=", 1)
        name, val = name.strip(), val.strip()
    else:
        val = raw.strip()
        name = pathlib.Path(val).name if is_project else urllib.parse.urlsplit(val).netloc
    if not name or not val:
        raise ValueError(f"invalid source specification {raw!r}; expected NAME=VALUE")
    if is_project:
        return {"name": name, "project": str(pathlib.Path(val).expanduser().resolve())}
    return {"name": name, "url": val.rstrip("/")}


def _parse_kv_flags(raw_list: list[str], flag_name: str, expected: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in raw_list or []:
        if "=" not in raw:
            raise ValueError(f"invalid {flag_name} {raw!r}; expected {expected}")
        name, val = raw.split("=", 1)
        if not name.strip() or not val.strip():
            raise ValueError(f"invalid {flag_name} {raw!r}; expected {expected}")
        out[name.strip()] = val.strip()
    return out


def load_sources(args: argparse.Namespace) -> list[dict]:
    """Load deduplicated Hub sources and optional --model / --started-at overrides from CLI or config."""
    sources: list[dict] = []
    if args.config:
        cfg = json.loads(pathlib.Path(args.config).read_text(encoding="utf-8"))
        for entry in cfg.get("sources") or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            item = {"name": str(entry["name"])}
            if entry.get("model"):
                item["model"] = str(entry["model"])
            if entry.get("started_at"):
                item["started_at"] = str(entry["started_at"])
            if entry.get("project"):
                item["project"] = str(pathlib.Path(entry["project"]).expanduser().resolve())
            elif entry.get("url"):
                item["url"] = str(entry["url"]).rstrip("/")
            else:
                continue
            sources.append(item)
    for raw in args.source:
        sources.append(parse_source_spec(raw, is_project=False))
    for raw in args.project:
        sources.append(parse_source_spec(raw, is_project=True))
    models = _parse_kv_flags(getattr(args, "model", []), "--model", "NAME=MODEL")
    starts = _parse_kv_flags(getattr(args, "started_at", []), "--started-at", "NAME=ISO")
    deduped: dict[str, dict] = {}
    for src in sources:
        if src["name"] in models:
            src["model"] = models[src["name"]]
        if src["name"] in starts:
            src["started_at"] = starts[src["name"]]
        deduped[src["name"]] = src
    return list(deduped.values())


def fetch_source_view(src: dict, timeout: float = 3.0) -> dict:
    """Fetch full view.json dict for either an HTTP source or a local --project checkout."""
    if "project" in src:
        view_path = pathlib.Path(src["project"]) / "tools" / "board_graph_view" / "view.json"
        if not view_path.is_file():
            raise FileNotFoundError(f"missing {view_path}")
        return json.loads(view_path.read_text(encoding="utf-8"))
    with urllib.request.urlopen(f"{src['url']}/view.json", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


ROUTED_PREVIEW_CARDS = ("visual_review", "routing_batch", "verification", "quote_files")
HERO_VIEW_ORDER = {
    "3d_top.png": 0, "3d_bottom.png": 1, "front_copper.png": 2,
    "front_silk_and_mask.png": 3, "back_copper.png": 4, "back_silk_and_mask.png": 5, "all_copper_stack.png": 6,
}


def _collect_preview_images(src_name: str, view: dict) -> list[dict]:
    """Return routed PCB PNGs first (preferring final visual_review/routing_batch over unrouted placement)."""
    cards = ((view.get("artifacts") or {}).get("cards") or {})
    routed_sets = [cid for cid in ROUTED_PREVIEW_CARDS if any(
        str(i.get("path") or "").lower().endswith(".png") and i.get("exists")
        for i in ((cards.get(cid) or {}).get("items") or [])
    )]
    ordered_ids = routed_sets or [cid for cid in ("placement", *sorted(cards.keys())) if cid in cards]
    encoded_src = urllib.parse.quote(src_name, safe="")
    out, seen_names = [], set()
    for cid in ordered_ids:
        items = sorted(
            (i for i in ((cards.get(cid) or {}).get("items") or [])
             if str(i.get("path") or "").lower().endswith(".png") and i.get("exists")),
            key=lambda i: (HERO_VIEW_ORDER.get(pathlib.PurePosixPath(i["path"]).name.lower(), 50), i["path"].lower()),
        )
        for item in items:
            rel = str(item["path"]).lstrip("/")
            base = pathlib.PurePosixPath(rel).name.lower()
            if base in seen_names:
                continue
            seen_names.add(base)
            encoded_rel = "/".join(urllib.parse.quote(part, safe="") for part in rel.split("/"))
            out.append({"card": cid, "label": item.get("label") or base, "path": rel,
                        "url": f"/api/source/{encoded_src}/files/{encoded_rel}"})
    return out


def _compute_timing(src: dict, view: dict, board_track: dict) -> dict:
    """Compute run start/finish timestamps from explicit override, view.timing, or transitions/log."""
    timing = view.get("timing") or {}
    transitions = board_track.get("transitions") or []
    log_rows = (view.get("log") or {}).get("rows") or []
    started_at = src.get("started_at") or timing.get("started_at")
    if not started_at:
        ats = [r["at"] for r in transitions if r.get("at")] + [r["at"] for r in log_rows if r.get("at")]
        started_at = min(ats) if ats else None
    finished_at = timing.get("finished_at")
    if not finished_at and transitions:
        last = transitions[-1]
        until = (view.get("autonomy") or {}).get("until")
        if board_track.get("done") or (until and last.get("node") == until and last.get("outcome") == "pass"):
            finished_at = last.get("at")
    return {"started_at": started_at, "finished_at": finished_at, "running": bool(started_at and not finished_at)}


def summarize_source(src: dict, timeout: float = 3.0) -> dict:
    """Build compact live status summary for one configured Hub source."""
    base = {"name": src["name"], "url": src.get("url"), "project_dir": src.get("project"), "model": src.get("model")}
    try:
        view = fetch_source_view(src, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {**base, "online": False, "error": str(exc), "cards": [], "preview_images": []}

    graph_nodes = [n for n in (view.get("graph") or {}).get("nodes", []) if n.get("track") == "board"]
    node_states = (view.get("state") or {}).get("nodes") or {}
    board_track = next((t for t in (view.get("where") or {}).get("tracks", []) if t.get("id") == "board"), {})
    transitions = board_track.get("transitions") or []
    last = transitions[-1] if transitions else None
    position = board_track.get("position") or ((view.get("state") or {}).get("tracks") or {}).get("board", {}).get("position")
    model = src.get("model") or view.get("model") or (view.get("autonomy") or {}).get("model")

    cards = []
    passed_count = 0
    working = None
    blocked_outcome = None
    for n in graph_nodes:
        nid = n["id"]
        st = node_states.get(nid) or {}
        status = st.get("status", "ahead")
        if status == "passed":
            passed_count += 1
        attempt = st.get("attempt")
        last_node = st.get("last")
        last_out = last_node.get("outcome") if last_node else None
        if status == "current":
            if attempt:
                working = attempt.get("by")
            elif last_out in ("needs_owner", "needs_lead", "needs_skill"):
                blocked_outcome = last_out
        cards.append({
            "id": nid,
            "label": n.get("label") or nid,
            "who": n.get("who"),
            "status": status,
            "working": attempt.get("by") if attempt else None,
            "last_outcome": last_out,
        })

    return {
        **base,
        "online": True,
        "project": view.get("project") or src["name"],
        "model": model,
        "autonomy_until": (view.get("autonomy") or {}).get("until"),
        "position": position,
        "passed_count": passed_count,
        "working": working,
        "blocked_outcome": blocked_outcome,
        "tool": view.get("tool"),
        "tool_heal": view.get("tool_heal"),
        "timing": _compute_timing(src, view, board_track),
        "preview_images": _collect_preview_images(src["name"], view),
        "last": {
            "node": last.get("node"),
            "outcome": last.get("outcome"),
            "target": last.get("target"),
            "actor": last.get("actor"),
            "note": last.get("note"),
            "at": last.get("at"),
        } if last else None,
        "cards": cards,
    }


def _fetch_source_file(src: dict, rel: str, raw: bool = False, timeout: float = 5.0) -> tuple[bytes, str]:
    """Read raw file bytes and content-type from an HTTP or local project source."""
    parts = [urllib.parse.unquote(p) for p in rel.split("/") if p]
    if not parts or any(p in (".", "..") or "\\" in p for p in parts) or parts[0].startswith("."):
        raise PermissionError("forbidden path")
    if "project" in src:
        base = pathlib.Path(src["project"]).resolve()
        target = base.joinpath(*parts).resolve()
        target.relative_to(base)
        if not target.is_file():
            raise FileNotFoundError(rel)
        ctype = "text/plain; charset=utf-8" if raw else (mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        return target.read_bytes(), ctype
    encoded = "/".join(urllib.parse.quote(p, safe="") for p in parts)
    suffix = "?raw=1" if raw else ""
    with urllib.request.urlopen(f"{src['url']}/files/{encoded}{suffix}", timeout=timeout) as resp:
        return resp.read(), resp.headers.get_content_type() or "application/octet-stream"


def _fetch_live_pcb_bytes(src: dict, timeout: float = 5.0) -> tuple[bytes, str]:
    """Fetch /live-pcb.png or fall back to newest artifact PNG on older Cycle 1 servers."""
    if "project" in src:
        import board_graph_serve
        png, _, _ = board_graph_serve.latest_live_pcb(pathlib.Path(src["project"]))
        if png and png.is_file():
            return png.read_bytes(), "image/png"
    else:
        try:
            with urllib.request.urlopen(f"{src['url']}/live-pcb.png", timeout=timeout) as resp:
                return resp.read(), resp.headers.get_content_type() or "image/png"
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    view = fetch_source_view(src, timeout=timeout)
    previews = _collect_preview_images(src["name"], view)
    if not previews:
        raise FileNotFoundError("no live PCB render or artifact PNG available")
    return _fetch_source_file(src, previews[0]["path"], timeout=timeout)


def _start_source_watchers(
    sources: list[dict], listeners: HubListeners, stop_event: threading.Event,
    tool_dev_dir: pathlib.Path | None = None,
) -> None:
    def watch_http(src: dict) -> None:
        sse_url = f"{src['url']}/events"
        while not stop_event.is_set():
            try:
                req = urllib.request.Request(sse_url, headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    while not stop_event.is_set():
                        raw = resp.readline()
                        if not raw:
                            break
                        if raw.startswith(b"data:"):
                            listeners.wake()
            except Exception:  # noqa: BLE001
                stop_event.wait(2.0)

    def watch_local(projects: list[dict]) -> None:
        mtimes: dict[str, tuple] = {}
        while not stop_event.is_set():
            changed = False
            for src in projects:
                p = pathlib.Path(src["project"]) / "tools" / "board_graph_view" / "view.json"
                try:
                    ns = (p.stat().st_mtime_ns,)
                except OSError:
                    ns = (0,)
                if mtimes.get(src["name"]) != ns:
                    if src["name"] in mtimes:
                        changed = True
                    mtimes[src["name"]] = ns
            if tool_dev_dir is not None:
                td = tool_dev_dir / "logs" / "tool-dev"
                cur = td / "current.json"
                snap = [cur.stat().st_mtime_ns if cur.is_file() else 0]
                rel_dir = td / "releases"
                if rel_dir.is_dir():
                    snap.extend(st.stat().st_mtime_ns for st in sorted(rel_dir.glob("*/state.sqlite3")))
                tkey = tuple(snap)
                if mtimes.get("__tool_dev__") != tkey:
                    if "__tool_dev__" in mtimes:
                        changed = True
                    mtimes["__tool_dev__"] = tkey
            if changed:
                listeners.wake()
            stop_event.wait(1.0)

    for src in sources:
        if "url" in src:
            threading.Thread(target=watch_http, args=(src,), daemon=True).start()
    locals_list = [s for s in sources if "project" in s]
    if locals_list or tool_dev_dir is not None:
        threading.Thread(target=watch_local, args=(locals_list,), daemon=True).start()


class HubServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], sources: list[dict], listeners: HubListeners,
                 default_scope: str = "auto", tool_dev_dir: pathlib.Path | None = None):
        self.sources = sources
        self.source_map = {s["name"]: s for s in sources}
        self.listeners = listeners
        self.default_scope = default_scope
        self.tool_dev_dir = tool_dev_dir
        super().__init__(server_address, HubHandler)


class HubHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route_source(self, path: str, query: str) -> bool:
        rest = path[len("/api/source/"):]
        if "/" not in rest:
            return False
        raw_name, sub = rest.split("/", 1)
        src = self.server.source_map.get(urllib.parse.unquote(raw_name))
        if not src:
            self.send_error(404)
            return True
        if sub == "view.json":
            try:
                view = fetch_source_view(src, timeout=5.0)
                self._send(200, "application/json; charset=utf-8", json.dumps(view).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                self._send(502, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode("utf-8"))
            return True
        if sub == "live-pcb.png" or sub.startswith("files/"):
            raw = urllib.parse.parse_qs(query).get("raw") == ["1"]
            try:
                data, ctype = (_fetch_live_pcb_bytes(src) if sub == "live-pcb.png"
                               else _fetch_source_file(src, sub[len("files/"):], raw=raw))
                self._send(200, ctype, data)
            except (FileNotFoundError, urllib.error.HTTPError):
                self.send_error(404)
            except PermissionError:
                self.send_error(403)
            except Exception as exc:  # noqa: BLE001
                self._send(502, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode("utf-8"))
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HUB_HTML.encode("utf-8"))
        elif path == "/api/summary":
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(self.server.sources))) as pool:
                rows = list(pool.map(summarize_source, self.server.sources))
            local_heal = board_heal.tool_heal_summary(self.server.tool_dev_dir) if self.server.tool_dev_dir else None
            payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "default_scope": self.server.default_scope,
                "local_tool_heal": local_heal,
                "sources": rows,
            }
            self._send(200, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))
        elif path == "/api/tool-heal":
            local_heal = board_heal.tool_heal_summary(self.server.tool_dev_dir) if self.server.tool_dev_dir else None
            self._send(200, "application/json; charset=utf-8", json.dumps(local_heal).encode("utf-8"))
        elif path.startswith("/api/source/") and self._route_source(path, parsed.query):
            return
        elif path == "/events":
            self._serve_events()
        else:
            self.send_error(404)

    def _serve_events(self) -> None:
        turn: queue.Queue = queue.Queue()
        self.server.listeners.add(turn)
        try:
            self.send_response(200)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps({'ts': time.time()})}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    turn.get(timeout=KEEPALIVE)
                    while not turn.empty():
                        turn.get_nowait()
                    self.wfile.write(f"data: {json.dumps({'ts': time.time()})}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.listeners.drop(turn)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", default=[], help="NAME=URL of a live board-graph serve endpoint")
    parser.add_argument("--project", action="append", default=[], help="NAME=DIR of a local board-graph project")
    parser.add_argument("--model", action="append", default=[], help="NAME=MODEL label for a configured source")
    parser.add_argument("--started-at", action="append", default=[], help="NAME=ISO start timestamp for a configured source")
    parser.add_argument("--until", default="auto",
                        help="Stop card for visible pipeline scope ('auto' uses each run's autonomy.until, 'all' shows every card)")
    parser.add_argument("--config", help="JSON file with {\"sources\": [{\"name\": ..., \"url\"|\"project\": ...}]}")
    parser.add_argument("--tool-dev", help="board-graph repo root for 6-card local tool self-healing status")
    parser.add_argument("--no-tool-dev", action="store_true", help="Disable local 6-card tool workflow status")
    parser.add_argument("--bind", default="127.0.0.1", help="Address to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    return parser.parse_args(argv)


def _resolve_tool_dev_dir(args: argparse.Namespace) -> pathlib.Path | None:
    if getattr(args, "no_tool_dev", False):
        return None
    raw = getattr(args, "tool_dev", None)
    cand = pathlib.Path(raw).expanduser().resolve() if raw else pathlib.Path(tool_paths.TOOL_ROOT).resolve()
    return cand if board_heal.tool_dev_available(cand) else None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        sources = load_sources(args)
    except (OSError, ValueError) as exc:
        print(f"board-graph hub: {exc}", file=sys.stderr)
        return 2
    if not sources:
        try:
            sources = [parse_source_spec(f"{pathlib.Path.cwd().name}={pathlib.Path.cwd()}", is_project=True)]
        except ValueError:
            pass
    tool_dev_dir = _resolve_tool_dev_dir(args)
    listeners = HubListeners()
    stop_event = threading.Event()
    _start_source_watchers(sources, listeners, stop_event, tool_dev_dir=tool_dev_dir)
    httpd = HubServer((args.bind, args.port), sources, listeners, default_scope=args.until, tool_dev_dir=tool_dev_dir)
    print(f"board-graph hub serving {len(sources)} source(s) at http://{args.bind}:{httpd.server_port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
