#!/bin/bash
# Test Railway deployment status and wake up services

echo "=== Railway Status Check ==="
echo ""

# Check if Railway CLI is installed
if ! command -v railway &> /dev/null; then
    echo "❌ Railway CLI not found. Install with: npm install -g @railway/cli"
    exit 1
fi

# Check if logged in
echo "Checking Railway authentication..."
railway whoami 2>&1 | grep -q "Logged in as" && echo "✅ Authenticated" || { echo "❌ Not logged in"; exit 1; }
echo ""

# Check current project
echo "Current Railway project:"
railway status
echo ""

# Check services
echo "=== Service Status ==="
railway status 2>&1 || echo "No services running yet"
echo ""

# Wake up services by pulling status
echo "=== Waking up services (if sleeping) ==="
railway status > /dev/null 2>&1 || echo "Services may be sleeping..."
echo ""

# List all services
echo "=== Services in project ==="
railway list 2>&1 || echo "No services found"
echo ""

# Check environment variables
echo "=== Environment Variables ==="
railway variables 2>&1 | head -20
echo ""

# List deployments
echo "=== Recent Deployments ==="
railway deploy list 2>&1 | head -10
echo ""

# Test webhook endpoint (if we have the URL)
echo "=== Test Webhook Endpoint (if deployed) ==="
WEBHOOK_URL=$(railway domain 2>&1 | head -1)
if [[ $WEBHOOK_URL != *"error"* ]]; then
    HEALTH_URL="${WEBHOOK_URL}/health"
    echo "Testing: $HEALTH_URL"
    curl -s "$HEALTH_URL" 2>&1 && echo "✅ Web service is awake!" || echo "❌ Web service not responding"
else
    echo "❌ Unable to determine webhook URL"
fi
echo ""

echo "=== Next Steps ==="
echo "1. If services are sleeping, run: railway up"
echo "2. To view logs: railway logs --follow"
echo "3. To upgrade from free tier: railway upgrade"
echo "4. For testing guide, see: docs/testing-pr-review.md"