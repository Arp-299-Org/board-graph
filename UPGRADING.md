# What a project changes after taking a version

One section per public release, newest first: what a project does in its own
files after moving to a new release. Pin a release with
`pip install git+https://github.com/Arp-299-Org/board-graph.git@v<version>`
(nothing in a project changes until you choose to upgrade). Read every section
between your old version and the new one before upgrading to see whether any
project-owned files need updates. A release that asks nothing of a project
still has a section saying so.

## 0.42.2

Existing projects already initialized with `board-graph init` should run
`board-graph tool sync-agents` after upgrading to refresh the shared `AGENTS.md`
rules (preserving each project's `## Product` section). Engine changes:

- **`AGENTS.md` propagation (`board-graph tool sync-agents`):**
  Refreshes shared `board-graph` rules in initialized projects while keeping
  `## Product` intact, and runs automatically for the linked `board_project`
  when a `board-graph heal` cycle finishes `release`.
- **Mode-aware `BROKEN_CARD` & non-removable self-healing rules:**
  `scripts/board_autonomy.py` (`broken_card_note`) puts delegated-lead
  self-healing first in `Mode: AUTONOMOUS` briefs instead of supervised
  stop-first text, and `AGENTS.md` clarifies that explicit owner requests to edit
  `AGENTS.md` are direct instructions and 6-card self-healing is a core invariant.
- **Closed env-var gate bypass & `--mode heal` alignment:**
  Removed `BOARD_GRAPH_SKIP_HEAL_GATE` from `scripts/board_heal.py` (while
  allowing scratch unit tests targeting temp `state_path` + `records_path`),
  aligned `dev/tool_graph.json` + `dev/TOOL_DEV_PROCESS.md` for `--mode heal`,
  and added the `manifest --version` hint in `dev/tool_actions.py:run_release_gate`.
- **Resilient event-driven SSE watcher (`docker/watch_benchmark_events.py`):**
  Added 60s stream read timeout, exponential-backoff reconnection up to
  `--max-retries`, consecutive-error reset on connect, immediate `heal_blocked`
  (exit 12) detection, and explicit no-cron supervision rules in `AGENTS.md` and
  `README.md`.

## 0.42.1

Nothing has to change in existing projects already initialized with
`board-graph init` (pulling `AGENTS.md` and `tools/board_graph.json` brings in
updated autonomous lead-loop text). Engine changes:

- **IPC/interactive worker retry on obstacle/quality refusal:**
  `scripts/editor_step_prompts.py` now instructs routing workers in both `ipc`
  and `interactive` modes to read `obstacle_collision` / `route_quality` / DRC
  refusals, adjust waypoints, layer, or vias, and retry within their window
  before stopping for `needs_lead`.
- **Autonomous `needs_lead` delegated lead guidance:**
  `scripts/board_autonomy.py` (`wait_guidance`, `brief_note`),
  `scripts/board_graph_workspace.py`, `scripts/board_graph.py`,
  `scripts/board_heal.py`, `AGENTS.md`, and `tools/board_graph.json` now clarify
  that in `Mode: AUTONOMOUS` (`autonomy.mode == "delegated"`), the agent acts
  as the delegated lead to self-heal genuine tool bugs via `board-graph heal`
  and iterate routing retries rather than exiting.

## 0.42.0

Nothing has to change in existing projects already initialized with
`board-graph init` (though pulling `AGENTS.md` updates enables the new
`board-graph heal` rule text). Engine changes:

- **Enforced 6-card self-healing (`board-graph heal`):** When running from an
  editable `board-graph` git checkout, `board-graph next`, `record`, and
  `route` block advancing a PCB if the tool repo has uncommitted or unverified
  tool edits outside `dev/tool_dev.py` (`Triage -> Implement -> Tool Checks ->
  Cold Review -> Live KiCad Smoke -> Release`).
- **6-card Self-Heal strip on Hub (`board-graph hub`):** Displays active tool
  self-healing / release cycles directly on the Hub with clickable cards instead
  of raw git SHA badges.
- **Immediate `needs_lead` view refresh:** `scripts/routing_run.py` now attaches
  `board_graph.follow(...)` so routing `needs_lead` / `more_work` transitions
  immediately update `tools/board_graph_view/view.json` and the Hub.
- **Routing reviewer + 4-layer fixes:** Prevents false open-stub rejections on
  verified same-net pad-center connections, hides synced BOM fields on
  `F.Fab`/`B.Fab`, fixes DNP `LCSC Part #` and back-face `Flip()` reload
  crashes in `board-graph sync-pcb`, ignores QFN thermal vias inside
  same-numbered SMD pads in `manual_joints`, matches DRC warning severity in
  `kicad_lay_plan`, splits >2,000-line datasheet text files, and fixes off-page
  PDF render clipping in `render_board_views`.

## 0.41.2

Nothing has to change in existing projects already initialized with
`board-graph init`. New projects using `--fab jlcpcb-4layer` get 4-layer
baseline design rules and `board-graph sync-pcb` initializes new `.kicad_pcb`
files with the copper layer count configured in `.kicad_pro`.

## 0.33.1

Initial public release of `board-graph` (MIT license, KiCad 10, Python 3.9+).
Nothing has to change in existing projects already initialized with
`board-graph init`. Includes the full graph engine, KiCad 10 IPC and
interactive routing workflow, explicit delegated autonomous mode with
owner-locked `order` guardrails, single-machine and SSH KiCad host modes,
Linux `Xvfb` Docker smoke testing, and self-contained wheel packaging.
