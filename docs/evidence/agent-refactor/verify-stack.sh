#!/usr/bin/env bash
# For each branch of the stack: ruff, format, pytest, the contract snapshot against
# the pin commit, and line counts. Prints one TSV row per branch.
# usage: verify-stack.sh BASE BRANCH...   (needs TEST_DATABASE_URL)
set -euo pipefail
repo=$(git rev-parse --show-toplevel)
pin=$(git -C "$repo" log --format=%h --diff-filter=A -1 -- services/agent/tests/snapshots/contract.json)
parent=$1
shift
printf 'branch\thead\truff\tformat\tpytest\tsnapshot_vs_pin\tsrc_lines\ttest_lines\tdiff_vs_parent\n'
row() {
	local ref=$1 parent=$2 dir
	dir=$(mktemp -d /tmp/verify-XXXX)
	git -C "$repo" worktree add -q --detach "$dir" "$ref"
	local agent=$dir/services/agent
	local ruff fmt tests snap src tst diff
	ruff=$(cd "$agent" && uv run -q --locked ruff check . >/dev/null 2>&1 && echo ok || echo FAIL)
	fmt=$(cd "$agent" && uv run -q --locked ruff format --check . >/dev/null 2>&1 && echo ok || echo FAIL)
	tests=$(cd "$agent" && uv run -q --locked pytest -q -p no:cacheprovider 2>&1 | tail -1 | sed 's/ in .*//')
	if [ -f "$agent/tests/snapshots/contract.json" ]; then
		snap=$(git -C "$repo" diff --quiet "$pin" "$ref" -- services/agent/tests/snapshots && echo same || echo CHANGED)
	else
		snap=none
	fi
	src=$(find "$agent/src" -name '*.py' -print0 | xargs -0 cat | wc -l)
	tst=$(find "$agent/tests" -name '*.py' -print0 | xargs -0 cat | wc -l)
	diff=$(git -C "$repo" diff --shortstat "$parent" "$ref" -- services/agent docs | sed 's/^ //')
	printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$ref" "$(git -C "$repo" rev-parse --short "$ref")" \
		"$ruff" "$fmt" "$tests" "$snap" "$src" "$tst" "$diff"
	git -C "$repo" worktree remove --force "$dir"
}
row "$parent" "$parent"
for branch in "$@"; do
	row "$branch" "$parent"
	parent=$branch
done
