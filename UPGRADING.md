# What a project changes after taking a version

One section per public release, newest first: what a project does in its own
files after moving to a new release. Pin a release with
`pip install git+https://github.com/Arp-299-Org/board-graph.git@v<version>`
(nothing in a project changes until you choose to upgrade). Read every section
between your old version and the new one before upgrading to see whether any
project-owned files need updates. A release that asks nothing of a project
still has a section saying so.

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
