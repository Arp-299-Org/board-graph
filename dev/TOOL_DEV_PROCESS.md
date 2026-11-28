# board-graph Tool Release Workflow (`dev/tool_graph.json`)

Every release of `board-graph` runs through this 6-card repo-local workflow under `dev/`.
This workflow stays out of `tools/board_graph.json` and `tools/board_graph_tool.json` so
downstream PCB repositories never receive it.

## Track: `tool` (ordered, `approval_mode: "enforce"`)

1. **`triage`** (`agent`): Read `docs/KICAD_10_INVARIANTS.md`, probe any affected KiCad/OS/CLI boundary directly, and record root-cause class, affected sibling modules and contracts, and required realistic integration tests before editing code.
2. **`implement`** (`agent`): Apply clean unified fixes sharing `kicad_rules_geom` and
   `kicad_ipc` adapters with realistic integration and contract tests.
3. **`tool_checks`** (`auto`, `approval_source: "tool_code"`): Runs `.github/scripts/check_hygiene.py`
   as a fail-fast preflight, then runs `.github/scripts/audit_workflow.py .`, `dev/test_tool_dev.py`,
   and all unit/integration `test_*.py` suites in parallel separate Python processes with live progress
   and timing telemetry (`manifest` and `notes` are verified at `release` via `release_gate`).
4. **`cold_review`** (`agent`, `approval_source: "tool_code"`, requires `tool_checks` current):
   Cold diff review rejecting band-aids, duplicated parsers/IPC adapters, N+1 IPC loops,
   toy-only fixtures, and documentation drift in `README.md`, `AGENTS.md`, `docs/ROUTING_NOTES.md`, or `UPGRADING.md`.
5. **`live_smoke`** (`agent` with connected script `live_smoke`, `approval_source: "tool_code"`,
   requires `tool_checks` current): First checks `pgrep -fl 'pcbnew|editor_step|routing_run'`
   on `BOARD_GRAPH_DEV_MAC_HOST` (or `BOARD_GRAPH_DEV_LINUX_HOST` / local Linux Xvfb) and
   refuses immediately if active work is running. When idle and IPC or routing files changed,
   runs a disposable KiCad 10 CLI + live IPC smoke in `/tmp`.
6. **`release`** (`agent`, `pass_check: "release_gate"`, requires current `tool_checks`,
   `cold_review`, `live_smoke`): In release mode, bump `pyproject.toml`, prepend
   `UPGRADING.md` section, run `scripts/board_graph_tool.py manifest --version <ver>`,
   commit, create annotated tag `v<ver>`, push `origin main v<ver>`. In `--mode heal`
   (`board-graph heal start <slug>`), verify clean commit ahead of `origin/main` and passing
   `board_graph_tool.py check` without requiring an upstream tag.

## Release & Self-Heal Cycling

Start a release cycle with `python dev/tool_dev.py cycle v0.42.0`, or start a self-heal cycle
from an editable checkout with `board-graph heal start <slug> --from-card <node>`.
Check status with `python dev/tool_dev.py where` or `board-graph heal status`.

## Friction and Slow-Check Signal

`run_tool_checks()` records `elapsed_s` and per-suite `timings`, and emits a warning when total
runtime exceeds 90s. When a tool check, card, or script triggers a slow-check warning or forces
redundant `needs_lead` retries due to workflow drag, log the file, line, and elapsed time as a tool
defect rather than silently powering through.
