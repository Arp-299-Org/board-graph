#!/usr/bin/env python3
"""Rasterise a kicad-cli schematic PDF into full-page PNGs and block crops.

Run with a Python that has PyMuPDF; KiCad's bundled Python does not.
Typical use::

    python tools/kicad/invoke_kicad_cli.py sch export pdf --exclude-drawing-sheet --black-and-white \
        --output out/sch.pdf design.kicad_sch
    py -3 tools/kicad/render_schematic_pages.py --pdf out/sch.pdf --out out/sch --dpi 300
    py -3 tools/kicad/render_schematic_pages.py --pdf out/sch.pdf --out out/sch --max-px 2400 \
        --crop power 0.00 0.00 0.40 0.52 --crop mcu 0.40 0.00 0.71 0.52

Crops are given as fractions of the drawn-content box (x0 y0 x1 y1, origin at
the top-left, 0..1), so the same crop names work whatever the sheet size is.
The script prints the content box in PDF points and the pixel size of every
PNG it wrote.  A full page at 300 dpi is the overview; block crops sized with
``--max-px 2400`` are what the Read tool needs to check pin numbers, pin names
and net labels (it shows at most about 2000 px across, so larger rasters are
only downsampled).
"""

from __future__ import annotations

import argparse
import pathlib

import fitz  # pymupdf


def content_box(page: fitz.Page, margin_pt: float = 6.0) -> fitz.Rect:
    """Bounding box of everything drawn on the page (lines, text), plus a margin."""
    box = fitz.Rect()
    for d in page.get_drawings():
        box |= d["rect"]
    for b in page.get_text("blocks"):
        box |= fitz.Rect(b[:4])
    if box.is_empty:
        return page.rect
    box = fitz.Rect(box.x0 - margin_pt, box.y0 - margin_pt, box.x1 + margin_pt, box.y1 + margin_pt)
    return box & page.rect


def render(page: fitz.Page, clip: fitz.Rect, dpi: int, out: pathlib.Path, max_px: int = 0) -> None:
    if max_px:
        # Size the raster to the viewer, not to a fixed dpi: the Read tool shows
        # about 2000 px across, so a crop wider than that is only downsampled.
        dpi = int(max_px * 72.0 / max(clip.width, clip.height))
    pix = page.get_pixmap(dpi=dpi, clip=clip)
    out.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out))
    print(f"{out}  {pix.width}x{pix.height} px  clip=({clip.x0:.1f},{clip.y0:.1f})-({clip.x1:.1f},{clip.y1:.1f}) pt")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", type=pathlib.Path, required=True)
    ap.add_argument("--out", type=pathlib.Path, required=True, help="output prefix, e.g. out/sch")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--page", type=int, default=None, help="1-based page; default all pages")
    ap.add_argument(
        "--max-px",
        type=int,
        default=0,
        help="if set, ignore --dpi and size each image so its long side is this many pixels (2400 suits the Read tool)",
    )
    ap.add_argument(
        "--crop",
        nargs=5,
        action="append",
        metavar=("NAME", "X0", "Y0", "X1", "Y1"),
        default=[],
        help="named crop as fractions of the content box; repeatable. With crops, full pages are not written.",
    )
    args = ap.parse_args()

    doc = fitz.open(str(args.pdf))
    pages = [args.page - 1] if args.page else range(len(doc))
    for i in pages:
        page = doc[i]
        box = content_box(page)
        print(f"page {i + 1}: sheet {page.rect.width:.0f}x{page.rect.height:.0f} pt, content box "
              f"({box.x0:.1f},{box.y0:.1f})-({box.x1:.1f},{box.y1:.1f}) pt")
        suffix = f"_p{i + 1}" if len(doc) > 1 else ""
        if not args.crop:
            render(page, box, args.dpi, args.out.with_name(args.out.name + f"{suffix}.png"), args.max_px)
            continue
        for name, x0, y0, x1, y1 in args.crop:
            fx0, fy0, fx1, fy1 = (float(v) for v in (x0, y0, x1, y1))
            clip = fitz.Rect(
                box.x0 + fx0 * box.width,
                box.y0 + fy0 * box.height,
                box.x0 + fx1 * box.width,
                box.y0 + fy1 * box.height,
            )
            render(page, clip, args.dpi, args.out.with_name(args.out.name + f"{suffix}_{name}.png"), args.max_px)


if __name__ == "__main__":
    main()
