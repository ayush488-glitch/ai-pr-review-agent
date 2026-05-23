# Multi-Platform Deployment: Railway vs Render

## Current Status

### Railway Deployment
- ✅ **Web Service**: Online (`https://web-production-d9e54.up.railway.app`)
- ❌ **Worker Service**: Failed (blocked by free-tier peak hours)
- 🚧 **Problem**: Railway free tier blocks deployments 8 AM–8 PM Europe/Amsterdam
- ✅ **Configuration**: Procfile added, committed, pushed

### Render Deployment  
- 🆕 **Configuration**: `render.yaml` added to repository
- ⏳ **Status**: Ready for deployment (no time restrictions)
- ✅ **Blueprint**: Creates both web and worker services automatically

## Deployment Instructions

### Railway (After 8 PM CEST)

1. **Manual Redeploy**:
   ```bash
   railway up
   ```
   
2. **Or use Railway Dashboard**:
   - Go to `ai-pr-review-worker` service
   - Click "Manual Deploy"
   - Select `main` branch

3. **Monitor Deployment**:
   ```bash
   railway status
   railway logs --service worker
   ```

### Render (Immediate)

1. **Create Blueprint Deployment**:
   - Go to [render.com/dashboard](https://render.com/dashboard)
   - Click "New Blueprint"
   - Connect GitHub account
   - Select: `ayush488-glitch/ai-pr-review-agent`
   - Branch: `main`
   - Render will detect `render.yaml`

2. **Set Environment Variables**:
   - Copy same values from Railway (Neon, Upstash, Qdrant, API keys)
   - Set in both web and worker services
   - Variables marked `sync: false` in render.yaml need manual setup

3. **Deploy**: Click "Save & Deploy"

4. **Get URLs**:
   - Web Service: Render provides URL
   - Update GitHub webhook to use Render URL

## Shared Cloud Services

Both deployments use the same infrastructure (recommended):

```bash
# Database (Neon Cloud)
DATABASE_URL=postgresql+asyncpg://neondb_owner:npg_vdmci8JzQ7MU@ep-patient-queen-apnt6mg0.c-7.us-east-1.aws.neon.tech/neondb?ssl=require

# Redis (Upstash Cloud)  
REDIS_URL=rediss://default:gQAAAAAAAXs9AAIgcDJlNDZlYzUwYTU3MDg0ZDI1YWUwODQ4YjkwYzQ5MThhYw@touched-teal-97085.upstash.io:6380

# Vector Store (Qdrant Cloud)
QDRANT_URL=https://249ade2d-37f6-4251-9b75-13dc71753edd.eu-west-1-0.aws.cloud.qdrant.io:6333
QDRANT_API_KEY=eyJhbG...h5w0
```

## Configuration Files Created

### Railway
- **Procfile**: Service startup commands
  - `web: sh -c "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"`
  - `worker: python3 -m arq backend.job_queue.arq_worker.WorkerSettings`

### Render  
- **render.yaml**: Blueprint configuration
  - Defines web service and worker service
  - Environment variables structure
  - Build and start commands

## Deployment Flow

```
GitHub Push → Railway Deploy (after 8 PM CEST) → Web + Worker Services
              ↓
         Render Deploy (immediate) → Web + Worker Services
```

## Service URLs (After Deployment)

### Railway
- **Web**: `https://web-production-d9e54.up.railway.app`
- **Webhook URL**: `https://web-production-d9e54.up.railway.app/webhooks/github`
- **Health Check**: `https://web-production-d9e54.up.railway.app/health`

### Render (TBD after deployment)
- **Web**: `https://ai-pr-review-web.onrender.com` (example)
- **Webhook URL**: `https://ai-pr-review-web.onrender.com/webhooks/github`  
- **Health Check**: `https://ai-pr-review-web.onrender.com/health`

## Testing Strategy

### 1. Testing Railway Worker Fix
- Wait until after 8 PM CEST (Europe/Amsterdam)
- Trigger redeploy of worker service
- Monitor logs for successful ARQ worker startup
- Verify worker processing jobs

### 2. Testing Render Deployment
- Deploy immediately (no time restrictions)
- Monitor both web and worker service logs
- Test health endpoints
- Verify worker connects to same Redis/Neon/Qdrant

### 3. Testing End-to-End
1. Create test PR on GitHub repository
2. Configure webhook to point to chosen platform
3. Monitor webhook delivery
4. Watch worker process the review job
5. Verify review comments posted on PR

## Advantages of Dual Deployment

### Railway
- ✅ Already deployed web service (working)
- ✅ Familiar with Railway dashboard
- ✅ Built-in service discovery
- ❌ Free tier time restrictions

### Render
- ✅ No deployment time restrictions
- ✅ More generous free tier (750 vs 500 hours)
- ✅ Worker service type designed for background jobs
- ✅ Blueprint-based configuration (easily replicable)

## Recommended Approach

1. **Immediate**: Deploy to Render (no time restrictions)
   - Full system can be tested immediately
   - Worker service starts right away
   - Faster iteration during development

2. **Tonight (after 8 PM CEST)**: Fix Railway worker
   - Maintain Railway deployment for redundancy
   - Compare performance between platforms
   - Hot-swap if needed

3. **Production Decision**: Choose based on:
   - Performance benchmarks
   - Cost comparison
   - Operational preference

## Environment Variable Checklist

For both platforms, ensure these are set:

- [ ] `GITHUB_WEBHOOK_SECRET` - Generate with `openssl rand -hex 32`
- [ ] `GITHUB_TOKEN` - GitHub PAT with repo:read+write
- [ ] `DATABASE_URL` - Neon Postgres connection string
- [ ] `REDIS_URL` - Upstash Redis connection string  
- [ ] `QDRANT_URL` - Qdrant Cloud endpoint
- [ ] `QDRANT_API_KEY` - Qdrant Cloud API key
- [ ] `OPENAI_API_KEY` - OpenAI API key
- [ ] `ANTHROPIC_API_KEY` - Anthropic API key (optional)
- [ ] `API_KEY` - Internal API authentication

## Next Actions

1. ** Deploy to Render** (immediate):
   ```bash
   # Use Render dashboard to deploy via render.yaml
   # No CLI needed
   ```

2. **Monitor Railway** (tonight after 8 PM CEST):
   ```bash
   railway up  # Deploy worker fix
   ```

3. **Test Both Platforms**:
   - Create test PR
   - Configure webhooks
   - Compare behavior

4. **Choose Production Platform**:
   - Performance metrics
   - Cost analysis
   - Operational preference