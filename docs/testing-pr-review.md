# Testing AI PR Review Agent - Step-by-Step Guide

## 🚨 CURRENT STATUS

- Production deployment: ✅ Railway (crashed + sleep issue on free tier)
- Render deployment: 🔄 Attempting Blueprint deployment (sync issues)
- Test PR created: ✅ https://github.com/ayush488-glitch/test-pr-review-demo/pull/11

## 📋 Testing Checklist

### Phase 1: Test PR Created ✅

- Repository: https://github.com/ayush488-glitch/test-pr-review-demo
- PR: https://github.com/ayush488-glitch/test-pr-review-demo/pull/11
- Branch: test-issue-42
- Issues in PR:
  - ❌ SQL Injection: `SELECT * FROM users WHERE id = " + user_id`
  - ❌ Hardcoded Credentials: `username == "admin" and password == "admin123"`
  - ❌ Weak Hashing: `md5()` instead of bcrypt/argon2
  - ❌ AttributeError: `data.some_method()` when data is None

### Phase 2: GitHub Webhook Setup (Manually)

The webhook needs to point to your Railway/Webhook receiver URL.

**Option A: Railway (Currently Sleep/Crashed)**

1. Go toRailway Dashboard
2. Find your web service deployment
3. Copy the URL (e.g., `https://ai-pr-review-web.production.railway.app`)
4. Add GitHub webhook:
   ```
   Webhook URL: https://ai-pr-review-web.production.railway.app/webhooks/github
   Secret: ebaa831c6af59b3db6602713b194fe147ef75ae52b6abb969f542a169b79d367
   Content type: application/json
   Events: Pull requests, Pull request reviews
   ```

**Option B: Render (If Blueprint works)**

1. Go to Render Dashboard
2. Find `ai-pr-review-web` (if created)
3. Copy the URL: `https://ai-pr-review-web.onrender.com`
4. Add GitHub webhook:
   ```
   Webhook URL: https://ai-pr-review-web.onrender.com/webhooks/github
   Secret: ebaa831c6af59b3db6602713b194fe147ef75ae52b6abb969f542a169b79d367
   Content type: application/json
   Events: Pull requests, Pull request reviews
   ```

### Phase 3: Manual Webhook Test (If Still Issues)

If the webhook isn't set up or Railway is asleep:

```bash
# Test the webhook manually (requires Railway URL)
WEBHOOK_URL="https://your-railway-url.railway.app/webhooks/github"
SECRET="ebaa831c6af59b3db6602713b194fe147ef75ae52b6abb969f542a169b79d367"

# Create a simple webhook payload
cat > /tmp/webhook-payload.json << 'EOJ'
{
  "action": "opened",
  "pull_request": {
    "id": 11,
    "number": 11,
    "state": "open",
    "title": "Test PR Review - Added buggy authentication code",
    "user": {
      "login": "ayush488-glitch"
    },
    "head": {
      "sha": "7ff0e77abc123456789",
      "ref": "test-issue-42",
      "repo": {
        "full_name": "ayush488-glitch/test-pr-review-demo",
        "private": false
      }
    },
    "base": {
      "ref": "main"
    },
    "html_url": "https://github.com/ayush488-glitch/test-pr-review-demo/pull/11"
  },
  "repository": {
    "full_name": "ayush488-glitch/test-pr-review-demo",
    "private": false
  }
}
EOJ

# Send webhook with signature
curl -X POST $WEBHOOK_URL \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: $(echo -n "$SECRET"$(cat /tmp/webhook-payload.json) | openssl dgst -sha256 -hex | sed 's/^.* //')" \
  -d @/tmp/webhook-payload.json
```

### Phase 4: Monitor Logs

**Railway Logs:**
```bash
# Via CLI (if Railway is awake)
railway logs --service web

# Or visit Railway Dashboard:
# Dashboard → your project → web service → Logs
```

**Render Logs (if deployed):**
```bash
# Visit Render Dashboard:
# Dashboard → ai-pr-review-web → Logs
# Dashboard → ai-pr-review-worker → Logs
```

### Phase 5: Expected Behavior

**Success:**
1. Webhook received (timestamp)
2. PR reviewed by LLM
3. Review comments posted on PR
4. Comments structured by domain:
   - Security: SQL injection, weak hashing
   - Quality: NoneType error, code smells
   - Test: Missing tests
   - Docs: No docstrings

**Failure Signs:**
- Worker not running (asleep/crashed)
- No logs from webhook endpoint
- Review comments not posted
- Timeout errors

### Phase 6: Debug Guide

#### Issue: Worker Not Running

**Symptoms:**
- Webhook received but no review
- Worker logs show "sleeping" or "starting"

**Fix:**
```bash
# On Railway
railway up  # Deploys everything
railway status  # Check if services are running

# Upgrade to Pro (bypass sleep)
railway upgrade
```

#### Issue: Database Connection Failure

**Symptoms:**
- Logs show "connection refused" or "timeout"

**Fix:**
```bash
# Check DATABASE_URL env variable
railway variables  # List all variables
# Verify format: postgresql+asyncpg://user:pass@host:port/db?ssl=require
```

#### Issue: GitHub API Rate Limits

**Symptoms:**
- 403 errors from GitHub API

**Fix:**
- Use a GitHub PAT with higher rate limits
- Set GITHUB_TOKEN with proper scopes (repo, read:org)

#### Issue: Invalid Webhook Signature

**Symptoms:**
- 403 "Invalid signature" errors

**Fix:**
- Ensure GITHUB_WEBHOOK_SECRET matches GitHub webhook secret
- Check webhook payload format

#### Issue: Qdrant/Embedding Failures

**Symptoms:**
- "embedding failed" or "vector store error"

**Fix:**
```bash
# Check Qdrant connection
curl https://your-qdrant-url.cloud.qdrant.io/health

# Verify QDRANT_URL and QDRANT_API_KEY
railway variables | grep QDRANT
```

## 🔍 Expected Review Content

Based on test PR issues, the AI reviewer should flag:

### Security Domain (CRITICAL)
❌ **SQL Injection** (lines 11-12)
```python
query = "SELECT * FROM users WHERE id = " + user_id  # VULNERABLE
```
- Suggestion: Use parameterized queries
- Severity: CRITICAL

❌ **Hardcoded Credentials** (line 6)
```python
if username == "admin" and password == "admin123"  # VULNERABLE
```
- Suggestion: Use environment variables or secure config
- Severity: CRITICAL

❌ **Weak Password Hashing** (line 15)
```python
return hashlib.md5(password.encode()).hexdigest()  # INSECURE
```
- Suggestion: Use bcrypt or argon2
- Severity: HIGH

### Quality Domain (HIGH)
❌ **AttributeError Risk** (lines 19-21)
```python
data = None
return data.some_method()  # Will crash
```
- Suggestion: Add null check or use Optional typing
- Severity: HIGH

### Test Domain (HIGH)
❌ **Missing Tests**
- No test files for login function
- No test cases for SQL query validation
- No tests for authentication flow
- Suggestion: Add pytest tests with >80% coverage

### Docs Domain (MEDIUM)
❌ **Missing Docstrings**
- No docstring for login() function
- No docstring for get_user_data() function
- No parameters documentation
- Suggestion: Add Google-style docstrings

## 📊 Success Criteria

- ✅ Webhook endpoint responds with 200 OK
- ✅ Worker logs show job processing
- ✅ Review comments posted on PR within 30-60 seconds
- ✅ Comments structured by domain (security, quality, test, docs)
- ✅ At least one CRITICAL security issue flagged
- ✅ Actionable suggestions provided for each issue

## 🚫 Fail Criteria

- ❌ Worker service sleeping/crashed
- ❌ No webhook received or processed
- ❌ Review comments not posted
- ❌ Generic responses only (no specific code issues)
- ❌ Timeout errors

## 🎯 Next Actions (In Order)

1. **Check Railway status**: Is web service running?
   ```bash
   railway status
   # Watch for "sleeping" or "crashed" status
   ```

2. **Set up GitHub webhook** (or manually trigger):
   - Add webhook to test repository
   - Or use manual webhook test script

3. **Monitor Railway logs**:
   ```bash
   railway logs --service web --follow
   # Look for webhook events
   ```

4. **Check PR for review comments**:
   - https://github.com/ayush488-glitch/test-pr-review-demo/pull/11
   - Wait 30-60 seconds after webhook

5. **If no review appears**:
   - Check worker logs: `railway logs --service worker`
   - Check database connectivity
   - Check environment variables

6. **If Railway stays asleep**:
   - Try Render deployment: `docs/render-manual-deployment.md`
   - Or upgrade Railway to Pro: `railway upgrade`

## 💡 Quick Diagnosis Flow

```
Webhook received? → N → Fix webhook
           ↓ Y
Worker running? → N → Wake up railway up
           ↓ Y
Job queued? → N → Check Redis connectivity
           ↓ Y
Database connected? → N → Check DATABASE_URL
           ↓ Y
LLM API reachable? → N → Check OPENAI_API_KEY
           ↓ Y
Review posted? → N → Look for errors in worker logs
           ↓ Y
SUCCESS!
```

## 📞 Help References

- Railway logs: Dashboard → Project → Service → Logs
- Render logs (if using): Dashboard → Service → Logs
- GitHub webhook deliveries: Repo → Settings → Webhooks → View deliveries
- Test PR: https://github.com/ayush488-glitch/test-pr-review-demo/pull/11
- Repository: https://github.com/ayush488-glitch/ai-pr-review-agent

**Current blocker: Railway is asleep on free tier. Next step: Wake it up `railway up` or switch to Render.**