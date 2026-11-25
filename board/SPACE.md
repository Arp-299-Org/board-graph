# The board in 3D

Every position, height and direction in this repository uses these words.
Numbers below come from the saved board and its models. A model is evidence
about the model, not the part: confirm against a datasheet or calipers before
relying on one.

## Axes

| Axis | Direction | Range on the board |
|---|---|---|
| X | grows east | fill in from the board outline, in mm |
| Y | grows south, as in KiCad | fill in from the board outline, in mm |
| Z | grows out of the front face | the board's thickness, in mm |

## Words

| Word | Means | Never means |
|---|---|---|
| Front face, back face | The board's two sides: F.Cu (+Z) and B.Cu (-Z) | An edge |
| North, south, east, west edge | The four board edges in KiCad's top view | A face |
| Height | Distance along Z from the face a part mounts on | Length or width in the board's plane |
| Thickness | The enclosure body's total along Z | A part's height |
| Overhang | How far a part passes a board edge, along X or Y | Height |

"Back", "top" and "bottom" alone are not used: "back" can mean a face or an
edge, and that ambiguity has caused errors before.

## In the user's hands

How the finished product is held or mounted, which face points at the user,
and which edge is north as the user sees it. Document the finger/thumb pitch
and touch clearance around every tactile control (buttons, D-pads, knobs,
switches) against tall parts, sockets, overhanging mating modules or displays,
and board edges. Cards that check which way a part faces and whether a user can
reach it cleanly (Placement, Visual review) read this.

## Where the parts sit

| Part | Face | In the plane | Height |
|---|---|---|---|
| One row per part whose position, face or height matters to the enclosure or the user, with its source (datasheet page, drawing, measurement) | | | |

Run `py -3 scripts/board_space.py` for every part's model height. It moves
each model by its saved scale, rotation and offset first, as KiCad draws it,
and says which models are missing.
