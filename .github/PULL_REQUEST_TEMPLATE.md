## Summary

Describe what changed and why. Link the affected card, check, or KiCad behavior.

## Failure Receipt / Repro

Paste the exact error output, failing check, or minimal reproduction steps that triggered this change.

## Verification Checklist

- [ ] `python .github/scripts/check_hygiene.py`
- [ ] `python scripts/board_graph_tool.py check`
- [ ] `python dev/test_tool_dev.py`
- [ ] Added or updated regression tests (and verified Linux Docker smoke if KiCad/IPC/runner code changed)
- [ ] Updated `docs/KICAD_10_INVARIANTS.md` or docs if KiCad behavior or CLI usage changed

## Model Policy

- [ ] This PR does not remove or weaken `BANNED_MODELS`, and any failure or routing issue it addresses was reproduced on a supported routing provider (forks are free to experiment with banned models locally, but upstream PRs working around banned-model routing behavior are not accepted).
