#!/usr/bin/env python3
"""Write 3D models (STEP) of parts that have none, from the dimensions in their datasheets.

  py -3 scripts/kicad_step_model.py [--out <folder>]
      writes every part of the board's part models file into the board's first model folder (or <folder>)

The model folder, the project's name in each file's header and the part models file come from the board's
description (scripts/board_description.py; its optional part_models key). That file is the board's own Python: its
PARTS maps each model's name to a function that returns the model's prisms, built with box(), cylinder() and
d_shaft() from here. Each part is a set of boxes and cylinders in its footprint's own coordinates: x right, y down
(as KiCad draws footprints), z up from the face the part mounts on, in mm. KiCad places the
model with the footprint, so a model drawn here sits where the footprint's pads are. Cylinders
are 48-sided. Only what the datasheet dimensions is drawn; each part names its source.
"""
from __future__ import annotations

import argparse
import importlib.util
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import tool_paths  # noqa: E402

if __name__ == "__main__":
    tool_paths.take_argument()

import board_description  # noqa: E402

ROOT = tool_paths.project_root()
DESCRIPTION = board_description.load()
OUT = DESCRIPTION.paths("model_folders")[0]
PROJECT = DESCRIPTION["project"]  # named in each model's header

BLACK = (0.10, 0.10, 0.10)
TIN = (0.82, 0.82, 0.84)
IVORY = (0.93, 0.89, 0.78)
CERAMIC = (0.72, 0.60, 0.42)


def box(x0, y0, z0, x1, y1, z1, colour):
    return ([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], z0, z1, colour)


def cylinder(cx, cy, r, z0, z1, colour, sides=48):
    return ([(cx + r * math.cos(2 * math.pi * i / sides), cy + r * math.sin(2 * math.pi * i / sides))
             for i in range(sides)], z0, z1, colour)


def d_shaft(cx, cy, r, flat, z0, z1, colour, sides=48):
    """A round shaft with one flat facing +y, `flat` across from the flat to the far side."""
    cut = flat - r
    points = []
    for i in range(sides):
        x, y = cx + r * math.cos(2 * math.pi * i / sides), cy + r * math.sin(2 * math.pi * i / sides)
        points.append((x, min(y, cy + cut)))
    deduped = [p for i, p in enumerate(points) if p != points[i - 1]]
    return (deduped, z0, z1, colour)


class Step:
    """A minimal AP214 writer: each prism is a closed solid of planar faces with one colour."""

    def __init__(self, name: str):
        self.name, self.lines, self.solids, self.styles = name, [], [], []

    def add(self, text: str) -> int:
        self.lines.append(text)
        return len(self.lines) + 99

    def point(self, p):
        return self.add(f"CARTESIAN_POINT('',({p[0]:.6f},{p[1]:.6f},{p[2]:.6f}))")

    def direction(self, d):
        return self.add(f"DIRECTION('',({d[0]:.6f},{d[1]:.6f},{d[2]:.6f}))")

    def placement(self, origin, normal, ref):
        return self.add(f"AXIS2_PLACEMENT_3D('',#{self.point(origin)},#{self.direction(normal)},#{self.direction(ref)})")

    def prism(self, polygon, z0, z1, colour):
        # KiCad's model y points away from the footprint's y; keep the outline counter-clockwise.
        poly = [(x, -y) for x, y in polygon]
        if sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1])) < 0:
            poly.reverse()
        n = len(poly)
        bottom = [(x, y, z0) for x, y in poly]
        top = [(x, y, z1) for x, y in poly]
        vb = [self.add(f"VERTEX_POINT('',#{self.point(p)})") for p in bottom]
        vt = [self.add(f"VERTEX_POINT('',#{self.point(p)})") for p in top]

        def edge(a, b, va, vb_):
            d = [b[i] - a[i] for i in range(3)]
            length = math.sqrt(sum(c * c for c in d))
            unit = [c / length for c in d]
            start, direction = self.point(a), self.direction(unit)
            vector = self.add(f"VECTOR('',#{direction},{length:.6f})")
            line = self.add(f"LINE('',#{start},#{vector})")
            return self.add(f"EDGE_CURVE('',#{va},#{vb_},#{line},.T.)")

        eb = [edge(bottom[i], bottom[(i + 1) % n], vb[i], vb[(i + 1) % n]) for i in range(n)]
        et = [edge(top[i], top[(i + 1) % n], vt[i], vt[(i + 1) % n]) for i in range(n)]
        ev = [edge(bottom[i], top[i], vb[i], vt[i]) for i in range(n)]

        def face(oriented, origin, normal, ref):
            used = [self.add(f"ORIENTED_EDGE('',*,*,#{e},{s})") for e, s in oriented]
            loop = self.add("EDGE_LOOP('',(" + ",".join(f"#{u}" for u in used) + "))")
            bound = self.add(f"FACE_OUTER_BOUND('',#{loop},.T.)")
            plane = self.add(f"PLANE('',#{self.placement(origin, normal, ref)})")
            return self.add(f"ADVANCED_FACE('',(#{bound}),#{plane},.T.)")

        faces = [face([(eb[i], ".F.") for i in reversed(range(n))], bottom[0], (0, 0, -1), (1, 0, 0)),
                 face([(et[i], ".T.") for i in range(n)], top[0], (0, 0, 1), (1, 0, 0))]
        for i in range(n):
            a, b = poly[i], poly[(i + 1) % n]
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy)
            normal = (dy / length, -dx / length, 0)
            faces.append(face([(eb[i], ".T."), (ev[(i + 1) % n], ".T."), (et[i], ".F."), (ev[i], ".F.")],
                              bottom[i], normal, (dx / length, dy / length, 0)))
        shell = self.add("CLOSED_SHELL('',(" + ",".join(f"#{f}" for f in faces) + "))")
        solid = self.add(f"MANIFOLD_SOLID_BREP('',#{shell})")
        rgb = self.add(f"COLOUR_RGB('',{colour[0]:.3f},{colour[1]:.3f},{colour[2]:.3f})")
        fill_colour = self.add(f"FILL_AREA_STYLE_COLOUR('',#{rgb})")
        fill = self.add(f"FILL_AREA_STYLE('',(#{fill_colour}))")
        side = self.add(f"SURFACE_SIDE_STYLE('',(#{self.add(f'SURFACE_STYLE_FILL_AREA(#{fill})')}))")
        usage = self.add(f"PRESENTATION_STYLE_ASSIGNMENT((#{self.add(f'SURFACE_STYLE_USAGE(.BOTH.,#{side})')}))")
        self.styles.append(self.add(f"STYLED_ITEM('',(#{usage}),#{solid})"))
        self.solids.append(solid)

    def text(self) -> str:
        head = [
            "APPLICATION_CONTEXT('automotive design')",
            "APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,#1)",
            "PRODUCT_CONTEXT('',#1,'mechanical')",
            f"PRODUCT('{self.name}','{self.name}','',(#3))",
            "PRODUCT_DEFINITION_FORMATION('','',#4)",
            "PRODUCT_DEFINITION_CONTEXT('part definition',#1,'design')",
            "PRODUCT_DEFINITION('design','',#5,#6)",
            "PRODUCT_DEFINITION_SHAPE('','',#7)",
            "(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.))",
            "(NAMED_UNIT(*)PLANE_ANGLE_UNIT()SI_UNIT($,.RADIAN.))",
            "(NAMED_UNIT(*)SI_UNIT($,.STERADIAN.)SOLID_ANGLE_UNIT())",
            "UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-06),#9,'distance_accuracy_value','')",
            "(GEOMETRIC_REPRESENTATION_CONTEXT(3)GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#12))"
            "GLOBAL_UNIT_ASSIGNED_CONTEXT((#9,#10,#11))REPRESENTATION_CONTEXT('',''))",
            "CARTESIAN_POINT('',(0.,0.,0.))",
            "DIRECTION('',(0.,0.,1.))",
            "DIRECTION('',(1.,0.,0.))",
            "AXIS2_PLACEMENT_3D('',#14,#15,#16)",
            "ADVANCED_BREP_SHAPE_REPRESENTATION('',(#17," + ",".join(f"#{s}" for s in self.solids) + "),#13)",
            "SHAPE_DEFINITION_REPRESENTATION(#8,#18)",
            "MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION('',(" + ",".join(f"#{s}" for s in self.styles) + "),#13)",
        ]
        body = [f"#{i + 1}={line};" for i, line in enumerate(head)]
        body += [f"#{i + 100}={line};" for i, line in enumerate(self.lines)]
        return ("ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('" + PROJECT + " part model from datasheet dimensions'),"
                "'2;1');\n"
                f"FILE_NAME('{self.name}.step','',(''),(''),'scripts/kicad_step_model.py','','');\n"
                "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\nENDSEC;\nDATA;\n"
                + "\n".join(body) + "\nENDSEC;\nEND-ISO-10303-21;\n")


def parts(description=DESCRIPTION) -> dict:
    """The PARTS of the board's part models file, by model name; {} for a board whose description names none."""
    file = description.part_models
    if file is None:
        return {}
    spec = importlib.util.spec_from_file_location("board_part_models", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module.PARTS)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=pathlib.Path, help="the folder to write into; the board's first model folder "
                                                         "by default")
    args = parser.parse_args(argv)
    found = parts()
    if not found:
        print("no part models: the board's description names no part_models file")
        return 0
    out = args.out or OUT
    out.mkdir(parents=True, exist_ok=True)
    for name, build in found.items():
        step = Step(name)
        for polygon, z0, z1, colour in build():
            step.prism(polygon, z0, z1, colour)
        path = out / f"{name}.step"
        path.write_bytes(step.text().encode("utf-8"))
        shown = path.resolve().relative_to(ROOT) if path.resolve().is_relative_to(ROOT) else path
        print(f"{shown.as_posix()}: {len(step.solids)} solids")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
