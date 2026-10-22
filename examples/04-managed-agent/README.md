# Example 04: Managed Agents (Gemini Enterprise Agent Platform)

This example demonstrates how to configure the **Google Cloud CLI Remote MCP Server** (`https://cloudcli.googleapis.com/mcp`) on a stateful, server-managed agent using the **Gemini Enterprise Agent Platform Managed Agents API**.

## Security & Architecture Highlights

- **Server-Side MCP Routing & Header Confidentiality**: The Gemini Enterprise Agent Platform routes tool execution requests directly from Google's managed control/data plane to `https://cloudcli.googleapis.com/mcp` and guarantees header confidentiality by **only** sending custom `headers`/tokens to that configured MCP server URL.
- **Patching with `update_mask`**: When updating an existing agent's tools or refreshed OAuth headers via `PATCH`, always include `?update_mask=tools` (or `?update_mask=system_instruction,tools`) so other agent settings are preserved.
- **Data Plane Conversations**: Once created, invoke the agent via the **Interactions API** (`client.interactions.create`). Refer to the `gemini-interactions-api` documentation for full multi-turn and streaming options.

## Usage

This project uses [`uv`](https://docs.astral.sh/uv/) for dependency and virtual environment management (`pyproject.toml` + PEP 723 inline script metadata).

### 1. Preview Control Plane & Data Plane Payloads (`--dry-run`)
```bash
uv sync
uv run manage_agent.py --project YOUR_PROJECT_ID --dry-run
```

### 2. Provision the Managed Agent in Google Cloud
```bash
uv run manage_agent.py --project YOUR_PROJECT_ID
```
This script automatically:
1. Registers `https://cloudcli.googleapis.com/mcp` in **Google Cloud Agent Registry** (`agentregistry.googleapis.com/v1alpha/projects/YOUR_PROJECT_ID/locations/global/services?serviceId=gcloud-remote-mcp`) if not already present, obtaining its canonical `mcpServers` resource URI.
2. Provisions the Managed Agent (`cloud-cli-ops-agent`) via `POST https://aiplatform.googleapis.com/v1beta1/projects/YOUR_PROJECT_ID/locations/global/agents` and polls the Long-Running Operation (LRO).
