# syntax=docker/dockerfile:1.7
# Generic image for every containerised PM Agent component (docs/12 §2). One image per component:
#
#   docker build -f deploy/Dockerfile --build-arg COMPONENT=mcp-ado \
#     -t us-central1-docker.pkg.dev/<project>/pm-agent/mcp-ado:<sha> .
#
# COMPONENT = the service name: pm-workbench, signal-event-gateway, mcp-verifier, mcp-ledger, mcp-store,
# mcp-correlation, mcp-ado, mcp-ado-write, mcp-servicenow, mcp-pagerduty, mcp-msgraph, mcp-msgraph-write,
# mcp-figma, mcp-figma-write, mcp-kb, mcp-kb-write, svc-prototype-renderer, dls-catalog-sync, ledger-anchor.
# A single dispatcher (`python -m pm_agent.serve`) reads PM_AGENT_COMPONENT; servers listen on $PORT (8080),
# MCP at /mcp, health at /healthz.
#
# Agent Runtime apps (pm_agent.core_app:app, pm_agent.ingestion_app:app, pm_agent.executor_app:app) are deployed
# with the Agent Platform SDK, not with this image — see deploy/agent_runtime/README.md.
#
# The build context is the repo root; deploy/Dockerfile.dockerignore (BuildKit, Docker >= 23) allow-lists only
# pyproject.toml, README.md, src/ and config/ (pm_agent.yaml, DLS snapshot) — the SLB corpus (shared_files/), .venv and caches never
# enter an image.

ARG PYTHON_IMAGE=python:3.13-slim

# ---------------------------------------------------------------- build: install the package from pyproject
FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip \
 && pip install ".[gcp]"

# ---------------------------------------------------------------- runtime: minimal, non-root
FROM ${PYTHON_IMAGE} AS runtime
ARG COMPONENT
RUN test -n "${COMPONENT}" || { echo "build-arg COMPONENT is required (e.g. mcp-ado)" >&2; exit 1; } \
 && echo "${COMPONENT}" | grep -Eq '^[a-z][a-z0-9-]*$' || { echo "invalid COMPONENT: ${COMPONENT}" >&2; exit 1; }

# OS security patches (supply-chain gate: no critical CVEs, docs/12 §4 stage 7).
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app

COPY --from=build /opt/venv /opt/venv
COPY --chown=10001:10001 config/pm_agent.yaml /app/config/pm_agent.yaml
COPY --chown=10001:10001 config/dls_catalog.sample.json /app/config/dls_catalog.sample.json

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PM_AGENT_COMPONENT=${COMPONENT} \
    PM_AGENT_CONFIG=/app/config/pm_agent.yaml \
    PM_AGENT_DLS_SNAPSHOT=/app/config/dls_catalog.sample.json \
    PORT=8080

LABEL org.opencontainers.image.title="pm-agent-${COMPONENT}" \
      org.opencontainers.image.description="SLB PM Agent component ${COMPONENT} (reference architecture)" \
      org.opencontainers.image.source="deploy/Dockerfile"

WORKDIR /app
USER 10001:10001
EXPOSE 8080

# Runtime env (set by Cloud Run / k8s, never baked): PM_AGENT_ENV, GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION,
# GOOGLE_GENAI_USE_VERTEXAI=true, GOOGLE_GENAI_USE_ENTERPRISE=true, PM_AGENT_VERIFIER_KEY, PM_AGENT_APPROVAL_KEY.
CMD ["python", "-m", "pm_agent.serve"]
