# Contributing to board-graph (Fork-and-Evolve)

`board-graph` is built so hardware builders and their AI agents can improve the
engine safely while building real circuit boards.

## How the loop works

1. Fork `Arp-299-Org/board-graph` on GitHub, clone your fork locally (and on
   your KiCad machine or Linux container if using a remote runner), and install
   it in editable mode:
   ```sh
   pip install --no-build-isolation -e .
   ```
2. Point your board's `tools/board_graph_site.json` at `"one_machine": true`
   (or set `kicad_machine.tool_checkout` to your fork clone on the KiCad machine).
3. When a board hits a genuine tool bug or KiCad edge case, **do not hack around
   it inside the board repository**. Let the card record `needs_lead` with the
   exact failure receipt.
4. Switch into your `board-graph` fork and run the fix through `board-graph`'s
   own release checklist (`dev/TOOL_DEV_PROCESS.md`):
   - `triage`: inspect the real KiCad file format, IPC behavior, or CLI boundary
     first (`docs/KICAD_10_INVARIANTS.md`) instead of guessing.
   - `implement`: write one shared fix (never duplicate parsers or N+1 IPC loops)
     and add realistic contract tests.
   - `tool_checks`: verify hygiene, manifest integrity, workflow contracts, and
     the full test suite:
     ```sh
     python .github/scripts/check_hygiene.py
     python scripts/board_graph_tool.py check
     python dev/test_tool_dev.py
     ```
   - `cold_review`: check the full diff and make sure `README.md`, `AGENTS.md`,
     `docs/ROUTING_NOTES.md`, and `UPGRADING.md` match the new behavior.
5. Push to your fork and resume your board immediately on the upgraded engine.
6. Open a pull request from your fork back to `Arp-299-Org/board-graph`. GitHub
   Actions runs the full unit suite plus the live KiCad 10 + `Xvfb` Docker
   smoke so verified fixes merge cleanly upstream for everyone.

## Ground rules

- Keep scripts Python 3.9+ compatible (`from __future__ import annotations`).
- Keep `check_hygiene.py` limits clean (new files <= 1000 lines, new functions
  <= 120 lines, LF line endings, no dated chat transcripts or `TODO`/`FIXME`).
- Record every new KiCad 10 S-expression, IPC, or runner quirk in
  `docs/KICAD_10_INVARIANTS.md` alongside a regression test.
- No attribution footers or co-author trailers in commits.

## Model policy for upstream PRs

- Forks are free to experiment locally with any model, including models listed in
  `BANNED_MODELS` (`scripts/routing_run.py`).
- Upstream pull requests to `Arp-299-Org/board-graph` must reproduce reported
  failures on a supported provider. Pull requests that weaken `BANNED_MODELS`
  or work around failures caused by running a banned model on routing will not
  be merged.
