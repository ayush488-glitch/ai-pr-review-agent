# Procfile — Railway process definitions
# web:    FastAPI API (Railway injects $PORT)
# worker: ARQ background job queue (no HTTP port)
#
# Railway creates one service per process type.
# Both services share the same GitHub repo and the same build image.
# Set env vars on EACH service separately in the Railway dashboard.

web: uvicorn backend.main:app --host 0.0.0.0 --port $PORT
worker: python3 -m arq backend.job_queue.arq_worker.WorkerSettings
