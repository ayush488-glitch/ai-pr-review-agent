# Dockerfile — AI PR Review Agent
#
# Stage: single-stage build for development/demo.
# Production note: for a true production image you would use a multi-stage
# build (builder stage installs deps, final stage copies only dist-info).
# That optimisation is deferred to Phase 18 (CI/CD for AI) when we harden
# the image for deployment.
#
# release-it/Design-for-Production: "Systems spend much more of their life
# in operation than in development." The Dockerfile is part of the system.
#
# Base image: python:3.10-slim avoids the 1.3 GB full Debian image while
# keeping the system-level libraries (libpq, libssl) that asyncpg and
# httpx need. Alpine alternatives can be faster to pull but have caused
# C-extension build failures — not worth the risk here.

FROM python:3.10-slim

# Prevents Python from writing .pyc files (saves disk I/O in containers)
ENV PYTHONDONTWRITEBYTECODE=1

# Prevents Python output from being buffered — ensures logs appear in
# real time in docker-compose logs and Kubernetes pod logs.
ENV PYTHONUNBUFFERED=1

# Install system-level dependencies needed at runtime:
#   libpq-dev   — asyncpg (async Postgres driver) needs libpq
#   curl        — used in healthcheck probes
# We run apt-get in a single RUN layer and clean up apt lists to keep
# the image small. Each RUN creates a new image layer.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency manifest FIRST — before source code.
# Docker caches each layer. If pyproject.toml hasn't changed, the
# expensive pip install layer is served from cache even if source changed.
# This makes iterative development rebuilds significantly faster.
COPY pyproject.toml ./

# Install the package in editable mode so the 'backend' package is importable.
# --no-cache-dir: don't store the pip wheel cache inside the image.
# The editable install also reads all [project.dependencies] from pyproject.toml.
RUN pip install --no-cache-dir -e .

# Now copy the full source. Changing source code invalidates only this layer
# and anything below it — the pip layer above is still cached.
COPY . .

# Expose the API port. This is documentation — it does not publish the port.
# The actual port binding is in docker-compose.yml (ports: "8000:8000").
EXPOSE 8000

# Default command: run the FastAPI API server.
# The ARQ worker uses a different CMD, overridden in docker-compose.yml.
#
# --host 0.0.0.0: bind to all interfaces so Docker can forward traffic to it.
# --port 8000: matches EXPOSE and docker-compose ports mapping.
# --workers 1: single worker in dev; Phase 18 bumps this for production.
#
# Note: --reload is NOT used here. We only want reload in local dev
# (outside Docker). Inside Docker, source changes require a rebuild anyway.
# WHY ["sh", "-c", "..."] form:
#   Railway parses the Dockerfile CMD to extract the start command.
#   Shell form (CMD uvicorn ... ${PORT:-8000}) confuses Railway's parser -> crash.
#   Plain JSON array (["uvicorn", "--port", "8000"]) doesn't expand $PORT env var.
#   ["sh", "-c", "..."] gives us BOTH: Railway parses it as valid JSON array,
#   and sh -c runs it through a shell which expands ${PORT:-8000} at runtime.
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
