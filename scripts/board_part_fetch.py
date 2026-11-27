#!/usr/bin/env python3
"""Fetch JLCPCB/LCSC component listings and datasheet PDFs into board stock & parts files."""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import sys
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_description  # noqa: E402
import tool_paths  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

ROOT = tool_paths.project_root()
DETAIL_URL = "https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode={code}"
LCSC_DETAIL_URL = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={code}"
# LCSC datasheet URLs often serve an HTML stub that redirects via JS to the real PDF.
JS_REDIRECT = re.compile(rb"""location\.href\s*=\s*['"]([^'"]+)['"]""")
LCSC_PDF_RE = re.compile(rb"https://datasheet\.lcsc\.com/datasheet/pdf/[0-9A-Za-z._/-]+\.pdf")
MAX_REDIRECT_HOPS = 3
HEADERS = {"User-Agent": "board-graph/1.0", "Accept": "application/json, */*"}


def _class_name(raw: object) -> str:
    text = str(raw or "").strip().lower()
    if text in ("base", "basic"):
        return "Basic"
    if text in ("preferred", "pref"):
        return "Preferred"
    return "Extended"


def _first_price(data: dict) -> float:
    for key in ("jlcPrices", "componentPrices", "prices"):
        prices = data.get(key)
        if isinstance(prices, list) and prices:
            val = prices[0].get("productPrice") if isinstance(prices[0], dict) else None
            if isinstance(val, (int, float)):
                return float(val)
    for key in ("productPrice", "initialPrice"):
        val = data.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return 0.0


def fetch_lcsc_detail(code: str, urlopen=urllib.request.urlopen) -> dict:
    """Fetch normalized JLCPCB/LCSC component metadata for `code` (e.g. C501473)."""
    clean = code.strip().upper()
    if not clean:
        raise ValueError("empty LCSC code")
    req = urllib.request.Request(DETAIL_URL.format(code=urllib.parse.quote(clean)), headers=HEADERS)
    with urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError(f"{clean}: unexpected JLCPCB response")
    mpn = str(data.get("componentModelEn") or data.get("componentCode") or clean).strip()
    maker = str(data.get("componentBrandEn") or data.get("brandNameEn") or "").strip()
    stock = int(data.get("stockCount") or 0)
    access_id = str(data.get("dataManualFileAccessId") or "").strip()
    access_url = f"https://jlcpcb.com/api/file/downloadByFileSystemAccessId/{access_id}" if access_id else ""
    return {
        "part": clean,
        "listed_manufacturer": maker,
        "listed_mpn": mpn,
        "class": _class_name(data.get("componentLibraryType")),
        "stock": stock,
        "unit_price_usd": _first_price(data),
        "package": str(data.get("componentSpecificationEn") or "").strip(),
        "description": str(data.get("describe") or "").strip(),
        "datasheet_url": str(data.get("dataManualUrl") or "").strip(),
        "datasheet_access_url": access_url,
    }


def fetch_lcsc_datasheet_url(code: str, urlopen=urllib.request.urlopen) -> str:
    """Ask LCSC's product detail endpoint for `code`'s PDF URL as fallback."""
    req = urllib.request.Request(LCSC_DETAIL_URL.format(code=urllib.parse.quote(code)), headers=HEADERS)
    with urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    result = payload.get("result") if isinstance(payload, dict) else None
    return str((result or {}).get("pdfUrl") or "").strip() if isinstance(result, dict) else ""


def _next_pdf_hop(url: str, data: bytes) -> str | None:
    snippet = data[:8192]
    hop = JS_REDIRECT.search(snippet)
    if hop:
        return urllib.parse.urljoin(url, hop.group(1).decode("utf-8", "replace"))
    direct = LCSC_PDF_RE.search(snippet)
    if direct:
        return direct.group(0).decode("ascii")
    return None


def download_datasheet(url: str, dest_dir: pathlib.Path, mpn: str, urlopen=urllib.request.urlopen) -> str | None:
    """Download datasheet PDF if `url` is non-empty; follow JS redirects and refuse non-PDF payloads."""
    if not url:
        return None
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", mpn).strip("_") or "datasheet"
    target = dest_dir / f"{safe}.pdf"
    if target.is_file():
        with target.open("rb") as existing:
            if existing.read(5) == b"%PDF-":
                return target.name
    data = b""
    current_url = url
    for _ in range(MAX_REDIRECT_HOPS + 1):
        req = urllib.request.Request(current_url, headers={"User-Agent": HEADERS["User-Agent"]})
        with urlopen(req, timeout=30) as resp:
            data = resp.read()
        if data[:5] == b"%PDF-":
            break
        nxt = _next_pdf_hop(current_url, data)
        if not nxt or nxt == current_url:
            break
        current_url = nxt
    if data[:5] != b"%PDF-":
        raise ValueError(f"{url} did not return a PDF (starts {data[:40]!r})")
    dest_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target.name


def fetch_datasheet(rec: dict, sheets_dir: pathlib.Path, urlopen=urllib.request.urlopen) -> str | None:
    """Try JLCPCB's URL, access-ID URL, and LCSC's product endpoint in order."""
    candidates: list[tuple[str, str]] = []
    if rec.get("datasheet_url"):
        candidates.append(("jlcpcb", str(rec["datasheet_url"])))
    if rec.get("datasheet_access_url"):
        candidates.append(("jlcpcb-access", str(rec["datasheet_access_url"])))
    tried: set[str] = set()
    for source, url in candidates:
        if not url or url in tried:
            continue
        tried.add(url)
        try:
            name = download_datasheet(url, sheets_dir, rec["listed_mpn"], urlopen=urlopen)
            if name:
                rec["datasheet_url"] = url
                return name
        except Exception as exc:  # noqa: BLE001
            print(f"warning: {source} datasheet for {rec['part']}: {exc}", file=sys.stderr)
    try:
        lcsc_url = fetch_lcsc_datasheet_url(rec["part"], urlopen=urlopen)
        if lcsc_url and lcsc_url not in tried:
            name = download_datasheet(lcsc_url, sheets_dir, rec["listed_mpn"], urlopen=urlopen)
            if name:
                rec["datasheet_url"] = lcsc_url
                return name
    except Exception as exc:  # noqa: BLE001
        print(f"warning: lcsc datasheet for {rec['part']}: {exc}", file=sys.stderr)
    return None


def update_listings(path: pathlib.Path, planning_qty: int, records: list[dict], read_at: str) -> dict:
    """Merge fetched records by supplier part ID into stock/listings.json."""
    existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    by_part = {str(r["part"]).upper(): r for r in existing.get("factory_parts", []) if isinstance(r, dict) and r.get("part")}
    for rec in records:
        prev = dict(by_part.get(rec["part"], {}))
        prev.update({
            "part": rec["part"],
            "read_at": read_at,
            "listed_manufacturer": rec["listed_manufacturer"],
            "listed_mpn": rec["listed_mpn"],
            "class": rec["class"],
            "stock": rec["stock"],
            "unit_price_usd": rec["unit_price_usd"],
        })
        by_part[rec["part"]] = prev
    out = {"planning_quantity": planning_qty, "factory_parts": [by_part[k] for k in sorted(by_part)]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


def update_parts_json(path: pathlib.Path, records: list[dict], pdf_map: dict[str, str | None]) -> dict:
    """Merge fetched MPN/LCSC/datasheet entries into hardware/reference/parts.json."""
    existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    parts = dict(existing.get("parts", {})) if isinstance(existing.get("parts"), dict) else {}
    for rec in records:
        mpn = rec["listed_mpn"]
        entry = dict(parts.get(mpn, {}))
        entry.setdefault("manufacturer", rec["listed_manufacturer"])
        entry["lcsc"] = rec["part"]
        if rec["package"]:
            entry.setdefault("package", rec["package"])
        sheet = pdf_map.get(rec["part"])
        if sheet and not entry.get("datasheet"):
            entry["datasheet"] = sheet
        parts[mpn] = entry
    out = {**existing, "parts": dict(sorted(parts.items()))}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lcsc", action="append", required=True, help="LCSC part code (repeatable)")
    parser.add_argument("--planning-qty", type=int, default=5, help="Planning board quantity (default 5)")
    parser.add_argument("--listings", type=pathlib.Path, help="Output listings JSON path")
    parser.add_argument("--parts", type=pathlib.Path, help="Output parts.json path")
    parser.add_argument("--datasheets-dir", type=pathlib.Path, help="Datasheets folder")
    parser.add_argument("--skip-pdf", action="store_true", help="Do not download datasheet PDFs")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    board_description.add_argument(parser)
    args = parser.parse_args(argv)

    desc = board_description.load(args.board_description, root=ROOT) if args.board_description else board_description.load(root=ROOT)
    listings_path = (args.listings or (desc.path("reports") / "stock" / "listings.json")).resolve()
    parts_path = (args.parts or desc.path("parts_data")).resolve()
    sheets_dir = (args.datasheets_dir or desc.path("datasheets")).resolve()

    read_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    records = [fetch_lcsc_detail(code) for code in args.lcsc]
    pdf_map: dict[str, str | None] = {}
    for rec in records:
        if args.skip_pdf:
            pdf_map[rec["part"]] = None
        else:
            pdf_map[rec["part"]] = fetch_datasheet(rec, sheets_dir)
    listings_out = update_listings(listings_path, args.planning_qty, records, read_at)
    parts_out = update_parts_json(parts_path, records, pdf_map)
    if args.json:
        print(json.dumps({"listings": listings_out, "parts": parts_out, "pdfs": pdf_map}, indent=2))
    else:
        for rec in records:
            print(f"{rec['part']}: {rec['listed_manufacturer']} {rec['listed_mpn']} stock={rec['stock']} price=${rec['unit_price_usd']} pdf={pdf_map[rec['part']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
