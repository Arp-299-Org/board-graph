#!/usr/bin/env python3
"""Search JLCPCB's in-stock SMT component catalog and print ranked matches."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_part_fetch  # noqa: E402
import tool_paths  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

SEARCH_URL = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList/v2"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, */*",
    "User-Agent": "Mozilla/5.0",
}


def parse_search_results(payload: dict, *, min_stock: int = 50) -> list[dict]:
    """Normalize JLCPCB catalog search response rows, filtering below `min_stock`."""
    rows = (((payload.get("data") or {}).get("componentPageInfo") or {}).get("list")) or []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("componentCode") or "").strip().upper()
        if not code:
            continue
        stock = int(row.get("stockCount") or 0)
        if stock < min_stock:
            continue
        out.append({
            "part": code,
            "manufacturer": str(row.get("componentBrandEn") or row.get("brandNameEn") or "").strip(),
            "mpn": str(row.get("componentModelEn") or code).strip(),
            "class": board_part_fetch._class_name(row.get("componentLibraryType")),
            "stock": stock,
            "unit_price_usd": board_part_fetch._first_price(row),
            "package": str(row.get("componentSpecificationEn") or "").strip(),
            "description": str(row.get("describe") or "").strip(),
        })
    return out


def search_catalog(
    query: str,
    *,
    limit: int = 10,
    min_stock: int = 50,
    urlopen=urllib.request.urlopen,
    sleep=time.sleep,
) -> list[dict]:
    """Query JLCPCB's SMT catalog for `query` and return normalized in-stock matches."""
    clean = query.strip()
    if not clean:
        raise ValueError("empty search query")
    body = json.dumps({
        "keyword": clean,
        "currentPage": 1,
        "pageSize": max(1, min(int(limit), 50)),
        "searchSource": "search_component",
        "componentAttributes": [],
        "stockFlag": True,
    }).encode("utf-8")
    req = urllib.request.Request(SEARCH_URL, data=body, headers=HEADERS)
    raw = board_part_fetch.urlopen_with_retry(req, timeout=20, urlopen=urlopen, sleep=sleep)
    payload = json.loads(raw.decode("utf-8"))
    return parse_search_results(payload, min_stock=min_stock)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True, help="Catalog search terms (e.g. 'RJ45 8P8C SMD')")
    parser.add_argument("--limit", type=int, default=10, help="Max results per page (default 10)")
    parser.add_argument("--min-stock", type=int, default=50, help="Minimum stock count (default 50)")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    args = parser.parse_args(argv)

    rows = search_catalog(args.query, limit=args.limit, min_stock=args.min_stock)
    if args.json:
        print(json.dumps({"query": args.query, "results": rows}, indent=2))
    elif not rows:
        print(f"no in-stock SMT parts found for {args.query!r} (min stock {args.min_stock})")
    else:
        for row in rows:
            print(
                f"{row['part']} | {row['class']:<9} | stock={row['stock']:<7} | "
                f"${row['unit_price_usd']:.4f} | {row['manufacturer']} {row['mpn']} | "
                f"{row['package']} | {row['description'][:80]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
