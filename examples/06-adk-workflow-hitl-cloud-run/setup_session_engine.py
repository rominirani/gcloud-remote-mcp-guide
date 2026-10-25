#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "google-auth>=2.35.0",
#     "requests>=2.32.0",
# ]
# ///
"""Provisions or reuses a lightweight Vertex AI ReasoningEngine for VertexAiSessionService.

`VertexAiSessionService` stores ADK sessions and HITL event checkpoints under:
  projects/{project}/locations/{region}/reasoningEngines/{agent_engine_id}/sessions/{session_id}

This script checks if a ReasoningEngine with displayName="gcloud-mcp-hitl-session-store"
already exists in your project/region, and creates an empty containerless one if not.

Usage:
  export GOOGLE_CLOUD_PROJECT="your-project-id"
  uv run examples/06-adk-workflow-hitl-cloud-run/setup_session_engine.py \
    --project="${GOOGLE_CLOUD_PROJECT}" \
    --region=us-central1
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from typing import Any

import requests

DEFAULT_PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "YOUR_PROJECT_ID")
DEFAULT_REGION = os.environ.get("AGENT_ENGINE_LOCATION", "us-central1")
DEFAULT_DISPLAY_NAME = "gcloud-mcp-hitl-session-store"


def get_access_token() -> str:
    """Retrieves an OAuth 2.0 access token via ADC or gcloud CLI."""
    try:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(google.auth.transport.requests.Request())
        if credentials.token:
            return credentials.token
    except Exception:
        pass

    gcloud_bin = shutil.which("gcloud")
    if not gcloud_bin:
        raise RuntimeError("Could not find 'gcloud' binary on PATH.")

    res = subprocess.run(
        [gcloud_bin, "auth", "print-access-token", "--quiet"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return res.stdout.strip()


def ensure_session_engine(
    project_id: str, region: str, display_name: str
) -> tuple[str, str]:
    """Finds or creates a ReasoningEngine resource and returns (engine_id, full_name)."""
    token = get_access_token()
    base_url = (
        f"https://{region}-aiplatform.googleapis.com/v1beta1"
        f"/projects/{project_id}/locations/{region}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "x-goog-user-project": project_id,
        "Content-Type": "application/json",
    }

    list_resp = requests.get(
        f"{base_url}/reasoningEngines", headers=headers, timeout=30
    )
    list_resp.raise_for_status()
    engines: list[dict[str, Any]] = list_resp.json().get("reasoningEngines", [])

    for eng in engines:
        if eng.get("displayName") == display_name:
            full_name = str(eng["name"])
            engine_id = full_name.split("/")[-1]
            print(f"Found existing ReasoningEngine: {full_name}")
            return engine_id, full_name

    print(
        f"Creating lightweight ReasoningEngine '{display_name}' in {project_id} ({region})..."
    )
    create_resp = requests.post(
        f"{base_url}/reasoningEngines",
        headers=headers,
        json={
            "displayName": display_name,
            "description": (
                "Serverless session and HITL checkpoint store for ADK Workflow on Cloud Run "
                "(VertexAiSessionService)."
            ),
        },
        timeout=30,
    )
    create_resp.raise_for_status()
    op = create_resp.json()
    op_name = op.get("name", "")

    if op.get("done") and op.get("response", {}).get("name"):
        full_name = str(op["response"]["name"])
        return full_name.split("/")[-1], full_name

    poll_url = f"https://{region}-aiplatform.googleapis.com/v1beta1/{op_name}"
    for attempt in range(1, 31):
        time.sleep(3)
        poll_resp = requests.get(poll_url, headers=headers, timeout=30)
        poll_resp.raise_for_status()
        status = poll_resp.json()
        if status.get("done"):
            if "error" in status:
                raise RuntimeError(f"ReasoningEngine creation failed: {status['error']}")
            full_name = str(status.get("response", {}).get("name", ""))
            engine_id = full_name.split("/")[-1]
            print(f"Created ReasoningEngine: {full_name}")
            return engine_id, full_name
        print(f"  Waiting for ReasoningEngine creation (poll {attempt}/30)...")

    raise TimeoutError("Timed out waiting for ReasoningEngine creation.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision or reuse a Vertex AI Agent Engine ID for VertexAiSessionService."
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--display-name", default=DEFAULT_DISPLAY_NAME)
    args = parser.parse_args()

    if args.project in ("YOUR_PROJECT_ID", "your-project-id", ""):
        raise SystemExit(
            "Error: Please set GOOGLE_CLOUD_PROJECT or pass --project=your-project-id"
        )

    engine_id, full_name = ensure_session_engine(
        args.project, args.region, args.display_name
    )
    session_uri = (
        f"agentengine://projects/{args.project}/locations/{args.region}"
        f"/reasoningEngines/{engine_id}"
    )

    print("\n" + "=" * 78)
    print("VertexAiSessionService Store Ready!")
    print("=" * 78)
    print(f"  ReasoningEngine Resource : {full_name}")
    print(f"  GOOGLE_CLOUD_AGENT_ENGINE_ID : {engine_id}")
    print(f"  ADK Session Service URI  : {session_uri}")
    print("=" * 78)
    print("\nExport for local & Cloud Run execution:")
    print(f'export GOOGLE_CLOUD_PROJECT="{args.project}"')
    print(f'export AGENT_ENGINE_LOCATION="{args.region}"')
    print(f'export GOOGLE_CLOUD_AGENT_ENGINE_ID="{engine_id}"')


if __name__ == "__main__":
    main()
