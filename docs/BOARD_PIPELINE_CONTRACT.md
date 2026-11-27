# Board workflow

This contract defines why the board graph tool works as it does. A project's
first milestone is the graph's `milestone`. The graph navigates work from the
current design; engineering decisions remain explicit nodes. Training serves a
blocked board task and returns to it.

## The graph

A project's `tools/board_graph.json` holds the graph: the stages on the board
track, each a row of nodes (the starter graph has seven: requirements and
constraints; schematic, parts, BOM; footprints, models, placement; layout and
routing; package, stock, quote; order and assemble; test the first boards),
other tracks beside the board track, and framework work as a list off the
board path. Beside tracks handle parallel deliverables such as firmware builds,
long-lead component reservations, enclosure spacers, or routing research as side
trips. Every node has the same shape: who does it (a script, an agent or the
owner), its task, what passing needs, the files it owns, the script that
decides it, and its outcomes: `pass` names the next node, `returned` lists
the earlier nodes a finding may send work back to, and `more_work`,
`needs_owner`, `needs_lead` and `needs_skill` stay put. A graph that is not a
board may also let the owner end a track early at a card (`dropped`; the
README says how). A node cannot pass while an open issue names it, or while a
node it requires has not passed. A card names the docs a person or agent on it
reads whole, in order (`docs`; the graph's own list, the product truth, comes
first for every card), and a script's card names none.
`python scripts/board_graph.py check` validates the file and refuses what an
agent could not read whole: a line over 2000 characters (a Read cuts it) and a
named doc, or AGENTS.md, that is missing or over the smallest
loader's cut, 30,000 characters or 2000 lines, and cards that wait on each
other in a loop; `graph` prints it in Mermaid form.

## The board description

The graph's scripts hold no board's paths or names. Each board has one
description file, a project's `board/board.json`, and the scripts read every board
file, folder and board-specific check from it: the KiCad files, the footprint
library and model folders, the exports, the owner's placement, the reports and
production folders, the accepted DRC list, the package name (a regular
expression matched before `_<date>`), the parts data and datasheets, the board's
own electrical audit with its waivers, the full board audit with its arguments,
and the hand-assembly notes. Every path in it is relative to the repository
root. A board without its own electrical audit or its own full board audit
leaves that key out, the checks that would run it say they skipped it, and the
package says "none for this board" for it.

Three more keys are optional, and a board without them leaves them out:
`part_models`, the board's own Python file of STEP part models, whose `PARTS`
`scripts/kicad_step_model.py` writes; `manual_joints`, the words the
hand-soldered joints report calls each part by (`names`, by reference) and
what it says about a recommended pin-to-hole clearance (`clearance_note`);
and `routing_agent`, what `scripts/editor_step.py` puts in its prompts: the
docs a reviewer reads first (`docs`), the routing recipe the agent is given
(`recipe`) and the skill it comes from (`recipe_skill`), and the nets the
reviewer guards (`sensitive_nets`). A field left out is left out of the
report or the prompt.

`scripts/board_description.py` finds the file, checks it and gives its paths:
a script's `--board-description <path>` first, where the script takes
arguments, then the environment variable `BOARD_GRAPH_BOARD`, else
`board/board.json`. A missing key is refused by name.
`python3 scripts/board_description.py` prints the description in use, with
absolute paths.

A new board enters the graph at the schematic stage. Its description names
every file, and until the board holds a part, the board file, the rules and
the exported BOM may not exist yet: Inventory passes when the project file and
the schematic read cleanly and each of the three that exists does too, and its
record names each one that is not there yet. A card that reads one of them
(BOM, Datasheets, Models, Footprints, Placement) records a finding that says so
until it is there. Once the board holds a part, Inventory needs all five files.

A board may carry copper shapes and no part: every footprint on it is kept out
of the BOM (a flexible strip's contact tabs). Inventory, BOM, Stock and
Placement then pass with nothing to compare and say so; the owner's approval
of the rendered view is still Placement's pass.

## Several boards

A repository may hold more than one board. `tools/board_graph_boards.json` lists them: `boards`
gives each board's name its description, and `first` names the board that
keeps the files a one-board repository has. Every other board has its own
cards and open issues (`tools/board_graph.<name>.json`), records
(`tools/board_graph_records.<name>/`), state database (beside the first
board's, in `<name>/`) and page data (`tools/board_graph_view/boards/<name>/`);
its reports, decisions file and footprint library come from its description.
A repository with no list has one board and nothing changes for it.
`scripts/boards.py` reads the list and prints each board's files.

One process works on one board, chosen before a script reads a description:
`--board <name>` on `board_graph.py`, `board_graph_view.py`,
`run_board_checks.py` and `routing_run.py`, or `BOARD_GRAPH_BOARD` naming the
description. With several boards and none named, `where` and `check` answer
for every board and a command that writes is refused. A description the list
does not name cannot reach the KiCad machine's database. The KiCad machine is
told the board with each write, except for the first, which every checkout
reaches as a one-board repository does. A card cannot require another
board's card: a question both boards depend on is an open issue in each
board's cards file.

## The site settings

The graph's scripts hold no machine's names either. The machine setup of a
project is one file, a project's `tools/board_graph_site.json`: the KiCad machine
(its SSH host and the key this PC uses for it, its main checkout, the steps
folder a routing run works in and the lock that holds its editor, the prefix
of a branch's worktree there, its cache folder, and the prefix of its
disposable KiCad copies, which tidy removes), the graph page's address and
its launchd job, and where the state database lives under that checkout.
Paths on the KiCad machine may start with `~/`, the home of the user SSH logs
in as. `graph_page.python` is optional: the Python the page's job runs under
there, `/usr/bin/python3` when left out.
Two projects that share one KiCad machine name the same editor lock, because
that machine has one screen and one editor; the checkout, the steps folder,
the worktree prefix, the page's address and the launchd job are each
project's own.

`scripts/site_settings.py` finds the file, checks it and gives its values:
the environment variable `BOARD_GRAPH_SITE` first, else
`tools/board_graph_site.json`; on the KiCad machine the file is the one in
the checkout the script runs from. A missing key is refused by name.
`python3 scripts/site_settings.py` prints the settings in use. The
environment variables the scripts read use neutral `BOARD_GRAPH_*` and `KICAD_*`
names (`BOARD_GRAPH_STATE`, `KICAD_PYTHON`, `KICAD_IPC_PYTHON`,
`BOARD_GRAPH_TEST_KICAD`, `BOARD_GRAPH_WINDOW`, and `BOARD_GRAPH_...` worker
variables).

A project with no KiCad machine sets `"one_machine": true` and
`state_database` alone: the database is then a file in the repository's main
checkout on this machine, and nothing is reached over SSH.

## The pack

The graph's engine holds nothing about a kind of work: it takes the card
scripts, the views, the reports folder and the branch folders from a pack.
A project with no pack file runs the electronics pack, a circuit board in
KiCad. `scripts/graph_pack.py` says what a pack gives and how a project's
pack file names the pack `none` for other work.

## The tool's repository and a project's pin

The graph's scripts, board cards, and general checks live in one tool
repository named by the manifest's `repository` field. In vendored mode, a
project keeps a pinned copy (`scripts/`, `tools/kicad/`,
`tools/board_graph_view/`, this doc) listed in `tools/board_graph_tool.json`
with LF-normalized SHA-256 hashes; project files (board description, site
settings, graph, records, and project scripts) stay outside the manifest.

`python3 scripts/board_graph_tool.py check` fails on any edited or missing
tool file. Change tool files in the tool repository, run
`manifest --version <new>`, then run
`update --from <tool-checkout> --ref <version>` in the project: `update`
refuses dirty copies, reads files through git, removes dropped files, and
writes the new manifest (idempotent). `export --to <folder>` installs a first
copy (`--replace` overwrites differing files).

The manifest's `starter` list names project-owned starter files with no hashes
(graph, board description, site settings, docs, critics, `AGENTS.md`,
`CLAUDE.md`). `check`, `update`, and `export` ignore them.
`starter --from <tool-checkout> --ref <version>` copies missing starter files
through git without overwriting (`--skip <path> ...` skips renamed files;
`manifest --add-starter` / `--remove-starter` edit the list; a path is never
both a tool file and a starter file).

`scripts/kicad_run.ps1` runs a KiCad script only when listed with the asked
mode in `scripts/kicad-script-modes.json` (tool) or
`scripts/kicad-script-modes.project.json` (optional, project); scripts in
neither or both are refused. Optional `scripts/board_graph_project_actions.py`
maps project action names in `ACTIONS` to functions returning `pass` or
`needs_lead`, findings, and detail, plus read paths; collisions with tool
actions are refused. `tools/board_graph_view/index.html` is a stateless tool
file; `board_graph_view.py snapshot` writes project state to
`tools/board_graph_view/snapshot.js` (kept in git, outside the manifest).

### External shared tool mode

Instead of vendoring tool files, desktop and the KiCad machine may each keep
one shared board-graph checkout installed with
`pip install --no-build-isolation -e .`. Running `board-graph ...` from a
project (or passing `--project DIR` / `BOARD_GRAPH_PROJECT`) resolves tool
assets from that checkout and project state from the project repo. Setting
`kicad_machine.tool_checkout` in `tools/board_graph_site.json` lets `mac.py`,
`board_graph_serve.py`, and `routing_run.py` use the Mac's shared clone and
sync it to desktop's exact commit SHA before routing. Vendored projects keep
working unchanged; `board_graph_tool.py unvendor` (optionally with
`--mac-tool-checkout ~/board-graph`) deletes unmodified tool files and
`tools/board_graph_tool.json` while keeping `tools/board_graph_view/.gitignore`,
starter files, records, and project scripts. In CI, check out the tool
repository beside the project and run `validate_board.py` with
`BOARD_GRAPH_PROJECT=$GITHUB_WORKSPACE` (or `board-graph validate`).

## Position is state

Each track's position is the target of its last transition, in one SQLite
database on the KiCad machine (the agents' machine: always on, and the one
with KiCad), at the site settings' `state_database` under its main checkout,
keeping one live state across desktop and KiCad-host checkouts, worktrees included,
each write one transaction under its lock with the check that decides it.
When the KiCad machine does not answer, a command refuses in one line and
writes
nothing anywhere; `--state` names another database only for a test or a
scratch run. Every transition is also one JSON record in
`tools/board_graph_records/`, committed and pushed by the command that made
it, so a card's move is on main, or on its pull request, the moment it is
made; the database imports the records the KiCad machine's main checkout holds,
so the position in git and the live position agree. CI's board checks are
in the database too: the validate workflow's last step, on the KiCad
machine, keeps
each run's result there (the commit, each check, the open count), and
`where`, the page and the Board checks card read it from there, never from
GitHub. The validate workflow is the project's own. A project without one
runs `python3 scripts/run_board_checks.py` on the KiCad machine, in a checkout
of the commit: it refuses a checkout that holds a change or an untracked file,
runs the same checks with their reports outside the checkout, and keeps the
result the same way, as a run named `local-<time>`. A card's reports live at `<reports>/<card>/` (the board
description's `reports` folder), the newest only: a
new report replaces the last at the same path, and git history keeps every
earlier one, so the tree holds nothing stale and a record's hash still names
what it cited (one fixed path per card). A report is at
most 2000 lines, the result at its top and a long list in a file beside it,
since a Read stops at 2000 lines; `check` refuses a longer one.

## The verbs

`python scripts/board_graph.py where` says where every track stands:
between two cards until something is on the next one, then on
it with who and since when, or waiting on the owner, the lead or a skill
when the last record asked them for something; then the card's task,
outcomes and script, the last transitions, and the workspace: main here and
the checkout it runs in against GitHub's main, the KiCad machine's checkout
and its
keep-pace run, the worktrees whose work is all on main that tidy has left,
the branches and worktrees holding work main lacks, the pull requests a
position waits on, where each open one's branch stands, and the last upkeep. A quick `where` reads this checkout,
the branches and worktrees here and the database; `where --full` fetches and
asks the KiCad machine and GitHub, and keeps that read for the quick ones
after it. Work is on main when its branch's tip is in main's history or every
commit past main is there as an equal patch, which is how a helper's commits
arrive when a lead cherry-picks them; `scripts/tidy.py` holds the rule and
clears such branches and worktrees here, on GitHub and on the KiCad machine.

`python scripts/board_graph.py record <node> <outcome> --attempt <id> --by <who>
--note "<why>" --evidence <paths>` writes one outcome at the card where its track
stands and closes that attempt. When no attempt is open, omit `--attempt`.
It refuses an unscoped record when an attempt is open, any other card, an
outcome the card does not allow, a `pass` while an open issue names the card
or a required card has not passed, a `pass` on an owner's card recorded by
anyone but `owner`, and a `pass` or `returned` without evidence; the
evidence and the card's owned files are hashed as recorded. The record's
commit includes individually cited local report files under the reports
folder
with text report, PDF, PNG or JPEG suffixes; it does not expand cited folders,
follow links or include evidence recorded at a pull request's head. Before
hashing a live record, eligible UTF-8 text reports are normalized to LF in the
checkout, matching the Git convention. Commit
other evidence, including source and design files, through its normal workflow. On a branch
other than main, the record's push also keeps the branch's pull request:
opened as a draft at the first
record there, its description made current with every record (the records
on the branch, then CI's board checks on its head as the graph holds them),
and marked ready only at the record that asks the owner to merge, once CI's
board checks as the graph holds them have passed on the commit it asks about
(the pull request's head the record fetched; the record's own commit has no
CI result yet). A pass alone leaves it a draft, and so does a merge ask
before CI has passed: its line says why, and recording the ask again once CI
has passed marks it ready. What
GitHub refuses is said in one line and the branch is pushed either way; the
next record tries again.

`python scripts/board_graph.py next` is the one verb: it starts the next
card in line on `--track`, and on the framework's list the card named with
`--node`. A person's card is started for `--by <who>`, so it is on until
their record closes it; a connected script on such a card (Placement) runs
first, its report theirs to cite. A script's card is run by its script, and
what it reports is recorded with its report, `<reports>/<card>/<script>.json`,
as evidence, and what it says that is not a finding (the report's `notes`) is
in the record's note; a script that raises records nothing. An unfinished script attempt
must be settled by its ID before `--again` can run it. A script that ended
`needs_lead` or in an error does not run again on the same inputs without
`--again`. The inputs are the card's owned files, the files the script reads
and the tool's manifest, so a new version of the tool counts as a change and
the script runs again. Before anything runs, the
upkeep brings the checkouts current: `scripts/tidy.py --apply`, then main
here must be GitHub's main exactly, the checkout `next` runs in must hold
everything on it, neither may hold an uncommitted change to a file a
card owns, and the checkout's git-LFS files must be files, not pointers (a
fresh worktree holds pointers until `git lfs pull`, which the upkeep runs, so
LFS-tracked 3D models are materialized locally), or `next` says why and runs nothing.
A `needs_owner` that asks for a merge names the pull request and the outcome
that follows (`--pull-request 84 --then pass`), and `next` at that card asks
GitHub; merged, it checks main's owned files against the hashes the question
kept at the pull request's head and records that outcome, or lists each file
that differs and records nothing.

A card with `unchanged_pass` in the graph passes by itself. Before `next` starts it, it
hashes the card's view of the board, what that card reads: for Placement the
board edge, mating pads, physical footprint drawings, model references, sides, positions and rotations; for Routing batch
and Visual review the board without its part fields (copper, vias, zones and their
fills, graphics, text, rule areas, the outline, the nets and the setup). When that hash is
the one at the card's last pass (kept in that record's inputs as `view:<name>`, or
read back through git from the board as it stood then), `next` records `pass` as
`script:unchanged`, with no evidence beyond the two hashes, an owner's card included:
the owner's pass on the same placement stands. A changed view starts the card as
usual (for instance, a two-field supplier part swap updates Purchasing without
invalidating approved Placement or clean Copper).
So does a card a `returned` record sent work back to: it owes that work and does not
pass by itself until it records a pass of its own after the return, and `where`,
`brief` and `next` say which card returned it.

`next --codex --by agent:<name> --minutes <n> --note "<remaining work>"`
is the single-card launcher for an agent card with no script `next` runs. It records the selected checkout, branch, commit,
brief, prior record and evidence on the exact attempt. It refuses owner cards,
a card whose last record waits on the owner, the lead or a skill, and a wall
limit longer than the card's stop. Automatic dispatch is available on Windows. Before opening an attempt,
it selects a native Codex CLI that supports `--approve-for-me`; an unavailable
or unsupported CLI opens no attempt. Its worker uses a workspace-write sandbox
with command networking off and automatic review of individual permission
requests. A reviewed command can run outside that sandbox; the review is not a
host allowlist. Plugins, apps and configured MCP servers are disabled for the
worker. On other platforms, and for an external agent anywhere, a manual
start remains available; the Codex launcher's wall-clock and repeated-failure
guard do not supervise those processes.

For Routing, commit and push one task file per connection or multi-wire chunk. Set
`routing_agent.tasks`, `provider`, optional `review_provider`, `model`, `review_model`, `effort` and `plan`
in the board description. From its Windows checkout, use the started brief's
Tool command. Top lines name endpoints and nets (`Connection: J1.1 J1.2 /NET`),
optional `Mode: ipc|hybrid|interactive` (default `hybrid`:
`kicad_lay.py` IPC, `kicad_route.py` Push-and-Shove), or `Repair:`. Ad-hoc wrapper scripts remain forbidden in all modes.
The locked Mac editor runs at the same commit. KiCad checks the saved pair
before and after. The run commits accepted tracks, arcs and vias with its
manifest and step evidence in `run-<attempt>.jsonl`. All steps kept records
`more_work`. A stopped batch records `needs_lead`. Passing needs a board
review and the lead's inspection.

[Board evidence](BOARD_EVIDENCE.md) gives the handoff rules, candidate contents and release receipt formats.

The Routing batch card refuses `pass` and `more_work`, and the board checks fail (Copper origin),
while the branch holds a track, arc, via or pour, or a change of copper
layers, that no adopted run committed (`scripts/board_copper_origin.py`).
Only the owner's own record on the card accepts such copper. When a card's
tool fails or refuses, the one on it records `needs_lead` with what it said
and stops: the work is never done another way.

`python scripts/board_graph.py brief <card>` prints a card's brief, the
predetermined context whoever starts the card is given: the card as `where` shows it, the open issues that
name it in full, the docs to read whole in order with their sizes, the files
it owns and whether they go on a branch, who was on it last and its newest
reports, CI on this checkout's commit, the stop, the command that starts it
and the command that closes it. `next` prints the same brief as it starts a
person's card and keeps the text on the attempt it opened, so what the one on
a card was given can be read back for every start; `brief` alone starts
nothing.

`python scripts/board_graph.py settle <card> --attempt <id> --by <launcher> --tokens <n>`
settles a run: the launcher's move once the thread it started has ended, every
time. When known, it keeps the thread's tokens on the attempt the thread was: the
provider's input and output totals for the run, which the Codex launcher reports
itself; a manual thread gives its own with `record --tokens`, and `where` shows
them on the transition. The Token use card sums what each run used. A thread can also die before it records (a
crash, a context that ran out, a hung tool), and the card would stay on with
nobody on it: with an attempt still open, `settle` records `needs_lead` for the
launcher, saying who was on it and how long, and tokens when known. With no
attempt ID and nothing open, it is a status-only query and keeps no tokens.
The stop is a loop guard, not a plan of work: a 2-hour wall-clock backstop
unless a card sets its own, and a launcher checkpoint after three failed
executions of the same command with unchanged source inputs. A checkpoint
on an open attempt records `needs_lead`. After a record, it preserves that
outcome and waits for the lead to acknowledge the stop with a revised approach
using `settle <card> --attempt <id> --ack --by <lead> --note "<approach>"`.
If the launcher could not confirm that the process tree stopped, the lead must
verify the worker and every descendant ended, then add `--verified-stopped` to
the acknowledgment. Until then, it remains blocked. On a forced Windows stop,
the launcher waits for the job members it can pin before termination. That
snapshot cannot establish the exit of every earlier job member, so forced
Windows stops keep this unconfirmed checkpoint even when those handles signal.
A launched process owns the ordered track until it returns; on a framework
list it owns only its card. `where` and the page show the attempt and any stop.
If a launcher disappears, a lead inspects the process and settles that exact
attempt. The checkpoint stays visible until the lead records an approach.
This checkpoint catches repeated failed commands, not every kind of loop. Start
and end are the launcher's two moves: `next` to start the card and hand over
its brief, `settle` once the thread is gone.

## The view

`tools/board_graph_view/index.html` draws the graph in 3D, to watch it run
and to share it: the stages as lanes climbing in order, the other tracks
beside them (each beside the stage its label names in parentheses, else the
earliest stage whose board card requires one of its cards or returns work to
one, else a stage no other track holds), the framework list apart, light for
state. A row of tabs at the top names every track with cards. The board's
tab is the whole graph; another track's tab is that track's own page
(`?track=<id>`): that track alone, its cards the main path in rows of four at
the size of the board's, with its position and the updates on its cards. A
card there that waits on another track's card names it in its panel, with a
link to that track's page. Clicking a card surfaces its files and
deliverables first (the schematic PDF, BOM table, part datasheets and folder,
3D models, placement and routing renders, reports and docs), served read-only
by the page server
under `/files/<path>` (`scripts/board_graph_serve.py`), with agent and check
rules tucked under details. Git shows beside the
graph, never in it: the strip along the bottom summarizes `where`'s workspace, and under the
positions is the history of step updates (with machine runs available in the
panel). The view only reads: it records nothing and changes neither the graph
nor a position.

The KiCad machine serves the page at the site settings' address
(`scripts/board_graph_serve.py`, kept running by their launchd job, which
`--install` writes from the site settings unless the project keeps its own
`tools/mac/<launchd_label>.plist`). Nothing polls: every write to the
database rewrites the page's data on the KiCad machine, a push to main tells
that machine, it tells the page server, and the open page is told of every change.
Hide private draws the page from a copy of the snapshot with the private
details taken out, so a screenshot or Save PNG holds none of them and
nothing is covered. The machine and account names it takes
out come with the snapshot (`private_names`: the site settings' SSH user and
host, and the names the machines that wrote it give), so the page names no
machine. A change plays as a light along the edge
the position moved by, under reduced motion too. `py -3 scripts/board_graph_view.py snapshot` makes the full
snapshot, which also writes `snapshot.js` beside the page, the copy shown when
the page is opened as a file; without it, that page says there is no
snapshot to show. The page takes its title from the snapshot, the project
named in the board description.
