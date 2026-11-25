# Workflow acceptance

The same 32 disposable project scenarios passed **8 times on main, 12 on the routing milestone, and 32 on the candidate**. Run from the candidate checkout using `python .github/scripts/audit_workflow.py <checkout>`. The argument chooses the implementation under test. Each case creates new files and state. Copper provenance cases use actual Git commits. First-article observations are synthetic.

| Observable behavior | Baseline | Routing milestone | Candidate |
|---|---|---|---|
| Fresh review authorizes release | Pass | Pass | Pass |
| Unrelated edit preserves approval | Pass | Pass | Pass |
| Changed reviewed input blocks release | Fail | Pass | Pass |
| A changed cited report blocks release | Fail | Fail | Pass |
| A missing cited report blocks release | Fail | Fail | Pass |
| A passing inspection log cannot replace the required drawing | Fail | Fail | Pass |
| An explicitly optional output may be absent and later changes reopen review | Fail | Fail | Pass |
| Changed required reading invalidates the review | Fail | Fail | Pass |
| Legacy pass needs a review before enforced release | Fail | Pass | Pass |
| Owner authority survives a return | Pass | Pass | Pass |
| Changed prerequisites during an open attempt block completion | Fail | Pass | Pass |
| A stale attempt cannot close its successor | Pass | Pass | Pass |
| Committed record replay agrees with live position | Pass | Pass | Pass |
| Out-of-order arrival agrees with a clean replay | Fail | Fail | Pass |
| Divergent decisions at the same position cannot silently combine | Fail | Fail | Pass |
| Incomplete history blocks further work until its parent arrives | Fail | Fail | Pass |
| A conflicting copy of the same transition is detected | Fail | Fail | Pass |
| An open shared product decision blocks issuing dependent work | Fail | Fail | Pass |
| Changing a shared decision invalidates dependent boards | Fail | Fail | Pass |
| Tested first article can finish | Pass | Pass | Pass |
| Failed bench observation blocks release | Fail | Pass | Pass |
| Changed motor board blocks assembled acceptance | Fail | Fail | Pass |
| Changed firmware blocks assembled acceptance | Fail | Fail | Pass |
| Assembly must test the shared owner decision | Fail | Fail | Pass |
| Reviewed assigned copper passes the Git provenance gate | Pass | Pass | Pass |
| Git rejects mismatched review hash | Fail | Fail | Pass |
| Git rejects a missing independent verdict | Fail | Fail | Pass |
| Git preserves a pad MUST FIX | Fail | Fail | Pass |
| Git rejects a substituted named connection | Fail | Fail | Pass |
| Git requires both requested pad observations | Fail | Fail | Pass |
| A reasoned warning acceptance keeps the reviewed board | Pass | Pass | Pass |
| A warning without the reviewer disposition blocks adoption | Fail | Fail | Pass |

## Follow-up acceptance cases

The acceptance command now includes two additional cases for required and optional deliverables. The graph refuses a missing required drawing, accepts it once present and reopens the approval if it disappears. An explicitly optional overlay can be absent at sign-off. Adding the overlay later requires review.

The routing suite also exercises two interruptions. A deadline between assigned tasks keeps the reviewed prefix, names the unfinished tasks and records `needs_lead`. After graph acceptance and an interrupted push, `--retain-only` preserves the pending publication for both complete and partial batches. Resuming the accepted delivery publishes the same commit without another operator call.

## What happens across a failure

The graph detects two different recorded decisions claiming the same predecessor. Importing a newer receipt first keeps its workflow incomplete until the missing predecessor arrives. A separate handoff test runs a saved Mac response through a real graph database and checks the published board and record on a disposable Git remote.

The project test suites also exercise SQLite commit followed by an interrupted export, a lost SSH reply after server commit, a refused graph transition during delivery, and a failed push after an accepted delivery. After reconnecting, the desktop can recover the original record. The routing card holds a reviewed candidate in a private worktree until the graph accepts its evidence. Repeating a pending delivery uses that worktree. A change of actor, branch, board, task folder or database prevents publication from the wrong context.

The Git provenance gate and the routing download both check the original assignment, the exact saved PCB, every kept step, and the independent reviewer's recorded observations. Earlier named connections in a multi-step batch receive a final connectivity and pad-exit check. Eligible warnings require a numbered disposition and reason. The graph trusts the configured operator, reviewer and repository writers. Its receipts document their observations.

A live Mac delivery of independently reviewed copper with a matching remote hash and a `more_work` record remains outstanding. The offline comparison and project CI exercise workflow behavior. Actual operator performance, a physical product test, supplier confirmation and the owner's judgment require their respective real-world evidence.
