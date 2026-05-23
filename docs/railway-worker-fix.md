# Railway Worker Fix & Webhook Setup

## 🚨 Current Situation

Web Service: ✅ Online and healthy
Worker Service: ❌ Failed (peak hour restriction on free tier)
Webhook Endpoint: ✅ Responding (`/webhook/github`)
Test PR: ✅ Created https://github.com/ayush488-glitch/test-pr-review-demo/pull/11

## 🔍 Diagnosis

The worker service failed to deploy because of Railway free tier peak hour restrictions:
```
Free-tier deploys to europe-west4-drams3a are not available during peak hours (8 AM – 8 PM Europe/Amsterdam)
```

This is the exact issue you mentioned - Railway's free tier blocks deployments during peak hours.

## 💡 Solutions

### Option 1: Wait for Off-Peak Hours (Free)

**Best time to deploy:**
- Any day **8 PM – 8 AM Europe/Amsterdam** timezone
- Wait until outside peak hours
- Then run: `railway redeploy --yes`

**Pros:** Free, no additional cost
**Cons:** Must wait, might miss testing window

### Option 2: Upgrade Railway to Pro (Recommended for Production)

**Cost:** ~$13.50/month minimum

**Commands:**
```bash
railway upgrade
railway redeploy --yes
```

**Benefits:**
- ❌ No peak hour restrictions
- ✓ Always-on operation
- ✓ Better worker performance
- ✓ No deployment failures

**When to choose:**
- You're serious about running this in production
- Want guaranteed availability
- Don't want to worry about peak hours

### Option 3: Use GitHub Webhook Without Worker (Partial Testing)

Since the web service is running, we can test webhook reception without the worker processing reviews:

**Step 1: Add GitHub Webhook**
1. Go to test repo: https://github.com/ayush488-glitch/test-pr-review-demo
2. Settings → Webhooks → Add webhook
3. **Payload URL:** `https://web-production-d9e54.up.railway.app/webhook/github`
4. **Content type:** `application/json`
5. **Secret:** Get from Railway env vars:
   ```bash
   railway variables | grep GITHUB_WEBHOOK_SECRET
   ```
6. **Events:** Pull requests, Pull request reviews
7. Click "Add webhook"

**Step 2: Test webhook delivery**
1. Go to webhook page → "Recent Deliveries"
2. Look for your test PR webhook
3. Check response code (should be 200 OK)
4. Expand to see response body

**Limitations:**
- ✅ Webhook will be received
- ❌ Worker won't process (failed to deploy)
- ❌ No PR reviews will be generated

**Purpose:**
- Test that Railwaysleep/workersleep
- Verify webhook endpoint exists and works
- Confirm GitHub integration is functional

### Option 4: Manual Job Processing (For Testing Only)

If you want to test PR review logic without the worker running:

```bash
# Connect to Railway web service and manually trigger a review
# This requires admin access and might not work on production builds
```

**Not recommended** - worker is specifically designed for async processing.

## 🔧 Step-by-Step: Fix Worker & Test Full Flow

### Step 1: Check Current Peak Hours

```bash
# Get current Europe time
date +'%Z %z'

# Or check Railway status
railway status
```

### Step 2: Wait for Off-Peak Hours (~8 PM – 8 AM Europe)

If it's currently during peak hours, you have two choices:
- **Wait** until 8 PM Europe tonight
- **Upgrade** to Railway Pro: `railway upgrade`

### Step 3: Redeploy Worker Service

When off-peak (or after upgrade):

```bash
# redeploy everything (web + worker)
railway redeploy --yes

# Or redeploy specific service
railway redeploy 
# Follow prompts to select worker service
```

### Step 4: Verify Worker Status

```bash
# Check if worker is now running
railway status

# Should see:
# worker: ● Online (not Failed)
```

### Step 5: Test Worker Logs

```bash
# Monitor worker logs
railway logs --service worker --follow

# Look for successful startup:
# "ARQ worker started"
# "Connected to Redis queue"
```

### Step 6: Get Webhook Secret

```bash
# Fetch from Railway env vars
railway variables | grep GITHUB_WEBHOOK_SECRET
```

### Step 7: Set up GitHub Webhook

1. Go to test repository webhooks page
2. Add new webhook
3. **URL:** `https://web-production-d9e54.up.railway.app/webhook/github`
4. **Secret:** Copy from step 6
5. **Events:** Pull requests, Pull request reviews
6. Save

### Step 8: Test with Manual Webhook Trigger

Alternatively, test by closing and reopening the PR:

```bash
# Via GitHub CLI
gh pr edit 11 --add-reviewer ayush488-glitch
gh pr merge 11 --squash --delete-branch=after-merge
```

Or just create a new issue/comment to trigger a webhook.

### Step 9: Monitor Full Flow

```bash
# Terminal 1: Web service logs
railway logs --service web --follow

# Terminal 2: Worker service logs
railway logs --service worker --follow
```

### Step 10: Check PR for Review Comments

Go to: https://github.com/ayush488-glitch/test-pr-review-demo/pull/11

Expected results:
- **Within 30-60 seconds:** Security review comments appear
- **Comments will flag:**
  - SQL injection vulnerability
  - Hardcoded credentials
  - Weak MD5 hashing
  - NoneType error potential
  - Missing tests
  - Missing docstrings

## ⚠️ Common Issues & Solutions

### Issue 1: Worker Still Failed After Redeploy

**Symptoms:** Worker shows "Failed" status even after redeploying

**Solutions:**
```bash
# Check build logs
railway status
# Click on worker → "Logs" → "Deploy INFINITE BUILD TIME (90s limit)"

# Try fresh deploy
railway redeploy --yes

# If still fails, check requirements.txt
railway logs --service worker
```

### Issue 2: GitHub Webhook Returns 401 Unauthorized

**Symptoms:** Webhook delivery fails with 401

**Solutions:**
- Double-check the secret matches
- Verify environment variable: `GITHUB_WEBHOOK_SECRET`
- Try regenerating secret:
  ```bash
  # Generate new secret
  openssl rand -hex 32
  
  # Update Railway env var
  railway variables set GITHUB_WEBHOOK_SECRET=<new-secret>
  
  # Update GitHub webhook with same secret
  ```

### Issue 3: Worker Runs But No Review Comments

**Symptoms:** Worker shows as "Online" but no reviews appear

**Solutions:**
```bash
# Check worker logs for job processing
railway logs --service worker --follow

# Look for:
# "Job received"
# "Processing PR #11"
# "Review posted"

# Missing environment variables?
railway variables | grep -E "OPENAI|ANTHROPIC|GITHUB"

# Check database connectivity
curl https://web-production-d9e54.up.railway.app/health
```

### Issue 4: Rate Limiting from GitHub API

**Symptoms:** 403 errors from GitHub API in logs

**Solutions:**
- Check GITHUB_TOKEN has proper scopes: `repo:scope`
- Use a Personal Access Token with higher rate limits
- Verify: `railway variables | grep GITHUB_TOKEN`

### Issue 5: Peak Hour "Deploy Failed" During Testing

**Symptoms:** Deploy fails with peak hour error in middle of testing

**Immediate action:**
```bash
# Check if there's an existing worker deployment
railway status

# If worker shows "Failed" but web is "Online"
# Try just waking services up (sometimes works)
curl https://web-production-d9e54.up.railway.app/health

# If worker is silent during peak hours:
# Cannot deploy during peak hours on free tier
# Either wait or upgrade: railway upgrade
```

## 📊 Success Criteria

- ✅ Worker service shows "Online" status
- ✅ Worker logs show "ARQ worker connected to Redis"
- ✅ Webhook responds with 200 OK (not 401/403)
- ✅ GitHub webhook shows successful delivery (green checkmark)
- ✅ Worker logs show "Job received: PR #11"
- ✅ Review comments appear on PR within 60 seconds
- ✅ Comments include security vulnerabilities (SQL injection, etc.)

## 🎯 Current Roadmap

### Phase 1: Worker Recovery (Current Block)
- ❌ Worker is failed due to peak hour restriction
- ✅ Web service is online and healthy
- ⏸️ **Next:** Wait for off-peak OR upgrade Railway

### Phase 2: Webhook Setup
- ⏸️ Add GitHub webhook to test repository
- ⏸️ Verify webhook delivery (200 OK response)

### Phase 3: Full Flow Test
- ⏸️ New PR → Webhook → Worker → Review Comments
- ⏸️ Verify all 4 domains reviewed (security, quality, test, docs)

### Phase 4: Production Decision
- ⏸️ Choose: Railway Pro vs Render Free vs Railway Free
- ⏸️ Migrate if needed

## 📚 Additional Resources

- Railway CLI docs: https://docs.railway.app/reference/cli
- Free tier limits: https://docs.railway.app/reference/free-usage
- GitHub webhooks: https://docs.github.com/en/developers/webhooks-and-events
- Test PR: https://github.com/ayush488-glitch/test-pr-review-demo/pull/11

## ⏱️ Time Estimates

- Wait for off-peak: 0-12 hours (depending on current time)
- Upgrade Railway: 2 minutes
- Redeploy worker: 5-10 minutes
- Webhook setup: 5 minutes
- Full flow test: 10 minutes

**Total time (if uptime): ~20 minutes**

---

## 🚀 Quick Decision Tree

```
Current time in Europe:
├─ During peak hours (8 AM – 8 PM)?
│  ├─ Yes and can wait?
│  │  └─ → Wait until 8 PM, then redeploy: railway redeploy --yes
│  └─ Yes and want to test NOW?
│     └─ → Upgrade: railway upgrade, then redeploy
└─ During off-peak?
   └─ → Redeploy immediately: railway redeploy --yes
```

**For production use:** Upgrade Railway to Pro (~$13.50/mo) to eliminate peak hour issues completely.