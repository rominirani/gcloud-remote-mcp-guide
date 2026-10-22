#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "google-auth>=2.35.0",
#     "requests>=2.32.0",
# ]
# ///
"""Simple Wire-Protocol Smoke Test for Google Cloud CLI Remote MCP Server.

Tests https://cloudcli.googleapis.com/mcp against a Google Cloud project:
  1. Unauthenticated tools/list discovery
  2. run_gcloud_command (Compute Engine regions)
  3. run_gcloud_command with input_files (Compute Engine VM instances)
  4. run_bq_command (BigQuery dataset listing)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Any

MCP_ENDPOINT = "https://cloudcli.googleapis.com/mcp"
MCP_PROTOCOL_VERSION = "2026-07-28"
DEFAULT_PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "YOUR_PROJECT_ID")


def get_oauth_access_token() -> str:
    """Retrieves an OAuth 2.0 access token via environment, google-auth ADC, or gcloud."""
    env_token = os.environ.get("GCP_ACCESS_TOKEN")
    if env_token:
        return env_token.strip()

    try:
        import google.auth
        import google.auth.transport.requests

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds.refresh(google.auth.transport.requests.Request())
        if creds.token:
            return creds.token
    except Exception:
        pass

    gcloud_bin = shutil.which("gcloud")
    if not gcloud_bin:
        raise RuntimeError(
            "Could not find 'gcloud' on PATH and google-auth ADC was not available."
        )

    for subcmd in (
        [gcloud_bin, "auth", "print-access-token", "--quiet"],
        [gcloud_bin, "auth", "application-default", "print-access-token", "--quiet"],
    ):
        res = subprocess.run(
            subcmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()

    raise RuntimeError("Failed to obtain an OAuth 2.0 access token from ADC or gcloud.")


def call_mcp(
    method: str,
    params: dict[str, Any],
    token: str | None = None,
    req_id: int = 1,
) -> tuple[int, dict[str, Any]]:
    """Sends a stateless JSON-RPC 2.0 request to cloudcli.googleapis.com/mcp."""
    payload = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params,
    }
    body = json.dumps(payload).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(
        MCP_ENDPOINT,
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw_error": raw}
        return err.code, parsed


def run_suite(project_id: str, output_file: str | None = None) -> dict[str, Any]:
    """Runs the smoke test suite and returns structured results."""
    if project_id in ("YOUR_PROJECT_ID", "your-project-id", ""):
        raise SystemExit(
            "Please provide your actual Google Cloud project ID via "
            "export GOOGLE_CLOUD_PROJECT=\"your-project-id\" or --project=\"${GOOGLE_CLOUD_PROJECT}\""
        )

    print("=== Google Cloud CLI Remote MCP Smoke Test ===")
    print(f"Target Endpoint : {MCP_ENDPOINT}")
    print(f"Target Project  : {project_id}\n")

    results: list[dict[str, Any]] = []

    # 1. Unauthenticated tools/list
    print("[1] Discovering MCP tools via tools/list...")
    status, resp = call_mcp(
        method="tools/list",
        params={
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION
            }
        },
        token=None,
        req_id=1,
    )
    tools = [t.get("name") for t in resp.get("result", {}).get("tools", [])]
    print(f"  -> HTTP {status} | Discovered tools: {tools}")
    results.append(
        {
            "step": "1. tools/list Discovery",
            "http_status": status,
            "discovered_tools": tools,
        }
    )

    token = get_oauth_access_token()

    # 2. run_gcloud_command (Compute regions list)
    cmd_2 = (
        f"gcloud compute regions list --project={project_id} "
        "--limit=3 --format=json --quiet"
    )
    print(f"\n[2] Running run_gcloud_command:\n  $ {cmd_2}")
    status, resp = call_mcp(
        method="tools/call",
        params={
            "name": "run_gcloud_command",
            "arguments": {
                "project": f"projects/{project_id}",
                "command": cmd_2,
            },
        },
        token=token,
        req_id=2,
    )
    print(f"  -> HTTP {status}")
    results.append(
        {
            "step": "2. run_gcloud_command (Compute Regions List)",
            "command": cmd_2,
            "http_status": status,
            "response": resp,
        }
    )

    # 3. Serverless input_files provisioning
    cmd_3 = (
        f"gcloud compute instances list --project={project_id} "
        "--limit=2 --format=json --quiet"
    )
    print(f"\n[3] Running run_gcloud_command with input_files:\n  $ {cmd_3}")
    status, resp = call_mcp(
        method="tools/call",
        params={
            "name": "run_gcloud_command",
            "arguments": {
                "project": f"projects/{project_id}",
                "command": cmd_3,
                "input_files": [
                    {
                        "path": "sample-config.yaml",
                        "contents": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: mcp-test\n",
                    }
                ],
            },
        },
        token=token,
        req_id=3,
    )
    print(f"  -> HTTP {status}")
    results.append(
        {
            "step": "3. run_gcloud_command with input_files",
            "command": cmd_3,
            "http_status": status,
            "response": resp,
        }
    )

    # 4. run_bq_command (BigQuery datasets list)
    cmd_4 = f"bq ls --datasets=true --project_id={project_id} --format=prettyjson"
    print(f"\n[4] Running run_bq_command:\n  $ {cmd_4}")
    status, resp = call_mcp(
        method="tools/call",
        params={
            "name": "run_bq_command",
            "arguments": {
                "project": f"projects/{project_id}",
                "command": cmd_4,
            },
        },
        token=token,
        req_id=4,
    )
    print(f"  -> HTTP {status}")
    results.append(
        {
            "step": "4. run_bq_command (BigQuery Dataset Listing)",
            "command": cmd_4,
            "http_status": status,
            "response": resp,
        }
    )

    summary = {
        "endpoint": MCP_ENDPOINT,
        "protocol_version": MCP_PROTOCOL_VERSION,
        "project_id": project_id,
        "results": results,
    }

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"\nSaved smoke test results to {output_file}")

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run smoke test against Google Cloud CLI Remote MCP."
    )
    parser.add_argument(
        "--project",
        default=DEFAULT_PROJECT_ID,
        help="Google Cloud Project ID (or set GOOGLE_CLOUD_PROJECT)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write JSON results",
    )
    parsed_args = parser.parse_args()
    run_suite(parsed_args.project, parsed_args.output)
