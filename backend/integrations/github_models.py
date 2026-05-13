# backend/integrations/github_models.py
#
# Pydantic models that represent GitHub REST API requests and responses.
#
# WHY A SEPARATE FILE FROM github_client.py?
#
# WIKI: Clean Architecture / Single-Responsibility-Principle
#   Each module should have one reason to change.
#   - This file changes when the GitHub API shape changes (new fields, deprecations).
#   - github_client.py changes when retry logic, timeouts, or HTTP details change.
#   - They change for different reasons -> they belong in different files.
#
# WIKI: Clean Architecture / Boundary-Lines.md
#   "Data that crosses an architectural boundary should be plain data structures."
#   -> These models are the data that crosses the boundary between the GitHub
#      API (external world) and the orchestrator (our domain).
#   -> They are Pydantic models (validated, typed) — not dicts.
#   -> This is the anti-corruption layer: we control what fields we care about,
#      and we give them names that make sense in OUR domain, not GitHub's.
#
# REUSE NOTE:
#   Phase 7  uses: PRMetadata, PRFile
#   Phase 8  uses: PRMetadata, PRFile, PostReviewPayload, PostReviewResponse
#   Phase 10 uses: PostReviewResponse (for audit logging review IDs)
#
# All models in this file are deliberately forward-complete (Phase 8 models
# are defined now even though they are not wired in yet). This is because
# adding a model later would require touching this file AND github_client.py
# at the same time — two reasons to change on the same ticket.
# Better to define the shape now when the design is fresh.

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# =============================================================================
# INBOUND MODELS
# Data we READ from GitHub (GET requests)
# =============================================================================

class PRMetadata(BaseModel):
    """
    Core metadata about a pull request.

    Retrieved via GET /repos/{owner}/{repo}/pulls/{pull_number}

    We deliberately capture ONLY the fields we use. GitHub returns 100+ fields.
    Capturing them all would create a fat dependency on GitHub's schema —
    any field rename or removal would break us even if we don't use it.

    WIKI: Clean Architecture / Interface-Segregation-Principle
      "Users should not be forced to depend on interfaces they do not use."
      -> We only capture the 7 fields our agents and orchestrator actually need.
    """

    # The PR number. e.g. 42
    # Maps to: pull_request.number
    number: int

    # The PR title. e.g. "feat: add retry logic to payment service"
    # READ BY: docs_agent (checks that title follows conventional commits format)
    #          aggregate_results (included in HITL queue entry)
    title: str

    # The PR description body. May be empty string ("") if author didn't write one.
    # READ BY: docs_agent (checks if description explains the why, not just the what)
    body: str = ""

    # GitHub login of the PR author. e.g. "jsmith"
    # Maps to: pull_request.user.login
    # READ BY: post_review node (to @mention in HITL comment if needed)
    author_login: str = Field(alias="user_login")

    # The full SHA of the HEAD commit on this PR branch.
    # e.g. "a3f8c1d9e2b4f6a8c0d2e4f6a8b0c2d4e6f8a0b2"
    # READ BY: post_review node (GitHub API requires commit SHA when posting a review)
    head_sha: str = Field(alias="head_commit_sha")

    # The base branch this PR targets. Usually "main" or "develop".
    # READ BY: quality_agent (checks branch naming conventions)
    base_branch: str = Field(alias="base_branch_name")

    # Number of changed files. Used for early-exit optimization:
    # if changed_files_count == 0, skip agents (nothing to review).
    # Maps to: pull_request.changed_files
    changed_files_count: int = 0

    # Allow populating model fields by their alias name (from GitHub JSON keys).
    # We also allow the field name itself (for constructing in tests).
    model_config = {"populate_by_name": True}

    @classmethod
    def from_github_response(cls, data: dict) -> "PRMetadata":
        """
        Constructs PRMetadata from the raw GitHub API JSON response.

        GitHub's response has nested structures (e.g., data["user"]["login"]).
        This classmethod flattens them into the clean PRMetadata shape.

        We do the transformation here (in the model layer) rather than in
        the client layer, because the transformation is about understanding
        the GitHub schema — it belongs with the model definitions.
        """
        return cls(
            number=data["number"],
            title=data.get("title", ""),
            body=data.get("body") or "",  # GitHub returns null for empty body
            user_login=data.get("user", {}).get("login", "unknown"),
            head_commit_sha=data.get("head", {}).get("sha", ""),
            base_branch_name=data.get("base", {}).get("ref", "main"),
            changed_files_count=data.get("changed_files", 0),
        )


class PRFileStatus(str, Enum):
    """
    The status of a file in a PR.

    Maps to the "status" field in GitHub's list-pull-request-files response.
    Defined as an Enum so security_agent can check for specific statuses
    (e.g., RENAMED files might bypass code review gates).
    """
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    RENAMED = "renamed"
    COPIED = "copied"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class PRFile(BaseModel):
    """
    A single file changed in a pull request.

    Retrieved via GET /repos/{owner}/{repo}/pulls/{pull_number}/files

    WIKI: DDIA / Data-System-Architecture-Patterns
      "Application code stitches together specialized tools."
      -> PRFile bridges GitHub's file list to our test_agent's coverage check:
         test_agent checks if every modified .py file has a corresponding test.
         Without this model, that check would require parsing the diff itself.
    """

    # The full file path relative to the repo root. e.g. "src/payments/processor.py"
    # READ BY: test_agent (checks for corresponding test file)
    #          security_agent (flags security-sensitive files: auth.py, settings.py)
    filename: str

    # How this file changed in the PR.
    # READ BY: quality_agent (REMOVED files may need issue references)
    status: PRFileStatus = PRFileStatus.MODIFIED

    # Lines added in this file.
    # Useful for detecting excessively large PRs (quality check).
    additions: int = 0

    # Lines removed in this file.
    deletions: int = 0

    # Total changes (additions + deletions).
    changes: int = 0

    # The patch (diff) for this specific file.
    # Optional because binary files and files exceeding GitHub's diff limit
    # do not have a patch field in the response.
    # READ BY: agents (fallback if we cannot get the full unified diff)
    patch: Optional[str] = None

    @classmethod
    def from_github_response(cls, data: dict) -> "PRFile":
        """
        Constructs PRFile from a single entry in GitHub's files list response.
        """
        return cls(
            filename=data["filename"],
            status=PRFileStatus(data.get("status", "modified")),
            additions=data.get("additions", 0),
            deletions=data.get("deletions", 0),
            changes=data.get("changes", 0),
            patch=data.get("patch"),  # may be None for binary files
        )


# =============================================================================
# OUTBOUND MODELS
# Data we SEND to GitHub (POST requests)
# Wired in Phase 8 but defined now (see module docstring for why).
# =============================================================================

class ReviewEvent(str, Enum):
    """
    The GitHub review event type.

    Determines whether the review approves, requests changes, or just comments.

    WIKI: Production-Hardening.md
      "Each guardrail is independent. Each can block."
      -> APPROVE and REQUEST_CHANGES are the two verdicts that matter.
         COMMENT is the safe fallback when confidence is borderline but
         we don't want to block the PR (Phase 9 HITL will decide).
    """
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    COMMENT = "COMMENT"


class ReviewComment(BaseModel):
    """
    An inline review comment on a specific line in a specific file.

    Maps to the "comments" array in GitHub's POST /pulls/{pull_number}/reviews.

    WIKI: Agentic Design Patterns — Tool Use pattern
      "The agent calls external functions."
      -> post_review is the agent's tool call to GitHub.
      -> ReviewComment is the structured payload the tool expects.
    """

    # The file path to comment on. Must match the exact filename from PR files list.
    path: str

    # The line number in the diff to attach this comment to.
    # GitHub counts from the first line of the diff hunk, not the file.
    # A value of 0 means the comment is on the file-level, not a specific line.
    line: int

    # The comment text. Markdown supported.
    body: str

    # Optional: the side of the diff this comment is on.
    # "RIGHT" = new version of the file (the added lines). Usually what we want.
    # "LEFT"  = old version of the file (the removed lines).
    side: str = "RIGHT"


class PostReviewPayload(BaseModel):
    """
    The full payload sent to GitHub's POST /repos/{owner}/{repo}/pulls/{pr}/reviews.

    Built by post_review node (Phase 8) from the aggregate_results output.
    Defined here now so Phase 8 only needs to import and fill it in.
    """

    # The HEAD commit SHA this review applies to.
    # Required by GitHub API. If this SHA is stale (new commits pushed since),
    # GitHub will warn the reviewer but still accept the review.
    commit_id: str

    # The top-level review body text.
    # Summary of all findings: "Found 3 issues: 1 HIGH, 2 MEDIUM"
    body: str

    # Whether this review approves, requests changes, or just comments.
    event: ReviewEvent

    # Inline comments on specific lines. May be empty list.
    # Each comment maps to one AgentFinding with a file_path and line_number.
    comments: list[ReviewComment] = Field(default_factory=list)


class PostReviewResponse(BaseModel):
    """
    GitHub's response to a successful POST /pulls/{pr}/reviews.

    We persist the review_id in the PRReviewRecord so we can later:
    - Dismiss a review (if the human reviewer overrides)
    - Link back to it from the HITL dashboard (Phase 9)

    WIKI: DDIA / Transactions-and-Isolation
      "Atomicity: the defining feature is the ability to abort a transaction
       on error and have all writes discarded."
      -> We only write review_id to Postgres AFTER GitHub confirms the review
         was accepted (2xx response). If GitHub returns an error, we do NOT
         write to Postgres. This prevents orphaned records.
    """

    # GitHub's internal ID for this review. e.g. 1234567890
    # Stored in PRReviewRecord.github_review_id (Phase 6 Postgres schema)
    id: int

    # The review state as GitHub reports it.
    # Should match what we sent in PostReviewPayload.event.
    state: ReviewEvent

    # ISO 8601 timestamp of when the review was submitted.
    submitted_at: Optional[datetime] = None

    # URL to the review on GitHub web UI. Useful for HITL dashboard links.
    html_url: Optional[str] = None

    @classmethod
    def from_github_response(cls, data: dict) -> "PostReviewResponse":
        """Constructs PostReviewResponse from GitHub's API JSON.

        GitHub returns review state in past-tense form (APPROVED / CHANGES_REQUESTED /
        COMMENTED / DISMISSED / PENDING) — different from the request 'event' values
        (APPROVE / REQUEST_CHANGES / COMMENT). Map response state back to the
        ReviewEvent enum we use in code.
        """
        state_map = {
            "APPROVED": ReviewEvent.APPROVE,
            "CHANGES_REQUESTED": ReviewEvent.REQUEST_CHANGES,
            "COMMENTED": ReviewEvent.COMMENT,
            "DISMISSED": ReviewEvent.COMMENT,   # treat as commentary
            "PENDING": ReviewEvent.COMMENT,
            # also accept canonical request-form values (defensive)
            "APPROVE": ReviewEvent.APPROVE,
            "REQUEST_CHANGES": ReviewEvent.REQUEST_CHANGES,
            "COMMENT": ReviewEvent.COMMENT,
        }
        raw_state = data.get("state", "COMMENTED")
        mapped = state_map.get(raw_state, ReviewEvent.COMMENT)
        return cls(
            id=data["id"],
            state=mapped,
            submitted_at=data.get("submitted_at"),
            html_url=data.get("html_url"),
        )
