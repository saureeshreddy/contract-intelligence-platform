# =============================================================================
# Contract Intelligence Pipeline
#
#   docker build -t contract-intelligence .
#   docker run --rm contract-intelligence
#
# WHY A DOCKERFILE WHEN run.py ALREADY WORKS EVERYWHERE
# -----------------------------------------------------
# run.py is the reviewer's path: clone, pip install, run. Nothing else needed.
# This image serves a different purpose -- a PINNED environment. It answers
# "which Python, which langchain, which platform produced these outputs", which
# `python run.py` cannot, and it is the seam where the production services from
# docs/production_readiness.md get attached (see docker-compose.yml).
#
# Deliberately not a multi-stage build: there is nothing to compile, and a
# second stage would add ceremony without removing a byte.
# =============================================================================
FROM python:3.12-slim

# Fail fast and log immediately -- an unbuffered container is debuggable.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies before source, so editing code does not re-resolve the world.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root. The pipeline only ever writes under /app/**/output, so the
# ownership is scoped to what it actually needs.
RUN useradd --create-home --uid 1000 pipeline \
    && chown -R pipeline:pipeline /app
USER pipeline

# Proves the image can execute the pipeline, not merely that it started.
HEALTHCHECK --interval=30s --timeout=20s --start-period=5s --retries=2 \
    CMD ["python", "run.py", "test"]

# Full demonstration: crash + resume, budget halt, refused resume, review,
# evaluation, tests. Override to run one phase:
#   docker run --rm contract-intelligence python run.py phase1
CMD ["python", "run.py", "all"]
