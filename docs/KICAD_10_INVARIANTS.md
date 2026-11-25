# KiCad 10 and Runner Invariants

Real-world invariants learned from live KiCad 10, OS, and CLI failures. Probe real files, configs, and schemas before mocking them.

## Rule for Triage

Before editing code that crosses a KiCad file format, IPC (`kipy`), X11/macOS GUI, shell, or agent CLI boundary, inspect the real schema, config file, or command output once first. Unit tests with mocks prove logic against assumptions; they do not prove the boundary behaves that way.

## `.kicad_pcb` S-Expression Normalizations

| Surface | Real KiCad 10 Behavior | Repo Guard |
|---|---|---|
| Angles (`at`) | Saves `270` deg as `-90` and omits `0` deg | Normalize via `kicad_sexp.normalize_angles()` |
| Numeric tokens | Strips trailing `.0` (`3.0` -> `3`, `1.0` -> `1`) on geometry coordinates/sizes | Normalize geometry heads via `kicad_rules_geom.normalize_numeric_tokens()` / `canonicalize_guard_node()` and `placement_view()` |
| UUIDs & timestamps | Adds `(uuid ...)` (before `(effects ...)`) on `gr_*`, `fp_*`, `pad`, and `property` items | Strip `(uuid ...)` and `(tstamp ...)` recursively in `canonicalize_guard_node()` |
| Nets inside pads/tracks/zones | Writes `(net "name")` (or legacy `(net ID "name")` / `(net_name "name")` on zones) and omits top-level `(net N "name")` declarations | Canonicalize via `kicad_rules_geom.canonicalize_guard_node()` and check `net_name` or `net` on zones |
| Setup & defaults | Rewrites empty `(zone_defaults)`, `(embedded_fonts no)`, `(pcbplotparams ...)`, and default via finish (`tenting yes/yes`, `covering no/no`, `plugging no/no`, `capping no`, `filling no`) on first save | Canonicalized in `routing_guard` and `kicad_reload_board` (`_KICAD10_SETUP_DEFAULTS`) |
| Version headers | Updates `(version ...)`, `(generator ...)`, `(generator_version ...)` on save | Strip volatile headers in board views and `beyond_copper` |
| Footprint children & metadata | Reorders children/`attr` flags, adds `(duplicate_pad_numbers_are_jumpers no)` and hidden empty `Description ""`, drops hidden redundant `Footprint <lib_id>` | Canonicalized in `canonicalize_guard_node()` and `purchasing_view()` |
| Graphic rects (`fp_rect`/`gr_rect`) | Adds explicit `(fill no)` on first save of KiCad 8/synthetic boards | Baseline `placement_view` after initial KiCad 10 save |
| 3D transform order | Evaluates `T(offset) * Rz(-z) * Ry(-y) * Rx(-x) * S(scale)` | Documented in `scripts/board_space.py` |

## KiCad 10 IPC (`kipy`) Contracts

- **Thermal zone defaults:** `kipy` `Zone()` writes `0 mm` thermal spoke gap and bridge width when optional fields are left unset (`v0.29.10`), causing through-hole thermal pads to connect solid. `plan_pour` in `scripts/kicad_lay_plan.py` defaults `thermal_gap` and `thermal_bridge_width` to `0.5 mm` for `thermal` / `pth_thermal`.
- **Timeout:** Use `60000` ms (`60 s`) IPC timeouts for dense board zone refills and saves (`5000 ms` times out on loaded boards).
- **O(1) session queries:** Read footprints, enabled layers, and netclasses once per session via `KipyBoardCacheMixin` (`scripts/kicad_ipc.py`), using bulk `get_netclass_for_nets()` instead of per-pad RPC loops.
- **Sockets:** Primary server binds `/tmp/kicad/api.sock`; secondary instances bind `/tmp/kicad/api-<pid>.sock`. Match `board.document.project.path` and `board.document.board_filename` before mutating.

## Headless Linux (`Xvfb`) and macOS Boundaries

- **First-run `"KiCad Setup"` wizard:** KiCad 10 pops up a modal `"KiCad Setup"` dialog (leaving `/tmp/kicad/api.sock` returning `KiCad is not ready to reply`) unless `~/.config/kicad/10.0/` has `kicad_common.json` (`meta.version = 6`, `api.enable_server = true`, `graphics.canvas_type = 2`, `do_not_show_again`), `kicad.json` (`system.first_run_shown = true`), `pcbnew.json` (`system.first_run_shown = true`), sibling app configs, and global library tables (`docker/entrypoint.sh`).
- **X11 window manager & visibility:** Run `openbox` inside `Xvfb`, pass `LIBGL_ALWAYS_SOFTWARE=1` and `NO_AT_BRIDGE=1`, and pass `--onlyvisible` to `xdotool search` (`scripts/kicad_reload_board.py`) so hidden GTK `pcbnew` helper windows do not trip `not_idle()`.
- **Locks & paths:** Linux locks `.kicad_pcb` via `os.chmod` write bits (`0o200`), macOS uses `chflags uchg`. Always `resolve()` temp/project roots on macOS (`/var` -> `/private/var`), except keep Python virtualenv binaries (`sys.executable` / `.venv/bin/python`) unresolved via `os.path.abspath()` so `pyvenv.cfg` stays visible.

## Fabrication and Agent CLI Boundaries

- **JLCPCB BOM:** Group one row per `LCSC Part #` (joining non-numeric functional labels, rejecting electrical/color mismatches) instead of emitting duplicate LCSC lines that JLCPCB's matcher leaves unticked.
- **Extra Gerbers:** Export and archive `User.Comments` (`Cmts.User`) when non-hidden elements exist.
- **CLI & shell hygiene:** Isolate `kicad-cli` under a temp config seeded with stock library tables (`tools/kicad/invoke_kicad_cli.py`). Handle 500-char `agy` stdout truncation and align chained `kicad_lay.py` JSON status lines by verb and order. Quote GitHub Actions step `name:` values whenever text contains `: `.
