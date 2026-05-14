# One-shot migrations

Project uses SQLAlchemy `create_all_tables()` (no Alembic — see
`docs/FUTURE_WORK.md` Phase 15). When a column type needs to change on a
table that already exists in production, the change is captured here as a
one-shot script. Run once against the live DB, then leave in tree for
audit.

## 2026-05 — github_review_id BIGINT

GitHub review IDs crossed int32 (>2.1B) on 2026-05-12 causing
`integer out of range` on insert. Column widened to `BIGINT`. ORM model
already declared `BigInteger` — this script reconciles existing rows.

Wiki ref: pragmatic-programmer/Steady-State — operational scripts that
ran against prod stay in the repo as evidence, not as `/tmp` ghosts.
