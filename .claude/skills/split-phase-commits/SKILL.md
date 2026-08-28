---
name: split-phase-commits
description: Split the working-tree changes for a completed phase or slice of work into several feature branches, each containing multiple small isolated commits, merged back into the base sequentially — matching this repo's established history (see `feature/repo-scaffold`, `feature/config-observability`, etc.). Use when the user asks to "split this phase into commits/branches", "break this up by feature", "isolate these changes into separate branches", or after finishing a large multi-file implementation (like a spec phase) that touched several unrelated concerns and needs to land as several small commits instead of one giant one.
---

# Split Phase Commits

Break a large set of uncommitted (or newly implemented) changes into several feature branches, **each containing multiple small atomic commits**, merged back into the base one branch at a time — so the final history reads the same way this repo's earlier phases do: a chain of `Merge <topic>` commits, each preceded by 2-4 single-concern commits.

**Check `git log --oneline --graph <base>` before starting.** This repo already has a convention (feature branch → several small commits → `--no-ff` merge back into the base → next branch starts from the updated base). Match it. If a different repo has no such convention, default to the same shape described below anyway — it's the safer default.

## When to use this

- The user just finished a phase/slice of work (e.g. "Phase 2") that touched many files across several concerns (e.g. core logic, API routes, schemas, tests, docs, CI config).
- The user explicitly asks to split changes into commits and/or branches by feature.

Do not use this for a single small, already-cohesive change — just commit it normally.

## Hard rules (never break these)

1. **Never cite or sign a commit message.** No `Co-Authored-By:`, no `Generated with Claude Code`, no tool attribution, no emoji signature, nothing that identifies AI involvement. Commit messages read exactly like a human terse commit.
2. **Commit messages are short.** One sentence, or a couple of short comma-packed phrases. No multi-paragraph bodies, no bullet lists, no "Summary"/"Changes" sections. Aim for under ~72 characters where possible; never more than one line unless a second short clarifying clause is genuinely needed.
3. **Every branch gets multiple commits, not one.** A branch that bundles everything for a feature area into a single commit defeats the point of splitting — each commit within the branch should isolate one tightly-scoped concern (one module, one file, or a couple of directly-related files). A branch with a genuinely single, indivisible file is the only exception (one commit is fine there — don't invent an artificial second commit).
4. **Never use `--no-verify`** or otherwise bypass hooks/checks to force a commit through.
5. Never force-push, never rewrite already-pushed/shared history, never delete branches that hold work you didn't create this session.

## Process

1. **Inventory the diff.** Run `git status` and `git diff --stat` to see every changed/new/deleted file. Check `git log --oneline --graph <base>` to see the repo's existing branch/commit granularity and copy it. If there's a plan file or phase description in context, use it to understand the intended feature boundaries.

2. **Propose a two-level grouping** and present it to the user before creating anything (unless they already specified the exact split):
   - **Branches** = feature areas, in dependency order (foundational/infra first, e.g. test harness or config; core domain logic next; API/integration layer next; a dedicated tests branch if the phase has substantial test-only additions not naturally paired with one implementation branch, matching this repo's pattern of a trailing `tests-ci`-style branch; docs last).
   - **Commits within each branch** = one concern per commit. Split by file where files are separately reviewable (e.g. `errors.py` in one commit, `money.py` in the next); only bundle a small file with its direct unit test in one commit when they're trivially small and inseparable in review value. Don't force an artificial split of one cohesive, indivisible file into multiple meaningless commits.
   - Prefer keeping bulk test additions (integration/property/concurrency suites) in their own branch and commits *after* the implementation they exercise lands, one commit per test file or tightly-related group of test files — this mirrors how this repo already separates `tests-ci` from the implementation branches that precede it.

   Present the plan as a nested list: branch name → one-line branch purpose → ordered list of (commit message → files). Get a quick go-ahead unless the user already specified the split.

3. **Decide the branch base chain.** Branches merge back sequentially, each one starting from the updated base *after* the previous branch's merge — not left sitting side-by-side as permanent divergent branches. This means:
   - Branch 1 starts at `<base>` (e.g. `main`).
   - After branch 1's commits are done, merge it into `<base>` with `--no-ff`.
   - Branch 2 starts from the now-updated `<base>` (which includes branch 1's work).
   - Repeat for every remaining branch.

   Only fall back to permanently-independent parallel branches (no merging back) if the user explicitly asks for unmerged/reviewable-in-parallel branches instead of a linear integrated history.

4. **For each branch, in order:**
   - `git checkout -b <branch-name> <base>` — `<base>` is the trunk branch, updated after each prior merge (see step 3).
   - For each planned commit in this branch: stage only that commit's files with explicit `git add <path>...` (never `git add -A`/`git add .`), confirm with `git status`/`git diff --cached --stat`, then commit with a short message via heredoc:
     ```
     git commit -m "$(cat <<'EOF'
     short message here
     EOF
     )"
     ```
   - Once every commit for this branch is made, merge it back:
     ```
     git checkout <base>
     git merge --no-ff <branch-name> -m "$(cat <<'EOF'
     Merge <topic>
     EOF
     )"
     ```
   - Move on to the next branch, branching from `<base>` again (now containing the just-merged work).

5. **Summarize.** After every branch is merged, show `git log --oneline --graph <base>` (or the relevant range) so the user can see the full chain of merges and commits at a glance. Do not push anything unless the user explicitly asks.

## Commit message style examples

Good (short, no attribution):
- `Add Money value type with per-currency minor-unit exponents`
- `Add ordered FOR UPDATE locking to post_transaction`
- `Wire RFC 7807 error handlers into the API`
- `Add concurrency and deadlock-order tests`
- `Merge core domain errors, money, and invariants`

Bad — too long / has a body:
```
Add Money value type

This commit introduces the Money value type which handles minor units
and validates currency codes according to ISO 4217...
```

Bad — attribution/signature (never do this):
```
Add Money value type

Co-Authored-By: Claude <noreply@anthropic.com>
```

Bad — a whole feature area squashed into one commit on its own branch (defeats the purpose; split it):
```
feature/core-domain: one commit "Add errors, money, and invariants" touching 3 unrelated files
```

## Notes

- If tests exist, prefer (when practical) to leave the branch in a state where its own relevant test subset passes by the time it's merged — but don't block the whole split on a full suite run after every single commit; that's a judgment call to flag to the user, not a hard gate.
- If the user has uncommitted changes in the working tree when this skill is invoked, treat the working tree as the source of truth for slicing (use `git add`/`git stash` per-slice as needed, not committed history rewriting).
- If a file logically belongs to two commits (e.g. `ledger/models/__init__.py` gets one new import per feature), split the file's own diff with `git add -p` rather than forcing the whole file into one commit.
- If the split needs to be redone after already being committed to local (unpushed) branches, it's fine to reset the trunk pointer back and redo — e.g. `git checkout -b tmp <tip-with-everything>; git reset --mixed <base>` recovers the full working-tree diff against `<base>` in one shot, then delete the old branches and start over from step 4.
