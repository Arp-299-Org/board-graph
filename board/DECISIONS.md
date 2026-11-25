# Board decisions

The owner's decisions for this board and why the board is like this. The
Decisions card writes this file; every other card reads it, and a critic
reports anything under review that contradicts it.

## How a decision goes in

- Only the owner decides product behavior, parts, placement, cost, purchases
  and tradeoffs. A line goes in after the owner adopts it, in the owner's
  words, with the date.
- Write what is true now. When a decision changes, rewrite its line; git
  keeps the old one.
- Each decision says why, so a later agent can tell when it no longer holds.
- Numbers name their source: a datasheet page, a measurement (who, when, with
  what), or "local choice" with whose.
- A question still open is an open issue in `tools/board_graph.json`, with the
  card it must close before. It is not a decision.

## Why it is like this

One bullet per decision, the subject in bold: what was decided, why, the
owner's words and the date.

## Parts on hand

What the owner already has for these boards (modules, displays, cables), with
quantities. The Order card lists what to buy against this.

## Orientation preview

Gate G11 of `board/ORDER_PACKAGE_CONTRACT.md`: for each order, the date and
the owner's result of looking at the assembler's placement preview for every
polarized or orientation-sensitive part.

## Waivers

Each accepted exception to a check: the check, the item, why, the owner's
words and the date. The waiver file the board description names holds the
same exceptions as data for the scripts.
