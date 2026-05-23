# Railway Free Tier - Sleep Issues & Workarounds

## The Problem

Railway's free tier has aggressive sleep behavior:

- ⏸️ **Sleeps after 15 minutes** of inactivity
- 🚫 **Peak hour restrictions**: Runs only 2 hours/day (10-14:00 UTC)
- 😴 **Cold start takes 30-60 seconds**: missed PR review opportunities
- 🔄 **Worker service also sleeps**: Job queue doesn't process

### What This Means for PR Review Agent

PR reviews happen **asynchronously** - they need to process quickly when webhooks arrive. With Railway sleep:

1. Webhook arrives → service is asleep ❌
2. Takes 30-60s to wake up
3. **By the time it wakes**, the critical "PR opened" window has passed
4. Reviews miss the important context (fresh PR changes)
5. User experience: delays or missed reviews

---

## Solutions

### Option 1: Upgrade Railway (Recommended for Production)

**Cost**: ~$5-20/month
- **Pro Plan**: No sleep restrictions
- **Always-on**: Worker and web service stay running
- **Better latency**: Instant webhook processing

**Commands**:
```bash
# Upgrade your project to Pro
railway upgrade

# Set to always-on
railway variables set RAILWAY_DISABLE_MUTEX=false
railway variables set RAILWAY_FAMILY_VARIES=false
```

**When to choose this**:
- You're serious about running this in production
- Want guaranteed PR review latency < 10 seconds
- Don't want to manage multiple platforms

---

### Option 2: Use Render Free Tier (No Sleep)

**Cost**: Free
- **No peak restrictions**: Runs 24/7
- **750 hours/month**: ~23 days continuous runtime
- **Better worker support**: Native Worker service type

**How to migrate**:
1. Follow `docs/render-manual-deployment.md` (step-by-step manual setup)
2. Use the **same cloud services** (Neon, Upstash, Qdrant) - just change host
3. Update GitHub webhook URL
4. Delete Railway services after verification

**Advantages**:
- ✅ Always-on during active development
- ✅ True 24/7 operation on free tier
- ✅ Better error visibility
- ✅ No CLI required

**Disadvantages**:
- ❌ 750 hours/month limit (slept after ~23 days, wake on webhook)
- ❌ Manual service creation (no ClickOps in docs)

**When to choose this**:
- Free dev/testing with occasional PR reviews
- You're okay with occasional free tier resets
- Want to try before paying

---

### Option 3: Railway + Watchdog / Keep-Alive

**Cost**: Free + $5-10/month external service

Use external services to keep Railway awake:

#### Approach A: Uptime Robot + Cron Job

```yaml
# Add this to your railway.yaml
services:
  - name: keep-alive-cron
    type: cron
    schedule: "*/14 * * * *"  # Every 14 minutes
    command: curl -s https://your-railway-app.railway.app/health > /dev/null
```

**Pros**: Simple, cheap
**Cons**: Cron jobs don't bypass peak restrictions

#### Approach B: External Worker + PING

Use GitHub Actions or a cron job to ping the web service every 10 minutes:

```yaml
# .github/workflows/keep-alive.yml
name: Keep Railway Awake
on:
  schedule:
    - cron: '*/10 * * * *'
jobs:
  ping:
    runs-on: ubuntu-latest
    steps:
      - run: curl -f https://your-railway-app.railway.app/health
```

**Pros**: Free, easy to set up
**Cons**: Still limited by peak hour restrictions, webhook may still miss

#### Approach C: UptimeRobot FREE Plan

1. Create UptimeRobot account (free)
2. Add monitor: `https://your-railway-app.railway.app/health`
3. Set interval: 5 minutes

**Pros**: 5-minute intervals keep service awake
**Cons**: Peak restrictions still apply

---

### Option 4: Railway Workers + Pro

**Cost**: $13.50/month minimum

Railway Workers run on a different pricing model:

```yaml
services:
  - name: review-worker
    type: worker
    runtime: node  # or python
    command: python3 -m arq backend.job_queue.arq_worker.WorkerSettings
```

**Worker pricing**:
- **Workers are always-on**: No sleep (even on free tier?)
- **Cost**: Hardware is proportional to usage + $13.50/month base

**Check current Railway docs** - worker pricing is frequently updated.

---

### Option 5: Switch Platform Completely

#### Render + RAILWAY_DISABLE_MUTEX

Render offers:
- 750 hours/month free (vs Railway's 500)
- No peak restrictions
- Better Worker service support

#### Other Free Platforms

| Platform | Free Tier | Sleep | Workers |
|----------|-----------|-------|---------|
| Railway  | 500h + peak | 15m + peak | Yes |
| Render   | 750h | 14 days | Yes (native) |
| Fly.io   | 256h | 48h | Background jobs |
| Railway  | $5/mo | None | Yes |
| AWS Fargate | $20+/mo | None | ECS tasks |

---

## Immediate Fix: Use Render Right Now

**Given your current situation** (Railway crashed + in sleep mode), I recommend:

1. **Deploy to Render** using manual setup:
   - Follow: `docs/render-manual-deployment.md`
   - Create web service + worker service
   - Use the SAME Neon, Upstash, Qdrant

2. **Update GitHub webhook**:
   - Old: `https://your-app.railway.app/webhooks/github`
   - New: `https://ai-pr-review-web.onrender.com/webhooks/github`

3. **Test with a PR**:
   - Create a test PR
   - Watch Render logs (no sleep delays!)
   - Reviews should appear in 30-60 seconds

4. **Keep Railway as fallback**:
   - Don't delete it yet
   - Use it for testing before upgrading to Pro

---

## Long-Term Decision Matrix

Ask yourself these questions:

1. **How many PRs/month do you review?**
   - < 10/month → Render free tier is fine
   - 10-50/month → Render or Railway free tier
   - 50+/month → Upgrade Railway or Render (save time)

2. **How critical is < 60s latency?**
   - Not critical → Free tiers okay
   - Very critical → Paid tier (Railway Pro / Render Pro)

3. **Do you care about "always-on"?**
   - No → Even free tiers work for testing
   - Yes → Need Railway Pro or Render Pro with auto-wake

4. **Budget constraints?**
   - $0/month → Render free tier (best free experience)
   - $5-20/month → Railway Pro (easiest CLI dev experience)
   - $20+/month → Any platform's Pro tier

---

## My Recommendation

### Phase 1: Right Now (Testing)
- Deploy to Render using manual setup ✅
- Keep both running (Railway + Render)
- Test PR reviews on both

### Phase 2: After Testing (Decision)
- **If reviews work well and you're happy**:
  - Stick with Render free tier (750h is plenty for testing)
- **If you want productionspeed**:
  - Upgrade Railway to Pro: `railway upgrade`
  - OR upgrade Render to Pro

### Phase 3: Production (Scaling)
-based on Phase 2 results
- Add monitoring / alerts
- Consider multi-region deployment
- Set up auto-scaling thresholds

---

## Quick Comparison Table

| Feature | Railway Free | Render Free | Railway Pro |
|---------|--------------|-------------|-------------|
| Runtime hours | 500 + peak only | 750 / month | Unlimited |
| Sleeps after | 15m + peak restriction | 14 days | Never |
| Peak hours | 2h/day (10-14 UTC) | None | None |
| Cold start | 30-60s | 30-60s | < 5s |
| Worker support | ✅ | ✅ Better | ✅ |
| SSH access | ✅ | ❌ | ✅ |
| CLI | ✅ railway up | ❌ (manual) | ✅ |
| Monthly cost | Free | Free | $5-20 |
| Good for | Testing | Testing / occasional use | Production |

---

## Next Actions

1. **Read**: `docs/render-manual-deployment.md` (5 minutes)
2. **Try**: Deploy to Render manually (10-15 minutes)
3. **Test**: Create a test PR, watch logs (5 minutes)
4. **Decide**: Between Railway Pro vs Render free vs Render Pro

Why bother with Railway's sleep issues when Render free tier gives you **24/7 operation (for 750 hours)** with better Worker support?

The decision: Pay $5-20/mo for Railway Pro, or use Render free tier with manual setup.

For testing and occasional PR reviews: **Render free tier wins**.

For serious production: **Railway Pro CLI experience** or **Render Pro** (your choice based on UX preference).

---

## Gotchas

### Railway Free Tier Won't Work For:
- Time-sensitive PR reviews (need instant processing)
- High concurrency (peak hour queuing)
- Reliable 24/7 operation

### Render Free Tier Won't Work For:
- Continuous operation beyond ~23 days/month
- Automatic scaling (need Pro)
- Advanced observability (Need Pro)

### Both Need:
- External services (Neon, Upstash, Qdrant, OpenAI, etc.)
- GitHub webhook configuration
- Environment variable management

### Neither Support:
- Free tier: High-volume PR reviews (> 100/day)
- Free tier: Multi-region deployment
- Free tier: SSH into web service containers

---

## Final Answer

**Deploy to Render right now.**

Why?
- Railway is currently asleep/crashed
- Render free tier has no peak restrictions
- You get 750 hours/month (~23 days continuous)
- Better Worker service support
- Same external services work (Neon, Upstash, Qdrant)

Steps:
1. Read `docs/render-manual-deployment.md`
2. Create web service + worker service in Render
3. Update GitHub webhook URL
4. Test with a PR

Then decide later: Stick with Render or upgrade Railway to Pro. |wry|