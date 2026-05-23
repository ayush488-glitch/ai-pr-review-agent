# Render Deployment Guide

This guide shows how to deploy the AI PR Review Agent on Render using the `render.yaml` configuration.

## Prerequisites

- A Render account (free tier available)
- GitHub repository access
- Cloud services (can be the same ones used for Railway):
  - Neon Postgres (Database)
  - Upstash Redis (Job Queue)
  - Qdrant Cloud (Vector Store)
  - OpenAI API Key (LLM)
  - Anthropic API Key (Optional)

## Quick Setup

1. **Create a Render account** at [render.com](https://render.com)
2. **Connect your GitHub account** to Render
3. **Deploy using render.yaml:**
   - Go to Render Dashboard → "New Blueprint"
   - Select repository: `https://github.com/ayush488-glitch/ai-pr-review-agent`
   - Branch: `main`
   - Render will detect `render.yaml` and create services automatically

## Services Created

The Blueprint creates two services:

### 1. Web Service (`ai-pr-review-web`)
- **Type**: Web Service
- **Runtime**: Python 3.10.13
- **Port**: Automatically assigned by Render
- **Health Check**: `/health` endpoint
- **Domain**: `https://ai-pr-review-web.onrender.com`

### 2. Worker Service (`ai-pr-review-worker`)  
- **Type**: Worker Service
- **Runtime**: Python 3.10.13
- **Purpose**: Processes PR review jobs from Redis queue
- **Runs continuously**: Background job queue processor

## Environment Variables

You'll need to set these environment variables in both services:

### Required Variables (marked `sync: false` in render.yaml):

```bash
# GitHub Integration
GITHUB_WEBHOOK_SECRET=your-webhook-secret
GITHUB_TOKEN=your-github-pat

# Database
DATABASE_URL=postgresql+asyncpg://user:pass@host.neon.tech/db?ssl=require

# Redis (use same Upstash Redis as Railway)
REDIS_URL=rediss://user:pass@host.upstash.io:6380

# Qdrant (use same Qdrant Cloud as Railway)
QDRANT_URL=https://cluster-id.region.aws.cloud.qdrant.io:6333
QDRANT_API_KEY=your-qdrant-api-key

# LLM Providers
OPENAI_API_KEY=your-openai-api-key
ANTHROPIC_API_KEY=your-anthropic-api-key

# API Authentication
API_KEY=your-internal-api-key
```

### Pre-configured Variables:

- `PYTHON_VERSION`: 3.10.13
- `GITHUB_API_BASE_URL`: https://api.github.com
- `QDRANT_COLLECTION_NAME`: codebase_embeddings
- `OPENAI_EMBEDDING_MODEL`: text-embedding-3-small
- `SECURITY_PROVIDER`: openai
- `SECURITY_MODEL`: gpt-4o
- `APP_ENV`: production
- `LOG_LEVEL`: INFO
- `MAX_CONCURRENT_REVIEWS`: 3
- `CONFIDENCE_THRESHOLD`: 0.7
- `WORKFLOW_TIMEOUT_SECONDS`: 300

## GitHub Webhook Setup

Once deployed:

1. **Get your Render web URL**: `https://ai-pr-review-web.onrender.com`
2. **Set up GitHub webhook**:
   - Go to your test repository → Settings → Webhooks
   - Add webhook URL: `https://ai-pr-review-web.onrender.com/webhooks/github`
   - Content type: `application/json`
   - Secret: Match `GITHUB_WEBHOOK_SECRET` env var
   - Events: Pull requests, Pull request reviews

## Verifying Deployment

### 1. Check Web Service Health
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

### 2. Check Worker Service Status
- Go to Render Dashboard → `ai-pr-review-worker`
- Look for "Running" status
- Check logs for worker startup messages

### 3. Test with Real PR
```bash
# Create a test PR on your repository
# Watch Render logs for webhook processing
# Check for review comments posted on the PR
```

## Monitoring

- **Logs**: Render Dashboard → Select service → Logs
- **Metrics**: Render Dashboard → Select service → Metrics
- **Deployments**: Render Dashboard → Select service → Deployments

## Troubleshooting

### Worker Service Issues:
- Check if Redis URL is correct and accessible
- Verify worker command: `python3 -m arq backend.job_queue.arq_worker.WorkerSettings`
- Look for connection errors in logs

### Web Service Issues:
- Verify all environment variables are set
- Check database connection string format
- Ensure GitHub webhook URL is correct

### Build Failures:
- Check requirements.txt for missing dependencies
- Verify Python version matches (3.10.13)
- Review build logs in Render Dashboard

## Scaling

Render renders handles scaling automatically:

- **Web Service**: Scales with traffic (free tier: 1 instance)
- **Worker Service**: Configured in render.yaml (`MAX_CONCURRENT_REVIEWS`)

## Costs

- **Free Tier**: 
  - Web Service: 750 hours/month
  - Worker Service: 750 hours/month
  - No database included (use Neon/Upstash free tiers)

- **Paid Plans**: Available for higher limits and features

## Migration from Railway

If you're moving from Railway:

1. **Keep same cloud services** (Neon, Upstash, Qdrant) - they work on both platforms
2. **Update webhook URLs** from Railway to Render URLs
3. **Copy environment variables** from Railway to Render
4. **Test with a PR** to verify functionality

## Next Steps

After successful deployment:

1. Configure GitHub webhooks to point to Render URL
2. Monitor first few PR reviews for expected behavior
3. Adjust `CONFIDENCE_THRESHOLD` based on review quality
4. Scale worker concurrency if needed

## Comparison: Railway vs Render

| Feature | Railway | Render |
|---------|---------|--------|
| Deployment | `railway up` CLI | render.yaml Blueprint |
| Worker Type | Service-based | Worker service type |
| Time Restrictions | Peak hours on free tier | No peak restrictions |
| Database | Built-in Redis plugin | External (Upstash/Neon) |
| Web Service | Auto-detected | Explicit in YAML |
| Free Tier Hours | 500 hours | 750 hours |

Both platforms work well with the same cloud services (Neon, Upstash, Qdrant).