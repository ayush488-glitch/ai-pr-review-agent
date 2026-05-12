# Procfile — process declarations for Railway (and Heroku-compatible platforms)
#
# WHY TWO PROCESSES:
#   web:    The FastAPI app — handles HTTP (webhooks, REST API, health check).
#           Railway injects $PORT at runtime; we bind to 0.0.0.0:$PORT so
#           Railway's internal router can reach it. Never hardcode 8001 here —
#           that port is the local HOST-side mapping in docker-compose only.
#
#   worker: The ARQ background worker — pulls jobs from Redis and runs the
#           LangGraph review pipeline. Has no HTTP port — it is a pure
#           consumer. Must be a separate Railway service (same repo, same
#           build, different start command).
#
# RAILWAY SETUP:
#   Service 1 (api):    start command = the "web" line below
#   Service 2 (worker): start command = the "worker" line below
#   Both services share the same GitHub repo and the same build image.
#
# LOCAL DEV:
#   docker-compose.yml ignores this file — it uses CMD in the Dockerfile.
#   This file only matters for Railway / Render / Heroku deploys.

web: uvicorn backend.main:app --host 0.0.0.0 --port $PORT
worker: python3 -m arq backend.job_queue.arq_worker.WorkerSettings
