# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

# `docker build --target test` runs the unit tests; the runtime image never includes them.
FROM base AS test
COPY requirements-dev.txt pyproject.toml ./
RUN pip install -r requirements-dev.txt
COPY indexer ./indexer
COPY tests ./tests
RUN python -m pytest -q

FROM base AS runtime
COPY indexer ./indexer
RUN useradd --uid 10001 --no-create-home app
USER 10001
EXPOSE 8081
CMD ["python", "-m", "indexer.main"]
