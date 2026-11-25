# Electrical audit

The rules this board's electrical audit computes from the saved board, each
with its basis. The Constraints card keeps this file current; the Routing
batch, Board checks and Verification cards check the board against it. The
script is the one the board description names under `electrical_audit`; until
the board has one, that key is null and the checks say the audit was skipped.
A skipped audit proves nothing about these rules.

## Intent

What the board's electrical design must serve, in a few sentences: the
signals that must stay quiet, the buses that must run at their rate, the
supplies that must hold their voltage. Every rule below exists for one of
these.

## Facts the rules rest on

The layer stack and what each layer carries; each bus with its clock rate and
how that rate was derived (firmware setting, library source, datasheet), and
whether it is a source calculation or a measurement. Name each source with
its page or file. Naming a layer GND does not prove an uninterrupted return
path: inspect the fills, holes and transitions.

## Net classes

Which nets are in which class, and any routed net in no class (no rule samples
it).

## Rules (each is a script check; a number in the report, not an opinion)

| # | Rule | Gate | Basis |
|---|---|---|---|
| R1 | One row per rule: what is measured and the limit | the number that passes | Sourced requirement (document and page), owner's decision (DECISIONS, date) or local choice (whose, since when) |

Every rule's basis is one of: a sourced requirement, the owner's decision, or a
local choice. A local choice is labeled as one; the first board's measurement
is what tests it.

## What the checks measure

Say, for each rule, what the script's model leaves out. These hold for the
tool's scripts as they are and for most audits like them:

- A track-to-track spacing does not cover pads, vias or dense fanout; those
  still need inspection against the copper-clearance rules.
- Curved tracks measured by their endpoint chord are not arc checks: inspect
  arcs separately.
- A sampled return path is an estimate: a clear result does not establish fill
  on both sides or model return current.
- A path length the model cannot establish (arcs, custom pads, contact through
  a zone) is a finding, not proof that the net is disconnected.
- A loop area or similar figure is a reproducible geometric estimate, not an
  electromagnetic simulation or a noise limit.

## Where subsystems meet

When a rule can be met only by breaking another, that is not the audit's call.
It is written up as a decision for the owner with both costs. The order the
owner weighs them in is recorded in `board/DECISIONS.md`; list it here with
the reason for each place.

## Method

Keep the audit script matched to the circuit, the datasheets, the firmware
interface settings and the rule table above; a changed part, pin or setting
reopens the Constraints card.
