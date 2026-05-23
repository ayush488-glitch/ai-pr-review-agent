web: sh -c "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"
worker: python3 -m arq backend.job_queue.arq_worker.WorkerSettings