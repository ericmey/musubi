# Sequencing deviation (recorded per Yua 2026-07-14 00:28:44)

The `slice/art-001-artifact-generation-spikes` branch and the
`/Users/ericmey/Projects/musubi-worktrees/tama-art001-spike`
worktree were created on exact `origin/main` (79cd13e) BEFORE
the issue (#451) was created. The branch has no own commits
and is not tracking/pushed (per Yua 00:28:44 verification).

The deviation:
1. empty branch created at origin/main tip
2. empty worktree created from that branch
3. THEN issue #451 created, atomically claimed, and
   status:ready -> status:in-progress
4. THEN push of the existing exact-base branch (still
   no own commits; non-force)
5. THEN start the spike (first substantive commit will
   be tests/docs only, per Yua 00:24:28)

This is a sequencing deviation but NOT a content deviation:
- The branch is at exact `origin/main` (verified).
- No source, no test, no slice, no lock files have
  been committed.
- No host, no central ledger, no production deploy.
- Per Yua 00:28:44: "Do not delete/recreate it. Record
  the sequencing deviation: local empty branch/worktree
  created before issue, no mutation/commit/push."

This file is the record of that deviation.
