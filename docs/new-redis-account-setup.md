# Create New Upstash Redis Account & Update Env Files

## 🌟 Why This Works

Upstash free tier is **per account**, not per Redis database.
- Current account: 500K/month used up
- New account: Fresh 500K/month limit
- Cost: $0 (completely free!)

## 📋 Step-by-Step Guide

### Step 1: Create New Upstash Account (2 minutes)

1. Go to: https://console.upstash.com
2. Click "Sign Up" or "Create Account"
3. Use a **different email address** than your current one
   - Options: `+alias` Gmail tricks (e.g., `yourname+prreview@gmail.com`)
   - Or a completely different email
4. Verify email (takes 30 seconds)
5. Choose **Free tier** option
6. Create account

### Step 2: Create New Redis Database (2 minutes)

In the new Upstash console:
1. Click "Create Database"
2. Choose server location (default is fine)
3. Name it: `ai-pr-review-redis`
4. Click "Create"
5. Copy the **Redis URL** from the database overview page

**Redis URL format will be:**
```
redis://default:password@hostname:port
```

### Step 3: Update Railway Environment Variables (2 minutes)

```bash
# Check current Redis URL
railway variables | grep REDIS_URL

# Replace with new Redis URL
railway variables set REDIS_URL="redis://default:YOUR_NEW_PASSWORD@YOUR_HOST:PORT"
```

**If Railway variables command has any weird characters in password:**
```bash
# Use Railway web interface instead
# 1. Go to: https://dashboard.railway.app/project/your-project-id
# 2. Select your service
# 3. Go to Variables tab
# 4. Find REDIS_URL
# 5. Replace the entire URL with your new one
# 6. Save
# 7. Railway will auto-redeploy
```

### Step 4: Update Render Environment Variables (2 minutes)

Via Render Dashboard:
1. Go to: https://dashboard.render.com
2. Find `ai-pr-review-web` service
3. Click on it
4. Go to "Environment" section
5. Find `REDIS_URL` variable
6. Copy the entire new URL (with password)
7. Replace the current value
8. Save - Render will auto-rebuild with new Redis

### Step 5: Test Fresh Redis Connection (2 minutes)

```bash
# Test Railway health endpoint
curl -s https://web-production-d9e54.up.railway.app/health

# Test Render health endpoint  
curl -s https://ai-pr-review-web.onrender.com/health

# Both should show "redis": "ok" with the fresh connection
```

### Step 6: Test Webhook with Fresh Redis (5 minutes)

```bash
# Create test PR to trigger webhook
cd /tmp/test-pr-review-demo
git checkout main
git pull
git checkout -b test-fresh-redis
echo "test" >> README.md
git add README.md
git commit -m "test: fresh Redis account"
git push -u origin test-fresh-redis

# Create PR - should work without Redis limit errors!
gh pr create --repo ayush488-glitch/test-pr-review-demo \
  --base main --head test-fresh-redis \
  --title "Test Fresh Redis Account" \
  --body "Testing with new Upstash Redis account - should not hit 500K limit"
```

**Expected Result:**
- ✅ Webhook receives event (200 OK - not 503)
- ✅ Redis enqueues job successfully
- ✅ Worker processes job
- ✅ AI review comments appear on PR within 30-60 seconds

## 🔧 Troubleshooting

### Issue: Redis connection still fails after update

**Check:**
```bash
# Verify the new Redis URL format
# Should be: redis://user:password@host:port
# NOT: upstash://... or redis+s://...

# Test connection manually
redis-cli -h YOUR_HOST -p PORT -u redis://default:PASSWORD
```

### Issue: Upstash console shows 0 requests but still hitting limits

**Cause:** You might have accidentally logged into the old account
**Fix:**
1. Log out of Upstash completely
2. Use browser Incognito/Private mode
3. Sign up with the new email address

### Issue: Railway/Render won't update env vars

**Railway:**
```bash
# If variable names have special characters, try quoting
railway variables set 'REDIS_URL=redis://default:P@SSw0rd@host:6379'
```

**Render:**
- Use the UI directly (Command line has issues with special chars in URLs)
- Copy-paste the entire URL from Upstash

## 📊 Fresh Redis Setup Summary

| Component | Current (Exhausted) | New (Fresh) |
|-----------|-------------------|-------------
| Email Account | original@domain.com | original+prreview@domain.com |
| Upstash Account | 500K used/month | 0K used / 500K limit |
| Redis URL | redis://old@... | redis://new@... |
| Status | ❌ 500K limit hit | ✅ Fresh capacity |
| Cost | $0 | $0 |

## 🎯 Advantages of This Approach

✅ **Completely Free:** No upgrade costs
✅ **Instant Fix:** 2 minutes account creation
✅ **Fresh Start:** Clean slate for testing
✅ **Dual Email Trick:** Gmail allows +alias for same inbox
✅ **No Code Changes:** Just env var updates
✅ **Reset Monitor:** Can track actual usage pattern

## 📈 Monitor Fresh Redis Usage

**After setup, watch Upstash console:**

1. Go to: https://console.upstash.com (new account)
2. Click on your database
3. Monitor "Requests" chart
4. Note: After our code fixes, you should use ~10% of previous usage

**Good usage target:**
- With optimizations: < 50K requests/month (vs 500K/month before)
- This = 10x buffer! Can test extensively without hitting limits

## ⚠️ Important Notes

**Account Management:**
- Both accounts exist independently
- Old account still has exhausted limit until next month
- New account starts fresh today

**Email Trick (Gmail):**
- `yourname@gmail.com` = `yourname+ai@gmail.com` = `yourname+test@gmail.com`
- All deliver to same inbox
- Upstash sees them as separate accounts with separate free tiers

**Naming Convention:**
- Old Redis: `ai-pr-review-redis` (exhausted)
- New Redis: `ai-pr-review-redis-v2` (fresh)

**Don't forget:**
- Update BOTH Railway AND Render env vars
- Test both platforms to ensure Redis works
- Keep the old Redis URL as backup (can revert if needed)

## 🔄 Rollback Plan (If Needed)

If new Redis has issues:

```bash
# Restore old Redis URL
railway variables set REDIS_URL="redis://old-account-url..."

# Or use Railway web UI to swap back
```

This is safe because we're not changing any code — just swapping Redis connection URL.

---

## ⏰ Time Estimates

- Create new Upstash account: 2 minutes
- Create Redis database: 2 minutes
- Update Railway env vars: 2 minutes
- Update Render env vars: 2 minutes
- Test fresh connection: 2 minutes
- Test webhook flow: 5 minutes

**Total: ~15 minutes to get back to full functionality**

**Then:** Your AI PR review system will work without Redis limits!