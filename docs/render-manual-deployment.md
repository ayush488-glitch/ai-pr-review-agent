# Render Manual Deployment Guide

If the Blueprint fails, use this manual setup approach.

## Why Manual?

Render's Blueprint system is powerful but can be complex. Manual setup gives you more control and better error visibility.

## Step 1: Web Service Deployment

### 1. Create Web Service
1. Go to Render Dashboard → **"New +"** → **"Web Service"**
2. **Connect Repository**: `https://github.com/ayush488-glitch/ai-pr-review-agent`
3. **Branch**: `main`
4. **Name**: `ai-pr-review-web`
5. **Region**: `Oregon` (or `Singapore` for Asia)
6. **Runtime**: `Python`
7. **Build Command**: `pip install -e .`
8. **Start Command**: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT --workers 1`

### 2. Configure Environment Variables (Critical!)

Add these environment variables one by one:

```bash
# GitHub Integration
GITHUB_WEBHOOK_SECRET=ebaa831c6af59b3db6602713b194fe147ef75ae52b6abb969f542a169b79d367
GITHUB_TOKEN=your_github_pat_here
GITHUB_API_BASE_URL=https://api.github.com

# Database (Neon)
DATABASE_URL=postgresql+asyncpg://neondb_owner:npg_vdmci8JzQ7MU@ep-patient-queen-apnt6mg0.c-7.us-east-1.aws.neon.tech/neondb?ssl=require

# Redis (Upstash)
REDIS_URL=rediss://default:gQAAAAAAAXs9AAIgcDJlNDZlYzUwYTU3MDg0ZDI1YWUwODQ4YjkwYzQ5MThhYw@touched-teal-97085.upstash.io:6380

# Qdrant
QDRANT_URL=https://249ade2d-37f6-4251-9b75-13dc71753edd.eu-west-1-0.aws.cloud.qdrant.io:6333
QDRANT_API_KEY=your_qdrant_api_key_here
QDRANT_COLLECTION_NAME=codebase_embeddings

# LLM Providers
OPENAI_API_KEY=your_openai_api_key_here
ANTHROPIC_API_KEY=your_anthropic_api_key_here
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
SECURITY_PROVIDER=openai
SECURITY_MODEL=gpt-4o

# Application Settings
APP_ENV=production
LOG_LEVEL=INFO
MAX_CONCURRENT_REVIEWS=3
CONFIDENCE_THRESHOLD=0.7
WORKFLOW_TIMEOUT_SECONDS=300

# API Authentication
API_KEY=your_internal_api_key_here
```

### 3. Deploy Web Service
- Click **"Create Web Service"**
- Wait 5-10 minutes for build and deploy
- Copy the URL provided: `https://ai-pr-review-web.onrender.com`

### 4. Test Web Service
```bash
curl https://ai-pr-review-web.onrender.com/health
```

Expected response:
```json
{
  "status": "ok",
  "services": {
    "postgres": "ok",
    "redis": "ok",
    "qdrant": "ok"
  }
}
```

## Step 2: Worker Service Deployment

### 1. Create Worker Service
1. Go to Render Dashboard → **"New +"** → **"Worker"**
2. **Connect Repository**: Same as web service
3. **Branch**: `main`
4. **Name**: `ai-pr-review-worker`
5. **Region**: `Oregon` (same as web service)
6. **Runtime**: `Python`
7. **Build Command**: `pip install -e .`
8. **Start Command**: `python3 -m arq backend.job_queue.arq_worker.WorkerSettings`

### 2. Copy Environment Variables
**Important**: Copy the SAME environment variables from the web service, except:
- Remove: `GITHUB_WEBHOOK_SECRET` (worker doesn't need webhook secret)
- Remove: `API_KEY` (worker doesn't need API key)

### 3. Deploy Worker Service
- Click **"Create Worker"**
- Wait 5-10 minutes for build and deploy

### 4. Verify Worker Service
In Render Dashboard:
1. Go to `ai-pr-review-worker` → **Logs**
2. Look for successful log messages: `ARQ worker redis_client connected`
3. Ensure service shows as "Running" status

## Step 3: Configure GitHub Webhook

Once both services are running:

1. **Get your webhook URL**:
   ```
   https://ai-pr-review-web.onrender.com/webhooks/github
   ```

2. **Add GitHub Webhook**:
   - Go to your test repository → Settings → Webhooks
   - Click "Add webhook"
   - **Payload URL**: `https://ai-pr-review-web.onrender.com/webhooks/github`
   - **Content type**: `application/json`
   - **Secret**: Copy your `GITHUB_WEBHOOK_SECRET` value
   - **Events**: Select "Pull requests" and "Pull request reviews"
   - Click "Add webhook"

## Step 4: Test with Real PR

1. **Create a test PR**:
   - Make a small change to your repository
   - Create a pull request

2. **Monitor webhook delivery**:
   - GitHub webhook page → Recent Deliveries
   - Look for 200 OK response

3. **Monitor Render logs**:
   - Web service logs: Dashboard → `ai-pr-review-web` → Logs
   - Worker logs: Dashboard → `ai-pr-review-worker` → Logs

4. **Check PR for review comments**:
   - Review comments should appear within 1-2 minutes
   - Comments will be structured by domain (security, quality, test, docs)

## Troubleshooting

### Web Service Issues

**Build fails:**
```bash
# Check requirements.txt has all dependencies
# Common issues: missing system libraries
```

**Runtime error:**
```bash
# Check environment variable names are exact
# Verify database connection string format
# Ensure API keys are correct
```

**Health check fails:**
- Check if all external services (Neon, Upstash, Qdrant) are accessible
- Verify network connectivity from Render to these services

### Worker Service Issues

**Worker won't start:**
```bash
# Check ARQ command is correct:
# python3 -m arq backend.job_queue.arq_worker.WorkerSettings

# Verify Redis URL is accessible
# Ensure Python version matches (3.10)
```

**Worker starts but doesn't process jobs:**
- Check Redis queue: `arq:queue:default`
- Verify worker logs show connected to Redis
- Check if webhook is actually enqueuing jobs

### Both Services

**Environment variable issues:**
```bash
# Render uses $PORT, not hardcoded ports
# Redis URL must be rediss:// (with double-s) for TLS
# Database URL must have ssl=require for Neon
```

**GitHub webhook issues:**
- Verify the webhook URL is accessible (test with curl)
- Check the secret matches between GitHub and Render
- Ensure webhook events are selected correctly

## Monitor Services

### Web Service Monitoring
```
Dashboard → ai-pr-review-web → Metrics
- Response time
- Request count  
- Error rate
```

### Worker Service Monitoring
```
Dashboard → ai-pr-review-worker → Metrics
- CPU usage
- Memory usage
- Job processing rate
```

### Logs Analysis
```
Dashboard → ai-pr-review-web → Logs
Dashboard → ai-pr-review-worker → Logs
# Filter by log level: INFO, DEBUG, ERROR
# Search for keywords: "webhook", "error", "completed"
```

## Advantages of Manual Setup

✅ **Better error visibility**: See exactly where each step fails
✅ **Environment variable control**: Copy/paste with verification
✅ **Step-by-step validation**: Test each service independently
✅ **Easier debugging**: Clear separation of web vs worker issues
✅ **No schema issues**: Don't depend on Blueprint validation

## Rollback Strategy

If deployment fails or issues arise:

```bash
# Revert to previous commit
git revert HEAD
git push

# Redeploy in Render
# Web Service → Manual Deploy → Select commit
# Worker Service → Manual Deploy → Select commit
```

## Next Steps

After successful manual deployment:
1. Test with several real PRs
2. Monitor performance and cost
3. Scale worker concurrency if needed
4. Set up alerts for service failures
5. Consider upgrading to paid plan for higher limits