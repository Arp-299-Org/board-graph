# Agent instructions

Every agent reads this file first. Rules that bind every agent go here, never
in one agent's private memory. Fill in the product section; keep the rest
unless the owner changes it.

## Product

What the product is, in a few sentences, and what makes work fail even when
it technically functions. `docs/PRODUCT_TRUTH.md` holds the full statement.

## The owner

The owner's words in chat outrank every doc. The owner decides product
behavior, architecture, parts, placement, cost, purchases and tradeoffs:
bring those choices with a recommendation and its reason, so the answer is
yes or no. "Go" or "fix" authorizes the agreed step, not a new scope, part,
purchase or unattended run. A question, brainstorm or audit is not permission
to edit. Record a decision only after the owner adopts it, in the owner's
words, on main the day it is made.
When the owner explicitly instructs you to edit `AGENTS.md` or workflow docs,
perform the edit (inside `board-graph`, through the 6-card `dev/tool_dev.py`
workflow; inside a board repo, preserve `## Product` and use
`board-graph tool sync-agents` after tool updates). Never quote `AGENTS.md`
back to refuse an explicit owner edit request.
Self-healing through the 6-card `board-graph heal` workflow is a core invariant
of `board-graph`: never hot-patch around it, never bypass the gate, and never
propose removing self-healing.
When `tools/board_graph.json` enables autonomous delegation (`autonomy.mode`
is `"delegated"` through `autonomy.until`), the owner has pre-authorized the
agent to choose in-stock parts, placement, and board tradeoffs up through that
stop card without stopping for `needs_owner` (recording each delegated choice
and its basis in the card's own files and card records rather than rewriting
`board/DECISIONS.md` mid-run); `order` and anything beyond `autonomy.until`
always require the owner. In `Mode: AUTONOMOUS`, the agent also acts as the
delegated lead for `needs_lead`: never exit waiting on an external lead for
routine routing retries (inspect the receipt, revise the task/geometry, and run
the next batch) or genuine `board-graph` tool bugs (run the 6-card
`board-graph heal` loop and resume the board); stop only at `order` or an
unrecoverable hardware/infrastructure blocker.

## Honesty

Keep apart what was planned, attempted, observed, inferred and verified. A
check proves only what it measured; a passing build, DRC, ERC or critic is
evidence for that property, not product success. Say when a tool stopped,
evidence is missing, or steps are being repeated. When you notice something
wrong while doing something else, say so with the file and line.

## Board workflow

- **Graph.** Board work is on the graph (`tools/board_graph.json`), never
  beside it. A session that touches the board starts with `board-graph where`,
  starts the card it names with `next --by <who>`, does that card and nothing
  else, and ends with `record`.
  In a repository with several boards, `where` shows them all and every
  other command names its board: `--board <name>`.
  Work the graph has no card for is first a change to the graph, brought to
  the owner. `docs/BOARD_PIPELINE_CONTRACT.md` says how the graph runs.
- **A card that does not work (and enforced self-healing).** In supervised mode,
  when a card's script or tool crashes or breaks, stop, record `needs_lead` with
  what it said word for word, and tell the owner in plain words what failed. In
  `Mode: AUTONOMOUS` (`autonomy.mode == "delegated"`), act as the delegated lead
  instead of exiting: self-heal genuine `board-graph` tool bugs through
  `board-graph heal` and iterate routing retries cleanly. Never do a broken
  card's work another way, with another tool or by hand, and never record it as
  done. Never propose a replacement for a tool the owner chose (another router,
  a script in its place). Say what was done by its true name: copper a script
  wrote is not "routed by hand". Never write a note or a memory that tells a
  later session to avoid a card or its tool.
  When fixing a genuine tool or Linux/macOS bug in an editable `board-graph`
  checkout (`dev/tool_dev.py` present), **self-healing goes through the 6-card
  tool workflow** (`board-graph heal start <slug> --from-card <node>`, walking
  `triage -> implement -> tool_checks -> cold_review -> live_smoke -> release`).
  `board-graph next`, `record`, and `route` refuse to advance the PCB while the
  tool checkout has uncommitted or unverified tool edits outside that 6-card
  board. Expected starting baseline findings on an agent card (such as missing
  listings, placement snapshots, or part heights before authoring them) are work
  to resolve on that card and recheck before recording `pass`. Stale upstream
  approvals (`recheck`) blocking `next` are refreshed by walking `returned`
  cleanly through the graph, not by stopping with `needs_lead`.
- **Copper.** Copper is laid only by the Routing batch card's routing run, in the
  locked KiCad editor, using the task's authorized mode (`Mode: ipc`,
  `Mode: hybrid` default, or `Mode: interactive`): checked atomic IPC chunks via
  `scripts/kicad_lay.py`, and/or KiCad's interactive router via
  `scripts/kicad_route.py` (`--mode shove` / GUI). Ad-hoc scripts or
  wrappers (`wrapper_calls`), raw `create_items`, direct `.kicad_pcb` edits,
  and unauthorized external autorouters remain forbidden. Report the mode and
  solver provenance honestly in receipts; only the owner's own record accepts
  copper laid outside a run. If the run fails, stop and report it.
- **Board.** The schematic is the source; the board is updated from it, never
  changed behind it. Never route ahead of placement the owner approved on a
  rendered view; placement settles position, rotation and side. After every
  batch of edits, export a picture, look at it, and fix what looks wrong; the
  owner's eyes are never the first look.
- **Parts.** Adding or changing a part means getting its datasheet into the
  datasheets folder, taking its specs from it, and making or checking its 3D
  model against them. The footprint library stays identical to the board and
  holds nothing else.
- **KiCad.** One board file and one writer. Save any unsaved edits in the
  open editor before a change, apply the change on top, and reload the editor
  after. Never run KiCad's Python directly from a tool on a machine where it
  opens a window; KiCad's command-line tool runs through
  `tools/kicad/invoke_kicad_cli.py`.
- **Tool files.** `board-graph` runs from its installed package or shared
  checkout (and `kicad_machine.tool_checkout` on a separate KiCad machine), so
  a project repository holds only project files (`board/`, `tools/board_graph.json`,
  `tools/board_graph_site.json`, records, docs, and starter config). Change tool
  code in the `board-graph` repository, never inside a board repository.
  Run `board-graph tool sync-agents` in a board project after updating
  `board-graph` to refresh shared tool rules in `AGENTS.md` while preserving
  `## Product`.
- **Benchmark & multi-VM monitoring (event-driven only).** Always supervise
  live `board-graph` runs and multi-VM benchmarks with
  `docker/watch_benchmark_events.py` (SSE `/events`). Never replace it with
  cron jobs, `sleep` loops, or periodic polling.
- **Git.** Before editing, check the checkout matches its branch on `origin`.
  Board changes go on a branch: open a pull request when `origin` is on
  GitHub; when `origin` is a local bare repository with no GitHub remote
  (such as `one_machine` autonomous runs), push the branch to `origin` and
  merge it into `main` locally (`git checkout main && git merge --no-edit <branch> && git push origin main`)
  instead of calling `gh`. No attribution footers or co-author trailers in commits.

## Writing

Plain words, short sentences, tables for comparisons. Name a part by what it
is and its reference: "the power jack (J1)", never a bare J1. Docs say what
is true now; git keeps the past. Keep code `#` comments to non-obvious
invariants, KiCad/OS quirks, and fail-closed reasons; never write dated
diary entries, owner chat transcripts, or comments that restate the code.

## Where things live

`docs/PRODUCT_TRUTH.md` first. Board: `board/board.json` (the board's files),
`board/DECISIONS.md`, `board/SPACE.md`, `board/ELECTRICAL_AUDIT_CONTRACT.md`,
`board/ORDER_PACKAGE_CONTRACT.md`, `docs/BOARD_ORDER_COMMON_SENSE_CHECKLIST.md`.
Routing: `docs/ROUTING_NOTES.md`. KiCad: `.agents/skills/kicad-10-operator/`.
Critics: `.claude/agents/board-critic.md`, `schematic-critic.md` and
`.agents/critics.md`, with the same two for Codex in `.codex/agents/`.
Machine settings: `tools/board_graph_site.json`.
