# Board evidence

## Routing handoff

The Routing attempt keeps the card instructions and hashes of its reading
when it starts. The runner compares them before launch and adoption. A
changed contract needs a new decision and attempt. The operator's clock
requests a handoff at the next safe gesture. The operator has up to 60 more
seconds to return. A passing board gets a separate read-only review window
of up to the configured minutes. A worker that fails to return is stopped.
The current tool window and this grace are elapsed-time bounds. Metered
usage is reported when the provider supplies it.

For one or more connections between elongated rectangular, rounded-rectangle, or
oval SMD pads, add a line `Pad exits: centered` below the task's consecutive top
`Connection:` lines when the paired pads should use the short edges facing each
other. `routing_run.py` checks every task file against the saved board before
starting an attempt or contacting the Mac (and supports `--preflight` for
standalone local checks). The runner checks every listed connection on the saved
board and measures the saved track centerline against each pad's true perimeter at
each edge. It allows 0.02 mm of lateral offset and one
degree from perpendicular. Each pad must have one crossing and a straight leg
from its center. The per-round connection report lists offsets and findings
and has a close-up PNG for each pad. The reviewer receives the deduplicated
close-ups for every unique pad alongside the original four editor views and must
describe each pad by name.
The centerline must run continuously through the pad to that crossing.
An explicit MUST FIX on a pad observation stays open until a later review
clears it. A measured pad defect goes back for correction even when a
route-quality warning is also present. A completed review records the SHA-256
of the saved board. Before adoption the desktop requires that exact hash on
the last kept step, including when resuming an older attempt. A missing or
mismatched hash records needs_lead and stores the downloaded PCB in a separate
ZIP beside the receipt for review.
Other tasks keep their existing routing criteria.

When the saved named pads join, the design check passes, and the only new
audit findings are route-quality warnings, the runner archives the exact
PCB, front and back views, DRC, pad check, and the close-up images named by
the pad check. The independent reviewer sees this saved PCB, the measured
geometry for each numbered warning and the archived views. Each warning
needs an explicit `FINE AS IS: Warning N:` with a reason. A `MUST FIX`
returns to the operator within the correction budget. Unresolved findings,
an incomplete review or a changed PCB prevent adoption. Each reviewed
warning round keeps its own ZIP beside the receipt, including when a later
correction fails. The receipt links each verdict to its PCB hash and ZIP.
The desktop verifies the last
kept hash before adopting copper. The other design and electrical checks
still apply. Downstream board checks (G3), production packaging, and the
Package card accept a remaining route-quality warning only when its exact
measured geometry matches a verified `FINE AS IS` verdict in an adopted
routing receipt (`run-*.jsonl`) for that board; any unreviewed warning or
changed geometry still fails G3.

Placement reuse now includes the board outline, the mating pads, model
references and physical footprint drawings. A prior approval using the
older fingerprint can require one fresh look.

## Routing modes and planning selection

Each routing task may declare `Mode: ipc`, `Mode: hybrid`
(default when omitted), or `Mode: interactive` alongside its `Connection:` and
`Pad exits: centered` lines:

| Mode | Authorized copper tools | When to choose in batch planning |
|---|---|---|
| `ipc` | `kicad_lay.py` (`route`, `batch --json` / `--stdin`, `pads`) + `kicad_edit.py` | Open channels, parallel buses, connector joiners, power trunks and straight fanouts with clear 45-degree geometry and nothing to shove. |
| `hybrid` | `kicad_lay.py` + `kicad_route.py` + `kicad_edit.py` | Default. Mostly clean runs with 1 to 3 pinch points or existing-copper crossings needing Push-and-Shove (`--mode shove`). |
| `interactive` | `kicad_route.py` + `kicad_edit.py` + GUI gestures | Dense IC escapes, congested areas requiring live shoving/walk-around, or surgical rework inside existing routes. |

Ad-hoc scripts and temporary wrappers (`wrapper_calls`), `write_file` scratch files, raw `create_items`,
and direct `.kicad_pcb` copper edits are forbidden in every mode. If `Mode:` is
declared on the committed task, preflight checks it, the step runner rejects
forbidden tool use (`mode_violation`), and adoption requires `routing_mode` in
the receipt to match. Configure optional `routing_agent.plan` in `board.json` to
inline the board-wide lane/layer strategy for operators alongside the offline
board geometry brief (`board_context.py`).

## Verification and Product judgment

At Verification, cite a JSON receipt and each reproduced report with
`--evidence`. The reviewer writes the receipt from the files reviewed.
The board and schematic SHA-256 values refer to the saved sources.
The following fields illustrate the format.

```json
{"schema":"board-graph/independent-review/v1","board_sha256":"<digest>","schematic_sha256":"<digest>","builder":"<builder>","reviewer":"<independent reviewer>","checks":[{"passed":true,"observation":"<what the reviewer independently reproduced>","evidence":"board/reports/verification/reproduced.md"}]}
```

At Product judgment, the owner cites a receipt and the actual measurement
or use report for every identified unit. List every board revision tested
together in the product configuration. Use the exact firmware build or
`none`. A failed or incomplete unit remains on the graph for disposition.

```json
{"schema":"board-graph/product-acceptance/v1","verdict":"accepted","board_revisions":{"motor":"<tested revision>"},"units":[{"id":"<unit>","firmware":"none","checks":[{"passed":true,"observation":"<measured result and acceptance criterion>","evidence":"board/reports/bench/unit-1.md"}]}]}
```

The graph checks the declared verdicts, the cited files and the source
hashes for Verification. The reviewer and owner stand behind the content
of their observations.
