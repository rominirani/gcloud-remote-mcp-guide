"""FastAPI entrypoint for the Part 3 Resumable HITL ADK Workflow on Cloud Run.

Configures ADK's FastAPI server (`get_fast_api_app`) with `VertexAiSessionService`
(`agentengine://{agent_engine_id}`) so session state and HITL event checkpoints
persist across stateless Cloud Run instances and scale-to-zero events.
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app

BASE_DIR = Path(__file__).resolve().parent

os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")

project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
agent_engine_id = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID", "").strip()

if agent_engine_id:
    if agent_engine_id.startswith("agentengine://"):
        session_service_uri = agent_engine_id
    else:
        session_service_uri = f"agentengine://{agent_engine_id}"
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
        "location": location,
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
