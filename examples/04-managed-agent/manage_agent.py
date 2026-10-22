#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "google-auth>=2.35.0",
#     "requests>=2.32.0",
# ]
# ///
"""Gemini Enterprise Agent Platform — Managed Agents Control Plane & Data Plane Utility.

Demonstrates how to:
1. Register https://cloudcli.googleapis.com/mcp in Google Cloud Agent Registry
2. Create a stateful server-managed agent wired to https://cloudcli.googleapis.com/mcp
3. Poll the Long-Running Operation (LRO) until provisioning completes
4. Print the Control Plane PATCH (update_mask) and Data Plane (Interactions API) payloads
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "YOUR_PROJECT_ID")
DEFAULT_LOCATION = "global"
DEFAULT_AGENT_ID = "cloud-cli-ops-agent"
GCLOUD_MCP_URL = "https://cloudcli.googleapis.com/mcp"


def get_access_token() -> str:
    """Retrieves an OAuth 2.0 access token via ADC, environment, or gcloud."""
    env_token = os.environ.get("GCP_ACCESS_TOKEN")
    if env_token:
        return env_token.strip()

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


def ensure_agent_registry_mcp_server(
    project_id: str, location: str, service_id: str, token: str
) -> str:
    """Ensures cloudcli.googleapis.com/mcp is registered in Agent Registry and returns its mcpServers resource name."""
    service_url = (
        f"https://agentregistry.googleapis.com/v1alpha/projects/{project_id}"
        f"/locations/{location}/services/{service_id}"
    )
    try:
        existing = api_request("GET", service_url, token, project_id)
        if existing.get("registryResource"):
            print(f"Found existing Agent Registry MCP server: {existing['registryResource']}")
            return existing["registryResource"]
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise

    create_url = (
        f"https://agentregistry.googleapis.com/v1alpha/projects/{project_id}"
        f"/locations/{location}/services?serviceId={service_id}"
    )
    service_payload = {
        "displayName": "cloudcli.googleapis.com",
        "description": "Google Cloud CLI Remote MCP Server (run_gcloud_command, run_bq_command)",
        "interfaces": [
            {
                "url": GCLOUD_MCP_URL,
                "protocolBinding": "JSONRPC",
            }
        ],
        "mcpServerSpec": {
            "type": "NO_SPEC",
        },
    }
    print(f"Registering {GCLOUD_MCP_URL} in Agent Registry ({service_id})...")
    op = api_request("POST", create_url, token, project_id, service_payload)
    if op.get("done") and op.get("response", {}).get("registryResource"):
        return op["response"]["registryResource"]

    op_name = op.get("name")
    if op_name:
        poll_url = f"https://agentregistry.googleapis.com/v1alpha/{op_name}"
        for _ in range(12):
            status = api_request("GET", poll_url, token, project_id)
            if status.get("done"):
                reg_res = status.get("response", {}).get("registryResource")
                if reg_res:
                    print(f"Registered Agent Registry MCP Server: {reg_res}")
                    return reg_res
                break
            time.sleep(2)

    svc = api_request("GET", service_url, token, project_id)
    return svc["registryResource"]


def build_create_payload(
    project_id: str,
    agent_id: str,
    mcp_token: str,
    mcp_registry_name: str | None = None,
) -> dict[str, Any]:
    """Builds the Control Plane POST payload for a Managed Agent with Remote MCP."""
    mcp_tool: dict[str, Any] = {
        "type": "mcp_server" if mcp_registry_name else "mcp",
        "name": mcp_registry_name or "gcloud-remote-mcp",
        "url": GCLOUD_MCP_URL,
        "headers": {
            "Authorization": f"Bearer {mcp_token}",
        },
    }
    return {
        "id": agent_id,
        "base_agent": "antigravity-preview-05-2026",
        "description": (
            "Server-managed Google Cloud Operations & BigQuery agent "
            "equipped with the Cloud CLI Remote MCP server."
        ),
        "system_instruction": (
            f"You are a Google Cloud Operations specialist. When invoking "
            f"run_gcloud_command or run_bq_command, always set "
            f"project='projects/{project_id}', include --project={project_id} "
            f"(or --project_id={project_id} for bq), and use '=' for all long flags."
        ),
        "tools": [mcp_tool],
    }


def api_request(
    method: str,
    url: str,
    token: str,
    project_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Executes an authenticated REST request against Google Cloud APIs."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "x-goog-user-project": project_id,
            "Content-Type": "application/json; charset=utf-8",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code} from {url}:\n{err_body}")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage a Gemini Enterprise Agent Platform Managed Agent with Cloud CLI MCP."
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Control Plane & Data Plane payloads without calling the API.",
    )
    args = parser.parse_args()

    endpoint = (
        f"https://aiplatform.googleapis.com/v1beta1/projects/{args.project}"
        f"/locations/{args.location}/agents"
    )

    if args.dry_run:
        sample_payload = build_create_payload(
            args.project, args.agent_id, "<OAUTH_ACCESS_TOKEN>"
        )
        print("=== 1. Control Plane: Create Agent Request (POST) ===")
        print(f"Endpoint: {endpoint}\n")
        print(json.dumps(sample_payload, indent=2))
        print("\n=== 2. Control Plane: Patch Agent Tools Request (PATCH) ===")
        print(
            f"Endpoint: {endpoint}/{args.agent_id}?update_mask=tools\n"
            "(Note: update_mask is required to avoid overwriting other agent fields.)"
        )
        print("\n=== 3. Data Plane: Converse via Interactions API ===")
        print(
            json.dumps(
                {
                    "agent": f"projects/{args.project}/locations/{args.location}/agents/{args.agent_id}",
                    "input": [
                        {
                            "type": "user_input",
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"List the first 3 Compute Engine regions in {args.project}.",
                                }
                            ],
                        }
                    ],
                },
                indent=2,
            )
        )
        return

    if args.project == "YOUR_PROJECT_ID":
        raise SystemExit(
            "Please pass --project YOUR_PROJECT_ID or export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID"
        )

    token = get_access_token()
    mcp_registry_name = ensure_agent_registry_mcp_server(
        args.project, args.location, "gcloud-remote-mcp", token
    )
    payload = build_create_payload(args.project, args.agent_id, token, mcp_registry_name)
    print(f"Creating Managed Agent '{args.agent_id}' in {args.project}...")
    op = api_request("POST", endpoint, token, args.project, payload)
    print("LRO initiated:\n", json.dumps(op, indent=2))

    op_name = op.get("name")
    if op_name:
        poll_url = f"https://aiplatform.googleapis.com/v1beta1/{op_name}"
        for attempt in range(1, 13):
            status = api_request("GET", poll_url, token, args.project)
            if status.get("done"):
                print("Agent provisioning completed:\n", json.dumps(status, indent=2))
                break
            print(f"Waiting for LRO completion (poll {attempt}/12)...")
            time.sleep(5)
        else:
            print(f"LRO still in progress. Poll status via: GET {poll_url}")


if __name__ == "__main__":
    main()
