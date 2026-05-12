#!/usr/bin/env bash
# scripts/deploy.sh
#
# Deployment verification script for ai-pr-review-agent on Railway.
#
# WHAT THIS DOES:
#   1. Runs local smoke tests (offline, no LLM) — fast sanity check
#   2. Confirms git state is clean and pushed to main
#   3. Waits for Railway to finish deploying (polls /health)
#   4. Sends the sample PR fixture to the Railway URL and confirms the verdict
#
# WHAT THIS DOES NOT DO:
#   - Deploy to Railway (Railway auto-deploys on push to main via GitHub)
#   - Require Railway CLI (no CLI needed)
#
# USAGE:
#   RAILWAY_URL=https://your-app.up.railway.app bash scripts/deploy.sh
#
# PREREQUISITES:
#   - RAILWAY_URL env var set to your Railway public domain
#   - API_KEY env var set (or sourced from .env)
#   - GITHUB_WEBHOOK_SECRET env var set (or sourced from .env)
#   - git push to main already done (or run this after git push)
#
# EXIT CODES:
#   0 — deploy verified successfully
#   1 — smoke tests failed (don't push to main)
#   2 — Railway health check timed out
#   3 — verdict check failed (pipeline broke after deploy)

set -euo pipefail

# ---------------------------------------------------------------------------
# COLORS
# ---------------------------------------------------------------------------
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }
info() { echo -e "${YELLOW}[INFO]${NC} $1"; }

# ---------------------------------------------------------------------------
# LOAD .env if not already sourced
# (demo-day-readiness Bug #8: always source .env fresh, never rely on stale vars)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    source "$REPO_ROOT/.env"
    set +a
    info "Loaded .env from $REPO_ROOT/.env"
fi

# ---------------------------------------------------------------------------
# CONFIG — override any of these via environment variable
# ---------------------------------------------------------------------------
RAILWAY_URL="${RAILWAY_URL:-}"
API_KEY="${API_KEY:-change-me-in-production}"
WEBHOOK_SECRET="${GITHUB_WEBHOOK_SECRET:-}"
FIXTURE_FILE="$REPO_ROOT/fixtures/sample_pr_opened.json"
HEALTH_TIMEOUT=180      # seconds to wait for Railway health check
REVIEW_TIMEOUT=120      # seconds to wait for review verdict after webhook

# ---------------------------------------------------------------------------
# STEP 0: Pre-flight checks
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo " AI PR Review Agent — Deploy Verify"
echo "========================================"
echo ""

info "Step 0: Pre-flight checks"

# Check required tools
for cmd in curl jq openssl python3; do
    if ! command -v "$cmd" &>/dev/null; then
        fail "Required tool not found: $cmd"
        exit 1
    fi
done
pass "curl, jq, openssl, python3 all available"

# Check RAILWAY_URL is set
if [ -z "$RAILWAY_URL" ]; then
    fail "RAILWAY_URL is not set."
    echo ""
    echo "  Set it like this:"
    echo "    RAILWAY_URL=https://your-app.up.railway.app bash scripts/deploy.sh"
    echo ""
    echo "  Or export it in your shell:"
    echo "    export RAILWAY_URL=https://your-app.up.railway.app"
    echo ""
    echo "  Find your Railway public domain at:"
    echo "    railway.app -> your project -> api service -> Settings -> Domains"
    exit 1
fi
pass "RAILWAY_URL=$RAILWAY_URL"

# Check fixture exists
if [ ! -f "$FIXTURE_FILE" ]; then
    fail "Fixture not found: $FIXTURE_FILE"
    exit 1
fi
pass "Fixture file found"

# ---------------------------------------------------------------------------
# STEP 1: Local smoke tests
# (Catches broken imports / syntax before pushing)
# ---------------------------------------------------------------------------
echo ""
info "Step 1: Running local smoke tests (offline, mocked LLM)..."

# Minimal env for the settings module to initialize
export APP_ENV=test
export DATABASE_URL="postgresql+asyncpg://test:test@localhost:5432/test"
export REDIS_URL="redis://localhost:6379"
export QDRANT_URL="http://localhost:6333"
export QDRANT_API_KEY="test-key"
export QDRANT_COLLECTION_NAME="test_collection"
export OPENAI_API_KEY="${OPENAI_API_KEY:-sk-test-placeholder}"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-test-placeholder}"
export OPENAI_EMBEDDING_MODEL="text-embedding-3-small"
export SECURITY_PROVIDER="openai"
export SECURITY_MODEL="gpt-4o"
export GITHUB_WEBHOOK_SECRET="${GITHUB_WEBHOOK_SECRET:-test-secret}"
export GITHUB_TOKEN="${GITHUB_TOKEN:-ghp-test-placeholder}"
export GITHUB_API_BASE_URL="https://api.github.com"
export LOG_LEVEL="ERROR"
export API_KEY="${API_KEY:-test-api-key}"
export CONFIDENCE_THRESHOLD="0.85"
export WORKFLOW_TIMEOUT_SECONDS="300"
export MAX_CONCURRENT_REVIEWS="5"

if python3 -m pytest tests/smoke_phase14.py -q --tb=short 2>&1; then
    pass "Smoke tests passed"
else
    fail "Smoke tests failed — fix before deploying"
    exit 1
fi

# Re-source .env to restore real values after smoke test env override
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    source "$REPO_ROOT/.env"
    set +a
fi

# ---------------------------------------------------------------------------
# STEP 2: Git state check
# Warns if there are uncommitted changes that won't be in the Railway deploy.
# ---------------------------------------------------------------------------
echo ""
info "Step 2: Git state check"

UNCOMMITTED=$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
if [ "$UNCOMMITTED" -gt 0 ]; then
    echo -e "${YELLOW}[WARN]${NC} $UNCOMMITTED uncommitted change(s) in the repo."
    echo "       These changes are NOT in the Railway deploy (Railway deploys from git)."
    echo "       Run: git add -A && git commit && git push origin main"
    echo "       if you want them included."
else
    pass "Git working tree is clean"
fi

CURRENT_BRANCH=$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || echo "unknown")
info "Current branch: $CURRENT_BRANCH"
if [ "$CURRENT_BRANCH" != "main" ]; then
    echo -e "${YELLOW}[WARN]${NC} Not on main branch. Railway auto-deploys from main only."
fi

# ---------------------------------------------------------------------------
# STEP 3: Wait for Railway health check
# Railway can take 1-3 minutes to finish a deploy after git push.
# We poll /health until it returns {"status": "ok"} or we time out.
# ---------------------------------------------------------------------------
echo ""
info "Step 3: Waiting for Railway /health to return ok (max ${HEALTH_TIMEOUT}s)..."
info "Railway URL: $RAILWAY_URL"

ELAPSED=0
INTERVAL=10
HEALTH_OK=false

while [ $ELAPSED -lt $HEALTH_TIMEOUT ]; do
    HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$RAILWAY_URL/health" 2>/dev/null || echo "000")

    if [ "$HTTP_STATUS" = "200" ]; then
        HEALTH_BODY=$(curl -s "$RAILWAY_URL/health" 2>/dev/null)
        HEALTH_STATUS=$(echo "$HEALTH_BODY" | jq -r '.status // "unknown"' 2>/dev/null || echo "unknown")

        if [ "$HEALTH_STATUS" = "ok" ] || [ "$HEALTH_STATUS" = "degraded" ]; then
            pass "Health check passed (status=$HEALTH_STATUS, HTTP $HTTP_STATUS) after ${ELAPSED}s"
            HEALTH_OK=true
            break
        else
            info "Health returned HTTP 200 but status=$HEALTH_STATUS — retrying..."
        fi
    else
        info "Health check HTTP $HTTP_STATUS (elapsed ${ELAPSED}s, retrying in ${INTERVAL}s)..."
    fi

    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))
done

if [ "$HEALTH_OK" = false ]; then
    fail "Railway health check timed out after ${HEALTH_TIMEOUT}s"
    echo "  Possible causes:"
    echo "    - Railway is still deploying (check railway.app dashboard)"
    echo "    - Neon Postgres cold start taking longer than usual (~30s)"
    echo "    - A startup error — check Railway logs: railway logs --tail 50"
    echo "    - RAILWAY_URL is wrong (check Settings -> Domains in Railway dashboard)"
    exit 2
fi

# ---------------------------------------------------------------------------
# STEP 4: Send fixture webhook + poll for verdict
# ---------------------------------------------------------------------------
echo ""
info "Step 4: Sending fixture webhook and waiting for verdict..."

# Flush Redis idempotency keys so a previous demo run doesn't block this one.
# demo-day-readiness Pitfall #9: stale idempotency key silently skips the job.
# We can't flush Railway's Redis directly (no shell access), so we append a
# unique suffix to the fixture to generate a different workflow_id each time.
#
# Strategy: patch the fixture SHA temporarily so each deploy run gets a unique
# workflow_id (avoids "already reviewed" idempotency skip).
TIMESTAMP=$(date +%s)
# Create a temporary fixture with a unique SHA so each deploy run is fresh.
# The SHA must be exactly 40 hex characters.
TEMP_SHA=$(echo -n "deploy${TIMESTAMP}000000000000000000000" | head -c 40)
TEMP_FIXTURE=$(mktemp /tmp/pr_fixture_XXXXXX.json)
# Ensure cleanup even on script error
trap "rm -f $TEMP_FIXTURE" EXIT

jq --arg sha "$TEMP_SHA" '.pull_request.head.sha = $sha' "$FIXTURE_FILE" > "$TEMP_FIXTURE"

# Sign the fixture for HMAC verification.
# demo-day-readiness Bug #6: sign the FILE directly, not via shell variable.
SIGNATURE=$(openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" "$TEMP_FIXTURE" | awk '{print $2}')

HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" \
    -X POST "$RAILWAY_URL/webhook/github" \
    -H "Content-Type: application/json" \
    -H "X-Hub-Signature-256: sha256=$SIGNATURE" \
    -H "X-GitHub-Event: pull_request" \
    --data-binary "@$TEMP_FIXTURE" 2>/dev/null)

WEBHOOK_HTTP=$(echo "$HTTP_RESPONSE" | tail -n1)
WEBHOOK_BODY=$(echo "$HTTP_RESPONSE" | sed '$d')

if [ "$WEBHOOK_HTTP" != "200" ]; then
    fail "Webhook returned HTTP $WEBHOOK_HTTP (expected 200)"
    echo "  Response body: $WEBHOOK_BODY"
    exit 3
fi
pass "Webhook accepted (HTTP 200)"

# ---------------------------------------------------------------------------
# STEP 5: Poll for verdict
# ---------------------------------------------------------------------------
echo ""
info "Step 5: Polling for review verdict (max ${REVIEW_TIMEOUT}s)..."

REVIEW_ELAPSED=0
REVIEW_INTERVAL=8
VERDICT_FOUND=false

while [ $REVIEW_ELAPSED -lt $REVIEW_TIMEOUT ]; do
    REVIEWS_RESPONSE=$(curl -s \
        -H "X-API-Key: $API_KEY" \
        "$RAILWAY_URL/api/v1/reviews" 2>/dev/null || echo "{}")

    # Get the most recent review
    LATEST_STATUS=$(echo "$REVIEWS_RESPONSE" | jq -r '.items[0].status // empty' 2>/dev/null || echo "")
    LATEST_VERDICT=$(echo "$REVIEWS_RESPONSE" | jq -r '.items[0].verdict // empty' 2>/dev/null || echo "")

    if [ "$LATEST_STATUS" = "completed" ] || [ "$LATEST_STATUS" = "needs_human_review" ]; then
        pass "Review complete: status=$LATEST_STATUS verdict=$LATEST_VERDICT (after ${REVIEW_ELAPSED}s)"
        VERDICT_FOUND=true
        break
    elif [ -n "$LATEST_STATUS" ]; then
        info "Review status: $LATEST_STATUS (elapsed ${REVIEW_ELAPSED}s)..."
    else
        info "No review yet (elapsed ${REVIEW_ELAPSED}s)..."
    fi

    sleep $REVIEW_INTERVAL
    REVIEW_ELAPSED=$((REVIEW_ELAPSED + REVIEW_INTERVAL))
done

if [ "$VERDICT_FOUND" = false ]; then
    fail "No verdict after ${REVIEW_TIMEOUT}s"
    echo "  Possible causes:"
    echo "    - Worker service not running (check Railway dashboard)"
    echo "    - Redis connection issue between api and worker services"
    echo "    - Neon Postgres cold start adding latency"
    echo "    - Check worker logs: railway logs --service worker --tail 50"
    exit 3
fi

# ---------------------------------------------------------------------------
# DONE
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo -e "${GREEN}  Deploy verified successfully!${NC}"
echo "========================================"
echo ""
echo "  Public URL:  $RAILWAY_URL"
echo "  API docs:    $RAILWAY_URL/docs"
echo "  Health:      $RAILWAY_URL/health"
echo "  Reviews API: $RAILWAY_URL/api/v1/reviews"
echo ""
echo "  Next step: add this webhook URL to your GitHub repo:"
echo "    $RAILWAY_URL/webhook/github"
echo "  Settings -> Webhooks -> Add webhook"
echo "  Content type: application/json"
echo "  Secret: (your GITHUB_WEBHOOK_SECRET from .env)"
echo "  Events: Pull requests"
echo ""
exit 0
