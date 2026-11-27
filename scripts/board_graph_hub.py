#!/usr/bin/env python3
"""Multi-board / multi-run flat 2D board-graph Hub with live SSE updates."""
from __future__ import annotations

import argparse
import concurrent.futures
import http.server
import json
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

KEEPALIVE = 15

HUB_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>board-graph Hub</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #08090c; --surface: #111318; --surface-2: #171b24; --surface-hover: #1e2330;
    --ink-1: #f2f1ed; --ink-2: #c2c1b8; --ink-3: #85847c;
    --hair: rgba(255,255,255,0.10); --hair-strong: rgba(255,255,255,0.18);
    --blue: #3987e5; --blue-bg: rgba(57,135,229,0.16); --blue-soft: #86b6ef;
    --amber: #e2a336; --amber-bg: rgba(226,163,54,0.15);
    --red: #e5534b; --red-bg: rgba(229,83,75,0.16);
    --green: #3fb950; --green-bg: rgba(63,185,80,0.14);
    --font: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
    --mono: ui-monospace, "Cascadia Mono", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink-1); font: 400 13.5px/1.5 var(--font); -webkit-font-smoothing: antialiased; }
  a { color: var(--blue-soft); text-decoration: none; }
  a:hover { text-decoration: underline; }
  button { font: inherit; color: inherit; cursor: pointer; background: none; border: none; }
  code { font-family: var(--mono); }
  .header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px;
            padding: 14px 20px; border-bottom: 1px solid var(--hair); background: var(--surface); position: sticky; top: 0; z-index: 20; }
  .header h1 { margin: 0; font-size: 18px; font-weight: 650; }
  .sub { font-size: 12px; color: var(--ink-3); }
  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px; border-radius: 999px;
          border: 1px solid var(--hair); background: var(--surface-2); font-size: 12px; color: var(--ink-2); }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ink-3); }
  .dot.live { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .wrap { padding: 18px 20px 44px; display: flex; flex-direction: column; gap: 16px; }
  .run-row { background: var(--surface); border: 1px solid var(--hair); border-radius: 10px; padding: 14px 16px;
             display: flex; flex-direction: column; gap: 10px; }
  .run-top { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; }
  .run-title { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .run-title h2 { margin: 0; font-size: 16px; }
  .badge { padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; border: 1px solid var(--hair); color: var(--ink-2); }
  .badge.ok { color: var(--green); border-color: rgba(63,185,80,0.45); background: var(--green-bg); }
  .badge.working { color: #9ecaff; border-color: rgba(134,182,239,0.6); background: var(--blue-bg); }
  .badge.owner { color: var(--amber); border-color: rgba(226,163,54,0.55); background: var(--amber-bg); }
  .badge.lead { color: var(--red); border-color: rgba(229,83,75,0.55); background: var(--red-bg); }
  .badge.off { color: var(--red); border-color: rgba(229,83,75,0.4); }
  .btn { padding: 5px 10px; border: 1px solid var(--hair); border-radius: 6px; background: var(--surface-2); color: var(--ink-2); font-size: 12px; }
  .btn:hover { background: var(--surface-hover); color: var(--ink-1); }
  .strip { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip { padding: 6px 9px; border-radius: 7px; border: 1px solid var(--hair); background: var(--surface-2);
          font-size: 12px; text-align: left; display: flex; flex-direction: column; gap: 2px; min-width: 112px; }
  .chip:hover { border-color: var(--hair-strong); background: var(--surface-hover); }
  .chip.passed { border-color: rgba(57,135,229,0.55); background: rgba(28,92,171,0.16); }
  .chip.current { border-color: rgba(134,182,239,0.85); background: rgba(134,182,239,0.14); }
  .chip.owner { border-color: rgba(226,163,54,0.8); background: var(--amber-bg); }
  .chip.lead { border-color: rgba(229,83,75,0.85); background: var(--red-bg); }
  .chip.ahead { opacity: 0.72; }
  .chip-lbl { font-weight: 600; color: var(--ink-1); }
  .chip-st { font-size: 10.5px; color: var(--ink-3); font-family: var(--mono); }
  .last-line { font-size: 12.5px; color: var(--ink-2); background: #0c0e13; border: 1px solid var(--hair);
               border-radius: 6px; padding: 8px 10px; white-space: pre-wrap; word-break: break-word; }
  .tool-note { font-size: 11.5px; color: var(--ink-2); font-family: var(--mono); }
  .drawer-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.45); z-index: 30; display: none; }
  .drawer-backdrop.open { display: block; }
  .drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(560px, 100vw); background: var(--surface);
            border-left: 1px solid var(--hair-strong); z-index: 35; display: flex; flex-direction: column;
            transform: translateX(100%); transition: transform .15s ease-out; }
  .drawer.open { transform: translateX(0); }
  .drawer-head { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-bottom: 1px solid var(--hair); }
  .drawer-head h3 { margin: 0; font-size: 16px; }
  .drawer-body { flex: 1; overflow-y: auto; padding: 16px 18px 28px; display: flex; flex-direction: column; gap: 12px; }
  pre.box { margin: 0; padding: 10px; border-radius: 6px; border: 1px solid var(--hair); background: #0c0e13; white-space: pre-wrap; word-break: break-word; font-size: 12px; font-family: var(--mono); }
</style>
</head>
<body>
<header class="header">
  <div>
    <h1>board-graph Hub</h1>
    <div class="sub" id="sub">Loading sources...</div>
  </div>
  <span class="pill"><span id="dot" class="dot"></span><span id="live-lbl">Connecting</span></span>
</header>
<main class="wrap" id="rows"></main>
<div class="drawer-backdrop" id="backdrop"></div>
<aside class="drawer" id="drawer">
  <div class="drawer-head"><h3 id="d-title">Card</h3><button class="btn" id="d-close">Close</button></div>
  <div class="drawer-body" id="d-body"></div>
</aside>
<script>
(() => {
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const $ = (id) => document.getElementById(id);

  function closeDrawer() { $('drawer').classList.remove('open'); $('backdrop').classList.remove('open'); }
  $('d-close').addEventListener('click', closeDrawer);
  $('backdrop').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

  async function inspectCard(sourceName, nodeId) {
    $('d-title').textContent = `${sourceName} / ${nodeId}`;
    $('d-body').innerHTML = '<div class="sub">Loading view...</div>';
    $('drawer').classList.add('open'); $('backdrop').classList.add('open');
    try {
      const resp = await fetch(`/api/source/${encodeURIComponent(sourceName)}/view.json`, { cache: 'no-store' });
      const view = await resp.json();
      const node = ((view.graph && view.graph.nodes) || []).find((n) => n.id === nodeId) || {};
      const st = (view.state && view.state.nodes && view.state.nodes[nodeId]) || {};
      const last = st.last;
      const attempt = st.attempt;
      $('d-title').textContent = `${view.project || sourceName} / ${node.label || nodeId}`;
      const blocks = [`<div><code>${esc(nodeId)}</code> &middot; status <code>${esc(st.status || 'ahead')}</code> &middot; who <code>${esc(node.who || '')}</code></div>`];
      if (attempt) blocks.push(`<div><strong>Active attempt</strong><pre class="box">${esc(attempt.by)} since ${esc(attempt.since)}\\n${esc(attempt.note || '')}</pre></div>`);
      if (last) blocks.push(`<div><strong>Last transition (${esc(last.outcome)} by ${esc(last.actor)})</strong><pre class="box">${esc(last.note || '')}</pre></div>`);
      if (node.task) blocks.push(`<div><strong>Task</strong><pre class="box">${esc(node.task)}</pre></div>`);
      $('d-body').innerHTML = blocks.join('');
    } catch (err) {
      $('d-body').innerHTML = `<pre class="box">${esc(err.message)}</pre>`;
    }
  }

  function chipState(c) {
    if (c.status === 'passed') return ['passed', 'passed'];
    if (c.status === 'current') {
      if (c.working) return ['current', `working (${c.working})`];
      if (c.last_outcome === 'needs_owner') return ['owner', 'needs you'];
      if (c.last_outcome === 'needs_lead' || c.last_outcome === 'needs_skill') return ['lead', c.last_outcome];
      return ['current', 'current'];
    }
    return ['ahead', c.who || 'ahead'];
  }

  function render(data) {
    const sources = data.sources || [];
    $('sub').textContent = `${sources.length} source${sources.length === 1 ? '' : 's'} · updated ${new Date().toLocaleTimeString()}`;
    const wrap = $('rows');
    wrap.innerHTML = '';
    for (const src of sources) {
      const card = document.createElement('section');
      card.className = 'run-row';
      const statusBadge = !src.online
        ? `<span class="badge off">Offline</span>`
        : src.blocked_outcome === 'needs_owner'
          ? `<span class="badge owner">Needs you at ${esc(src.position)}</span>`
          : src.blocked_outcome
            ? `<span class="badge lead">${esc(src.blocked_outcome)} at ${esc(src.position)}</span>`
            : src.working
              ? `<span class="badge working">Working: ${esc(src.position)}</span>`
              : `<span class="badge ok">${esc(src.position || 'Ready')}</span>`;
      const tool = src.tool;
      const toolStr = tool ? `tool: ${tool.branch || ''}${tool.commit ? ' @' + tool.commit : ''}${tool.ahead ? ' +' + tool.ahead + ' ahead' : ''}${tool.dirty ? ' dirty' : ''}` : '';
      const toolCommits = (tool && tool.commits_ahead && tool.commits_ahead.length)
        ? `<div class="tool-note">${tool.commits_ahead.map((c) => `<code>${esc(c.sha)}</code> ${esc(c.subject)}`).join(' · ')}</div>` : '';
      const openLink = src.url ? `<a class="btn" href="${esc(src.url)}" target="_blank" rel="noopener">Open board</a>` : '';
      const chips = (src.cards || []).map((c) => {
        const [cls, stText] = chipState(c);
        return `<button class="chip ${cls}" type="button" data-src="${esc(src.name)}" data-node="${esc(c.id)}"><span class="chip-lbl">${esc(c.label)}</span><span class="chip-st">${esc(stText)}</span></button>`;
      }).join('');
      const last = src.last;
      const lastHtml = last
        ? `<div class="last-line"><strong>${esc(last.node)}</strong> <code>${esc(last.outcome)}</code> &rarr; <code>${esc(last.target)}</code> (${esc(last.actor)}): ${esc(last.note || '')}</div>`
        : (src.error ? `<div class="last-line" style="border-color:var(--red)">${esc(src.error)}</div>` : '');
      card.innerHTML = `
        <div class="run-top">
          <div class="run-title">
            <h2>${esc(src.project || src.name)}</h2>
            ${statusBadge}
            ${toolStr ? `<span class="pill" style="font-family:var(--mono);font-size:11.5px">${esc(toolStr)}</span>` : ''}
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="sub">${src.passed_count || 0}/${(src.cards || []).length} passed</span>
            ${openLink}
          </div>
        </div>
        ${toolCommits}
        <div class="strip">${chips}</div>
        ${lastHtml}
      `;
      card.querySelectorAll('.chip').forEach((el) => el.addEventListener('click', () => inspectCard(el.dataset.src, el.dataset.node)));
      wrap.appendChild(card);
    }
  }

  async function refresh() {
    try {
      const resp = await fetch('/api/summary', { cache: 'no-store' });
      render(await resp.json());
    } catch (err) {
      $('sub').textContent = `Error: ${err.message}`;
    }
  }

  const es = new EventSource('/events');
  es.onopen = () => { $('dot').className = 'dot live'; $('live-lbl').textContent = 'Live'; };
  es.onmessage = () => refresh();
  es.onerror = () => { $('dot').className = 'dot'; $('live-lbl').textContent = 'Reconnecting'; };
  refresh();
})();
</script>
</body>
</html>
"""


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


def load_sources(args: argparse.Namespace) -> list[dict]:
    """Load deduplicated Hub sources from --source, --project, and optional --config JSON."""
    sources: list[dict] = []
    if args.config:
        cfg = json.loads(pathlib.Path(args.config).read_text(encoding="utf-8"))
        for entry in cfg.get("sources") or []:
            if isinstance(entry, dict) and entry.get("name"):
                if entry.get("project"):
                    sources.append({"name": str(entry["name"]), "project": str(pathlib.Path(entry["project"]).expanduser().resolve())})
                elif entry.get("url"):
                    sources.append({"name": str(entry["name"]), "url": str(entry["url"]).rstrip("/")})
    for raw in args.source:
        sources.append(parse_source_spec(raw, is_project=False))
    for raw in args.project:
        sources.append(parse_source_spec(raw, is_project=True))
    deduped: dict[str, dict] = {}
    for src in sources:
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


def summarize_source(src: dict, timeout: float = 3.0) -> dict:
    """Build compact live status summary for one configured Hub source."""
    base = {"name": src["name"], "url": src.get("url"), "project_dir": src.get("project")}
    try:
        view = fetch_source_view(src, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {**base, "online": False, "error": str(exc), "cards": []}

    graph_nodes = [n for n in (view.get("graph") or {}).get("nodes", []) if n.get("track") == "board"]
    node_states = (view.get("state") or {}).get("nodes") or {}
    board_track = next((t for t in (view.get("where") or {}).get("tracks", []) if t.get("id") == "board"), {})
    transitions = board_track.get("transitions") or []
    last = transitions[-1] if transitions else None
    position = board_track.get("position") or ((view.get("state") or {}).get("tracks") or {}).get("board", {}).get("position")

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
        "position": position,
        "passed_count": passed_count,
        "working": working,
        "blocked_outcome": blocked_outcome,
        "tool": view.get("tool"),
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


def _start_source_watchers(sources: list[dict], listeners: HubListeners, stop_event: threading.Event) -> None:
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
        mtimes: dict[str, int] = {}
        while not stop_event.is_set():
            changed = False
            for src in projects:
                p = pathlib.Path(src["project"]) / "tools" / "board_graph_view" / "view.json"
                try:
                    ns = p.stat().st_mtime_ns
                except OSError:
                    ns = 0
                if mtimes.get(src["name"]) != ns:
                    if src["name"] in mtimes:
                        changed = True
                    mtimes[src["name"]] = ns
            if changed:
                listeners.wake()
            stop_event.wait(1.0)

    locals_list = [s for s in sources if "project" in s]
    for src in sources:
        if "url" in src:
            threading.Thread(target=watch_http, args=(src,), daemon=True).start()
    if locals_list:
        threading.Thread(target=watch_local, args=(locals_list,), daemon=True).start()


class HubServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], sources: list[dict], listeners: HubListeners):
        self.sources = sources
        self.source_map = {s["name"]: s for s in sources}
        self.listeners = listeners
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

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HUB_HTML.encode("utf-8"))
        elif path == "/api/summary":
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(self.server.sources))) as pool:
                rows = list(pool.map(summarize_source, self.server.sources))
            payload = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sources": rows}
            self._send(200, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))
        elif path.startswith("/api/source/") and path.endswith("/view.json"):
            raw_name = urllib.parse.unquote(path[len("/api/source/"):-len("/view.json")])
            src = self.server.source_map.get(raw_name)
            if not src:
                self.send_error(404)
                return
            try:
                view = fetch_source_view(src, timeout=5.0)
            except Exception as exc:  # noqa: BLE001
                self._send(502, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode("utf-8"))
                return
            self._send(200, "application/json; charset=utf-8", json.dumps(view).encode("utf-8"))
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
    parser.add_argument("--config", help="JSON file with {\"sources\": [{\"name\": ..., \"url\"|\"project\": ...}]}")
    parser.add_argument("--bind", default="127.0.0.1", help="Address to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    return parser.parse_args(argv)


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
    listeners = HubListeners()
    stop_event = threading.Event()
    _start_source_watchers(sources, listeners, stop_event)
    httpd = HubServer((args.bind, args.port), sources, listeners)
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
