"""FastAPI entrypoint for the Part 3 Resumable HITL ADK Workflow on Cloud Run.

Configures ADK's FastAPI server (`get_fast_api_app`) with `VertexAiSessionService`
using the explicit 6-part `agentengine://` resource URI:
  agentengine://projects/{project}/locations/{region}/reasoningEngines/{agent_engine_id}

This allows `VertexAiSessionService` to persist sessions and HITL event checkpoints
in a regional Vertex AI ReasoningEngine (e.g. `us-central1`) while `gemini-3-flash-preview`
continues using `GOOGLE_CLOUD_LOCATION=global`.
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app

BASE_DIR = Path(__file__).resolve().parent

os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")

project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
agent_engine_location = os.environ.get(
    "AGENT_ENGINE_LOCATION", os.environ.get("GOOGLE_CLOUD_REGION", "us-central1")
)
agent_engine_id = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID", "")

if project_id and agent_engine_id:
    if "/" in agent_engine_id:
        session_service_uri = f"agentengine://{agent_engine_id}"
    else:
        session_service_uri = (
            f"agentengine://projects/{project_id}/locations/{agent_engine_location}"
            f"/reasoningEngines/{agent_engine_id}"
        )
else:
    session_service_uri = os.environ.get("SESSION_SERVICE_URI", "memory://")

app: FastAPI = get_fast_api_app(
    agents_dir=str(BASE_DIR),
    session_service_uri=session_service_uri,
    web=True,
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Health check endpoint reporting the active session backend."""
    backend = (
        "VertexAiSessionService"
        if session_service_uri.startswith("agentengine://")
        else session_service_uri
    )
    return {
        "status": "ok",
        "service": "gcloud-mcp-hitl-workflow",
        "session_backend": backend,
        "session_service_uri": session_service_uri,
        "project": project_id or "unset",
        "agent_engine_location": agent_engine_location,
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
