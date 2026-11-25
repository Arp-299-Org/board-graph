#!/usr/bin/env python3
"""Serve the board graph page from the Mac: tools/board_graph_view/ in the Mac's main checkout, whose view.json
every write to the graph's database rewrites, at ADDRESS (the site settings' graph_page.address), plus read-only
project files and folders under /files/<path> so clicking a card opens its schematic, datasheets, renders or docs.

  python3 scripts/board_graph_serve.py            serve until stopped (launchd runs this and keeps it running)
  python3 scripts/board_graph_serve.py --install  install or update the launchd job (it starts now): the project's
                                                  own tools/mac/<launchd_label>.plist when it keeps one, else the
                                                  job this script writes from the site settings (written_plist)
  python3 scripts/board_graph_serve.py --status   one JSON line: the address, launchd's copy of the plist, the
                                                  job's state and pid, and whether the page answers
  python3 scripts/board_graph_serve.py --notify   tell the server the folder changed: exit 0, or 1 and why not

It answers on the address's host name, the name the PC reaches the Mac by and the Mac's own name on the tailnet,
so the page is reachable from the owner's devices over Tailscale and from nothing else; when that name does not
resolve yet
(Tailscale not up after a reboot) it exits and launchd starts it again. A request is answered from the folder and
never logged. Every answer says Cache-Control: no-cache.

The page never asks whether something changed: it is told. An open page holds GET /events, a stream that says
`data: {"view": <view.json's modification time>, "page": <index.html's>}` once when it opens and again each time
the Mac itself says POST /changed (notify() below, which every writer of the folder calls: the snapshot after it
writes view.json, the sync run after it fast-forwards main). The page reloads itself when the page stamp differs
from the one it was served with, and reads view.json again otherwise. A connection from any other machine is
refused with 403. `where --full` reads --status over SSH (scripts/tidy.py's Mac status) and says whether the page
is served and how many pages are listening.
"""
from __future__ import annotations

import argparse
import csv
import functools
import html
import http.server
import io
import json
import mimetypes
import os
import pathlib
import plistlib
import queue
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

import tool_paths

if __name__ == "__main__":
    tool_paths.take_argument()

import launchd_job  # noqa: E402
import mac  # noqa: E402

TOOL_ROOT = tool_paths.TOOL_ROOT
ROOT = tool_paths.project_root()
FOLDER = ROOT / "tools" / "board_graph_view"
LABEL = mac.SITE.launchd_label  # the page's address and launchd job come from the site settings
PLIST = ROOT / "tools" / "mac" / f"{LABEL}.plist"  # a project's own plist, when it keeps one; job() says which is used
ADDRESS = mac.SITE.page_address
HOST = urllib.parse.urlsplit(ADDRESS).hostname  # the name it answers on
PORT = urllib.parse.urlsplit(ADDRESS).port or 80
KEEPALIVE = 15  # seconds between comment lines on a quiet /events stream, so a dropped link is noticed
NOTES_EVERY = 3  # seconds between looks at the notes files' stamps, in a project whose graphs keep notes

HIDDEN_ROOTS = {"logs", "__pycache__"}
INLINE_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".rpt": "text/plain; charset=utf-8",
    ".dru": "text/plain; charset=utf-8",
    ".kicad_dru": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
}
LCSC_PART = re.compile(r"^C\d{3,}$")
INLINE_LINK = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)]+)\)")


class Listeners:
    """The open /events streams: one queue each. `wake` hands every one a turn to send the stamps now."""

    def __init__(self):
        self.queues: set[queue.Queue] = set()
        self.lock = threading.Lock()

    def add(self, turn: queue.Queue) -> None:
        with self.lock:
            self.queues.add(turn)

    def drop(self, turn: queue.Queue) -> None:
        with self.lock:
            self.queues.discard(turn)

    def wake(self) -> int:
        with self.lock:
            turns = list(self.queues)
        for turn in turns:
            turn.put(True)
        return len(turns)

    def __len__(self) -> int:
        with self.lock:
            return len(self.queues)


def notes_files(root: pathlib.Path = ROOT) -> list:
    """The notes files of this project's graphs (the pack's `notes`, one per listed graph), as paths; none for a
    pack with no notes, and when the pack or the list cannot be read."""
    try:
        import boards
        import graph_pack
        notes = graph_pack.read(root).get("notes")  # the pack file alone: no pack's scripts are loaded to serve
        if not notes:
            return []
        names = list(boards.read(root)) or [None]
    except Exception:  # noqa: BLE001 - a page is served whatever the project's files say
        return []
    pack = graph_pack.Pack(graph_pack.NONE, "", notes=notes)
    return [pathlib.Path(root) / path for path in (pack.notes_path(name) for name in names) if path]


def page_index(folder: pathlib.Path, external: bool = False) -> pathlib.Path:
    """The folder's index.html, falling back to TOOL_ROOT's copy in external mode when the project has none."""
    local_page = pathlib.Path(folder) / "index.html"
    if local_page.is_file() or not external:
        return local_page
    shared_page = TOOL_ROOT / "tools" / "board_graph_view" / "index.html"
    return shared_page if shared_page.is_file() else local_page


def stamps(folder: pathlib.Path, notes=(), *, external: bool = False) -> dict:
    """What an /events line carries: the modification time of view.json and of index.html in whole seconds (the
    page compares `page` with the Last-Modified header it was served with, which has whole seconds), 0 when the
    file is missing. With `notes`, the graphs' notes files, also the newest of their modification times, so a
    page shows notes written between two records."""
    def stamp(path) -> int:
        try:
            return int(os.stat(path).st_mtime)
        except OSError:
            return 0
    found = {"view": stamp(folder / "view.json"), "page": stamp(page_index(folder, external=external))}
    if notes:
        found["notes"] = max(stamp(path) for path in notes)
    return found


def local(client: str, bound: str) -> bool:
    """Whether a request comes from the Mac itself: loopback, or the address the server is bound to."""
    return client in ("127.0.0.1", "::1") or client == bound


def _url_path(rel: str) -> str:
    return "/".join(urllib.parse.quote(part) for part in rel.split("/") if part)


def _size_words(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB" if size < 10 * 1024 else f"{round(size / 1024)} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _file_kind(path: pathlib.Path) -> str:
    if path.is_dir():
        return "Folder"
    ext = path.suffix.lower()
    return {
        ".pdf": "PDF",
        ".png": "Image",
        ".jpg": "Image",
        ".jpeg": "Image",
        ".webp": "Image",
        ".svg": "SVG",
        ".csv": "CSV",
        ".md": "Doc",
        ".json": "JSON",
        ".rpt": "Report",
        ".txt": "Text",
        ".step": "3D STEP",
        ".stp": "3D STEP",
        ".zip": "Archive",
        ".kicad_sch": "KiCad Sch",
        ".kicad_pcb": "KiCad PCB",
        ".kicad_mod": "Footprint",
    }.get(ext, ext.lstrip(".").upper() or "File")


def resolve_repo_path(repo_root: pathlib.Path, raw_rel: str) -> tuple[str | None, pathlib.Path | None, int]:
    """Resolve a /files/<raw_rel> path safely inside `repo_root`.

    Returns (clean_rel_posix, resolved_path, http_status_if_error). Dotfiles, `..`, `logs/` and symlinks outside
    `repo_root` are refused.
    """
    unquoted = urllib.parse.unquote(raw_rel)
    if "\\" in unquoted or "\x00" in unquoted:
        return None, None, 403
    parts = [part for part in unquoted.split("/") if part]
    if any(part in (".", "..", "__pycache__") or part.startswith(".") for part in parts):
        return None, None, 403
    if parts and parts[0] in HIDDEN_ROOTS:
        return None, None, 403
    base = repo_root.resolve()
    candidate = base.joinpath(*parts) if parts else base
    try:
        resolved = candidate.resolve()
        resolved.relative_to(base)
    except (OSError, ValueError):
        return None, None, 403
    if not resolved.exists():
        return None, None, 404
    return "/".join(parts), resolved, 200


READER_CSS = """
  :root {
    color-scheme: dark;
    --bg: #08090c; --surface: #111318; --surface-hover: #191c24;
    --ink-1: #f2f1ed; --ink-2: #c2c1b8; --ink-3: #85847c;
    --hair: rgba(255, 255, 255, 0.10); --accent: #6ea8f0;
    --font: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
    --mono: ui-monospace, "Cascadia Mono", "SF Mono", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink-1); font: 400 15px/1.6 var(--font);
         -webkit-font-smoothing: antialiased; }
  .wrap { max-width: 960px; margin: 0 auto; padding: 28px 24px 64px; }
  .topbar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px;
            padding-bottom: 16px; margin-bottom: 24px; border-bottom: 1px solid var(--hair); font-size: 13.5px; }
  .crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; color: var(--ink-3); }
  .crumbs a, .btn { color: var(--ink-2); text-decoration: none; }
  .crumbs a:hover { color: var(--ink-1); text-decoration: underline; }
  .btn { padding: 6px 12px; border: 1px solid var(--hair); border-radius: 6px; background: var(--surface);
         font-weight: 500; font-size: 13px; }
  .btn:hover { background: var(--surface-hover); color: var(--ink-1); }
  h1 { font-size: 24px; line-height: 1.25; margin: 0 0 6px; letter-spacing: -0.01em; }
  .sub { color: var(--ink-3); font-size: 13.5px; margin: 0 0 20px; }
  .search { width: 100%; max-width: 360px; padding: 8px 12px; margin: 0 0 16px; border-radius: 6px;
            border: 1px solid var(--hair); background: var(--surface); color: var(--ink-1); font: inherit; font-size: 14px; }
  .search:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
  .files { list-style: none; padding: 0; margin: 0; border: 1px solid var(--hair); border-radius: 8px; overflow: hidden; }
  .files li + li { border-top: 1px solid var(--hair); }
  .files a { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 14px;
             padding: 11px 16px; color: var(--ink-1); text-decoration: none; background: var(--surface); }
  .files a:hover { background: var(--surface-hover); }
  .fname { overflow-wrap: anywhere; font-weight: 500; }
  .badge { font-size: 11.5px; color: var(--ink-2); padding: 2px 8px; border-radius: 999px;
           border: 1px solid var(--hair); white-space: nowrap; }
  .fsize { font-size: 12.5px; color: var(--ink-3); font-variant-numeric: tabular-nums; min-width: 64px; text-align: right; }
  .doc h1, .doc h2, .doc h3, .doc h4 { line-height: 1.3; margin: 26px 0 10px; }
  .doc h1 { font-size: 26px; margin-top: 0; }
  .doc h2 { font-size: 19px; padding-top: 10px; border-top: 1px solid var(--hair); }
  .doc h3 { font-size: 16px; color: var(--ink-2); }
  .doc p, .doc ul, .doc ol, .doc blockquote { margin: 0 0 12px; max-width: 76ch; }
  .doc ul, .doc ol { padding-left: 22px; }
  .doc li + li { margin-top: 4px; }
  .doc code { font: 0.9em var(--mono); background: rgba(255,255,255,0.06); padding: 1px 5px; border-radius: 4px; }
  .doc pre { font: 13px/1.5 var(--mono); background: var(--surface); border: 1px solid var(--hair);
             border-radius: 8px; padding: 12px 14px; overflow-x: auto; margin: 0 0 14px; }
  .doc pre code { background: none; padding: 0; }
  .doc a, table a { color: var(--accent); text-decoration: underline; text-underline-offset: 2px; }
  .doc blockquote { border-left: 2px solid var(--hair); padding-left: 14px; color: var(--ink-2); }
  .table-wrap { overflow-x: auto; border: 1px solid var(--hair); border-radius: 8px; margin: 0 0 16px; }
  table { width: 100%; border-collapse: collapse; font-size: 13.5px; background: var(--surface); }
  th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--hair); vertical-align: top; }
  th { color: var(--ink-2); font-weight: 600; background: rgba(255,255,255,0.03); white-space: nowrap; }
  tr:last-child td { border-bottom: 0; }
"""


BOARD_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")  # a listed graph's name, as scripts/boards.py takes it


class Back:
    """Where a file's page leads back to: the graph's page under `label`, and, when the page that opened the
    file shows a graph that is not the first, that graph by name (`board`), carried on every link between
    files so the way back stays the same graph."""

    def __init__(self, label="Board graph", board=None):
        self.label = label
        self.board = board if isinstance(board, str) and BOARD_NAME.match(board) else None

    @property
    def home(self):
        return f"/?board={self.board}" if self.board else "/"

    def files(self, rel, raw=False):
        query = "&".join(part for part in ("raw=1" if raw else "", f"board={self.board}" if self.board else "")
                         if part)
        return "/files/" + _url_path(rel) + (f"?{query}" if query else "")


def back_label(repo_root) -> str:
    """What the graph's page is called on a file's page: a board graph for a circuit board, a graph otherwise."""
    try:
        import graph_pack
        return "Board graph" if graph_pack.read(repo_root)["pack"] == graph_pack.ELECTRONICS else "Graph"
    except Exception:  # a pack file that cannot be read stops the graph's commands, not a page that shows a file
        return "Graph"


def _breadcrumbs(rel: str, back=None) -> str:
    back = back or Back()
    crumbs = [f'<a href="{back.home}">{html.escape(back.label)}</a>']
    parts = [part for part in rel.split("/") if part]
    for i, part in enumerate(parts):
        href = back.files("/".join(parts[:i + 1]))
        if i == len(parts) - 1:
            crumbs.append(f"<span>{html.escape(part)}</span>")
        else:
            crumbs.append(f'<a href="{href}">{html.escape(part)}</a>')
    return ' <span>/</span> '.join(crumbs)


def _inline_md(text: str, base_dir: str, back=None) -> str:
    back = back or Back()
    out = []
    pos = 0
    for match in INLINE_LINK.finditer(text):
        out.append(html.escape(text[pos:match.start()]))
        code, bold, label, href = match.groups()
        if code is not None:
            out.append(f"<code>{html.escape(code)}</code>")
        elif bold is not None:
            out.append(f"<strong>{html.escape(bold)}</strong>")
        else:
            clean = href.strip()
            if clean.startswith(("http://", "https://")):
                url, extra = html.escape(clean, quote=True), ' target="_blank" rel="noopener"'
            elif clean.startswith("#"):
                url, extra = html.escape(clean, quote=True), ""
            else:
                joined = pathlib.PurePosixPath(base_dir).joinpath(clean).as_posix() if base_dir else clean
                url, extra = back.files(joined.lstrip("/")), ""
            out.append(f'<a href="{url}"{extra}>{html.escape(label)}</a>')
        pos = match.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def render_markdown_html(rel: str, text: str, back=None) -> bytes:
    """Render a repository Markdown file as a clean dark-mode document page with a Raw button."""
    base_dir = pathlib.PurePosixPath(rel).parent.as_posix()
    if base_dir == ".":
        base_dir = ""
    lines = text.splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("```"):
            i += 1
            code_lines = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            blocks.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline_md(heading.group(2), base_dir, back)}</h{level}>")
            i += 1
            continue
        if "|" in stripped and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:-]+\|", lines[i + 1]):
            headers = [cell.strip() for cell in stripped.strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append([cell.strip() for cell in lines[i].strip().strip("|").split("|")])
                i += 1
            thead = "".join(f"<th>{_inline_md(h, base_dir, back)}</th>" for h in headers)
            tbody = "".join("<tr>" + "".join(f"<td>{_inline_md(c, base_dir, back)}</td>" for c in r) + "</tr>" for r in rows)
            blocks.append(f'<div class="table-wrap"><table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table></div>')
            continue
        if re.match(r"^[-*]\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                item = re.sub(r"^\s*[-*]\s+", "", lines[i]).strip()
                i += 1
                while i < len(lines) and lines[i].startswith("  ") and not re.match(r"^\s*[-*]\s+", lines[i]):
                    item += " " + lines[i].strip()
                    i += 1
                items.append(f"<li>{_inline_md(item, base_dir, back)}</li>")
            blocks.append("<ul>" + "".join(items) + "</ul>")
            continue
        if re.match(r"^\d+\.\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                item = re.sub(r"^\s*\d+\.\s+", "", lines[i]).strip()
                i += 1
                while i < len(lines) and lines[i].startswith("   ") and not re.match(r"^\s*\d+\.\s+", lines[i]):
                    item += " " + lines[i].strip()
                    i += 1
                items.append(f"<li>{_inline_md(item, base_dir, back)}</li>")
            blocks.append("<ol>" + "".join(items) + "</ol>")
            continue
        para = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt or nxt.startswith(("#", "```", "- ", "* ")) or re.match(r"^\d+\.\s+", nxt):
                break
            if "|" in nxt and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:-]+\|", lines[i + 1]):
                break
            para.append(nxt)
            i += 1
        blocks.append(f"<p>{_inline_md(' '.join(para), base_dir, back)}</p>")
    title = html.escape(pathlib.PurePosixPath(rel).name)
    raw_href = (back or Back()).files(rel, raw=True)
    page = (
        f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{title}</title><style>{READER_CSS}</style></head><body><div class=\"wrap\">"
        f"<div class=\"topbar\"><div class=\"crumbs\">{_breadcrumbs(rel, back)}</div>"
        f"<a class=\"btn\" href=\"{raw_href}\">Raw file</a></div>"
        f"<article class=\"doc\">{''.join(blocks)}</article></div></body></html>\n"
    )
    return page.encode("utf-8")


def render_csv_html(rel: str, text: str, back=None) -> bytes:
    """Render a BOM or CSV export as a clean table with supplier links and a CSV download button."""
    reader = list(csv.reader(io.StringIO(text)))
    headers = reader[0] if reader else []
    rows = reader[1:] if len(reader) > 1 else []

    def cell_html(value: str) -> str:
        clean = value.strip()
        if LCSC_PART.match(clean):
            url = f"https://jlcpcb.com/parts/componentSearch?searchTxt={urllib.parse.quote(clean)}"
            return f'<a href="{url}" target="_blank" rel="noopener">{html.escape(clean)}</a>'
        if clean.startswith(("http://", "https://")):
            return f'<a href="{html.escape(clean, quote=True)}" target="_blank" rel="noopener">{html.escape(clean)}</a>'
        return html.escape(value)

    thead = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    tbody = "".join("<tr>" + "".join(f"<td>{cell_html(c)}</td>" for c in r) + "</tr>" for r in rows)
    name = html.escape(pathlib.PurePosixPath(rel).name)
    raw_href = (back or Back()).files(rel, raw=True)
    page = (
        f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{name}</title><style>{READER_CSS}</style></head><body><div class=\"wrap\">"
        f"<div class=\"topbar\"><div class=\"crumbs\">{_breadcrumbs(rel, back)}</div>"
        f"<a class=\"btn\" href=\"{raw_href}\">Download CSV</a></div>"
        f"<h1>{name}</h1><p class=\"sub\">{len(rows)} {'row' if len(rows) == 1 else 'rows'}</p>"
        f"<div class=\"table-wrap\"><table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table></div>"
        f"</div></body></html>\n"
    )
    return page.encode("utf-8")


def render_directory_html(rel: str, folder: pathlib.Path, back=None) -> bytes:
    """Render a folder (such as hardware/reference datasheets or production outputs) with instant filtering."""
    children = [
        item for item in folder.iterdir()
        if not item.name.startswith(".") and item.name != "__pycache__" and (rel or item.name not in HIDDEN_ROOTS)
    ]
    children.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
    items = []
    for item in children:
        child_rel = f"{rel}/{item.name}".lstrip("/")
        href = (back or Back()).files(child_rel)
        kind = _file_kind(item)
        if item.is_dir():
            sub_count = sum(1 for sub in item.iterdir() if not sub.name.startswith("."))
            size = f"{sub_count} {'item' if sub_count == 1 else 'items'}"
        else:
            try:
                size = _size_words(item.stat().st_size)
            except OSError:
                size = ""
        target = ' target="_blank" rel="noopener"' if item.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".svg") else ""
        items.append(
            f'<li data-name="{html.escape(item.name.lower(), quote=True)}">'
            f'<a href="{href}"{target}><span class="fname">{html.escape(item.name)}</span>'
            f'<span class="badge">{html.escape(kind)}</span>'
            f'<span class="fsize">{html.escape(size)}</span></a></li>'
        )
    title = html.escape(pathlib.PurePosixPath(rel).name if rel else folder.name)
    filter_box = (
        '<input class="search" id="q" type="search" placeholder="Filter files…" autocomplete="off">'
        "<script>const q=document.getElementById('q'),rows=[...document.querySelectorAll('.files li')];"
        "q.addEventListener('input',()=>{const v=q.value.trim().toLowerCase();"
        "rows.forEach(r=>{r.hidden=v&&!r.dataset.name.includes(v);});});</script>"
        if len(children) > 5 else ""
    )
    empty = '<p class="sub">This folder is empty.</p>' if not items else f'<ul class="files">{"".join(items)}</ul>'
    page = (
        f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{title}</title><style>{READER_CSS}</style></head><body><div class=\"wrap\">"
        f"<div class=\"topbar\"><div class=\"crumbs\">{_breadcrumbs(rel or folder.name, back)}</div></div>"
        f"<h1>{title}</h1><p class=\"sub\">{len(children)} {'item' if len(children) == 1 else 'items'} in {html.escape(rel or '/')}</p>"
        f"{filter_box}{empty}</div></body></html>\n"
    )
    return page.encode("utf-8")


class Handler(http.server.SimpleHTTPRequestHandler):
    """The folder's files, read-only repo files under /files/, with no log line and no browser cache."""

    def __init__(self, *args, listeners: Listeners, repo_root: pathlib.Path = ROOT, **kwargs):
        self.listeners = listeners
        self.repo_root = repo_root
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):  # noqa: A002 - the base class names it so
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _fallback_index(self, parsed: urllib.parse.SplitResult) -> pathlib.Path | None:
        if parsed.path not in ("/", "/index.html"):
            return None
        if (pathlib.Path(self.directory) / "index.html").is_file() or not tool_paths.is_external():
            return None
        shared = TOOL_ROOT / "tools" / "board_graph_view" / "index.html"
        return shared if shared.is_file() else None

    def _send_index(self, target: pathlib.Path, head_only: bool) -> None:
        try:
            stat = target.stat()
            body = target.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Last-Modified", self.date_time_string(stat.st_mtime))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def do_HEAD(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/files" or parsed.path.startswith("/files/"):
            self.serve_repo_path(parsed, head_only=True)
        elif (fallback := self._fallback_index(parsed)) is not None:
            self._send_index(fallback, head_only=True)
        else:
            super().do_HEAD()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/events":
            self.events()
        elif path == "/listening":
            body = json.dumps({"listening": len(self.listeners)}).encode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/files" or path.startswith("/files/"):
            self.serve_repo_path(parsed, head_only=False)
        elif (fallback := self._fallback_index(parsed)) is not None:
            self._send_index(fallback, head_only=False)
        else:
            super().do_GET()

    def _send_bytes(self, body: bytes, content_type: str, head_only: bool, disposition: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def serve_repo_path(self, parsed: urllib.parse.SplitResult, head_only: bool = False) -> None:
        raw_rel = parsed.path[len("/files"):]
        rel, target, status_code = resolve_repo_path(self.repo_root, raw_rel)
        if status_code != 200 or target is None or rel is None:
            self.send_error(status_code)
            return
        query = urllib.parse.parse_qs(parsed.query)
        back = Back(back_label(self.repo_root), (query.get("board") or [None])[0])
        if target.is_dir():
            body = render_directory_html(rel, target, back)
            self._send_bytes(body, "text/html; charset=utf-8", head_only)
            return
        raw = query.get("raw") == ["1"]
        ext = target.suffix.lower()
        try:
            data = target.read_bytes()
        except OSError:
            self.send_error(404)
            return
        if not raw and ext == ".md":
            body = render_markdown_html(rel, data.decode("utf-8", "replace"), back)
            self._send_bytes(body, "text/html; charset=utf-8", head_only)
            return
        if not raw and ext == ".csv":
            body = render_csv_html(rel, data.decode("utf-8", "replace"), back)
            self._send_bytes(body, "text/html; charset=utf-8", head_only)
            return
        safe_name = target.name.replace('"', "")
        if raw and ext == ".csv":
            self._send_bytes(data, "text/csv; charset=utf-8", head_only, f'attachment; filename="{safe_name}"')
            return
        if ext in INLINE_TYPES:
            self._send_bytes(data, INLINE_TYPES[ext], head_only, f'inline; filename="{safe_name}"')
            return
        guessed, _ = mimetypes.guess_type(target.name)
        content_type = guessed or "application/octet-stream"
        disposition = f'attachment; filename="{safe_name}"' if content_type == "application/octet-stream" else None
        self._send_bytes(data, content_type, head_only, disposition)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/changed":
            self.send_error(404)
        elif not local(self.client_address[0], self.server.server_address[0]):
            self.send_error(403, "only the Mac itself says the files changed")
        else:
            self.listeners.wake()
            self.send_response(204)
            self.end_headers()

    def events(self):
        turn: queue.Queue = queue.Queue()
        self.listeners.add(turn)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            sent = self.send_stamps()
            while True:
                try:
                    turn.get(timeout=NOTES_EVERY if "notes" in sent else KEEPALIVE)
                except queue.Empty:
                    # Nothing said the folder changed. Notes are written by hand between records, and nobody
                    # tells the server: their stamp is looked at here, and sent when it moved.
                    if "notes" in sent and self.stamps() != sent:
                        sent = self.send_stamps()
                        continue
                    self.wfile.write(b": alive\n\n")
                    self.wfile.flush()
                    continue
                sent = self.send_stamps()
        except OSError:  # the page went away (closed, reloaded, or the link dropped): nothing to tell it
            pass
        finally:
            self.listeners.drop(turn)

    def stamps(self):
        return stamps(pathlib.Path(self.directory), notes_files(self.repo_root), external=tool_paths.is_external())

    def send_stamps(self):
        found = self.stamps()
        self.wfile.write(f"data: {json.dumps(found)}\n\n".encode("ascii"))
        self.wfile.flush()
        return found


def server(bind: str = HOST, port: int = PORT, folder: pathlib.Path = FOLDER,
           repo_root: pathlib.Path | None = None) -> http.server.ThreadingHTTPServer:
    """A server for `folder` bound to `bind`:`port`, with `/files/` rooted at `repo_root` (`ROOT` when `folder` is
    `FOLDER`, else `folder` unless given); serve_forever() runs it. Raises OSError when it cannot bind.
    Its `listeners` are the open /events streams."""
    listeners = Listeners()
    root = pathlib.Path(repo_root) if repo_root is not None else (ROOT if pathlib.Path(folder) == FOLDER else pathlib.Path(folder))
    serving = http.server.ThreadingHTTPServer(
        (bind, port),
        functools.partial(Handler, directory=str(folder), listeners=listeners, repo_root=root),
    )
    serving.listeners = listeners
    return serving


def notify(address: str = ADDRESS, timeout: float = 3) -> str | None:
    """Tell the server at `address` the folder changed, so every open page is told: None, or why it was not told.
    Only a call from the Mac itself is accepted (the server checks the address it comes from)."""
    request = urllib.request.Request(f"{address}/changed", data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return None
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    except (OSError, ValueError) as error:
        return f"no ({' '.join(str(error).split())[:120]})"


def answers(address: str = ADDRESS, timeout: float = 3) -> str:
    """Whether the page answers at `address`: "yes" (its view.json came back as JSON), or why not."""
    try:
        with urllib.request.urlopen(f"{address}/view.json", timeout=timeout) as reply:
            json.loads(reply.read())
        return "yes"
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    except (OSError, ValueError) as error:
        return f"no ({' '.join(str(error).split())[:120]})"


def listening(address: str = ADDRESS, timeout: float = 3) -> int | None:
    """How many pages hold the server's /events stream, or None when the server does not say."""
    try:
        with urllib.request.urlopen(f"{address}/listening", timeout=timeout) as reply:
            return int(json.loads(reply.read())["listening"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _resolve_tool_checkout(raw_tool: str, checkout: pathlib.PurePosixPath) -> pathlib.PurePosixPath:
    if raw_tool.startswith("~/"):
        parts = checkout.parts
        if len(parts) >= 3 and parts[0] == "/" and parts[1] in ("Users", "home"):
            return pathlib.PurePosixPath("/", parts[1], parts[2], raw_tool[2:])
        return pathlib.PurePosixPath(pathlib.Path(raw_tool).expanduser().as_posix())
    return pathlib.PurePosixPath(raw_tool)


def written_plist(checkout: str, settings=mac.SITE) -> bytes:
    """The page's launchd job as this script writes it from the site settings, for the main checkout at `checkout`
    (a path on the Mac): the settings' label; their Python (macOS's own unless graph_page.python names another)
    running this script there, started at login and kept running with no window, ten seconds apart while the
    name it answers on does not resolve; its errors in page.err beside the state database. Always the same bytes
    for the same settings, so installed() can compare them with launchd's copy."""
    checkout = pathlib.PurePosixPath(checkout)
    raw_tool = getattr(settings, "tool_checkout", None)
    if raw_tool:
        tool_path = _resolve_tool_checkout(raw_tool, checkout)
        entry = str(tool_path / "scripts" / "board_graph_serve.py")
    else:
        entry = str(checkout / "scripts" / "board_graph_serve.py")
    data = {
        "Label": settings.launchd_label,
        "ProgramArguments": [settings.page_python, entry],
        "WorkingDirectory": str(checkout),
        "KeepAlive": True,
        "RunAtLoad": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(checkout / pathlib.PurePosixPath(settings.state_database).parent / "page.err"),
    }
    if raw_tool:
        data["EnvironmentVariables"] = {tool_paths.ENV: str(checkout)}
    return plistlib.dumps(data, sort_keys=False)


def job(plist: pathlib.Path = PLIST) -> tuple[str, bytes]:
    """The launchd job's file name and bytes: the project's own plist when it keeps one in tools/mac/, else the
    one written from the site settings for the Mac's main checkout."""
    if plist.is_file():
        return plist.name, plist.read_bytes()
    return plist.name, written_plist(pathlib.Path(mac.CHECKOUT).expanduser().as_posix())


def status() -> dict:
    """What --status prints: the address, launchd's copy of the plist (installed, differs, not installed), the
    job as launchd sees it (None when not loaded), whether the page answers, and how many pages are listening."""
    return {"address": ADDRESS, "job": launchd_job.installed(*job()), "launchd": launchd_job.running(LABEL),
            "answers": answers(), "listening": listening()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--install", action="store_true", help="install or update the launchd job; it starts now")
    parser.add_argument("--status", action="store_true", help="one JSON line: whether the page is served")
    parser.add_argument("--notify", action="store_true", help="tell the page server the folder changed (notify()); "
                                                              "exit 1 and say why when it was not told")
    parser.add_argument("--bind", default=HOST, help=f"the name or address to answer on (default {HOST})")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    if args.status:
        print(json.dumps(status()))
        return 0
    if args.notify:
        why = notify()
        if why:
            print(why)
        return 1 if why else 0
    if not mac.LOCAL:
        print(f"refused: the page is served from the Mac at {ADDRESS}; this is not the Mac")
        return 2
    if args.install and not LABEL:
        print("refused: the site settings name no graph_page.launchd_label for the page's job; run this script "
              "without --install to serve the page while it runs")
        return 2
    if args.install:
        code, said = launchd_job.install(*job())
        print(said + (f"; the page is at {ADDRESS}" if code == 0 else ""))
        return code
    try:
        serving = server(args.bind, args.port)
    except OSError as error:
        print(f"could not bind {args.bind}:{args.port} ({error}); launchd starts this again", file=sys.stderr)
        return 1
    try:
        serving.serve_forever()
    finally:
        serving.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
