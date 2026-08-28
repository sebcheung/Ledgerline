---
name: split-phase-commits
description: Split the working-tree changes for a completed phase or slice of work into multiple isolated commits, grouped across separate feature branches so each feature/concern can be reviewed or reverted independently. Use when the user asks to "split this phase into commits/branches", "break this up by feature", "isolate these changes into separate branches", or after finishing a large multi-file implementation (like a spec phase) that touched several unrelated concerns and needs to land as several small commits instead of one giant one.
---

# Split Phase Commits

Break a large set of uncommitted (or newly implemented) changes into several small, logically isolated commits, each on its own branch, so every feature/concern can be reviewed, tested, and reverted independently.

## When to use this

- The user just finished a phase/slice of work (e.g. "Phase 2") that touched many files across several concerns (e.g. core logic, API routes, schemas, tests, docs, CI config).
- The user explicitly asks to split changes into commits and/or branches by feature.

Do not use this for a single small, already-cohesive change — just commit it normally.

## Hard rules (never break these)

1. **Never cite or sign a commit message.** No `Co-Authored-By:`, no `Generated with Claude Code`, no tool attribution, no emoji signature, nothing that identifies AI involvement. Commit messages read exactly like a human terse commit.
2. **Commit messages are short.** One sentence, or a couple of short comma-packed phrases. No multi-paragraph bodies, no bullet lists, no "Summary"/"Changes" sections. Aim for under ~72 characters where possible; never more than one line unless a second short clarifying clause is genuinely needed.
3. **Never use `--no-verify`** or otherwise bypass hooks/checks to force a commit through.
4. Never force-push, never rewrite already-pushed/shared history, never delete branches that hold work you didn't create this session.

## Process

1. **Inventory the diff.** Run `git status` and `git diff` (or `git diff --stat` first for a big change) to see every changed/new/deleted file. If there's a plan file or phase description in context, use it to understand what the intended feature boundaries were.

2. **Propose a grouping.** Cluster files into logical, independently-reviewable slices. Good boundaries look like:
   - One core module/concern at a time (e.g. `money.py` + its unit tests; `invariants.py` + its unit tests; `posting.py` + its integration tests).
   - Schemas/API layer as its own slice (or split further: errors/RFC7807 handling vs. routes vs. schemas) if they're substantial.
   - Test-harness/infra changes (conftest fixtures, pyproject/CI config) as an early foundational slice, since later slices' tests may depend on it.
   - Docs updates (DECISIONS.md, ARCHITECTURE.md) as their own slice, or folded into the last code slice if trivial.
   - Never split a single feature's implementation from the tests that exercise it — those land together.
   - Keep each slice buildable/testable in isolation where practical (i.e. don't split a module from something it imports that doesn't exist yet on that branch, unless stacking branches — see step 3).

   Present this grouping to the user as a short numbered list (branch name → one-line description → files) and get a quick go-ahead before creating branches, unless the user already specified the exact split.

3. **Decide the branch topology.** Two valid shapes — pick based on whether slices are independent or dependent:
   - **Independent slices** (no slice depends on another's code to compile/import): branch each slice off the same base commit (e.g. `main`) in parallel. Simplest, easiest to review/merge in any order.
   - **Dependent slices** (e.g. API routes need the core module from an earlier slice): stack branches — branch 2 off branch 1's tip, branch 3 off branch 2's tip, etc. Say explicitly which base each branch uses.

   Default to independent branches off the current base unless there's a genuine compile-time dependency forcing a stack.

4. **For each slice, in order:**
   - `git checkout -b <branch-name> <base>` from the correct base (see step 3).
   - Stage only that slice's files with explicit `git add <path>...` (never `git add -A`/`git add .`) — re-check `git status` after staging to confirm nothing extra slipped in.
   - Verify the staged diff is what you intend (`git diff --cached --stat`).
   - Commit with a short message per the hard rules above, passed via heredoc to avoid quoting issues:
     ```
     git commit -m "$(cat <<'EOF'
     short message here
     EOF
     )"
     ```
   - Return to the base branch (or leave checked out on the new branch, per user preference) before starting the next slice: `git checkout <base>`.

5. **Summarize.** After all slices are committed, list the branches created, each with its one-line commit message and file count, so the user can see the split at a glance. Do not push any branch unless the user explicitly asks.

## Commit message style examples

Good (short, no attribution):
- `Add Money value type with per-currency minor-unit exponents`
- `Add ordered FOR UPDATE locking to post_transaction`
- `Wire RFC 7807 error handlers into the API`
- `Add concurrency and deadlock-order tests, fix sign convention bugs`

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

## Notes

- If tests exist, prefer (when practical) to leave each branch in a state where its own relevant test subset passes — but don't block the split on a full suite run for every branch; that's a judgment call to flag to the user, not a hard gate.
- If the user has uncommitted changes in the working tree when this skill is invoked, treat the working tree as the source of truth for slicing (use `git add`/`git stash` per-slice as needed, not committed history rewriting).
- If a file logically belongs to two slices (e.g. `ledger/models/__init__.py` gets one new import per slice), split the file's own diff with `git add -p` rather than forcing the whole file into one slice.
