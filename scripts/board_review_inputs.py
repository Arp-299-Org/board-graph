"""KiCad inputs used by revision-scoped reviews."""
import hashlib
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from board_space import STOCK_MODELS, STOCK_MODEL_INSTALLS
from kicad_sexp import parse


def model_path(reference, board_dir, root):
    stock = "${KICAD10_3DMODEL_DIR}/"
    project = "${KIPRJMOD}/"
    if reference.startswith(stock):
        relative = reference[len(stock):]
        installs = [*STOCK_MODEL_INSTALLS, Path("/usr/share/kicad/3dmodels")]
        for base in [*STOCK_MODELS, *installs]:
            path = (base / relative).resolve()
            if path.is_file():
                if not any(path.is_relative_to(p.resolve()) for p in installs):
                    raise ValueError("Stock model is outside KiCad's model directories")
                return path, True
        raise ValueError("Stock model unavailable")
    relative = reference.removeprefix(project)
    if "${" in relative or ":" in relative or PureWindowsPath(relative).anchor or PurePosixPath(relative).is_absolute():
        raise ValueError("Unsupported saved model path")
    root = Path(root).resolve()
    path = (board_dir / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Model is outside the project")
    return path, False


def model_inputs(text, board_dir, root, *, freeze=None, git_read=None):
    """Hash every model reference. For rendering, point the private PCB at those exact frozen bytes."""
    root = Path(root).resolve()
    spans = []
    parse(text, model_spans=spans)
    models, frozen = {}, {}
    for ref in dict.fromkeys(ref for _, _, ref in spans):
        try:
            path, stock = model_path(ref, board_dir, root)
            suffix = path.suffix.lower()
            if suffix not in (".step", ".stp", ".wrl"):
                raise ValueError("Unsupported model format")
            data = git_read(path.relative_to(root).as_posix()) if git_read and not stock else path.read_bytes()
            if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
                oid = re.search(rb"(?m)^oid sha256:([0-9a-f]{64})\r?$", data)
                if git_read and not stock and oid:
                    models[ref] = oid[1].decode("ascii")
                    continue
                raise ValueError("Model file needs Git LFS content")
            if not data or (suffix == ".wrl" and re.search(rb"\b(?:url|Inline|EXTERNPROTO)\b", data)):
                raise ValueError("Model is empty or needs external files")
            fingerprint = hashlib.sha256(data).hexdigest()
            if freeze is not None:
                target = freeze / (fingerprint + suffix)
                target.write_bytes(data)
                frozen[ref] = json.dumps(target.as_posix(), ensure_ascii=False)
            models[ref] = fingerprint
        except (OSError, ValueError):
            models[ref] = None
    if frozen:
        parts, end = [], 0
        for start, stop, ref in spans:
            parts.extend((text[end:start], frozen.get(ref, text[start:stop])))
            end = stop
        text = "".join([*parts, text[end:]])
    return models, text


def routing_project(data):
    """Project constraints, class assignments and routing geometry, independent of display preferences."""
    if not isinstance(data, dict) or not isinstance(data.get("board", {}), dict):
        raise ValueError("Invalid KiCad project board settings")
    design = data.get("board", {}).get("design_settings", {})
    if not isinstance(design, dict) or not isinstance(design.get("defaults", {}), dict):
        raise ValueError("Invalid KiCad design settings")
    design = dict(design)
    defaults = design.pop("defaults", {})
    design["defaults"] = {k: v for k, v in defaults.items() if k in ("pads", "zones") or k.startswith("copper_")}
    nets = data.get("net_settings", {})
    if not isinstance(nets, dict) or not isinstance(nets.get("classes", []), list) or not all(
            isinstance(group, dict) for group in nets.get("classes", [])):
        raise ValueError("Invalid KiCad net class settings")
    nets = dict(nets)
    nets.pop("net_colors", None)
    if "classes" in nets:
        style = {"pcb_color", "schematic_color", "line_style", "wire_width", "bus_width"}
        nets["classes"] = [{k: v for k, v in group.items() if k not in style} for group in nets["classes"]]
    return {"design_settings": design, "net_settings": nets,
            **{key: data.get(key) for key in ("component_class_settings", "tuning_profiles", "text_variables")}}
