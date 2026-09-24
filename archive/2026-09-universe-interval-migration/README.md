# Archived: universe_snapshots → universe_membership migration (completed)

**Historical record only. Not runnable, not wired into any workflow.**

This one-off migration converted the daily `universe_snapshots` table (one row
per symbol per day) into `universe_membership` change-intervals. It ran on
Neon in September 2026, verified equivalence for every observed date, and
dropped `universe_snapshots`. The interval table is now the source of truth and
is created by `src/db/migrations/0001_baseline.sql`.

Why it was archived in Phase 1a rather than kept in `scripts/`:

- Its workflow was `workflow_dispatch`-able and could run `DROP TABLE`. A
  completed destructive migration should not stay one click away.
- It called `apply_schema`, which no longer exists: normal code no longer runs
  DDL, and schema changes go through `scripts/migrate.py` and the
  "Database migrations" workflow.

The files are kept unchanged (apart from being moved) so the audit trail in
`docs/PHASE_1_REPORT.md` Addendum C still resolves to real code.
