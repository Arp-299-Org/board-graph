#!/usr/bin/env python3
"""Strict parsing-only allowlist audit for the query-only PCB snapshot."""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[2]
TARGET = ROOT / "tools" / "kicad" / "kicad_semantic_snapshot.py"
REGISTRY = ROOT / "scripts" / "kicad-script-modes.json"
PREFLIGHT = ROOT / "scripts" / "kicad_preflight.ps1"
TARGET_SHA256 = "B1780DB3CEEFCCAB866E654A432BDCD5E4350DDB5C871A70E77D0017BB2A2499"

APPROVED_IMPORTS = {
    ("__future__", "annotations", None),
    ("collections.abc", "Iterable", None),
    ("collections.abc", "Sequence", None),
    ("importlib.metadata", "version", "installed_version"),
    ("typing", "Any", None),
    ("kipy", "KiCad", None),
    ("kipy.board_types", "ArcTrack", None),
    ("kipy.board_types", "FootprintInstance", None),
    ("kipy.board_types", "Pad", None),
    ("kipy.board_types", "Track", None),
    ("kipy.board_types", "Via", None),
    ("kipy.proto.board.board_types_pb2", "BoardLayer", None),
    ("kipy.proto.board.board_types_pb2", "PadStackShape", None),
}
APPROVED_DIRECT_IMPORTS = {"json", "re"}
LOCAL_FUNCTIONS = {
    "copper_layers",
    "footprint_row",
    "item_id",
    "layer_name",
    "main",
    "millimetres",
    "net_row",
    "pad_copper_geometry",
    "pad_row",
    "point_mm",
    "selection_row",
    "sorted_rows",
    "track_row",
    "via_row",
}
APPROVED_DIRECT_CALLS = {
    "KiCad",
    "RuntimeError",
    "installed_version",
    "int",
    "isinstance",
    "len",
    "list",
    "print",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "type",
    "zip",
} | LOCAL_FUNCTIONS
IMPORTED_BINDINGS = APPROVED_DIRECT_IMPORTS | {
    alias_name or name for _, name, alias_name in APPROVED_IMPORTS
}
PROTECTED_BINDINGS = APPROVED_DIRECT_CALLS | IMPORTED_BINDINGS | LOCAL_FUNCTIONS
IMMUTABLE_API_ROOT_BINDINGS = {"board", "kicad"}
APPROVED_MUTABLE_CONTAINER_BINDINGS = {"details_by_id", "owners"}
SENSITIVE_BINDINGS = (
    PROTECTED_BINDINGS
    | IMMUTABLE_API_ROOT_BINDINGS
    | APPROVED_MUTABLE_CONTAINER_BINDINGS
)
EXPECTED_SENSITIVE_BINDING_COUNTS = {
    **{name: 0 for name in APPROVED_DIRECT_CALLS},
    **{name: 1 for name in IMPORTED_BINDINGS},
    **{name: 1 for name in LOCAL_FUNCTIONS},
    "board": 1,
    "details_by_id": 2,
    "kicad": 1,
    "owners": 1,
}
APPROVED_ATTRIBUTE_CALLS = {
    "', '.join",
    "'.'.join",
    "BoardLayer.Name",
    "PadStackShape.Name",
    "board.get_active_layer",
    "board.get_enabled_layers",
    "board.get_footprints",
    "board.get_nets",
    "board.get_pads",
    "board.get_selection",
    "board.get_tracks",
    "board.get_vias",
    "board.get_visible_layers",
    "details_by_id.get",
    "enum_name.startswith",
    "enum_name[3:].split",
    "json.dumps",
    "kicad.get_api_version",
    "kicad.get_board",
    "kicad.get_version",
    "owners.get",
    "re.fullmatch",
    "row.get",
}
APPROVED_ATTRIBUTE_READS = {
    "', '.join",
    "'.'.join",
    "BoardLayer.Name",
    "PadStackShape.Name",
    "board.document",
    "board.document.board_filename",
    "board.document.project",
    "board.document.project.path",
    "board.get_active_layer",
    "board.get_enabled_layers",
    "board.get_footprints",
    "board.get_nets",
    "board.get_pads",
    "board.get_selection",
    "board.get_tracks",
    "board.get_vias",
    "board.get_visible_layers",
    "details_by_id.get",
    "enum_name.startswith",
    "enum_name[3:].split",
    "footprint.definition",
    "footprint.definition.pads",
    "footprint.layer",
    "footprint.orientation",
    "footprint.orientation.degrees",
    "footprint.position",
    "footprint.reference_field",
    "footprint.reference_field.text",
    "footprint.reference_field.text.value",
    "footprint.value_field",
    "footprint.value_field.text",
    "footprint.value_field.text.value",
    "item.id",
    "item.id.value",
    "json.dumps",
    "kicad.get_api_version",
    "kicad.get_board",
    "kicad.get_version",
    "layer.layer",
    "layer.offset",
    "layer.shape",
    "layer.size",
    "net.name",
    "owners.get",
    "pad.net",
    "pad.number",
    "pad.pad_type",
    "pad.padstack",
    "pad.padstack.copper_layers",
    "pad.position",
    "point.x",
    "point.y",
    "re.fullmatch",
    "row.get",
    "track.end",
    "track.layer",
    "track.mid",
    "track.net",
    "track.start",
    "track.width",
    "type(item).__name__",
    "via.diameter",
    "via.drill_diameter",
    "via.net",
    "via.position",
    "via.type",
}
FORBIDDEN_NAME_REFERENCES = {
    "__import__",
    "__builtins__",
    "breakpoint",
    "compile",
    "delattr",
    "eval",
    "exec",
    "getattr",
    "globals",
    "locals",
    "open",
    "setattr",
    "vars",
}


class BindingCollector(ast.NodeVisitor):
    """Collect every syntax form that can introduce or shadow a name."""

    def __init__(self) -> None:
        self.events: list[tuple[str, int, str]] = []

    def record(self, name: str | None, node: ast.AST, kind: str) -> None:
        if name:
            self.events.append((name, getattr(node, "lineno", 0), kind))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.record(alias.asname or alias.name.split(".")[0], node, "import")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.record(alias.asname or alias.name, node, "from-import")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.record(node.name, node, "function definition")
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.record(node.name, node, "async function definition")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.record(node.name, node, "class definition")
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        self.record(node.arg, node, "function or lambda argument")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.record(node.id, node, "store target")

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.record(node.name, node, "exception alias")
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        self.record(node.name, node, "match-as capture")
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        self.record(node.name, node, "match-star capture")
        self.generic_visit(node)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        self.record(node.rest, node, "match-mapping rest capture")
        self.generic_visit(node)


def binding_errors(tree: ast.AST) -> list[str]:
    collector = BindingCollector()
    collector.visit(tree)
    counts: dict[str, int] = {}
    for name, _line, _kind in collector.events:
        counts[name] = counts.get(name, 0) + 1

    errors: list[str] = []
    for name, expected in sorted(EXPECTED_SENSITIVE_BINDING_COUNTS.items()):
        actual = counts.get(name, 0)
        if actual == expected:
            continue
        if name in IMMUTABLE_API_ROOT_BINDINGS:
            label = "immutable API-root binding drift"
        elif name in APPROVED_MUTABLE_CONTAINER_BINDINGS:
            label = "protected mutable-container binding drift"
        else:
            label = "protected binding drift"
        details = ", ".join(
            f"line {line} ({kind})"
            for event_name, line, kind in collector.events
            if event_name == name
        )
        errors.append(
            f"{label} {name}: expected {expected}, got {actual}"
            + (f" [{details}]" if details else "")
        )

    for node in ast.walk(tree):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declaration = type(node).__name__.lower()
            for name in node.names:
                if name in SENSITIVE_BINDINGS:
                    errors.append(
                        f"line {node.lineno}: protected {declaration} declaration {name}"
                    )
            errors.append(f"line {node.lineno}: {declaration} declarations are not approved")
    return errors


def definition_shape_errors(tree: ast.AST) -> list[str]:
    errors: list[str] = []
    definitions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    counts: dict[str, int] = {}
    for node in definitions:
        counts[node.name] = counts.get(node.name, 0) + 1
        if isinstance(node, ast.AsyncFunctionDef):
            errors.append(f"line {node.lineno}: async function definitions are not approved")
        if node.name not in LOCAL_FUNCTIONS:
            errors.append(f"line {node.lineno}: unapproved function definition {node.name}")
    for name in sorted(LOCAL_FUNCTIONS):
        if counts.get(name, 0) != 1:
            errors.append(
                f"function definition count drift {name}: expected 1, got {counts.get(name, 0)}"
            )
    lambda_count = sum(isinstance(node, ast.Lambda) for node in ast.walk(tree))
    if lambda_count != 2:
        errors.append(f"lambda count drift: expected 2, got {lambda_count}")
    return errors


EXPECTED_MUTABLE_CONTAINER_LOADS = {
    "details_by_id": {
        "main|Call.args[1]:selection_row|ListComp|Assign:selection": 1,
        "selection_row|Attribute.value:get|-|Assign:details": 1,
    },
    "owners": {
        "main|Call.args[0]:set|-|Assign:missing_owners": 1,
        "main|Attribute.value:get|ListComp|Assign:pad_rows": 1,
        "main|Subscript.value|-|Assign:owners[item_id(pad)]": 1,
    },
}


def parent_links(tree: ast.AST) -> dict[int, tuple[ast.AST, str, int | None]]:
    links: dict[int, tuple[ast.AST, str, int | None]] = {}
    for parent in ast.walk(tree):
        for field, value in ast.iter_fields(parent):
            if isinstance(value, ast.AST):
                links[id(value)] = (parent, field, None)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    if isinstance(child, ast.AST):
                        links[id(child)] = (parent, field, index)
    return links


def assignment_label(node: ast.AST) -> str:
    if isinstance(node, ast.Assign):
        return "Assign:" + ",".join(ast.unparse(target) for target in node.targets)
    if isinstance(node, ast.AnnAssign):
        return "AnnAssign:" + ast.unparse(node.target)
    return type(node).__name__


def mutable_load_signature(
    node: ast.Name,
    links: dict[int, tuple[ast.AST, str, int | None]],
) -> str:
    parent, field, index = links[id(node)]
    if isinstance(parent, ast.Attribute):
        immediate = f"Attribute.{field}:{parent.attr}"
    elif isinstance(parent, ast.Call):
        suffix = f"[{index}]" if index is not None else ""
        immediate = f"Call.{field}{suffix}:{ast.unparse(parent.func)}"
    else:
        suffix = f"[{index}]" if index is not None else ""
        immediate = f"{type(parent).__name__}.{field}{suffix}"

    function = "<module>"
    comprehension = "-"
    statement = "-"
    current: ast.AST = node
    while id(current) in links:
        ancestor, _field, _index = links[id(current)]
        if comprehension == "-" and isinstance(
            ancestor, (ast.DictComp, ast.GeneratorExp, ast.ListComp, ast.SetComp)
        ):
            comprehension = type(ancestor).__name__
        if statement == "-" and isinstance(ancestor, ast.stmt):
            statement = assignment_label(ancestor)
        if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = ancestor.name
            break
        current = ancestor
    return "|".join((function, immediate, comprehension, statement))


def mutable_container_load_errors(tree: ast.AST) -> list[str]:
    links = parent_links(tree)
    actual: dict[str, dict[str, int]] = {
        name: {} for name in APPROVED_MUTABLE_CONTAINER_BINDINGS
    }
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in APPROVED_MUTABLE_CONTAINER_BINDINGS
        ):
            continue
        signature = mutable_load_signature(node, links)
        counts = actual[node.id]
        counts[signature] = counts.get(signature, 0) + 1

    errors: list[str] = []
    for name in sorted(APPROVED_MUTABLE_CONTAINER_BINDINGS):
        expected_signatures = EXPECTED_MUTABLE_CONTAINER_LOADS[name]
        actual_signatures = actual[name]
        expected_count = sum(expected_signatures.values())
        actual_count = sum(actual_signatures.values())
        if actual_count != expected_count:
            errors.append(
                f"protected mutable-container load count drift {name}: "
                f"expected {expected_count}, got {actual_count}"
            )
        for signature in sorted(set(actual_signatures) | set(expected_signatures)):
            expected = expected_signatures.get(signature, 0)
            observed = actual_signatures.get(signature, 0)
            if observed != expected:
                errors.append(
                    f"protected mutable-container load context drift {name}: "
                    f"{signature!r} expected {expected}, got {observed}"
                )
    return errors


def annotation_nodes(tree: ast.AST) -> set[int]:
    nodes: set[int] = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        annotations = [argument.annotation for argument in (*function.args.args, *function.args.kwonlyargs)]
        annotations.append(function.args.vararg.annotation if function.args.vararg else None)
        annotations.append(function.args.kwarg.annotation if function.args.kwarg else None)
        annotations.append(function.returns)
        for annotation in annotations:
            if annotation is not None:
                nodes.update(id(node) for node in ast.walk(annotation))
    return nodes


def isinstance_type_nodes(tree: ast.AST) -> set[int]:
    nodes: set[int] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
        ):
            continue
        nodes.update(id(child) for child in node.args[1:] for child in ast.walk(child))
    return nodes


def kipy_attribute_base_nodes(tree: ast.AST) -> set[int]:
    nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            nodes.add(id(node.value))
    return nodes


def permitted_subscript_assignment(target: ast.Subscript) -> bool:
    if not isinstance(target.value, ast.Name):
        return False
    if target.value.id == "row":
        return (
            isinstance(target.slice, ast.Name)
            and target.slice.id == "key"
        ) or (
            isinstance(target.slice, ast.Constant)
            and target.slice.value in {"footprint_id", "mid_mm", "reference"}
        )
    if target.value.id == "owners":
        return (
            isinstance(target.slice, ast.Call)
            and isinstance(target.slice.func, ast.Name)
            and target.slice.func.id == "item_id"
        )
    return False


class SubscriptMutationCollector(ast.NodeVisitor):
    """Record the exact reviewed functions that intentionally mutate local maps."""

    def __init__(self) -> None:
        self.function_stack: list[str] = []
        self.sites: list[tuple[str, str, str, int]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def record_target(self, target: ast.AST, node: ast.AST) -> None:
        if not isinstance(target, ast.Subscript):
            return
        function = self.function_stack[-1] if self.function_stack else "<module>"
        owner = target.value.id if isinstance(target.value, ast.Name) else ast.unparse(target.value)
        if isinstance(target.slice, ast.Name):
            key = f"name:{target.slice.id}"
        elif isinstance(target.slice, ast.Constant):
            key = f"constant:{target.slice.value!r}"
        else:
            key = f"expression:{ast.unparse(target.slice)}"
        self.sites.append((function, owner, key, getattr(node, "lineno", 0)))

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self.record_target(target, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.record_target(node.target, node)
        self.generic_visit(node)


EXPECTED_SUBSCRIPT_MUTATION_COUNTS = {
    ("main", "owners", "expression:item_id(pad)"): 1,
    ("pad_row", "row", "constant:'footprint_id'"): 1,
    ("pad_row", "row", "constant:'reference'"): 1,
    ("selection_row", "row", "name:key"): 1,
    ("track_row", "row", "constant:'mid_mm'"): 1,
}


def subscript_mutation_errors(tree: ast.AST) -> list[str]:
    collector = SubscriptMutationCollector()
    collector.visit(tree)
    counts: dict[tuple[str, str, str], int] = {}
    errors: list[str] = []
    for function, owner, key, line in collector.sites:
        signature = (function, owner, key)
        counts[signature] = counts.get(signature, 0) + 1
        if signature not in EXPECTED_SUBSCRIPT_MUTATION_COUNTS:
            errors.append(
                f"line {line}: unapproved subscript mutation site "
                f"{function}:{owner}[{key}]"
            )
    for signature, expected in sorted(EXPECTED_SUBSCRIPT_MUTATION_COUNTS.items()):
        actual = counts.get(signature, 0)
        if actual != expected:
            errors.append(
                "subscript mutation count drift "
                f"{signature[0]}:{signature[1]}[{signature[2]}]: "
                f"expected {expected}, got {actual}"
            )
    return errors


def assignment_errors(tree: ast.AST) -> list[str]:
    errors: list[str] = []
    forbidden_syntax = (
        ast.AsyncFor,
        ast.AsyncWith,
        ast.Await,
        ast.Delete,
        ast.Match,
        ast.NamedExpr,
        ast.With,
        ast.Yield,
        ast.YieldFrom,
    )
    for node in ast.walk(tree):
        if isinstance(node, forbidden_syntax):
            errors.append(f"line {node.lineno}: implicit-mutation syntax {type(node).__name__}")
        if isinstance(node, ast.AugAssign):
            errors.append(f"line {node.lineno}: augmented assignment is not approved")
        if isinstance(node, ast.ClassDef):
            errors.append(f"line {node.lineno}: class definitions are not approved")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list:
            errors.append(f"line {node.lineno}: function decorators are not approved")
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in PROTECTED_BINDINGS:
                    errors.append(f"line {node.lineno}: protected binding reassignment {target.id}")
                elif isinstance(target, ast.Attribute):
                    errors.append(f"line {node.lineno}: attribute assignment is not approved")
                elif isinstance(target, ast.Subscript) and not permitted_subscript_assignment(target):
                    errors.append(f"line {node.lineno}: subscript assignment is not approved")
                elif not isinstance(target, (ast.Name, ast.Subscript)):
                    errors.append(f"line {node.lineno}: assignment target is not approved")
    return errors


def audit_tree(tree: ast.AST) -> list[str]:
    errors: list[str] = []
    seen_imports: set[tuple[str, str, str | None]] = set()
    seen_direct_imports: set[str] = set()
    imported_kipy_types: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in APPROVED_DIRECT_IMPORTS:
                    errors.append(f"line {node.lineno}: unexpected direct import {alias.name}")
                seen_direct_imports.add(alias.name)
            if any(alias.asname is not None for alias in node.names):
                errors.append(f"line {node.lineno}: aliased direct import is not approved")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                entry = (node.module or "", alias.name, alias.asname)
                seen_imports.add(entry)
                if alias.name == "*":
                    errors.append(f"line {node.lineno}: wildcard import is not approved")
                elif entry not in APPROVED_IMPORTS:
                    errors.append(f"line {node.lineno}: unexpected from import {entry[0]}.{entry[1]}")
                if node.module and node.module.startswith("kipy") and alias.name != "KiCad":
                    imported_kipy_types.add(alias.asname or alias.name)

    if seen_imports != APPROVED_IMPORTS:
        errors.append("imports do not exactly match the approved snapshot imports")
    if seen_direct_imports != APPROVED_DIRECT_IMPORTS:
        errors.append("direct imports do not exactly match the approved snapshot imports")

    permitted_annotation_nodes = annotation_nodes(tree)
    permitted_isinstance_nodes = isinstance_type_nodes(tree)
    permitted_kipy_attribute_nodes = kipy_attribute_base_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in APPROVED_DIRECT_CALLS:
                    errors.append(f"line {node.lineno}: unapproved direct call {node.func.id}")
            elif isinstance(node.func, ast.Attribute):
                expression = ast.unparse(node.func)
                if expression not in APPROVED_ATTRIBUTE_CALLS:
                    errors.append(f"line {node.lineno}: unapproved attribute call {expression}")
            else:
                errors.append(f"line {node.lineno}: indirect or dynamic call")
        elif isinstance(node, ast.Attribute):
            expression = ast.unparse(node)
            if expression not in APPROVED_ATTRIBUTE_READS:
                errors.append(f"line {node.lineno}: unapproved attribute read {expression}")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAME_REFERENCES:
                errors.append(f"line {node.lineno}: forbidden name reference {node.id}")
            if (
                node.id in imported_kipy_types
                and id(node) not in permitted_annotation_nodes
                and id(node) not in permitted_isinstance_nodes
                and id(node) not in permitted_kipy_attribute_nodes
            ):
                errors.append(f"line {node.lineno}: unexpected KiCad type reference {node.id}")
    return (
        errors
        + assignment_errors(tree)
        + binding_errors(tree)
        + definition_shape_errors(tree)
        + mutable_container_load_errors(tree)
        + subscript_mutation_errors(tree)
    )


def audit_source(source: str) -> list[str]:
    return audit_tree(ast.parse(source))


def require_rejected(known_good: str, mutation: str, label: str, expected: str) -> None:
    errors = audit_source(known_good + "\n" + mutation)
    if not any(expected in error for error in errors):
        raise AssertionError(
            f"allowlist did not report {expected!r} for {label}: {errors!r}"
        )


def require_accepted(known_good: str, addition: str, label: str) -> None:
    errors = audit_source(known_good + "\n" + addition)
    if errors:
        raise AssertionError(f"clean control {label} was rejected: {errors!r}")


def run_negative_self_tests(known_good: str) -> None:
    adversarial = (
        ("f = board.save\nf()", "aliased_save", "unapproved attribute read board.save"),
        (
            "object.__getattribute__(board, 'save')()",
            "object_getattribute",
            "unapproved attribute read object.__getattribute__",
        ),
        ("vars(board)['save']()", "vars_lookup", "forbidden name reference vars"),
        ("f = open\nf('x', 'w')", "aliased_open", "forbidden name reference open"),
        ("f = eval\nf('1')", "aliased_eval", "forbidden name reference eval"),
        ("f = setattr\nf(board, 'x', 1)", "aliased_setattr", "forbidden name reference setattr"),
        (
            "print = __builtins__['open']\nprint('x', 'w')",
            "builtins_open_alias",
            "protected binding reassignment print",
        ),
        (
            "print = globals()['open']\nprint('x', 'w')",
            "globals_open_alias",
            "protected binding reassignment print",
        ),
        ("KiCad = fake", "protected_import_binding", "protected binding reassignment KiCad"),
        (
            "__import__('os').remove('x')",
            "dynamic_import_remove",
            "forbidden name reference __import__",
        ),
        ("board.proto.ClearField('x')", "proto_clear_field", "unapproved attribute read board.proto"),
        ("collection.append(item)", "collection_append", "unapproved attribute read collection.append"),
        ("list([Via()])", "nested_kicad_constructor", "unapproved direct call Via"),
        ("x = board.__dict__", "dict_traversal", "unapproved attribute read board.__dict__"),
        ("x = board.proto", "proto_traversal", "unapproved attribute read board.proto"),
        ("import os", "module_import", "unexpected direct import os"),
        ("from os import *", "wildcard_import", "wildcard import is not approved"),
        ("board.save = f", "attribute_assignment", "unapproved attribute read board.save"),
        ("board['x'] = 1", "subscript_assignment", "subscript assignment is not approved"),
        ("del board.x", "delete_syntax", "implicit-mutation syntax Delete"),
        ("with context: pass", "with_syntax", "implicit-mutation syntax With"),
        ("await action()", "await_syntax", "implicit-mutation syntax Await"),
        ("yield value", "yield_syntax", "implicit-mutation syntax Yield"),
        ("(value := 1)", "named_expression", "implicit-mutation syntax NamedExpr"),
        ("value += 1", "augmented_assignment", "augmented assignment is not approved"),
        ("class Extra: pass", "class_definition", "class definitions are not approved"),
        ("@decorator\ndef extra(): pass", "decorator", "function decorators are not approved"),
        (
            "for print in [str]:\n    pass",
            "protected_for_target",
            "protected binding drift print",
        ),
        (
            "[print for print in [str]]",
            "protected_comprehension_target",
            "protected binding drift print",
        ),
        (
            "def print():\n    pass",
            "protected_function_name",
            "protected binding drift print",
        ),
        (
            "def extra(print):\n    pass",
            "protected_positional_argument",
            "protected binding drift print",
        ),
        (
            "def extra(print, /):\n    pass",
            "protected_positional_only_argument",
            "protected binding drift print",
        ),
        (
            "def extra(*, print):\n    pass",
            "protected_keyword_only_argument",
            "protected binding drift print",
        ),
        (
            "def extra(*print):\n    pass",
            "protected_vararg",
            "protected binding drift print",
        ),
        (
            "def extra(**print):\n    pass",
            "protected_kwarg",
            "protected binding drift print",
        ),
        (
            "lambda print: print",
            "protected_lambda_argument",
            "protected binding drift print",
        ),
        (
            "try:\n    pass\nexcept Exception as print:\n    pass",
            "protected_exception_alias",
            "protected binding drift print",
        ),
        (
            "safe, print = (str, str)",
            "protected_unpack_target",
            "protected binding drift print",
        ),
        (
            "for board in [board.document]:\n    board.get_tracks()",
            "board_for_shadow",
            "immutable API-root binding drift board",
        ),
        (
            "[board.get_tracks() for board in [board]]",
            "board_comprehension_shadow",
            "immutable API-root binding drift board",
        ),
        (
            "def extra(board):\n    return board.get_tracks()",
            "board_parameter_shadow",
            "immutable API-root binding drift board",
        ),
        (
            "board = kicad.get_board()",
            "board_reassignment",
            "immutable API-root binding drift board",
        ),
        (
            "owners: dict = {}",
            "owners_annotated_rebinding",
            "protected mutable-container binding drift owners",
        ),
        (
            "owners, other = ({}, {})",
            "owners_unpack_rebinding",
            "protected mutable-container binding drift owners",
        ),
        (
            "for owners in [pad.padstack.copper_layers]:\n    pass",
            "owners_loop_rebinding",
            "protected mutable-container binding drift owners",
        ),
        (
            "owners = pad.padstack.copper_layers\nowners[item_id(pad)] = {}",
            "owners_replacement_then_approved_mutation",
            "protected mutable-container binding drift owners",
        ),
        (
            "details_by_id = {}",
            "details_map_replacement",
            "protected mutable-container binding drift details_by_id",
        ),
        (
            "row = owners\nrow['footprint_id'] = 'x'",
            "owners_direct_alias",
            "protected mutable-container load count drift owners",
        ),
        (
            "row = [details_by_id][0]",
            "details_wrapped_alias",
            "protected mutable-container load count drift details_by_id",
        ),
        (
            "row = [owners for _ in [0]][0]",
            "owners_list_comprehension_alias",
            "protected mutable-container load count drift owners",
        ),
        (
            "row = {0: details_by_id for _ in [0]}[0]",
            "details_dict_comprehension_alias",
            "protected mutable-container load count drift details_by_id",
        ),
        (
            "row = tuple(owners for _ in [0])[0]",
            "owners_generator_expression_alias",
            "protected mutable-container load count drift owners",
        ),
        (
            "wrapped = {details_by_id for _ in [0]}",
            "details_set_comprehension_alias",
            "protected mutable-container load count drift details_by_id",
        ),
        (
            "row = list([owners])[0]",
            "owners_call_wrapper_alias",
            "protected mutable-container load count drift owners",
        ),
        (
            "row = tuple([details_by_id])[0]",
            "details_call_wrapper_alias",
            "protected mutable-container load count drift details_by_id",
        ),
        (
            "owners[item_id(pad)] = {}",
            "extra_owners_mutation_site",
            "unapproved subscript mutation site <module>:owners",
        ),
        (
            "row['reference'] = 'x'",
            "extra_row_mutation_site",
            "unapproved subscript mutation site <module>:row",
        ),
        (
            "global print",
            "protected_global_declaration",
            "protected global declaration print",
        ),
        (
            "nonlocal board",
            "protected_nonlocal_declaration",
            "protected nonlocal declaration board",
        ),
        (
            "match 1:\n    case print:\n        pass",
            "protected_match_as_capture",
            "protected binding drift print",
        ),
        (
            "match []:\n    case [*board]:\n        pass",
            "protected_match_star_capture",
            "immutable API-root binding drift board",
        ),
        (
            "match {}:\n    case {**owners}:\n        pass",
            "protected_match_mapping_capture",
            "protected mutable-container binding drift owners",
        ),
    )
    for mutation, label, expected in adversarial:
        require_rejected(known_good, mutation, label, expected)


def run_clean_self_tests(known_good: str) -> None:
    controls = (
        ("safe = [value for value in [0]]", "list_comprehension"),
        ("safe = {value: value for value in [0]}", "dict_comprehension"),
        ("safe = tuple(value for value in [0])", "generator_expression"),
        ("safe = {value for value in [0]}", "set_comprehension"),
        ("safe = list([0])", "call_wrapper"),
    )
    for addition, label in controls:
        require_accepted(known_good, addition, label)


def preflight_compares_live_project_path(source: str) -> bool:
    return all(
        fragment in source
        for fragment in (
            "project_path=",
            "$liveProjectLine",
            "$liveProject",
            "wrong_live_project",
        )
    )


def main() -> int:
    # LF line endings before hashing: a Windows checkout may write CRLF, and the reviewed source is the same either way.
    target_bytes = TARGET.read_bytes().replace(b"\r\n", b"\n")
    actual_hash = hashlib.sha256(target_bytes).hexdigest().upper()
    if actual_hash != TARGET_SHA256:
        raise AssertionError(
            "snapshot source hash changed; deliberate re-review and TARGET_SHA256 update are required "
            f"(expected {TARGET_SHA256}, got {actual_hash})"
        )
    source = target_bytes.decode("utf-8")
    errors = audit_source(source)
    if errors:
        raise AssertionError("query-only snapshot violates AST allowlist: " + "; ".join(errors))
    run_negative_self_tests(source)
    run_clean_self_tests(source)

    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    modes = registry["scripts"].get("tools/kicad/kicad_semantic_snapshot.py")
    if modes != ["Live"]:
        raise AssertionError(f"snapshot registry mode must be exactly ['Live'], got {modes!r}")
    if "net.code" in source:
        raise AssertionError("snapshot must not emit or order by deprecated net.code")
    if not preflight_compares_live_project_path(PREFLIGHT.read_text(encoding="utf-8")):
        raise AssertionError(
            "live preflight must compare the resolved project_path, not only the board filename"
        )
    print("preflight_project_path_guard=ok")
    print("semantic_snapshot_ast_allowlist=ok")
    return 0


if __name__ == "__main__":
    # The fixtures parse match statements, which need Python 3.10's parser. The
    # snapshot it audits runs in Live mode and stays 3.9-compatible for KiCad's
    # Python on macOS; only this test needs 3.10.
    if sys.version_info < (3, 10):
        sys.exit(f"needs Python 3.10 or newer (this is {sys.version.split()[0]}); run it with py -3 on Windows")
    sys.exit(main())
