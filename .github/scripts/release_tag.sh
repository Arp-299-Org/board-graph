#!/usr/bin/env bash
# Tag the version the manifest names, when main has it and no tag does.
#
# The version is tools/board_graph_tool.json's. Its tag, v<version>, goes on the commit of main that set that
# version (the merge that brought it, or the commit itself when it was pushed straight to main), so the tag is on
# the commit a project's `board_graph_tool.py update` recorded even when later commits have landed. The tag's
# message is what `git tag -n99 v<version>` shows a project: the version, the subject of every commit since the tag
# before it, then what a project changes in its own files after taking it: the version's section of UPGRADING.md,
# which `board_graph_tool.py update` also prints. A version with no section there is not tagged.
#
#   release_tag.sh           make the tag and push it
#   release_tag.sh --print   say what it would do, and change nothing
set -euo pipefail

manifest=tools/board_graph_tool.json
version=$(python3 -c "import json, sys; print(json.load(open(sys.argv[1]))['version'])" "$manifest")
tag="v$version"

if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "$tag is already a tag, at $(git rev-list -n1 "$tag"): nothing to do"
  exit 0
fi

commit=$(git log -s --first-parent --diff-merges=first-parent -S"\"version\": \"$version\"" --format=%H -- "$manifest" | tail -1)
if [ -z "$commit" ]; then
  echo "no commit of this branch set the version to $version in $manifest" >&2
  exit 1
fi

after=$(python3 scripts/board_graph_tool.py notes --version "$version") || {
  echo "UPGRADING.md has no section for $version: say what a project changes, or that it is nothing" >&2
  exit 1
}

before=$(git describe --tags --abbrev=0 --match 'v[0-9]*' "$commit^" 2>/dev/null || true)
message=$(
  echo "board-graph $version"
  echo
  git log --no-merges --reverse --format='- %s' ${before:+"$before.."}"$commit"
  echo
  echo "After taking it, in the project's own files:"
  echo
  echo "$after"
)

echo "$tag -> $commit${before:+ (the tag before it: $before)}"
echo "$message"
if [ "${1:-}" = "--print" ]; then
  exit 0
fi
git tag -a "$tag" "$commit" -m "$message"
git push origin "refs/tags/$tag"
