# backend/database/models.py
#
# SQLAlchemy ORM table definitions for the structured persistence layer.
#
# TABLES:
#   PRReviewRecord   — one row per PR review run
#   FindingRecord    — one row per finding within a review
#
# DESIGN DECISIONS:
#
# 1. UUID PRIMARY KEYS (from Storage-Engines.md wiki):
#    "Use UUIDs for distributed-safe primary keys."
#    Integer auto-increment IDs create a bottleneck: every insert must hit the
#    sequence. UUIDs are generated client-side (no DB round-trip) and work
#    safely across multiple application instances. We generate them in Python
#    (uuid.uuid4()) not in the DB, so the app controls the ID before insert.
#
# 2. COMPOSITE INDEX ON (repo_full_name, severity):
#    (From Storage-Engines.md wiki, "Index Design"):
#    "Index the columns you filter on, in the order you filter them."
#    The most common query is: "all HIGH+ findings for repo X in last 30 days"
#    That's WHERE repo_full_name = ? AND severity IN (?, ?).
#    A composite index on (repo_full_name, severity) serves this query in O(log n)
#    instead of a full table scan.
#    ORDER: repo_full_name first (higher cardinality prefix = better selectivity).
#
# 3. ENUM AS VARCHAR:
#    (From Production-Hardening.md wiki, "Operational Flexibility"):
#    Storing enum values as VARCHAR rather than native DB ENUM types means
#    we can add new enum values without a DB migration.
#    Native ENUM requires ALTER TYPE in Postgres — a DDL operation that takes
#    an exclusive lock. VARCHAR avoids that operational risk.
#    Trade-off: no DB-level enum validation. We enforce that in the Python layer
#    (ReviewStatus, FindingSeverity etc. are validated before insert).
#
# 4. RELATIONSHIP LOADING:
#    FindingRecord has a FK to PRReviewRecord.
#    We use lazy="selectin" so that when we load a PRReviewRecord, SQLAlchemy
#    automatically loads its findings in a second SELECT (not N+1 queries).
#    "selectin" is the async-safe loading strategy — "lazy" loading (the default)
#    does not work with async sessions.
#    (Storage-Engines.md: "N+1 queries are the #1 async ORM footgun.")

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.postgres import Base


# ---------------------------------------------------------------------------
# PRReviewRecord
#
# One row per PR review run.
# A review run = one execution of the LangGraph review workflow.
# If the same PR is re-reviewed (e.g. after a new commit), a new row is created.
# ---------------------------------------------------------------------------
class PRReviewRecord(Base):
    """
    Persisted record of a completed (or failed) PR review run.

    This is the DURABLE record of what happened. Redis holds the live status
    (ephemeral, TTL-based). Postgres holds the permanent history.
    (Polyglot-Persistence.md: "Redis = ephemeral cache, Postgres = audit log.")
    """

    __tablename__ = "pr_review_records"

    # ---------------------------------------------------------------------------
    # Primary key: UUID generated in Python before insert.
    # WHY UUID AND NOT SERIAL?
    # (Storage-Engines.md wiki: "UUID PKs are distributed-safe.")
    # We generate the ID in the orchestrator before starting the LangGraph graph.
    # This lets us log the review_id in all nodes BEFORE the DB insert happens.
    # With SERIAL, we'd need to insert first, then get the ID back — awkward with async.
    # ---------------------------------------------------------------------------
    id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="Primary key — workflow_id format: owner/repo:pr_number:commit_sha (up to 128 chars).",
    )

    # The "owner/repo" string. e.g. "acme-corp/payment-service"
    # Used to scope all queries to a single repository.
    repo_full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,   # standalone index for queries filtering only by repo
        comment="GitHub repository full name in 'owner/repo' format.",
    )

    # The PR number on GitHub. e.g. 42
    pr_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="GitHub pull request number within the repository.",
    )

    # The PR title at review time. Stored for display in the dashboard.
    pr_title: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        default="",
        comment="PR title at the time of review.",
    )

    # The full SHA of the commit that triggered this review.
    # Stored so we can correlate review results with specific commits.
    head_commit_sha: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="",
        comment="Git commit SHA of the PR head at review time.",
    )

    # MD5 of the diff text. Used for deduplication.
    # If we receive two webhooks for the same diff, the hash lets us skip re-review.
    # WHY MD5 AND NOT SHA256?
    # This is for deduplication, not security. MD5 is fast and 16 bytes.
    # We are not defending against adversarial hash collisions here — just
    # checking if the diff content is identical. MD5 is fine.
    diff_hash: Mapped[str] = mapped_column(
        String(32),
        nullable=True,
        comment="MD5 of the PR diff for deduplication. Not security-critical.",
    )

    # The final review verdict. Stored as string (see #3 ENUM AS VARCHAR above).
    # Values: "approve", "request_changes", "needs_human_review"
    verdict: Mapped[str] = mapped_column(
        String(32),
        nullable=True,
        comment="Final review verdict (approve / request_changes / needs_human_review).",
    )

    # The review status at completion.
    # Values from ReviewStatus enum: "pending", "in_progress", "completed", "failed"
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        comment="Review lifecycle status (pending / in_progress / completed / failed).",
    )

    # Weighted average confidence across all findings (0.0 to 1.0)
    overall_confidence: Mapped[float] = mapped_column(
        Float,
        nullable=True,
        comment="Weighted average agent confidence across all findings.",
    )

    # Whether this review was routed to the human approval queue
    needs_human_review: Mapped[bool] = mapped_column(
        # Stored as INTEGER (0 or 1) — SQLite-compatible, works in Postgres too.
        Integer,
        nullable=False,
        default=0,
        comment="1 if this review was routed to the HITL approval queue.",
    )

    # Why it went to HITL. Empty string if it didn't.
    human_review_reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        comment="Reason this review was routed to human review. Empty if auto-posted.",
    )

    # The GitHub review ID returned by POST /pulls/{n}/reviews.
    # Populated AFTER a successful GitHub API response (Phase 8).
    # NULL for HITL-routed reviews that were never posted to GitHub.
    #
    # WIKI: DDIA / Transactions-and-Isolation
    #   "Atomicity: only write this ID after GitHub confirms the review."
    #   -> If the GitHub API call fails, save_review() is never called,
    #      so this column never gets a partial value.
    github_review_id: Mapped[int | None] = mapped_column(
        nullable=True,
        default=None,
        comment="GitHub review ID from the API response. NULL if routed to HITL or not yet posted.",
    )

    # Timestamp this record was created.
    # stored as UTC-aware datetime.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="UTC timestamp of when this review run was created.",
    )

    # Timestamp of last update (set when status changes, verdict set, etc.)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        comment="UTC timestamp of last update to this record.",
    )

    # ---------------------------------------------------------------------------
    # Relationship: one PRReviewRecord -> many FindingRecords
    #
    # lazy="selectin": when we load a PRReviewRecord, SQLAlchemy issues a second
    # SELECT to fetch all related FindingRecords. This is the async-safe strategy.
    # (Storage-Engines.md: "selectin is the async-safe loading strategy".)
    # Alternative "lazy" (default) issues the second SELECT lazily — which
    # does not work in an async context (no event loop to await the query).
    # ---------------------------------------------------------------------------
    findings: Mapped[list["FindingRecord"]] = relationship(
        "FindingRecord",
        back_populates="review",
        lazy="selectin",
        cascade="all, delete-orphan",
        # Note: comment= is not a valid kwarg for relationship(); see column comments above.
    )

    def __repr__(self) -> str:
        return (
            f"<PRReviewRecord id={self.id} repo={self.repo_full_name} "
            f"pr={self.pr_number} verdict={self.verdict} status={self.status}>"
        )

    @staticmethod
    def compute_diff_hash(diff: str) -> str:
        """
        Computes the MD5 hash of a diff string for deduplication.

        Usage:
            record.diff_hash = PRReviewRecord.compute_diff_hash(pr_diff)
        """
        return hashlib.md5(diff.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# FindingRecord
#
# One row per finding within a PR review.
# A finding = one specific issue identified by one specialist agent.
# ---------------------------------------------------------------------------
class FindingRecord(Base):
    """
    A single code issue found by a specialist agent during a PR review.

    Each PRReviewRecord has zero or more FindingRecords (one per issue found).
    """

    __tablename__ = "finding_records"

    # UUID primary key (same rationale as PRReviewRecord)
    id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key.",
    )

    # FK to the review that produced this finding.
    # ON DELETE CASCADE: if the review is deleted, its findings are deleted too.
    review_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("pr_review_records.id", ondelete="CASCADE"),
        nullable=False,
        comment="FK to the parent PRReviewRecord.",
    )

    # Denormalized repo name for efficient querying without joining.
    # (From Storage-Engines.md: "Denormalize hot query paths when join cost exceeds
    # storage cost." The list_findings_for_repo() query filters by repo_full_name.
    # Doing a JOIN to pr_review_records on every such query is wasteful.)
    repo_full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Denormalized repo name for efficient per-repo queries (no JOIN needed).",
    )

    # Which agent produced this finding.
    # Values: "security", "quality", "test", "docs"
    agent_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Specialist agent that produced this finding.",
    )

    # Finding severity. Stored as string (ENUM AS VARCHAR rationale above).
    # Values: "critical", "high", "medium", "low"
    severity: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Severity level: critical / high / medium / low.",
    )

    # Finding category.
    # Values: "security", "quality", "test_coverage", "documentation", "performance"
    category: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Category of the issue: security / quality / test_coverage / documentation.",
    )

    # One-sentence description of the issue.
    summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="One-sentence summary of the finding.",
    )

    # The file path where the issue was found. Nullable (e.g. repo-level issues).
    file_path: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="File path where the issue was found. Null for repo-level issues.",
    )

    # Line number range.
    line_start: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="First line of the relevant code range.",
    )
    line_end: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Last line of the relevant code range.",
    )

    # Agent's suggested fix. May be None if the agent didn't produce a suggestion.
    suggestion: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Concrete fix suggestion from the agent, or null.",
    )

    # Agent's confidence in this specific finding (0.0 to 1.0).
    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.5,
        comment="Agent's confidence in this finding (0.0 to 1.0).",
    )

    # When this finding was recorded.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="UTC timestamp of when this finding was recorded.",
    )

    # ---------------------------------------------------------------------------
    # Back-reference to the parent review
    # ---------------------------------------------------------------------------
    review: Mapped["PRReviewRecord"] = relationship(
        "PRReviewRecord",
        back_populates="findings",
    )

    # ---------------------------------------------------------------------------
    # INDEXES
    #
    # Composite index on (repo_full_name, severity):
    # (From Storage-Engines.md wiki: "Index the columns you filter on,
    #  in the order you filter them.")
    #
    # The hottest query is list_findings_for_repo(repo, min_severity="high"):
    #   WHERE repo_full_name = ? AND severity IN ('critical', 'high')
    #
    # With this index, Postgres does an index seek on repo_full_name first
    # (high selectivity — prunes most of the table), then filters severity
    # within that slice (much smaller set). Without it: full table scan.
    #
    # We put repo_full_name first because it has higher cardinality
    # (many repos) than severity (only 4 values). Higher cardinality prefix
    # = better index selectivity.
    # ---------------------------------------------------------------------------
    __table_args__ = (
        Index(
            "ix_finding_repo_severity",
            "repo_full_name",
            "severity",
            # comment= is not valid for Index(); intent documented in the block comment above.
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<FindingRecord id={self.id} severity={self.severity} "
            f"category={self.category} file={self.file_path}>"
        )


# ---------------------------------------------------------------------------
# RepoFileIndexRecord
#
# Tracks which files have been embedded into Qdrant and at what SHA.
# This is the FRESHNESS TABLE — the bridge between the immutable primary
# source (GitHub file content) and the derived store (Qdrant embeddings).
#
# DESIGN RATIONALE (Derived-Data-Systems.md wiki):
#   "Secondary indexes can always be rebuilt from the primary source."
#   Qdrant is the derived store. GitHub is the source of truth.
#   This table records: "we last embedded file X at blob SHA Y."
#   On next ingestion: if current SHA == recorded SHA, skip (fresh).
#   If current SHA != recorded SHA, re-embed (stale).
#
# NEW TABLE — ADDITIVE CHANGE (Encoding-and-Schema-Evolution.md wiki):
#   "Adding a new table is always safe — it cannot break existing code."
#   create_all() in main.py lifespan will create this table on next startup.
#   No Alembic migration needed.
#
# PRIMARY KEY DESIGN (demo-day-readiness Bug #1):
#   id = "{repo_full_name}:{file_path}" e.g. "octocat/hello-world:src/auth.py"
#   Max expected length: ~30 (repo) + 1 (:) + ~80 (path) = ~111 chars.
#   VARCHAR(128) is sufficient with headroom. NOT VARCHAR(36) — that was Bug #1.
#   We assert len(id) <= 128 in freshness.py before insert.
# ---------------------------------------------------------------------------
class RepoFileIndexRecord(Base):
    """
    Tracks per-file embedding status for freshness checking.

    One row per (repo, file_path) combination. Updated on each successful
    embedding run. Used by ingestion.py to skip unchanged files.
    """

    __tablename__ = "repo_file_index"

    # Primary key: "{repo_full_name}:{file_path}"
    # Composite natural key — no surrogate UUID needed (one row per file per repo).
    id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
        comment="Composite PK: '{repo_full_name}:{file_path}'. Max 128 chars.",
    )

    # The repository this file belongs to. e.g. "octocat/hello-world"
    # Indexed: the hottest query is get_stale_files(repo=X) which filters by this.
    repo_full_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
        comment="GitHub repository full name in 'owner/repo' format.",
    )

    # Relative file path within the repo. e.g. "src/auth.py"
    file_path: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Relative path to the file within the repository.",
    )

    # GitHub blob SHA of the file content at time of embedding.
    # 40 hex characters (SHA-1). GitHub uses SHA-1 for object IDs.
    # If current GitHub SHA != this value, file is stale and needs re-embedding.
    file_sha: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="GitHub blob SHA of the embedded file version. Compare to detect staleness.",
    )

    # When this file was last embedded. UTC-aware.
    # Useful for debugging (how stale is the index?) and for future
    # time-based freshness policies (e.g. re-embed files older than 30 days).
    embedded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="UTC timestamp when this file was last embedded into Qdrant.",
    )

    # How many chunks were created from this file.
    # Currently always 1 (file-as-unit strategy from RAG-Architecture.md wiki).
    # Stored for future use: if we switch to sub-file chunking, this tells us
    # how many Qdrant points to expect per file.
    chunk_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        comment="Number of vector chunks generated from this file (currently 1, file-as-unit).",
    )

    def __repr__(self) -> str:
        return (
            f"<RepoFileIndexRecord repo={self.repo_full_name} "
            f"path={self.file_path} sha={self.file_sha[:8]}>"
        )
