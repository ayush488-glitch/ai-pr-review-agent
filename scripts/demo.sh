#!/usr/bin/env bash
# scripts/demo.sh — AI PR Review Agent end-to-end demo
#
# USAGE:
#   1. Copy .env.example to .env and fill in your API keys
#   2. docker-compose up --build  (wait for "Application startup complete")
#   3. bash scripts/demo.sh
#
# WHAT THIS DOES:
#   1. Waits for the API to be healthy
#   2. Computes the HMAC-SHA256 signature GitHub would send
#   3. POSTs the sample PR webhook payload
#   4. Polls GET /api/v1/reviews until the verdict appears
#   5. Pretty-prints the full review result
#
# release-it/Operations-Patterns: "The best interface for long-term
# operation is the command line." — this script IS the demo.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config — override via env vars
# Auto-source .env if present (so GITHUB_WEBHOOK_SECRET is picked up)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

API_BASE="${API_BASE:-http://localhost:8001}"
API_KEY="${API_KEY:-change-me-in-production}"
GITHUB_WEBHOOK_SECRET="${GITHUB_WEBHOOK_SECRET:-change-me-generate-with-openssl-rand-hex-32}"
FIXTURE="${FIXTURE:-fixtures/sample_pr_opened.json}"
MAX_POLL="${MAX_POLL:-120}"       # max seconds to wait for verdict
POLL_INTERVAL="${POLL_INTERVAL:-2}"

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[demo]${RESET} $*"; }
success() { echo -e "${GREEN}[demo]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[demo]${RESET} $*"; }
error()   { echo -e "${RED}[demo]${RESET} $*" >&2; }
header()  { echo -e "\n${BOLD}$*${RESET}"; }

# ---------------------------------------------------------------------------
# Step 0: dependency checks
# ---------------------------------------------------------------------------
header "Step 0: Checking dependencies"

for cmd in curl jq openssl python3; do
  if ! command -v "$cmd" &>/dev/null; then
    error "Required command not found: $cmd"
    exit 1
  fi
done
success "curl, jq, openssl, python3 all available"

if [[ ! -f "$FIXTURE" ]]; then
  error "Fixture file not found: $FIXTURE"
  error "Run this script from the repo root: bash scripts/demo.sh"
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 1: Wait for API health
# ---------------------------------------------------------------------------
header "Step 1: Waiting for API to be ready at $API_BASE"

HEALTH_WAIT=0
HEALTH_MAX=60
until curl -sf "${API_BASE}/health/live" >/dev/null 2>&1; do
  if [[ $HEALTH_WAIT -ge $HEALTH_MAX ]]; then
    error "API did not become healthy after ${HEALTH_MAX}s."
    error "Is docker-compose up? Try: docker-compose up --build"
    exit 1
  fi
  info "Waiting... (${HEALTH_WAIT}s elapsed)"
  sleep 2
  HEALTH_WAIT=$((HEALTH_WAIT + 2))
done
success "API is live."

# Check readiness (deep health — Postgres + Redis + Qdrant)
READINESS=$(curl -s "${API_BASE}/health")
READINESS_STATUS=$(echo "$READINESS" | jq -r '.status')
info "Readiness status: $READINESS_STATUS"
if [[ "$READINESS_STATUS" != "ok" ]]; then
  warn "API is degraded (some services unhealthy). Continuing anyway..."
  warn "Services: $(echo "$READINESS" | jq -c '.services')"
fi

# ---------------------------------------------------------------------------
# Step 2: Compute HMAC-SHA256 signature
# ---------------------------------------------------------------------------
header "Step 2: Computing webhook signature"

PAYLOAD=$(cat "$FIXTURE")

# GitHub signs the raw body with HMAC-SHA256 using the webhook secret.
# IMPORTANT: sign the FILE directly — shell variables strip trailing newlines
# which causes a signature mismatch. openssl dgst on the file = exact bytes.
SIG="sha256=$(openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" "$FIXTURE" | awk '{print $2}')"
info "Signature: ${SIG:0:20}..."

# ---------------------------------------------------------------------------
# Step 3: POST the webhook
# ---------------------------------------------------------------------------
header "Step 3: Sending webhook to $API_BASE/webhook/github"

WEBHOOK_RESPONSE=$(curl -s -w "\n%{http_code}" \
  -X POST "${API_BASE}/webhook/github" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: $SIG" \
  -H "X-GitHub-Delivery: demo-$(date +%s)" \
  --data-binary "@$FIXTURE")

HTTP_BODY=$(echo "$WEBHOOK_RESPONSE" | python3 -c "import sys; lines=sys.stdin.read().splitlines(); print('\n'.join(lines[:-1]))")
HTTP_CODE=$(echo "$WEBHOOK_RESPONSE" | tail -n1)

if [[ "$HTTP_CODE" == "202" || "$HTTP_CODE" == "200" ]]; then
  success "Webhook accepted (HTTP $HTTP_CODE)"
  REVIEW_ID=$(echo "$HTTP_BODY" | jq -r '.review_id // .id // empty')
  info "Review ID from response: ${REVIEW_ID:-'(not in response — will poll all reviews)'}"
else
  error "Webhook rejected (HTTP $HTTP_CODE): $HTTP_BODY"
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 4: Poll for the verdict
# ---------------------------------------------------------------------------
header "Step 4: Polling for verdict (max ${MAX_POLL}s)..."

ELAPSED=0
VERDICT=""

while [[ $ELAPSED -lt $MAX_POLL ]]; do
  # Fetch all reviews, grab the most recent one
  REVIEWS=$(curl -s \
    -H "X-API-Key: $API_KEY" \
    "${API_BASE}/api/v1/reviews?limit=1")

  REVIEW_COUNT=$(echo "$REVIEWS" | jq '.items | length' 2>/dev/null || echo "0")

  if [[ "$REVIEW_COUNT" -gt 0 ]]; then
    STATUS=$(echo "$REVIEWS" | jq -r '.items[0].status // empty')
    VERDICT=$(echo "$REVIEWS" | jq -r '.items[0].verdict // empty')
    info "Review status: $STATUS | Verdict: ${VERDICT:-pending}"

    if [[ "$STATUS" == "completed" || "$STATUS" == "failed" || "$STATUS" == "hitl_required" ]]; then
      break
    fi
  else
    info "No reviews found yet... (${ELAPSED}s elapsed)"
  fi

  sleep "$POLL_INTERVAL"
  ELAPSED=$((ELAPSED + POLL_INTERVAL))
done

# ---------------------------------------------------------------------------
# Step 5: Print result
# ---------------------------------------------------------------------------
header "Step 5: Result"

if [[ -z "$VERDICT" ]]; then
  warn "Review did not complete within ${MAX_POLL}s."
  warn "The workflow may still be running — try:"
  warn "  curl -H 'X-API-Key: $API_KEY' ${API_BASE}/api/v1/reviews"
  exit 0
fi

FULL_REVIEW=$(curl -s \
  -H "X-API-Key: $API_KEY" \
  "${API_BASE}/api/v1/reviews?limit=1" | jq '.items[0]')

# Colour the verdict
case "$VERDICT" in
  APPROVE)        COLOUR=$GREEN ;;
  REQUEST_CHANGES) COLOUR=$YELLOW ;;
  BLOCK|CRITICAL_BLOCK) COLOUR=$RED ;;
  HITL_REQUIRED)  COLOUR=$YELLOW ;;
  *)              COLOUR=$CYAN ;;
esac

echo ""
echo -e "${BOLD}Verdict: ${COLOUR}${VERDICT}${RESET}"
echo ""
echo "$FULL_REVIEW" | jq '{
  verdict: .verdict,
  status:  .status,
  confidence: .overall_confidence,
  needs_human_review: .needs_human_review,
  findings_count: (if .findings then (.findings | length) else "n/a" end),
  top_findings: (if .findings then [.findings[:3][] | {severity: .severity, category: .category, summary: .summary}] else [] end)
}'

success "Demo complete."

# ---------------------------------------------------------------------------
# Step 6: Check HITL queue
# ---------------------------------------------------------------------------
header "Step 6: Checking HITL queue"

HITL_QUEUE=$(curl -s \
  -H "X-API-Key: $API_KEY" \
  "${API_BASE}/api/v1/hitl/queue")

HITL_COUNT=$(echo "$HITL_QUEUE" | jq '.items | length' 2>/dev/null || echo "0")
info "Pending HITL items: $HITL_COUNT"

if [[ "$HITL_COUNT" -gt 0 ]]; then
  echo "$HITL_QUEUE" | jq '[.items[] | {id: .id, repo: .repo_full_name, pr: .pr_number, agent_verdict: .agent_verdict, reason: .escalation_reason, confidence: .overall_confidence}]'

  # Step 7: Submit a HITL decision on the first pending item (demo mode)
  header "Step 7: Submitting demo HITL decision (approve)"

  HITL_ID=$(echo "$HITL_QUEUE" | jq -r '.items[0].id')
  info "Approving HITL item: $HITL_ID"

  DECISION_RESPONSE=$(curl -s -w "\n%{http_code}" \
    -X POST "${API_BASE}/api/v1/hitl/${HITL_ID}/decision" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $API_KEY" \
    -d "{
      \"human_verdict\": \"approve\",
      \"reason\": \"Demo: reviewed and approved by demo script\",
      \"reviewer_id\": \"demo-script\"
    }")

  DECISION_BODY=$(echo "$DECISION_RESPONSE" | python3 -c "import sys; lines=sys.stdin.read().splitlines(); print('\n'.join(lines[:-1]))")
  DECISION_CODE=$(echo "$DECISION_RESPONSE" | tail -n1)

  if [[ "$DECISION_CODE" == "200" ]]; then
    success "HITL decision submitted (HTTP $DECISION_CODE)"
    echo "$DECISION_BODY" | jq '{hitl_id: .hitl_review_id, previous_status: .previous_status, new_status: .new_status, posted_to_github: .posted_to_github}'
  else
    warn "HITL decision response (HTTP $DECISION_CODE): $DECISION_BODY"
  fi
else
  info "No items in HITL queue — review was auto-posted (thresholds not exceeded)."
  info "To trigger HITL: lower confidence in fixture or add more CRITICAL findings."
fi

echo ""
echo "More commands:"
echo "  View all reviews:  curl -H 'X-API-Key: $API_KEY' ${API_BASE}/api/v1/reviews | jq"
echo "  View HITL queue:   curl -H 'X-API-Key: $API_KEY' ${API_BASE}/api/v1/hitl/queue | jq"
echo "  Rebuild queue:     curl -X POST -H 'X-API-Key: $API_KEY' ${API_BASE}/api/v1/hitl/queue/rebuild | jq"
echo "  Service health:    curl ${API_BASE}/health | jq"
echo "  API docs:          open ${API_BASE}/docs"
