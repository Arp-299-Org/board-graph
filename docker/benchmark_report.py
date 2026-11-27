#!/usr/bin/env python3
"""Compare board-graph benchmark runs across providers using records, SQLite DB, and routing receipts."""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import pathlib
import sqlite3
import sys


def _parse_iso(raw: str | None) -> datetime.datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def _elapsed_s(start_raw: str | None, end_raw: str | None) -> float | None:
    start_dt, end_dt = _parse_iso(start_raw), _parse_iso(end_raw)
    if start_dt is None or end_dt is None:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds())


def _read_records(repo: pathlib.Path) -> list[dict]:
    records_dir = repo / "tools" / "board_graph_records"
    if not records_dir.is_dir():
        return []
    rows = []
    for path in sorted(records_dir.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(item, dict) and item.get("node"):
            item["_file"] = path.name
            rows.append(item)
    rows.sort(key=lambda r: (_parse_iso(r.get("at")) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), r["_file"]))
    return rows


def _sqlite_path(repo: pathlib.Path) -> pathlib.Path | None:
    site_file = repo / "tools" / "board_graph_site.json"
    if site_file.is_file():
        try:
            site = json.loads(site_file.read_text(encoding="utf-8"))
            rel = site.get("state_database")
            if isinstance(rel, str) and rel:
                candidate = repo / rel
                if candidate.is_file():
                    return candidate
        except (OSError, ValueError):
            pass
    default = repo / "logs" / "board-graph" / "state.sqlite3"
    return default if default.is_file() else None


def _read_sqlite_metrics(db_path: pathlib.Path | None) -> dict:
    if db_path is None or not db_path.is_file():
        return {"upkeep_count": 0, "upkeep_s": 0.0, "attempt_count": 0, "attempt_s": 0.0, "attempt_tokens": 0}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        upkeeps = conn.execute("SELECT started_at, finished_at FROM upkeep").fetchall()
        attempts = conn.execute("SELECT started_at, finished_at, tokens FROM attempts").fetchall()
    except sqlite3.OperationalError:
        return {"upkeep_count": 0, "upkeep_s": 0.0, "attempt_count": 0, "attempt_s": 0.0, "attempt_tokens": 0}
    finally:
        conn.close()
    upkeep_s = sum(_elapsed_s(r["started_at"], r["finished_at"]) or 0.0 for r in upkeeps)
    attempt_s = sum(_elapsed_s(r["started_at"], r["finished_at"]) or 0.0 for r in attempts)
    attempt_tokens = sum(int(r["tokens"] or 0) for r in attempts)
    return {
        "upkeep_count": len(upkeeps),
        "upkeep_s": round(upkeep_s, 1),
        "attempt_count": len(attempts),
        "attempt_s": round(attempt_s, 1),
        "attempt_tokens": attempt_tokens,
    }


def _scan_routing_receipts(repo: pathlib.Path) -> dict:
    board_dir = repo / "board"
    board_json = board_dir / "board.json"
    if board_json.is_file():
        try:
            folder = json.loads(board_json.read_text(encoding="utf-8")).get("folder")
            if isinstance(folder, str) and folder:
                board_dir = repo / folder
        except (OSError, ValueError):
            pass
    totals = {"total_s": 0.0, "llm_s": 0.0, "tool_s": 0.0, "review_s": 0.0, "harness_s": 0.0, "refusal_s": 0.0, "thinking_tokens": 0}
    tokens_total = 0
    refusals: collections.Counter = collections.Counter()
    receipt_files = sorted(board_dir.rglob("run-*.jsonl")) if board_dir.is_dir() else []
    for path in receipt_files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("summary"):
                t = row.get("timing") or {}
                for k in totals:
                    totals[k] += float(t.get(k) or 0.0) if k != "thinking_tokens" else int(t.get(k) or 0)
                tok = row.get("tokens_total")
                if isinstance(tok, dict):
                    tokens_total += sum(int(tok.get(k) or 0) for k in ("input", "output", "cache_read", "cache_write"))
                by_reason = (row.get("refusals") or {}).get("by_reason") or {}
                for reason, info in by_reason.items():
                    refusals[reason] += int((info or {}).get("count") or 0)
    return {
        "receipts": len(receipt_files),
        "timing": {k: (round(v, 1) if k != "thinking_tokens" else int(v)) for k, v in totals.items()},
        "tokens_total": tokens_total,
        "top_refusals": [{"reason": k, "count": v} for k, v in refusals.most_common(5)],
    }


def summarize_run(label: str, repo: pathlib.Path) -> dict:
    """Summarize one board-graph run directory into card waterfall, friction, and routing timing metrics."""
    records = _read_records(repo)
    db_metrics = _read_sqlite_metrics(_sqlite_path(repo))
    routing = _scan_routing_receipts(repo)
    card_s: dict[str, float] = collections.defaultdict(float)
    card_outcomes: dict[str, list[str]] = collections.defaultdict(list)
    needs_lead = 0
    returned = 0
    for idx, rec in enumerate(records):
        node = rec["node"]
        outcome = rec.get("outcome", "")
        card_outcomes[node].append(outcome)
        if outcome == "needs_lead":
            needs_lead += 1
        elif outcome == "returned":
            returned += 1
        if idx > 0:
            delta = _elapsed_s(records[idx - 1].get("at"), rec.get("at"))
            if delta is not None:
                card_s[node] += delta
    wall_s = (_elapsed_s(records[0].get("at"), records[-1].get("at")) or 0.0) if len(records) >= 2 else 0.0
    waterfall = [
        {"node": node, "seconds": round(sec, 1), "outcomes": card_outcomes[node]}
        for node, sec in sorted(card_s.items(), key=lambda kv: -kv[1])
    ]
    return {
        "label": label,
        "repo": str(repo),
        "transitions": len(records),
        "last_node": records[-1]["node"] if records else None,
        "last_outcome": records[-1].get("outcome") if records else None,
        "wall_s": round(wall_s, 1),
        "needs_lead": needs_lead,
        "returned": returned,
        "sqlite": db_metrics,
        "routing": routing,
        "waterfall": waterfall,
    }


def format_markdown(runs: list[dict]) -> str:
    """Format run summaries as a compact Markdown comparison table and per-card waterfall."""
    lines = [
        "## Benchmark Summary",
        "",
        "| Run | Last Card | Transitions | Wall (s) | needs_lead | returned | Upkeep (s) | Route LLM/Tool/Rev/Refusal (s) |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in runs:
        t = r["routing"]["timing"]
        rt = f"{t['llm_s']:.0f} / {t['tool_s']:.0f} / {t['review_s']:.0f} / {t['refusal_s']:.0f}"
        last = f"{r['last_node']} ({r['last_outcome']})" if r["last_node"] else "none"
        lines.append(
            f"| {r['label']} | {last} | {r['transitions']} | {r['wall_s']:.1f} | "
            f"{r['needs_lead']} | {r['returned']} | {r['sqlite']['upkeep_s']:.1f} | {rt} |"
        )
    for r in runs:
        lines.extend(["", f"### {r['label']} card waterfall"])
        if not r["waterfall"]:
            lines.append("- No multi-transition timing data.")
        else:
            for item in r["waterfall"][:12]:
                lines.append(f"- `{item['node']}`: {item['seconds']:.1f}s ({', '.join(item['outcomes'])})")
        if r["routing"]["top_refusals"]:
            top = ", ".join(f"{x['reason']} ({x['count']}x)" for x in r["routing"]["top_refusals"])
            lines.append(f"- Top routing refusals: {top}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, metavar="NAME=PATH",
                        help="labeled path to a benchmarked board repo")
    parser.add_argument("--json", type=pathlib.Path, help="optional JSON output file")
    args = parser.parse_args(argv)
    summaries = []
    for spec in args.run:
        if "=" not in spec:
            parser.error(f"--run must be NAME=PATH, got {spec!r}")
        name, raw_path = spec.split("=", 1)
        summaries.append(summarize_run(name.strip(), pathlib.Path(raw_path.strip()).resolve()))
    if args.json:
        args.json.write_text(json.dumps({"runs": summaries}, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(format_markdown(summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
