#!/usr/bin/env bash
# Raw curl Wire-Protocol Examples for Google Cloud CLI Remote MCP Server
# Endpoint: https://cloudcli.googleapis.com/mcp (Stateless MCP 2026-07-28)

set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-YOUR_PROJECT_ID}"
if [[ "${PROJECT_ID}" == "YOUR_PROJECT_ID" ]]; then
  echo "Please set GOOGLE_CLOUD_PROJECT to your Google Cloud project ID:"
  echo "  export GOOGLE_CLOUD_PROJECT=\"YOUR_PROJECT_ID\""
  exit 1
fi

MCP_ENDPOINT="https://cloudcli.googleapis.com/mcp"
MCP_VERSION="2026-07-28"

echo "=== 1. Tool Discovery (tools/list - Unauthenticated) ==="
curl -sS -X POST "${MCP_ENDPOINT}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: ${MCP_VERSION}" \
  -H "Mcp-Method: tools/list" \
  -d @- <<EOF | uv run python -m json.tool
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list",
  "params": {
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "${MCP_VERSION}"
    }
  }
}
EOF

echo ""
echo "=== 2. Execute run_gcloud_command (tools/call - Authenticated) ==="
ACCESS_TOKEN="$(gcloud auth print-access-token --quiet)"

curl -sS -X POST "${MCP_ENDPOINT}" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: ${MCP_VERSION}" \
  -H "Mcp-Method: tools/call" \
  -d @- <<EOF | uv run python -m json.tool
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "run_gcloud_command",
    "arguments": {
      "project": "projects/${PROJECT_ID}",
      "command": "gcloud compute regions list --project=${PROJECT_ID} --limit=3 --format=json --quiet"
    }
  }
}
EOF

echo ""
echo "=== 3. Execute run_gcloud_command with input_files (Virtual File Provisioning) ==="
curl -sS -X POST "${MCP_ENDPOINT}" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: ${MCP_VERSION}" \
  -H "Mcp-Method: tools/call" \
  -d @- <<EOF | uv run python -m json.tool
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "run_gcloud_command",
    "arguments": {
      "project": "projects/${PROJECT_ID}",
      "command": "gcloud compute instances list --project=${PROJECT_ID} --limit=2 --format=json --quiet",
      "input_files": [
        {
          "path": "sample-policy.yaml",
          "contents": "bindings:\n- members:\n  - user:demo@example.com\n  role: roles/viewer\n"
        }
      ]
    }
  }
}
EOF
