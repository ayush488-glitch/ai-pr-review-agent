# Upstash Redis Request Limit Issue & Solutions

## 🚨 Current Error

```
ERROR: Redis unavailable during enqueue: Failed to set idempotency key
max requests limit exceeded. Limit: 500000, Usage: 500000
```

**What this means:**
- ✓ Webhook is working perfectly
- ✓ Application is receiving events
- ❌ Redis has hit the free tier request limit
- ❌ Can't queue jobs → 503 Service Unavailable

## 📊 Why Are We Hitting Limits So Fast?

### Likely Causes:
1. **Idempotency checks** - Every webhook attempts to set a Redis key
2. **Health checks** - Frequent health endpoint calls check Redis
3. **Job queue polling** - Worker polling Redis for jobs
4. **Cache hits/misses** - Qdrant embeddings being cached

### Current Usage Pattern:
```
500,000 requests / month ≈ 16,000 requests / day
During testing: Possibly testing repeatedly = rapid consumption
```

## 🔧 Solutions

### Option 1: Reduce Redis Usage (Code Changes) - IMMEDIATE

**Fix 1: Optimize Health Checks**

Current: Every /health call checks Redis
Fix: Cache Redis status, check less frequently

```python
# In backend/main.py
from functools import lru_cache
import time

REDIS_CACHE_TIMEOUT = 60  # Check Redis every 60s
_last_redis_check = 0
_redis_status = "unknown"

def get_redis_status():
    global _last_redis_check, _redis_status
    now = time.time()
    if now - _last_redis_check > REDIS_CACHE_TIMEOUT:
        try:
            # Your existing Redis health check
            _redis_status = check_redis()  # Your existing function
            _last_redis_check = now
        except:
            _redis_status = "error"
    return _redis_status
```

**Fix 2: Reduce Idempotency Key TTL**

Current: Keys persist for hours
Fix: Use shorter TTL since PRs are short-lived

```python
# In backend/webhook_receiver/router.py
# Current (probably 24 hours)
IDEMPOTENCY_TTL = 3600  # 1 hour instead of 24

async def set_idempotency_key(key: str):
    await redis.setex(key, IDEMPOTENCY_TTL, "1")
```

**Fix 3: Batch Redis Operations**

Instead of multiple small requests:
```python
# Before
await redis.set(key1, value1)
await redis.set(key2, value2)
await redis.set(key3, value3)

# After
await redis.mset({key1: value1, key2: value2, key3: value3})
```

### Option 2: Use Upstash Paid Tier - QUICK FIX

**Upgrade Upstash to Grower tier:**
- $0.20/month
- 10M requests/month (20x more)
- No code changes needed

**Steps:**
1. Go to: https://console.upstash.com
2. Select your Redis database
3. Click "Upgrade" button
4. Choose "Grower" tier ($0.20/month)
5. Update: No code changes!

### Option 3: Alternative Redis Providers - FREE ALTERNATIVES

**Redis Cloud (Starter):**
- 30MB free
- 25M commands/month
- Better than Upstash free tier

**RedisToGo (Nano):**
- 10MB free
- Lower limits but no strict request caps

**Run Your Own Redis:**
- Railway Redis plugin (~$0.50/month)
- Render Redis (not available as free service)
- Fly.io Redis (free tier with limits)

### Option 4: Use Railway Redis Instead - EASIEST

**Since you're already using Railway:**
1. Add Redis plugin to Railway project
2. Update REDIS_URL env var
3. Use same credentials for both platforms

**Cost:** Railway Redis is very cheap (~$0.50/month)
**Pros:** Better limits, single billing, easy setup

**Steps:**
```bash
# In Railway CLI
railway add redis

# Copy the Redis URL
railway variables | grep REDIS_URL

# Update Render env var with new Redis URL
# In Render Dashboard → ai-pr-review-web → Environment
# Replace REDIS_URL with Railway Redis URL
```

## ⚡ IMMEDIATE Action - Try This Now

### Step 1: Check Upstash Console

Go to: https://console.upstash.com
- Look at your Redis database stats
- See request usage chart
- Are you really at 500,000 requests?

### Step 2: Reset Or Create New Free Database

If it's just testing debris:
1. Create new Redis database in Upstash
2. Update REDIS_URL for both Railway and Render
3. Start fresh with 500K requests

### Step 3: Apply Quick Code Fixes (5 minutes)

Change idempotency TTL to reduce duplicated requests.

**Edit:** `backend/webhook_receiver/router.py`
```python
# Find where idempotency key is set
# Change TTL from 86400 (24h) to 3600 (1h)
```

### Step 4: Test Again

```bash
# Create new test PR
gh pr create --repo ayush488-glitch/test-pr-review-demo \
  --title "Test After Redis Fix" \
  --body "Testing if Redis limit issue resolved"
```

## 📊 Request Usage Comparison

| Provider | Free Tier Requests | Your Current Usage |
|----------|-------------------|-------------------|
| Upstash Free | 500K/month | 500K (EXHAUSTED) |
| Upstash Grower | 10M/month | $0.20/mo (20x capacity) |
| Redis Cloud | 25M/month | Free |
| Railway Redis | Unlimited* | ~$0.50/mo |

*Railway limits are per instance, not per request

## 🎯 My Recommendation

**Choose based on your timeline:**

### A. Want to test NOW (5 minutes):
- Create new Upstash database (free tier reset)
- Apply TTL code fix (reduces repeated requests)
- Test webhook again

### B. Want reliable testing (2 minutes, $0.20):
- Upgrade Upstash to Grower tier ($0.20/month)
- Start testing immediately

### C. Long-term solution ($0.50/month):
- Migrate to Railway Redis
- Better limits, same billing
- More reliable than Upstash

## 🔍 Debugging Redis Usage

**Check how many requests your app makes:**

```python
# Add this temporarily to see Redis calls
import functools

def track_redis_calls(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        print(f"REDIS CALL: {func.__name__} {args}")
        return await func(*args, **kwargs)
    return wrapper

# Apply to redis client methods
redis.get = track_redis_calls(redis.get)
redis.set = track_redis_calls(redis.set)
redis.setex = track_redis_calls(redis.setex)
```

Run for a few minutes and count how many Redis calls happen.

## 📋 Success Criteria After Fix

- ✅ Webhook receives events (200 OK)
- ✅ Redis available during enqueue (no errors)
- ✅ Job queued successfully
- ✅ Worker processes job
- ✅ AI review comments appear on PR

## ⏰ Time Estimates

- Code fixes (reduce Redis usage): 5-10 minutes
- Upstash Grower upgrade: 2 minutes
- Create new Upstash DB: 5 minutes
- Migrate to Railway Redis: 10 minutes

**Fastest fix:** Upstash Grower upgrade ($0.20, 2 minutes)

---

## 🚨 Important: The Webhook Architecture is Working!

The real news (good news):
- ✅ Webhook endpoint working
- ✅ Event processing working
- ✅ Application logic working
- ✅ Only issue: Redis request limit hit

So once you fix Redis, the entire system should work flawlessly!