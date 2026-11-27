#!/usr/bin/env python3
"""Pre-schematic stock and datasheet validation helpers (before a PCB or BOM export exists)."""
from __future__ import annotations

import datetime
import json
import pathlib
from typing import Callable


def pre_schematic_supply(
    listings_path: pathlib.Path,
    now: datetime.datetime,
    parse_read_at: Callable[[object], datetime.datetime | None],
    max_age_days: int,
) -> tuple[list[str], list[str], dict]:
    """Validate `listings_path` during pre-schematic part selection (no PCB yet)."""
    if not listings_path.is_file():
        return [f"{listings_path.name} is not there: choose in-stock parts and write dated supplier listings"], [], {}
    try:
        listings = json.loads(listings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [f"listings file cannot be read: {error}"], [], {}
    if not isinstance(listings, dict):
        return ["listings file must be a JSON object with planning_quantity and factory_parts"], [], {}

    findings: list[str] = []
    notes: list[str] = []
    boards_planned = listings.get("planning_quantity")
    if not isinstance(boards_planned, int) or isinstance(boards_planned, bool) or boards_planned < 1:
        findings.append("planning_quantity is missing or not a whole number of boards")
        boards_planned = None
    rows = listings.get("factory_parts")
    if not isinstance(rows, list) or not rows:
        findings.append("factory_parts must list at least one chosen part before schematic work")
        return findings, notes, {"stage": "pre_schematic", "planning_quantity": boards_planned, "factory_parts": 0}

    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            findings.append(f"factory_parts[{index}] must be an object")
            continue
        part = row.get("part")
        if not isinstance(part, str) or not part.strip():
            findings.append(f"factory_parts[{index}] names no supplier part ID")
            continue
        part = part.strip()
        if part in seen:
            findings.append(f"{part}: listed twice")
            continue
        seen.add(part)
        for key in ("listed_manufacturer", "listed_mpn", "class"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                findings.append(f"{part}: listing has no {key}")
        price = row.get("unit_price_usd")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            findings.append(f"{part}: listing has no unit_price_usd")
        read = parse_read_at(row.get("read_at"))
        if read is None:
            findings.append(f"{part}: listing has no valid read_at date")
        elif read > now + datetime.timedelta(days=1):
            findings.append(f"{part}: read_at {row.get('read_at')} is in the future")
        elif now - read > datetime.timedelta(days=max_age_days):
            findings.append(f"{part}: read {(now - read).days} days ago, more than {max_age_days}")
        stock = row.get("stock")
        if isinstance(stock, bool) or not isinstance(stock, int) or stock < 0:
            findings.append(f"{part}: listing has no stock count")
        elif boards_planned is not None and stock < boards_planned:
            short = f"{part}: {stock} in stock, {boards_planned} boards need at least {boards_planned}"
            plan = row.get("owner_plan")
            if isinstance(plan, str) and plan.strip():
                notes.append(f"{short}; owner_plan: {plan.strip()}")
            else:
                findings.append(f"{short}, and carries no owner_plan")

    detail = {
        "stage": "pre_schematic",
        "planning_quantity": boards_planned,
        "factory_parts": len(seen),
        "notes": notes,
    }
    return findings, notes, detail


def _safe_datasheet_file(datasheets_dir: pathlib.Path, sheet: object) -> pathlib.Path:
    if not isinstance(sheet, str) or not sheet.strip():
        raise ValueError("missing datasheet path")
    rel = pathlib.PurePosixPath(sheet.strip().replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"datasheet path must stay inside {datasheets_dir.name}: {sheet!r}")
    base = datasheets_dir.resolve()
    target = (base / rel).resolve()
    target.relative_to(base)
    return target


def pre_schematic_datasheets(
    root: pathlib.Path,
    parts_rel: str,
    datasheets_rel: str,
    listings_rel: str,
) -> tuple[list[str], dict]:
    """Validate `parts.json` and referenced datasheets before a PCB or BOM export exists."""
    parts_file = root / parts_rel
    datasheets_dir = root / datasheets_rel
    if not parts_file.is_file():
        return [f"{parts_rel} is not there: record selected parts, datasheet filenames, and heights before schematic"], {
            "stage": "pre_schematic",
            "parts_checked": 0,
        }
    try:
        payload = json.loads(parts_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [f"{parts_rel} cannot be read: {error}"], {"stage": "pre_schematic"}
    parts = payload.get("parts") if isinstance(payload, dict) else None
    if not isinstance(parts, dict) or not parts:
        return [f"{parts_rel} has no 'parts' entries yet"], {"stage": "pre_schematic", "parts_checked": 0}

    findings: list[str] = []
    for mpn, spec in sorted(parts.items()):
        if not isinstance(spec, dict):
            findings.append(f"{mpn}: entry in {parts_rel} must be an object")
            continue
        try:
            ds_path = _safe_datasheet_file(datasheets_dir, spec.get("datasheet"))
            if not ds_path.is_file():
                findings.append(f"{mpn}: datasheet {spec.get('datasheet')!r} not in {datasheets_rel}")
        except ValueError as error:
            findings.append(f"{mpn}: {error}")
        height = spec.get("height_mm")
        if isinstance(height, bool) or not isinstance(height, (int, float)) or height < 0:
            findings.append(f"{mpn}: no valid height_mm taken from datasheet")

    listings_path = root / listings_rel
    if listings_path.is_file():
        try:
            listings = json.loads(listings_path.read_text(encoding="utf-8"))
            for row in (listings.get("factory_parts") or []) if isinstance(listings, dict) else []:
                mpn = row.get("listed_mpn") if isinstance(row, dict) else None
                if isinstance(mpn, str) and mpn.strip() and mpn.strip() not in parts:
                    findings.append(f"{mpn.strip()} ({row.get('part', '?')}): selected in stock listings but missing from {parts_rel}")
        except (OSError, ValueError):
            pass

    return findings, {"stage": "pre_schematic", "parts_checked": len(parts)}
