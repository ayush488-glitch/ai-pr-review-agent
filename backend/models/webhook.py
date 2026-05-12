# backend/models/webhook.py
#
# Pydantic models for GitHub webhook payloads.
#
# WHY A SEPARATE MODEL FOR WEBHOOKS?
# GitHub sends a large JSON payload with many fields we do not need.
# We parse only the fields we care about into typed models.
# The rest is ignored.
#
# This also acts as a schema contract: if GitHub changes their payload format
# and removes a field we depend on, Pydantic raises a validation error immediately
# rather than a silent KeyError somewhere deep in our business logic.
#
# The GitHub webhook for a pull_request event looks like this (simplified):
# {
#   "action": "opened",
#   "number": 42,
#   "pull_request": {
#     "title": "Add login feature",
#     "head": { "sha": "abc123", "ref": "feature/login" },
#     "base": { "ref": "main" },
#     "diff_url": "https://github.com/.../42.diff",
#     "user": { "login": "ayush488-glitch" }
#   },
#   "repository": {
#     "full_name": "ayush488-glitch/my-project",
#     "clone_url": "https://github.com/ayush488-glitch/my-project.git"
#   }
# }

from pydantic import BaseModel

from backend.models.enums import PullRequestAction


# -----------------------------------------------------------------------------
# Nested models - represent nested JSON objects in the payload
# -----------------------------------------------------------------------------

class WebhookPRHead(BaseModel):
    """
    The 'head' object inside a pull_request payload.
    Represents the branch being reviewed (the PR branch).
    """
    # The git commit SHA at the tip of the PR branch.
    # This is what we use for idempotency.
    sha: str

    # The branch name. e.g. "feature/add-login"
    ref: str


class WebhookPRBase(BaseModel):
    """
    The 'base' object inside a pull_request payload.
    Represents the target branch (usually 'main' or 'develop').
    """
    # The target branch name. e.g. "main"
    ref: str


class WebhookPRUser(BaseModel):
    """
    The 'user' object inside a pull_request payload.
    Represents the GitHub user who opened the PR.
    """
    # GitHub username. e.g. "ayush488-glitch"
    login: str


class WebhookPullRequest(BaseModel):
    """
    The 'pull_request' object inside the webhook payload.
    Contains all the information about the PR itself.
    """
    # PR title. e.g. "Add login feature"
    title: str

    # PR body / description (optional — can be empty)
    body: str = ""

    # Inline diff — non-standard field, present in our demo fixture only.
    # Real GitHub webhooks do NOT include the diff; it must be fetched separately.
    diff: str = ""

    # The PR branch (what is being reviewed)
    head: WebhookPRHead

    # The target branch (what it will be merged into)
    base: WebhookPRBase

    # URL to download the unified diff for this PR
    # We use this to fetch the actual code changes
    diff_url: str

    # GitHub user who opened the PR
    user: WebhookPRUser


class WebhookRepository(BaseModel):
    """
    The 'repository' object inside the webhook payload.
    Represents the GitHub repository.
    """
    # Full name in format "owner/repo". e.g. "ayush488-glitch/my-project"
    full_name: str

    # URL to clone the repo. Used in Phase 6 for RAG indexing.
    clone_url: str


# -----------------------------------------------------------------------------
# Top-level webhook event model
# -----------------------------------------------------------------------------

class WebhookEvent(BaseModel):
    """
    The complete parsed GitHub pull_request webhook payload.

    This is what the webhook receiver produces after:
    1. Validating the HMAC signature
    2. Parsing the raw JSON

    This model is then enqueued in the job queue for the orchestrator.

    Note: we only parse pull_request events. All other event types
    are rejected before reaching this model (see webhook_receiver/router.py).
    """

    # What happened to the PR: "opened", "synchronize", "reopened"
    action: PullRequestAction

    # PR number on GitHub. e.g. 42
    number: int

    # All the PR details
    pull_request: WebhookPullRequest

    # The repository this PR belongs to
    repository: WebhookRepository

    # -------------------------------------------------------------------------
    # Computed properties
    # These are not in the GitHub payload - we derive them for convenience
    # so callers do not have to dig into nested objects every time.
    # -------------------------------------------------------------------------

    @property
    def repo_full_name(self) -> str:
        """e.g. 'ayush488-glitch/my-project'"""
        return self.repository.full_name

    @property
    def pr_number(self) -> int:
        """e.g. 42"""
        return self.number

    @property
    def pr_title(self) -> str:
        """e.g. 'Add login feature'"""
        return self.pull_request.title

    @property
    def pr_body(self) -> str:
        """PR description body (may be empty)"""
        return self.pull_request.body

    @property
    def pr_author(self) -> str:
        """e.g. 'ayush488-glitch'"""
        return self.pull_request.user.login

    @property
    def author_login(self) -> str:
        """Alias for pr_author — used by webhook router"""
        return self.pull_request.user.login

    @property
    def base_branch(self) -> str:
        """Target branch name e.g. 'main'"""
        return self.pull_request.base.ref

    @property
    def head_commit_sha(self) -> str:
        """The git SHA at the tip of the PR branch. Used for idempotency."""
        return self.pull_request.head.sha

    @property
    def diff_url(self) -> str:
        """URL to fetch the unified diff for this PR."""
        return self.pull_request.diff_url

    @property
    def idempotency_key(self) -> str:
        """
        A unique key that identifies this exact PR at this exact commit.
        Format: "{repo_full_name}:{pr_number}:{head_commit_sha}"

        If GitHub replays this webhook, the idempotency_key will be identical.
        We check this key in Redis before processing to avoid duplicate reviews.
        """
        return f"{self.repo_full_name}:{self.pr_number}:{self.head_commit_sha}"
