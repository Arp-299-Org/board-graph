# Approvals on the current revision

`py -3 scripts/board_graph.py approvals` compares the records in this checkout with the files here. It reads local JSON files in the same chronological order as the live graph. It works offline and leaves the records, database and card position untouched. Use `--board motor` in a multi-board project and add `--json` for the original reviewer, time, latest outcome and cited evidence. The live KiCad machine can have additional records. Use `where` in observation mode for its history against the checkout running the command.

| Result | Meaning |
|---|---|
| Current | The original pass cited evidence. The declared inputs, card terms and prerequisite reviews still match. |
| Recheck | An input changed, disappeared or now refers to another review. The comparison names the input. |
| Open | A later finding or return reopened the card, or an open issue names it. The record carries the reason and evidence. |
| Unproven | The record lacks enough information to reuse the approval. A historical pass stays visible. |

## Start a baseline on a board already in progress

Preserve the saved design, physical work, old records and unresolved findings. Identify the exact starting PCB, schematic, rules and relevant release artifacts. Compare each original approval with what its owner or reviewer actually saw. A matching scoped receipt can stand. A legacy pass without that binding remains historical. To establish an approval for the current files, review those files and record today's evidence under the normal card authority.

Start with the first unsupported approval that the next consequential decision depends on. For instance, a fresh render and owner sign-off can establish placement on an existing layout. Changing the layout is warranted only if inspection finds a geometry problem. A completed purchase or physical delivery belongs in the history as it happened. Establishing a new baseline should never place a second order or manufacture a duplicate event. A board with inadequate order evidence needs the order confirmation and its associated package reconciled before another release.

## Try it on one board

1. Install the same candidate tool revision in disposable copies of the project on the machines that run it. Keep the project's graph and records. Run `approvals` once to see the starting point.
2. In that board's graph JSON set the top-level `"approval_mode": "observe"`. A new original pass records its input fingerprints. `where`, the card brief and the graph page show what the alternative sees. Card advancement still follows the previous rules.
3. Give each approval the inputs the reviewer actually examined. `approval_view` selects the comparison, including on a card that already uses `unchanged_pass`. Without an explicit view, that card compares its existing view. The starter gives Routing the copper view and Visual review the silkscreen view plus the Routing approval. `approval_files` names individual files relative to the project root. Card terms include the task, pass condition, the listed readings and completion check. The comparison hashes the contents of each required reading and each cited report. For a card with `approval_owned` enabled, each path in `owns` needs a saved output at sign-off. Directories must contain files. List any genuinely optional owned paths in `optional_outputs`. Their presence, absence and contents remain part of the approval. Use a separate report path for evidence that must survive subsequent checks.
4. Add `requires_current` to the cards that consume those approvals. Existing `requires` references to a card with declared approval inputs also use its revision check in enforce mode. Only declared dependencies take part. Run `board_graph.py check` to check the configuration, then run `approvals` again. The starter graph supplies example links for Stock, Routing, Visual review, Verification and Order.
5. Use normal reviews on the cards as they come up. An old pass or an automatic carry-forward cannot supply missing original evidence. To revisit an earlier card, use the graph's existing return and cite the specific finding. A graph with no return to the needed work needs an owner-approved graph change. Review any such gaps before enabling enforcement on a board already in progress.
6. If the owner adopts enforcement for that board, set `"approval_mode": "enforce"` in the chosen graph. `next` checks the required reviews before starting work. `record` checks them again at pass or more_work. A return or a stop can always be reported under the existing card rules. A change to the starting terms or prerequisite review during an open attempt calls for a fresh attempt.
7. To switch back, set the mode to `"observe"` or `"off"`. Existing records and the track position survive both switches. Omitting the mode selects off.

A typical Placement card can use these fields.

~~~json
{
  "unchanged_pass": "placement",
  "approval_files": ["interfaces/motor-socket.md"]
}
~~~

For a decision that must be settled before work begins, give each affected card the same `requires_decisions` list. The file belongs to the project. Record the owner's adopted words and how to test them in the assembled product.

~~~json
{"schema":"board-graph/product-decision/v1","status":"accepted","owner":"owner","decision":"The socket key faces outward.","acceptance":"Both sockets mate with the keyed harness in the closed enclosure."}
~~~

An open decision blocks issuing dependent work in enforce mode. At product judgment, configure `integration.boards` with the name and PCB path of each board in the assembly and `integration.firmware` with the tested binary when applicable. The receipt gives each board's SHA-256 and each unit's firmware hash. Each unit cites a passing observation for every required decision using `decision` and `decision_sha256` in its checks. The responsible owner supplies the physical observations.

The mating board can reference the same owner-maintained decision file. A change then reopens each board's affected review. Each board retains its own position and approval. A Routing card consumes its board's approval with `"requires_current": ["placement"]`.

The purchasing view includes footprint identities, assembly flags, side, fields and pin types. The starter BOM card also names the saved schematic and BOM export. Adjust these paths to the project. Supplier changes reopen the purchasing review. Placement uses the physical geometry view. Stock availability and price still need their existing time-sensitive checks.

## From presentation to sign-off

For Placement, run `tools/kicad/render_board_views.py` with `--board` and a new `--out` directory for each presentation. The renderer copies the referenced 3D models for this run. Its version 2 board-render.json receipt identifies the saved PCB, each model by its KiCad reference and SHA-256, and each fresh image. Keep project models available on both the rendering and reviewing machines. A missing model, a Git LFS pointer in the working copy or an older receipt calls for a fresh, verifiable presentation. Show the owner the images you cite. Keep that directory so a later review can still identify those files.

For example, with the board selected and the open attempt identified, record the presentation and then the owner's decision. Substitute the actual paths, attempt ID and words.

~~~text
record placement needs_owner --attempt <id> --by agent:<name> --note "Presented top and bottom" --evidence review/board-render.json review/3d_top.png review/3d_bottom.png
record placement pass --by owner --note "<owner's words>" --evidence review/board-render.json review/3d_top.png review/3d_bottom.png
~~~

At presentation the model hashes must match the rendered copies. At sign-off the models and the saved board must still match. An edit before sign-off needs a new render and presentation. After sign-off, changes to the physical placement or its referenced model files reopen Placement. A supplier-only edit still preserves it.

Routing compares the selected board’s custom KiCad rules and routing-relevant project settings along with the copper. These include design minima, rule severities, net classes and assignments, and tuning profiles. A changed routing constraint calls for a fresh check of the saved copper. Visual review shows the outstanding Routing review. Viewports and net display colors can change without reopening Routing.

At Orientation preview, upload the sealed archive, save the assembler's images locally and cite them in a `needs_owner` record. Identify the upload or job in the note. Cite those files again when recording the owner's approval. Order rechecks the archive, manifest, preview images and original approvals before advancing. The person presenting the images establishes which supplier upload produced them. The graph checks the local files and the recorded association.

When a card's reviewer requests a PR merge with `--then pass`, cite the completed review evidence at that PR head. Enforcement carries the review if its declared inputs, authority and evidence match across the merge and current checkout. If they differ, the card asks for a fresh review. Run `next` on that card to start it.

An approval only covers its declared inputs. A fresh review still needs the usual evidence and authority for its card. The checks for routing provenance, independent review, package release and owner order authorization continue to apply.
