# What a project changes after taking a version

One section per version, newest first: what a project does in its own files
after moving to a new release. Pin a release with
`pip install git+https://github.com/Arp-299-Org/board-graph.git@v<version>`
(nothing in a project changes until you choose to upgrade). Read the section
between your old version and the new one before upgrading to see whether any
project-owned files need updates. A version that asks nothing of a project
still has a section, which says so. Versions before 0.12.0 have their notes in
their tags only (`git tag -n99 v<version>`).

## 0.33.1

Nothing has to change in existing projects; bundles `scripts/board_autonomy.py` in `tools/board_graph_tool.json` so packaged pip wheels include it, restores executable permissions on shell scripts (`.github/scripts/release_tag.sh`, `docker/entrypoint.sh`, `scripts/mac_screen.sh`), enforces manifest coverage of `scripts/*.py` and `tools/kicad/*.py` in `.github/scripts/check_hygiene.py`, and adds a fresh-venv `Wheel install smoke` step to `.github/workflows/test.yml`.

## 0.33.0

Nothing has to change in existing projects. Highlights since 0.32.2:

- **Explicit autonomous delegated mode (`scripts/board_autonomy.py`, `board-graph init --autonomous`):** Allows opt-in `"autonomy": {"mode": "delegated", "until": "<card>", "authorized_by": "owner", "reason": "..."}` (default `"quote_files"`) so `agent:*` actors can pass owner cards cleanly while preserving all technical gates and `board-render.json` presentation checks, stamping `delegated: true` in records, surfacing a `Mode: AUTONOMOUS` banner in `where`/`brief`/`view.json`, and hard-blocking `order` and downstream assembly/first-article cards until re-approved by `owner`.
- **Track-craft remote/local CLI & `kicad-run` hardening (`scripts/board_track_craft.py`, `tools/kicad/check_track_craft.py`, `scripts/kicad_run.ps1`, `scripts/board_graph_cli.py`):** Adds `board-graph track-craft` to run track-craft on `kicad_machine` over SSH (or locally under `one_machine`), validates `Path(path).is_file()` before `pcbnew.LoadBoard()`, adds `--output` as an alias for `--json`, forwards `-- <args...>` as a PowerShell array, rejects comma-mangled `-ScriptArgs`, enforces `-TimeoutSeconds` (default 180), and refuses local Windows `Offline` KiCad runs when a remote `kicad_machine` is configured.
- **Dev check reliability & E2E coverage (`dev/tool_actions.py`, `dev/test_tool_dev.py`, `scripts/test_board_graph_e2e.py`, `.github/workflows/test.yml`):** Removes the Card 3 vs Card 6 metadata contradiction, fails fast on `hygiene`, runs test suites in parallel with live progress and `elapsed_s` warnings, removes the `_fast_pass` stub in `dev/test_tool_dev.py`, adds a no-KiCad CLI E2E suite, and wires `dev/test_tool_dev.py` into CI.
- **Public release preparation:** Adds MIT `LICENSE`, `CONTRIBUTING.md`, `.github/PULL_REQUEST_TEMPLATE.md` (with upstream model policy for `BANNED_MODELS`), single-machine Quick Start in `README.md`, and scrubs remaining private host, user, and project names from docs, workflows, fixtures, and unit tests.

## 0.32.2

Nothing has to change in existing projects; canonicalizes KiCad 9 to KiCad 10 first-save serialization diffs (`uuid`/`tstamp` additions, default `(setup ...)` via-finish blocks, `(duplicate_pad_numbers_are_jumpers no)`, hidden empty `Description ""`, dropped redundant `Footprint` matching the footprint library ID, reordered `(attr ...)` flags, and numeric `.0` coordinate formatting) in `scripts/kicad_rules_geom.py` (`canonicalize_guard_node`, `normalize_numeric_tokens`, `purchasing_footprint_row`) and `scripts/board_graph_actions.py` (`placement_view`, `purchasing_view`), and updates `docs/KICAD_10_INVARIANTS.md`.

## 0.32.1

Nothing has to change in existing projects; excludes ephemeral KiCad sidecar files (`*.kicad_prl`, `*.lck`, `~*.lck`, `*-backups/`, `fp-info-cache`) in `.gitignore`, `tool_paths.tool_dirty()`, `routing_run._external_tool_sync_checks()`, and `routing_run.wrapper()` via shared `tool_paths.DIRTY_EXCLUDES`, and adds `--project` external-mode argument parsing to `scripts/board_space.py`.

## 0.32.0

Nothing has to change in existing unvendored projects; anyone can now install the CLI directly from GitHub without cloning the repository (`pip install git+https://github.com/Arp-299-Org/board-graph.git@v0.32.0`) via self-contained wheel packaging (`board_graph_entry.py`, `setup.py`, `pyproject.toml`), initialize new repositories with `board-graph init` (and check the installed release with `board-graph version`), and run `board-graph route` from a non-git packaged wheel (`scripts/tool_paths.py`, `scripts/routing_run.py`) while verifying the KiCad machine's shared checkout by release tag and manifest SHA-256. Removes legacy vendored instructions from `README.md`, `AGENTS.md`, and `UPGRADING.md`.

## 0.31.0

Adds `docs/KICAD_10_INVARIANTS.md` wired into release triage (`dev/TOOL_DEV_PROCESS.md`, `dev/tool_graph.json`, `dev/tool_actions.py`) alongside the comment policy in `AGENTS.md`. Tightens `.github/scripts/check_hygiene.py` (`MAX_FILE_LINES = 1000`, scanning `scripts/`, `tools/kicad/`, `dev/`, `docker/`, stale-grandfather ratchet, comment hygiene, shell inline-Python compilation, LF checks, and workflow YAML step-colon checks), cleans dated owner-chat and private project comments in production scripts, and expands `docker/smoke_linux_ipc.py` to verify live thermal pour spoke defaults, `placement_view` save stability, and Gerber export.

## 0.30.2

Fix a Python syntax error in the Linux `windows()` branch of `scripts/mac_screen.sh` (`list` / `window` lookup) and add inline-Python syntax validation to `scripts/test_linux_runner.py`.

## 0.30.1

Filter Linux `xdotool search` to `--onlyvisible` in `scripts/kicad_reload_board.py` so hidden GTK `pcbnew` helper windows do not trip `not_idle()`, and add headless KiCad 10 first-run configuration in `docker/entrypoint.sh`.

## 0.30.0

Removes the unused `Mode: autoroute` solver path (`scripts/kicad_autoroute.py`, `scripts/test_kicad_autoroute.py`, `--autoroute-cleanup`, `kicad_machine.autorouter`, and `dru_autoroute_hints`) while retaining legacy `run-*.jsonl` receipt compatibility in `scripts/routing_evidence.py` and `scripts/routing_report.py`; new task files must use `Mode: ipc` (recommended), `Mode: hybrid`, or `Mode: interactive`. Adds native Linux + `Xvfb` support for `Mode: ipc` (`tools/kicad/kicad_install.py`, `scripts/board_space.py`, `scripts/kicad_run.ps1`, `scripts/kicad_preflight.ps1`, `scripts/setup_kicad_ipc.ps1`, `scripts/kicad_reload_board.py`, `scripts/editor_step_kicad.py`, `scripts/editor_step.py`, `scripts/mac.py`, `scripts/mac_screen.sh`, `scripts/routing_gemini.py`, `scripts/routing_codex.py`, `scripts/routing_run.py`, `scripts/test_linux_runner.py`), plus a reproducible KiCad 10 Docker runner (`docker/Dockerfile`, `docker/entrypoint.sh`, `docker/smoke_linux_ipc.py`) and GitHub Actions smoke workflow (`.github/workflows/linux-kicad-smoke.yml`).

## 0.29.11

Nothing has to change in existing projects or task files; chaining `kicad_lay.py` invocations in one shell command (such as `pour && zones`, `zones && pour`, or `pour && route`) previously parsed only the last JSON status line in combined stdout for every chained subcommand, hiding preceding `laid` edits or double-counting refusals. `audit_commands()` in `scripts/routing_codex.py` now aligns each `kicad_lay.py` subcommand with its own JSON status object by verb and execution order (`scripts/test_routing_codex.py`).

## 0.29.10

Nothing has to change in existing projects or task files; `kicad_lay.py pour` previously left `thermal_gap` and `thermal_bridge_width` unset (`None`) on `thermal`/`pth_thermal` pours, which caused KiCad 10's IPC protobuf `Zone()` to save `0 mm` spoke width/gap and connect through-hole pads solid to inner planes. Defaults `thermal_gap` and `thermal_bridge_width` to `0.5 mm` in `scripts/kicad_lay_plan.py` (`plan_pour`) whenever `connect in ("thermal", "pth_thermal")` and no explicit values are provided (`scripts/test_kicad_lay.py`).

## 0.29.9

Nothing has to change in existing projects or task files; KiCad 10 saves `270` deg footprint/pad/text angles as `-90` and omits `0` angles, which previously invalidated `view:placement` (blocking routing delivery approval rechecks) and `view:silkscreen`. Updates `placement_view`, `silkscreen_view`, `copper_view`, and `copper_and_silk_view` in `scripts/board_graph_actions.py` to normalize parsed S-expression angles via `kicad_sexp.normalize_angles()` while still detecting real orientation changes (`scripts/test_board_approvals.py`).

## 0.29.8

Nothing has to change in existing projects unless they want the new starter defaults; existing projects with `routing_agent.provider` unset can add `"provider": "gemini"` (and optionally `"review_provider": "gemini"`) under `routing_agent` in `board/board.json` to run Gemini via the Antigravity CLI (`agy`) by default. Updates starter `board/board.json` (`"provider": "gemini"`), `scripts/routing_run.py` CLI help and refusal hints noting `--provider gemini` uses `agy`, starter `board/SPACE.md` (`## In the user's hands`), and the `placement` card in `tools/board_graph.json` to include human-ergonomics finger/thumb touch clearance around tactile controls and signal/power pad-facing orientation for 2-pin passives and small ICs.

## 0.29.7

Nothing has to change in existing projects or task files; routing runs on KiCad 8/9-formatted `.kicad_pcb` boards saved for the first time by KiCad 10 no longer fail `routing_guard` adoption or `kicad_reload_board` item matching on benign KiCad 10 format normalizations. Adds a shared `canonicalize_guard_node()` helper in `scripts/kicad_rules_geom.py` used by both `scripts/routing_guard.py` (`beyond_copper`) and `scripts/kicad_reload_board.py` (`board_items`) to ignore top-level `(net N "name")` lookup declarations and default `(embedded_fonts no)`, strip UI-only `(pcbplotparams ...)` inside `setup`, normalize `(net ID "name")` to `(net "name")`, and canonically sort `footprint`/`fp_text` children while still flagging pad net swaps, `embedded_fonts yes`, moved footprints, and `stackup` or non-empty `zone_defaults` edits.

## 0.29.6

Nothing has to change in existing projects or task files; routing tasks can now reference multi-pad same-number footprints (such as 4-pin tactile switches and 4-pad buzzers) in non-centered `Connection:` lines, and 4-layer autorouting automatically protects poured inner reference/power planes from signal trace routing. Adds two fixes: (1) `scripts/pad_exits.py` (`preflight_tasks`), `scripts/kicad_check_connection.py` (`named_pad_groups`, `connected`), and `scripts/board_context.py` (`_resolve_pad`) support repeated `REF.PAD` groups when `centered` is false and every pad shares the target net (requiring all pads in both groups to be electrically connected, resolving non-centered targets in routing context briefs, and keeping centered exits strict); and (2) `scripts/kicad_autoroute.py` (`_autoroute_layer_args`) inspects parsed non-keepout copper zones on >2-layer boards and emits `--layers`/`--layer-costs` with `-1` (forbidden) for poured inner planes unless exclusively routing that pour's net.

## 0.29.5

Nothing has to change in existing projects or task files; cards with `approval_owned: true` whose `docs` list includes a deliverable in `owns` (such as `decisions` reading `board/DECISIONS.md` or `constraints` reading `board/ELECTRICAL_AUDIT_CONTRACT.md`) can now edit that owned doc during an open attempt under `approval_mode: "enforce"` without invalidating `approval_contract`. Fixes `approval_inputs()` in `scripts/board_graph_model.py` (`_is_owned`) so owned paths are excluded from the starting-terms `review.readings(node)` hash just as they already were for `approval_files`, while still rejecting mid-attempt edits to non-owned docs and still recording post-edit owned hashes in `approval_v1` at pass time (`scripts/test_board_approvals.py`).

## 0.29.4

Nothing has to change in existing projects or task files; JLCPCB output finalization now groups same-LCSC parts with distinct functional labels (such as LED `PWR` and `RUN`) while still rejecting electrical-spec or color mismatches, handles 0-part boards without crashing on empty BOM/CPL tables, and includes populated `Cmts.User` (`User.Comments`) Gerber layers in fabrication export and verification. Adds three fixes: (1) `scripts/kicad_finalize_jlc_outputs.py` (`group_smt_bom_rows`, `is_functional_label`, `read_csv_with_fields`) joins distinct non-numeric functional labels sharing an `LCSC Part #` (e.g. `PWR / RUN`) while still raising on numeric rating or LED color conflicts, preserves CSV fieldnames for 0-part boards instead of indexing `source_bom[0]`/`source_cpl[0]`, and records `extra_fabrication_layers` and `grouped_functional_labels` in `assembly-manifest.json`; (2) `scripts/kicad_generate_fabrication_archive.py` (`board_extra_layers`, `fabrication_layers`, `fabrication_member_suffixes`) detects non-hidden elements on `Cmts.User`/`User.Comments` so boards with flex stiffener outlines or fabrication notes export `User.Comments` alongside copper/mask/silk/paste/`Edge.Cuts`; and (3) `scripts/kicad_finalize_jlc_outputs.py` (`validate_fabrication_archive`) and `scripts/kicad_package_production.py` (`verify_fabrication_derives_from_board`) require and byte-compare those same extra layers.

## 0.29.3

Nothing has to change in existing projects or task files; repositories whose `.gitignore` only excludes `logs/board-graph/` no longer fail clean-checkout checks because `scripts/mac_keep_pace.py` writes `logs/mac-keep-pace/` on the Mac. Adds a two-layer fix: (1) `scripts/run_board_checks.py` (`standing`, `_untracked_path`) inspects `git status --porcelain --untracked-files=all` and ignores only untracked paths strictly under `logs/mac-keep-pace/` while still refusing tracked changes or any other untracked files; and (2) `scripts/mac_keep_pace.py` (`ensure_excluded`, `write`, `main`) idempotently adds `logs/mac-keep-pace/` to the repo's local `.git/info/exclude` without altering `.gitignore`.

## 0.29.2

Nothing has to change in existing projects or task files; running `scripts/editor_step.py` directly on the Mac no longer crashes at startup with `AttributeError: module 'editor_step_audit' has no attribute 'subprocess'`, standalone imports of `editor_step_audit.py` work without `editor_step` preloaded, and `main()` always closes the PCB Editor if startup checks fail. Fixes three things: (1) `scripts/editor_step.py` registers `sys.modules.setdefault("editor_step", sys.modules[__name__])` before submodule imports (matching `board_graph.py`) and wraps post-`open_editor` startup checks and step execution in `try ... finally: close_editor(proc)`; (2) `scripts/editor_step_audit.py`, `scripts/editor_step_kicad.py`, and `scripts/editor_step_prompts.py` resolve `__main__` when its script basename is `editor_step.py` in `_facade()`; and (3) `scripts/editor_step_audit.py` imports `subprocess` and `editor_step_kicad` directly for standalone `design_check()` and `decide()` calls.

## 0.29.1

Nothing has to change in existing projects or task files; routing steps that hit a timeout or deadline now preserve DRC-clean copper as adoptable candidates instead of discarding valid progress, strictly improving routing rounds can earn multiple deadline extensions, and KiCad IPC route connections use a 60-second timeout matching the editor session adapter. Adds three fixes: (1) `scripts/editor_step.py` (`_preserve_timeout_editor_state`, `_finalize_step_result`) unlocks and saves the open PCB Editor on operator/card timeouts when no file-tamper issue fired, runs `design_check`, and archives `candidate-round<N>.zip` whenever DRC errors do not increase and either open connections drop or errors stay zero, while also archiving DRC-clean candidates on `review_failed`, `card_deadline`, and `missing_visual_look`; (2) `scripts/editor_step.py` (`_maybe_grant_extension`) tracks `best_open` across rounds and grants up to `MAX_PROGRESS_EXTENSIONS = 3` extensions while every round strictly closes more connections with zero new errors, audit findings, or pad-exit issues; and (3) `scripts/kicad_route.py` (`KipyBoard`) raises its `KiCad(...)` `timeout_ms` from `5000` to `60000`.

## 0.29.0

Nothing has to change in existing projects or task files; shared rules, 2D pad/polygon geometry, and cached KiCad IPC adapters are unified into clean modules, oversized orchestration scripts (`board_graph.py`, `editor_step.py`, `kicad_lay.py`, `routing_run.py`) are decomposed while preserving all CLI entry points and receipt schemas, a dense 4-layer integration suite guards O(1) IPC calls and KiCad 10 compatibility, and `graph_pack` adds repo-local `"pack": "none"` extensions (`actions`, `upkeep`, `APPROVAL_SOURCES`). Summary of changes: (1) `scripts/kicad_rules_geom.py` consolidates `.kicad_pro` netclasses, `.kicad_dru` custom rules, oriented pad/polygon/clearance math, and S-expression copper/zone helpers (`board_context.py`, `kicad_lay.py`, `kicad_route.py`, `pad_exits.py`, `kicad_autoroute.py`, `routing_run.py`, `kicad_reload_board.py`), with zero `kipy` imports; (2) `scripts/kicad_ipc.py` and `scripts/kicad_lay_plan.py` unify session-level IPC caches (`KipyBoardCacheMixin`) and checked batch planning/readback/unconnected rollback (`kicad_lay.execute_batch`); (3) `editor_step.py` (`editor_step_prompts.py`, `editor_step_kicad.py`, `editor_step_audit.py`), `routing_run.py` (`routing_guard.py`, `routing_report.py`), and `board_graph.py` (`board_graph_git.py`, `board_graph_model.py`, `board_graph_state.py`, `board_graph_workspace.py`) split into focused modules with all existing CLI flags, monkeypatch seams, `move-text` removal, non-`isolated_copper` warning checks, and `routing_codex.py` no-op look preservation intact; (4) `scripts/test_dense_board_integration.py` and frozen receipts (`scripts/fixtures/receipts/run-v026.jsonl` through `run-v0281.jsonl`) verify 66-footprint/340-track/44-via/filled-zone scaling, O(1) IPC RPC counts, `board_context` active-box quotas, and KiCad 10 DRC validity; and (5) `.github/scripts/check_hygiene.py` enforces duplicate-parser and function-length guardrails in CI while `scripts/graph_pack.py` supports repo-local graph workflows (`dev/` remains excluded from the distributed starter template).

## 0.28.1

Nothing has to change in existing projects or task files; moved route-quality warnings are caught with exact coordinates for G3 reviewer acceptance, route-quality cleanup steps that reduce findings on 0-open/0-DRC boards pass progress checks, truncated `;`-chained commands and no-op `kicad_lay.py` refusals no longer trigger false `missing_visual_look` failures (which also get a 90s look-only finishing grace), and multi-route `kicad_lay.py` IPC lookups run in one cached pass. Fixes five bugs found in `v0.28.0` runs: (1) `scripts/editor_step.py` (`audits`, `finding_details`) and `scripts/routing_evidence.py` (`adoption_problem`) keep full `route_quality_warning_detail()` coordinates in audit findings and accept both exact and legacy short-label matches; (2) `scripts/editor_step.py` (`check_passes`, `decide`) adds `audits_reduced` so cleanup steps that strictly reduce `audits()` findings on 0-open/0-DRC boards pass; (3) `scripts/routing_codex.py` (`_edit_verb`, `audit_commands`) ignores `agy` 500-char `...`/`\u2026` truncation tokens and only resets `looked_after_edit` on `kicad_lay.py` when copper actually changed; (4) `scripts/editor_step.py` (`step_rounds`) grants a 90s look-only finishing extension with a compact look-only retry prompt when `missing_visual_look` hits near the handoff clock; and (5) `scripts/kicad_lay.py` (`KipyLayBoard._all_footprints`, `pad`, `pad_obstacles`, `enabled_layers`, `netclass_defaults`) caches footprints, enabled layers, and bulk `get_netclass_for_nets()` results across a batch so `obstacle_collision` refusals run sub-second.

## 0.28.0

Nothing has to change in existing projects or task files; routing briefs show every via and track inside the current task's routing box without hiding obstacles behind unsorted distant copper, `kicad_lay.py` catches track/via/pad crossings and clearance violations in pure Python before KiCad IPC commits or DRC zone refills, and passive telemetry groups DRC net-pair/clearance repeats alongside extra tool breakdowns. Adds three improvements: (1) `scripts/board_context.py` (`render_board_context`) scopes `Existing copper` to the task bounding box plus an 8 mm margin, puts `Vias` first and groups remaining active-box copper by copper layer (`F.Cu`, `B.Cu`, inner layers) sorted by `(x, y)` so no active-box net falls into `Other nets (summarized)`, while summarizing only distant items outside the active box; (2) `scripts/kicad_lay.py` (`_check_planned_obstacles`, `KipyLayBoard.pad_obstacles`, `KipyLayBoard.min_clearance_floor_mm`, `_dru_min_clearance_mm`) runs a fast 2D geometry and netclass clearance pre-check (honoring any smaller `.kicad_dru` custom clearance rule so local courtyard neckdowns never false-positive) across planned segments/vias vs existing items, oriented pads (`rect`, `roundrect`, `oval`, `circle`), and intra-batch copper, raising `obstacle_collision` naming the exact conflicting obstacle before `create_copper()`; and (3) `scripts/routing_codex.py` (`normalize_refusal_reason`, `other_command_head`, `audit_commands`, `summarize_extra_tools`), `scripts/editor_step.py` (`calls`, `run_totals`), and `scripts/routing_run.py` (`format_profile`) normalize `(nets X and Y)`, `(clearance ...; actual ...)`, and `obstacle_collision` literals for `repeated_across_steps`, record `other_by_cmd` command heads alongside `edit_by_verb`, and append passive `Extra tool calls:` lines to `format_profile()` when non-empty.

## 0.27.0

Nothing has to change in existing projects or task files; 4-layer IPC trunk-and-branch routing adds `to_point` T-taps and same-column `centered_exits` waypoint resolution, empty KiCad 10 `(zone_defaults)` setup metadata no longer blocks `--adopt-from`, all-SMD `Pour:` tasks tolerate `isolated_copper` DRC warnings without failing progress checks, and passive refusal telemetry groups cross-step repeats accurately. Fixes five issues found in `v0.26.1` routing receipts: (1) `scripts/routing_run.py` (`beyond_copper`) and `scripts/kicad_reload_board.py` (`board_items`) strip empty `["zone_defaults"]` children inside `(setup ...)` while still flagging non-empty `zone_defaults` settings, unblocking `--adopt-from` on KiCad 10 boards; (2) `scripts/routing_codex.py` (`_tool_status_and_reason`, `normalize_refusal_reason`, `summarize_refusals`), `scripts/editor_step.py` (`run_totals`), and `scripts/routing_run.py` (`_collect_profile`) raise the reason clip from 120 to 220 chars and group normalized refusal classes in `repeated_across_steps` while preserving verbatim `by_reason` entries; (3) `scripts/kicad_lay.py` (`_plan_route_geometry`) falls back `facing_escape` and `verify_centered_exits` to the adjacent `steps` waypoint when stacked same-column pads hit `neither short edge faces` (or fails actionably when `steps` is empty), and `scripts/editor_step.py` (`_ipc_cheat_sheet`, `mode_guidance`, `step_rounds`) omits `"centered_exits":true` from the default cheat-sheet example unless `Pad exits: centered` is active; (4) `scripts/kicad_lay.py` (`plan_route`, `_point_hits_copper`, `execute_batch`) adds `"to_point": [x, y]` T-taps onto existing or same-batch same-net tracks and vias (rejecting missing/wrong-layer/wrong-net targets fail-fast with `no_target_copper` before commit and guiding coordinates in `"from"` toward `"to_point"`); and (5) `scripts/editor_step.py` (`design_check`, `check_passes`) tracks KiCad `isolated_copper` warnings separately so `pours_changed=True` steps pass when only `isolated_copper` warnings increase and `open`, `errors`, and non-isolated findings stay clean.

## 0.26.1

Nothing has to change in existing projects or task files; timed-out or mid-step stopped Gemini routing invocations still report accumulated `thinking_tokens` and wall seconds in `timing` profiles. In `scripts/routing_gemini.py`, `session_timing()` tracks timing per invocation and falls back to deduplicated intermediate `step_update` usage and duration fields when an invocation ends before emitting a terminal `result` event, while completed invocations continue reading terminal `result` totals without double-counting.

## 0.26.0

Nothing has to change in existing projects or task files; 4-layer IPC routing tasks can connect pads directly into filled same-net copper planes with `to_via` stubs, routing receipts report passive timing and tool-friction profiles without any new gates or extra log files, and `tidy` cleans finished Mac step folders older than 7 days. Adds three improvements: (1) `scripts/kicad_lay.py` adds pad-to-via pour stubs (`"to_via": [x, y]` with optional `"pour_layer"`), refills zones, verifies via net read-back, checks `pad -> via` connectivity and point-in-filled-polygon membership (`not_in_pour` rollback on misses or cutout holes), validates JSON/route/via/pour schemas fail-fast before touching KiCad IPC (with a clear `"to_via"` hint when coordinates are passed in `"to"`), while `scripts/editor_step.py` (`_ipc_cheat_sheet`) gives a concrete `batch --stdin` example covering both `"to"` and `"to_via"`; (2) `scripts/routing_codex.py`, `scripts/routing_gemini.py`, `scripts/editor_step.py`, and `scripts/routing_run.py` record strictly passive in-place step and run profiles (`timing` wall split, `thinking_tokens`, `help_calls`, `refusal_seconds`, `refusal_reasons`, and `repeated_across_steps`) inside existing `run-<attempt>.jsonl` receipts, live status lines, `format_profile()`, and delivery `note()` with zero gates or thresholds; and (3) `scripts/tidy.py` adds `mac_step_runs_plan()` to list and (`--apply`) remove finished, inactive Mac `SITE.steps/run-*` folders at least 7 days old while preserving active, locked, open-in-KiCad, or unfinished runs.

## 0.25.1

Nothing has to change in existing projects or task files; Gemini reviews on poured multi-layer boards receive compact changed-copper coordinates and tolerate blocked-by-policy tool attempts when `agy` returns zero data, while `Mode: ipc` operators can use read-only viewport commands without triggering false `mode_violation` rollbacks. Fixes three edge cases across review execution and `ipc` inspection: (1) `scripts/editor_step.py` adds `changed_copper_details()` (per-net added/removed segment and via geometry without `(filled_polygon ...)` bulk) to the review table, states explicitly in Gemini review prompts that `run_command`/shell/search/edit/web/MCP tools and `AGENTS.md` are disallowed, and replaces GUI editing-dialog hints in `Mode: ipc` prompts with read-only viewport inspection guidance; (2) `scripts/routing_gemini.py` tracks review tool calls by `step_index` and lets `agy` `TOOL_ERROR` permission-denied or file-not-found attempts with empty output recover cleanly when a valid review verdict completes, while keeping executed errors, `DONE` violations, orphaned `ACTIVE` violations, and operator violations fail-closed; and (3) `scripts/routing_codex.py` (`audit_commands`) treats `mac.py menu <app> View <zoom/layer/redraw>` and read-only view keys (`Home`, `pageup`, `pagedown`, `F1`-`F4`, `escape`, `+`, `-`) as inspection (`mac`) rather than `gui_copper_calls`.

## 0.25.0

Nothing has to change in existing projects or non-pour tasks; routing tasks with `Pour: LAYERS NET` headers now inspect, lay, refill, visually review (including inner copper layers), verify, and adopt copper planes while preserving rule areas and non-copper zones. Adds end-to-end copper-pour support: (1) `scripts/kicad_lay.py` adds `zones` (read-only outline, enabled copper layers, and existing zone inspection), `pour` (`--board-outline`, `--rect`, `--polygon`, `--inset`, `--holes`, thermal spoke width/gap, `--replace`), `delete-zones` (unlocked copper zones only, refusing rule areas and locked zones), and `batch` `pours` + standalone stitching `vias` with read-back net verification, empty-fill checks, DRC checks, and atomic rollback; (2) `scripts/routing_evidence.py`, `scripts/pad_exits.py`, `scripts/editor_step.py`, and `scripts/routing_codex.py` parse `Pour: LAYERS NET` lines, verify enabled layers and nets in preflight, allow pour-only board changes when `open` and `errors` do not increase, capture requested inner copper layers for independent review, retry unfilled pours via `pour_missing`, verify persisted fills with `saved_pours`, and re-verify prior pours on the final PCB via `final_pours`; (3) `scripts/board_context.py` extracts non-rectangular stitched `Edge.Cuts` outlines and summarizes existing copper pours and rule areas; and (4) `scripts/routing_run.py` authorizes `.Cu` copper pours in `guard(..., allow_pours=True)` only when tasks declare `Pour:`, while still rejecting modified or removed rule areas (`keepout`) and non-copper zones.

## 0.24.3

Nothing has to change in existing projects or task files; quick `board-graph where` and `view.json` snapshots stop showing stale per-board cached Mac `behind`/`ahead` counts after `tidy` or `keep-pace` has synced the Mac. Fixes stale Mac status caching in `scripts/board_graph.py`: (1) `serve()` and `open_state()` attach the Mac checkout's live `logs/mac-keep-pace/status.json` to existing state reads with zero extra SSH or git fetch calls, so `carried_workspace()` and `render_workspace()` immediately show current Mac `head`, `behind`/`ahead`, and keep-pace status across all boards; and (2) when no newer `status.json` is present, quick `where` omits old cached Mac counts after a newer clean `upkeep` or once a cached `full_read` is at least 1 hour old (`STALE_MAC_READ_SECONDS = 3600`).

## 0.24.2

Nothing has to change in existing projects or task files; broken runs rejected solely by the 450-to-90 angle normalization bug can be adopted cleanly with `--adopt-from <prior-attempt>`. Fixes false `footprint changed` rejections on KiCad angle normalization and adds guarded run recovery: (1) `scripts/kicad_sexp.py` (`normalize_angles`) reduces `(at x y [angle] ...)` rotations modulo 360 (treating `0`, `360`, and omitted angles identically), applied in both `scripts/routing_run.py` (`beyond_copper`) and `scripts/kicad_reload_board.py` (`board_items`); and (2) `scripts/routing_run.py` (`--adopt-from`, registered in `scripts/board_graph_cli.py`) opens a fresh attempt on the Routing card only after re-verifying that the prior Mac run finished cleanly, matches branch/task/ancestor state, passes every `routing_evidence` hash, cold-review, pad-exit, and `guard()` check with zero banned models, and records a standard provenance-valid adoption commit without re-launching KiCad.

## 0.24.1

Nothing has to change in existing projects or task files; teams calibrating defaults on tougher boards or dense steps can still override `--model` and `--review-model` per run or board. Updates starting Gemini defaults and prevents via-on-pad-edge false aborts: (1) `scripts/routing_gemini.py` and `scripts/editor_step.py` set starting Gemini defaults to `gemini-4-neon-medium` (operator) and `gemini-4-neon-low` (independent review), matching initial multi-wire benchmarks while remaining overridable; and (2) `scripts/pad_exits.py` reports a via or curved track touching a pad bounding box as a retryable `pad_exit_failed` issue rather than raising an unhandled `ValueError` (`step_failed`), while `scripts/kicad_lay.py` (`verify_centered_exits`) rejects planned vias touching pad bounding boxes atomically when `centered_exits: true`.

## 0.24.0

Nothing has to change in existing projects or task files (`--autoroute-cleanup` is now opt-in, so default `Mode: autoroute` runs stop fast and capture previews without spending operator LLM tokens unless `--autoroute-cleanup` is passed). Fixes routing run reliability, observability, timer behavior, and telemetry: (1) `scripts/routing_run.py` records the wrapper PID inside `EDITOR_BUSY.d/routing-run-<attempt>`, automatically reclaims stale dead-PID locks instead of stalling behind a killed run, and streams `BOARD_GRAPH_STATUS_FILE` live progress (`detail`) plus 60-second heartbeats during `wait()`; (2) `scripts/routing_gemini.py` tails `agy` JSONL stdout live line-by-line into `transcript.jsonl` and live status; (3) `scripts/editor_step.py` adds a one-time finishing extension when the handoff clock hits mid-step with verified clean progress, archives clean partial `connection_open` or skipped dirty `autoroute` candidates before editor rollback, and makes `autoroute` LLM cleanup opt-in via `--autoroute-cleanup`; and (4) `scripts/routing_codex.py` excludes `"done"` (`kicad_lay.py pads`) from `lay_refused` and filters chained `kicad_edit.py` JSON statuses when auditing `kicad_lay.py`.

## 0.23.2

Nothing has to change in existing projects or task files. Improves `kicad_autoroute.py` `.kicad_dru` rule compliance and large-board convergence: (1) `_strip_kicad_dru_hints()` now extracts `.kicad_dru` global/per-net `clearance` constraints, `--routing-clearance-margin 0.005`, `--net-clearances`, wildcard `A.NetName == ...` patterns, and `intersectsCourtyard('REF')` patterns; (2) adds DRC-gated `widen_non_courtyard_segments()` post-snap promotion for tracks outside allowed courtyard neck zones plus zero-length neckdown taper (`--neckdown-taper-length 0`); and (3) restricts the 2-layer cost bias to true 2-layer unrouted PCBs and bounds wildcard/large runs (`--max-iterations 30000 --max-probe-iterations 2000 --max-ripup 2`).

## 0.23.1

Nothing has to change in existing projects or task files. Fixes `find_krt()` in `scripts/kicad_autoroute.py` to keep the unresolved `.venv/bin/python` path (`os.path.abspath(py)`) instead of calling `py.resolve()`, so macOS and Linux virtualenvs retain `pyvenv.cfg` and see their installed solver dependencies (`numpy`, `scipy`, `shapely`, `ortools`).

## 0.23.0

Nothing has to change in existing projects or task files; to enable `Mode: autoroute` on the Mac, clone and build stock `KiCadRoutingTools` once into `~/tools/KiCadRoutingTools` (or configure `kicad_machine.autorouter` in `board_graph_site.json` / `BOARD_GRAPH_AUTOROUTER`). Adds headless `KiCadRoutingTools` autorouting with verbatim target-net merging and deterministic cleanup: (1) `scripts/kicad_autoroute.py` runs `KiCadRoutingTools` offline on a disposable copy, preserves non-copper and untouched nets verbatim (passing `routing_run.guard`), runs DRC-gated post-snap passes (via-endpoint centering, non-45 expansion, overlapping collinear cleanup, H/V chamfer), and evaluates DRC + `kicad_route_quality.py` + `Pad exits: centered` before touching the editor; (2) `Mode: autoroute` in `editor_step.py`, `routing_evidence.py`, and `routing_run.py` loads the candidate once into the persistent open KiCad editor, skips the operator LLM cleanly with zero agent tokens when all checks pass (unless sent back by cold review), or hands only `residual_findings` to surgical LLM cleanup (`--no-autoroute-cleanup` disables cleanup) and records solver provenance (`autoroute`, `autoroute_steps`, `note()`); and (3) updates `AGENTS.md`, `tools/board_graph.json`, `docs/BOARD_EVIDENCE.md`, `docs/ROUTING_NOTES.md`, and `docs/BOARD_PIPELINE_CONTRACT.md`.

## 0.22.1

Nothing has to change in existing projects or task files; projects with a board-wide routing strategy can optionally set `"plan": "<relative-path.md>"` inside `routing_agent` in `board.json`. Removes `kicad_lay.py` startup rediscovery overhead and the `write_file` batch trap: (1) `editor_step.py` (`mode_guidance`) and `kicad_lay.py batch --help` now prescribe `--json '...'` or `--stdin <<'EOF'` and explicitly forbid calling `write_file` or creating scratch files; (2) `scripts/board_context.py` renders an offline world-coordinate, courtyard bbox, facing-escape, `.kicad_pro`/`.kicad_dru`, and existing-copper brief into `board-context.txt` and `prompt.md`; (3) `editor_step.py` inlines the `kicad_lay.py batch` JSON schema, `kicad_route_quality.py`/`pad_exits.py` tolerances, and tells operators KiCad is already verified idle at startup (skipping startup `kicad_ipc_status.py`); and (4) optional `routing_agent.plan` is shared with both operators and independent reviewers (including Gemini's read allowlist).

## 0.22.0

Nothing has to change in existing projects or task files. Introduces three locked routing modes via an optional `Mode: ipc|hybrid|interactive` task header (`hybrid` by default when omitted): (1) `scripts/kicad_lay.py` (`pads`, `route`, `batch`) lays checked 45-degree atomic IPC copper chunks with pad-center snapping, pre-commit `Pad exits: centered` verification, live route-quality checks, electrical connectivity verification, and `kicad-cli` DRC diffing with automatic rollback on any failure; (2) `Mode: ipc` restricts operators to `kicad_lay.py` + `kicad_edit.py` + `mac.py look`, `Mode: interactive` keeps Push-and-Shove (`kicad_route.py`) only, and `Mode: hybrid` permits both (`mode_violation` rejects off-mode copper tools, while `wrapper_calls` ad-hoc scripts stay forbidden across all modes); and (3) updates the routing planning rubric and starter graph (`tools/board_graph.json`, `AGENTS.md`, `docs/BOARD_EVIDENCE.md`, `docs/ROUTING_NOTES.md`, `docs/BOARD_PIPELINE_CONTRACT.md`).

## 0.21.0

Nothing has to change in existing projects or single-`Connection:` task files. A single routing task file can now list multiple consecutive `Connection: FROM_REF.PAD TO_REF.PAD NET` lines at the top (before optional `Pad exits: centered` and task prose). `scripts/pad_exits.py`, `scripts/editor_step.py`, `scripts/routing_evidence.py`, and `scripts/routing_run.py` preflight every listed connection, verify every connection starts open and closes cleanly with zero centered-exit issues, deduplicate shared pad close-ups before one cold independent review, preserve `final_connections` across steps, and support both single `connection` and multi `connections` receipts while keeping `--minutes` active per step.

## 0.20.3

Nothing has to change in projects. `scripts/kicad_route.py` keeps `.fast` macOS Vision OCR for the status bar (`read_status()`) and strip so small standalone tokens like `mm` and `X` are not dropped, while using `.accurate` (`ocr-accurate`) for the message panel (`read_panel()`) when reading net names on fine-pitch pads.

## 0.20.2

Nothing has to change in projects. Fixes from the first live external-mode Gemini test run: (1) `editor_step.py` / `routing_gemini.py` allowlist the saved board and `.kicad_dru` for read-only Gemini reviews and instruct Gemini operators to run host-IPC/Screen Recording commands with `BypassSandbox: true`; (2) `tools/kicad/render_board_views.py` resolves board names/folders to `.kicad_pcb` and invokes `mac.tool_script("tools/kicad/render_board_views.py")` with `BOARD_GRAPH_PROJECT="$PWD"` on the Mac; and (3) `scripts/kicad_route.py` uses `.accurate` Vision OCR on macOS.

## 0.20.1

Nothing has to change in vendored projects. `scripts/board_graph_cli.py` now accepts `--board <name>` both before and after commands (`board-graph --board <name> route ...`, `board-graph route --board <name> ...`, `board-graph where --board <name>`, `validate`, `run-checks`, `view`, `board-description`) and adds CLI subcommands `render-schematic`, `kicad-cli`, `kicad-preflight`, `site-settings`, and `board-description`.

## 0.20.0

Nothing has to change in existing vendored projects — they continue working identically (`TOOL_ROOT == PROJECT_ROOT`) until you decide to switch. `scripts/tool_paths.py` and `scripts/board_graph_cli.py` (installed via `pip install --no-build-isolation -e .` on Windows and Mac) let one shared `board-graph` checkout drive any un-vendored project via `board-graph ...` or `BOARD_GRAPH_PROJECT` / `--project DIR`. Adding `kicad_machine.tool_checkout` (e.g. `"~/board-graph"`) to `tools/board_graph_site.json` configures `mac.py`, `board_graph_serve.py`, and `routing_run.py` to sync the Mac's shared `board-graph` clone to desktop's exact git commit SHA automatically. Run `python3 scripts/board_graph_tool.py unvendor --mac-tool-checkout ~/board-graph` inside a project whenever you are ready to drop its vendored tool copy.

## 0.19.0

Install on both routing machines. `scripts/routing_gemini.py` adds a `gemini` routing adapter backed by the Antigravity CLI (`agy`, signed in on the macOS KiCad host at `~/.local/bin/agy` or on `PATH`), and `--review-provider` / `routing_agent.review_provider` decouple the independent reviewer provider from the routing operator provider so cross-model routing and review (such as Codex routing with Gemini reviewing, or vice versa) are enforced across `editor_step.py`, `routing_run.py`, and `routing_evidence.py`. Existing projects without `review_provider` keep using `provider` for both roles with no configuration change required.

## 0.18.7

Nothing has to change in project files. `scripts/pad_exits.py` now supports rounded-rectangle (`PAD_SHAPE_ROUNDRECT`) and oval (`PAD_SHAPE_OVAL`) SMD pads in addition to sharp rectangles, measuring crossings, inside-pad segments, and close-up PNGs against the pad's real rounded perimeter. `scripts/routing_run.py` now validates every task file in `TASKS` against the saved `.kicad_pcb` locally before starting an attempt or touching the Mac, and adds `--preflight` to run that check standalone in seconds.

## 0.18.6

Nothing has to change in project files. `scripts/kicad_route_quality.py` lowers `DEFAULT_MIN_SEGMENT_MM` from `0.25` to `0.15` mm for fine-pitch fanout and ignores `micro_segment` warnings on segments buried inside overlapping same-net copper (pads, vias, or wider/full-length tracks) or collinear straight tracks split by a T-junction. `scripts/validate_board.py` (G3), `scripts/kicad_package_production.py`, and `scripts/board_graph_actions.py` now accept a remaining route-quality warning when its exact geometry matches a verified `FINE AS IS` verdict in an adopted `run-*.jsonl` routing receipt for that board, while still failing on errors, unreviewed warnings, or modified geometry. Projects that want the updated starter prose may merge the G3 row from `board/ORDER_PACKAGE_CONTRACT.md`.

## 0.18.5

Nothing has to change in project files. `scripts/kicad_route.py` adds `--width-at X,Y,MM` for mid-route width changes (such as courtyard neck-downs) while preserving both widths through `true_up`, and `scripts/routing_codex.py` + `scripts/editor_step.py` reject ad-hoc Python KiCad wrappers (`unauthorized_wrapper`) and require `mac.py look` to capture the assigned board's PCB Editor window.

## 0.18.4

Nothing has to change in project files. `scripts/kicad_route.py` now keeps connected warning-only routes (still undoing hard route-quality errors) so `scripts/editor_step.py` can submit warning-only candidates to the independent reviewer, and `scripts/routing_codex.py` requires `scripts/mac.py look` to exit 0 and write a PCB Editor PNG before counting visual inspection.

## 0.18.3

Nothing has to change in project files. `scripts/board_review_inputs.py` and `scripts/test_editor_step.py` now resolve project and temp roots before path comparisons on macOS (`/var` -> `/private/var`), so 3D model hashing and all Mac runner tests pass cleanly.

## 0.18.2

Nothing has to change in project files. `scripts/routing_run.py` now resolves `ROOT` before `relative_to` so macOS `/var` -> `/private/var` temp paths do not fail resumed routing deliveries or tests on a Mac runner.

## 0.18.1

Install on both routing machines. The Codex routing operator now receives the starting board views, has `view_image` enabled, and must run `scripts/mac.py look` after its last edit before independent review. Shell reads of script files are no longer counted as `kicad_route.py` invocations.

## 0.18.0

Install this candidate on both routing machines together. Existing project graphs keep their own settings. Try a disposable copy in observation mode using [the approval guide](docs/BOARD_APPROVALS.md). Review each board's dependencies, owner decisions, assembly inputs and permitted returns before switching it to enforcement. New copies of the starter use enforcement.

New approvals bind the contents of cited evidence and required reading. Retain the files a reviewer saw. Old receipts with incomplete evidence can require a fresh review. The route delivery checks the original assignment, reviewer response and final PCB together. An interrupted delivery can resume from its private worktree using the original task command and attempt ID. To release a refused pending delivery, use that command with `--retain-only --attempt ID`. The printed Git ref preserves the candidate locally.

## 0.17.1

Install this version on both Routing machines. To request centered exits on
a connection task, add `Pad exits: centered` below its `Connection:` line.
See `docs/BOARD_EVIDENCE.md` for supported pads and the measured limits.
Resuming an older run whose last kept step lacks a review hash saves the
downloaded board for inspection and records `needs_lead`.
A newly joined connection with only route-quality warnings now goes to the
independent reviewer in the same run. The receipt records each decision and
the reviewed PCB hash. Install on both machines before starting the trial.

## 0.17.0

Approval comparison is optional. Existing graphs start in off mode. Install the same tool on the desktop and the KiCad host. Use `board_graph.py approvals` on a copy with records, then follow `docs/BOARD_APPROVALS.md` to observe or enforce on one board. Existing records keep their original outcomes. Merge the starter approval fields into the project graph for the checks you adopt and use the project’s own paths. For Placement, keep the renderer's board-render.json and the images shown at sign-off. For Orientation preview and Order, adopt the package source and dependency links together. Each preview identifies its sealed archive. Routing and Visual review can now compare copper and silkscreen separately. Re-render Placement with the version 2 model hashes before reusing that approval. Routing includes the selected board’s custom rules and routing-relevant project settings.

## 0.16.0

Existing project records and positions carry forward. Start new Routing
attempts under the installed version. Close a legacy active attempt with
its actual status before starting another. Both machines need the same
tool manifest. Routing candidates with only quality warnings now come
back as a ZIP beside the run receipt for lead disposition.

At Verification and Product judgment use the receipt examples in
`docs/BOARD_EVIDENCE.md`. The final Package pass rechecks the
sealed files. Older placement fingerprints can require one fresh approval.
To adopt the starter's new order in project-owned cards, assign quote-file
preparation before Quote, then final generation to the Package agent and
orientation preview before Order. Preserve the project's own work and
existing positions. Bring in `docs/BOARD_REVIEW.md` and
`docs/SCHEMATIC_REVIEW.md` and point card reading and provider wrappers
at the shared rubrics.

In each board description, set `routing_agent.tasks` to the committed batch
folder. Set `provider`, `model`, `review_model` and optionally `effort` in that
object. In the project's Routing card, direct the operator to use the Tool
command in the started brief. It includes the selected attempt and board.
Begin each pad-pair task with `Connection: FROM_REF.PAD TO_REF.PAD NET`.
Begin a geometry repair with `Repair:`. Existing unconfigured free-text
batches retain their original board-wide checks and need an explicit provider
when launched. Update both project checkouts from the same release.

## 0.15.1

Update the tool on the KiCad Mac and in the checkout that starts routing.
The Codex editor controls now recognize the `[Read Only]` title KiCad shows
after an edit while the graph holds the file lock.

## 0.15.0

Projects can select Codex for a locked routing run with `--provider codex`.
Install the version in the KiCad machine's main checkout as well as the
checkout that starts the run. The Mac needs a signed-in Codex CLI. The
operator and reviewer have separate sessions. Existing Claude runs keep
their settings.

## 0.14.0

Copper now reaches a board one way only, and the graph holds it. Three
changes in the project's own files, then one thing to know.

- **The Routing batch card's task** (`tools/board_graph.json`, and each
  other board's cards file). Replace "Route with the selected method, refill
  zones and save." with: "That run is the only way copper is laid: never
  write tracks, vias or pours with a script or through KiCad's programming
  interface, never use another router, and never run the route tool by hand
  outside the run. When the run cannot lay a connection, record needs_lead
  with what the route tool said and stop; the card refuses pass and more_work
  while the board holds copper that no run committed. Refill zones and save."
- **`docs/ROUTING_NOTES.md`.** Replace the section "Four choices", which
  listed other routers and agent-written geometry as alternatives, with this
  version's section "The one way copper is laid"
  (`git -C <checkout of this repository> show v0.14.0:docs/ROUTING_NOTES.md`).
- **`AGENTS.md`.** Add this version's three rules under the board workflow:
  "A card that does not work", "Copper" and "Opus is banned from routing".

What is new in the tool:

- **The Routing batch card's gate.** `record` refuses `pass` and `more_work`
  on the card, and the card no longer passes by itself, while the branch
  holds a track, arc, via or pour that no adopted routing run committed, or a
  change of copper layers. The refusal names each commit and what it added.
  `needs_lead`, `needs_owner` and `returned` are recorded as before. Only the
  owner's own record on the card (`--by owner`) accepts such copper, for the
  board as it stands then.
- **Copper origin in the board checks.** `validate_board.py --base` has the
  same check as a row, so a pull request with copper from outside the run
  fails its board checks. `py -3 scripts/board_copper_origin.py` prints it
  for the branch here. A branch that already holds such copper fails from
  this version on: that is the check working, and the owner decides whether
  to accept the copper or have it laid again.
- **Every brief** now says what to do when the card's script or tool fails:
  stop, record `needs_lead` with what it said, never do the work another way.
- **Opus models are banned from routing.** The routing run and the step
  runner refuse an Opus model as the agent or the reviewer before anything
  starts, and nothing is adopted from a run whose own logs show one did
  work. The step runner's agent was `claude-opus-5-5` unless told otherwise;
  it is now `claude-fable-5-2`. The Routing batch card's brief opens its
  rules with a warning that says this and names what agents did to avoid the
  editor. `record` cannot see which model types it, so there the ban is a
  rule, not a lock.
- **The routing run** makes the KiCad machine's Python environment
  (`scripts/setup_kicad_ipc.ps1`) when the main checkout there has none, and
  refuses before anything runs when it cannot, where it used to start and
  end at once with nothing run.

## 0.13.0

Nothing has to change. Two things are new.

- **Cards that wait on each other.** `board_graph.py check` now refuses a
  graph where cards wait on each other in a loop, so none could ever pass:
  through their order on a track, through `requires`, and through an open
  issue that names the card whose work closes it. An open issue may now say
  `"closed_by": "<card>"`; add it where one card's work is what closes the
  issue, so the check can see that wait. Only `check` reports a loop:
  `where`, `next` and `record` work as before.
- **A branch seen from main.** The page and `where --full` now say where
  each open pull request's branch stands by the records only it holds (the
  cards passed there and the card it stands at), marked as not merged.

## 0.12.1

Nothing to change. A board that carries no part (every footprint on it is
kept out of the BOM, as on a flexible strip whose only footprints are its
contact tabs) now passes the scripts of Inventory, BOM, Stock and Placement,
which stopped at it before: its exported BOM may hold no row, and it needs no
stock listings file and no owner's placement file. A board with a part on it
is checked as before.

## 0.12.0

Four board cards now run a script, so `next` no longer stops at them to be
done by hand. Each script reads a fixed place:

- **Schematic.** As the card starts, its script runs KiCad's ERC on the saved
  schematic. Nothing to move. On a machine without KiCad it says so as a
  finding and the card stays.
- **Stock.** The dated listings are read from `<reports>/stock/listings.json`:
  `planning_quantity` (the boards planned) and `factory_parts`, one entry per
  supplier part with `part`, `read_at`, `listed_manufacturer`, `listed_mpn`,
  `class`, `stock` and `unit_price_usd`, and `owner_plan` (the owner's words)
  where the stock is less than the planned boards need. A project that kept
  its listings elsewhere or under other names moves them there.
- **Verification.** The fresh check reports are read from
  `<reports>/verification/checks`, each stamped against the saved board
  (`scripts/kicad_report_freshness.py stamp`). A project that ran them into
  another folder runs them there.
- **Package.** The script makes nothing. It reads the package the packager
  sealed in the description's `production` folder and expects the one
  `<NAME>-complete-package.zip` and its `.sha256` in the description's
  `folder`. A project that wrote the complete package elsewhere writes it
  there.

The starter graph's wording for these four cards says the same. A project
that wants it copies each card's `task` from `tools/board_graph.json` at this
version; the scripts run either way.

Projects that are not a circuit board (the pack "none"):

- **Several graphs.** Each graph now keeps its own reports. The first graph
  keeps the `reports` folder; another graph's is `<reports>.<its name>`. Move
  a second graph's existing reports there, or write `{name}` in `reports`
  (such as `"reports": "reports/{name}"`) to choose the layout.
- **Optional.** `{name}` in a path a card owns or a doc it names, and in a
  path a project script reads, stands for the graph's name, so one cards file
  and one script serve every graph. A project script that takes a second
  parameter called `graph` is given the name.

Nothing else changes in a project's files: the page's fixes, the first board
outside `board/board.json`, and the hidden Linux paths need nothing.
