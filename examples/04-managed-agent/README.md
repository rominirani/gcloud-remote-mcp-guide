# Example 04: Managed Agents (Gemini Enterprise Agent Platform)

This example demonstrates how to configure, verify, and interact with the **Google Cloud CLI Remote MCP Server** (`https://cloudcli.googleapis.com/mcp`) on a stateful, server-managed agent using the **Gemini Enterprise Agent Platform Managed Agents API** and **Interactions API**.

## Security & Architecture Highlights

- **Server-Side MCP Routing & Header Confidentiality**: The Gemini Enterprise Agent Platform routes tool execution requests directly from Google's managed control/data plane to `https://cloudcli.googleapis.com/mcp` and guarantees header confidentiality by **only** sending custom `headers`/tokens to that configured MCP server URL.
- **Patching with `update_mask`**: When updating an existing agent's tools or refreshed OAuth headers via `PATCH`, always include `?update_mask=tools` (or `?update_mask=system_instruction,tools`) so other agent settings are preserved.
- **Data Plane Conversations (`google-genai >= 2.3.0`)**: Once created, invoke the agent via the **Interactions API** (`client.interactions.create`). Always use the unified `google-genai >= 2.3.0` SDK (`genai.Client(enterprise=True, ...)`). Legacy SDKs (`google-cloud-aiplatform`, `@google-cloud/vertexai`, `google-generativeai`) and legacy models (`gemini-2.5-*`, `gemini-2.0-*`, `gemini-1.5-*`) are unsupported for the Interactions API.

## Usage (`uv`)

This project uses [`uv`](https://docs.astral.sh/uv/) for dependency and virtual environment management ([`pyproject.toml`](./pyproject.toml) + PEP 723 inline script metadata).

### Step 1: Preview Control Plane & Data Plane Payloads (`--dry-run`)
Running [`manage_agent.py`](./manage_agent.py) with `--dry-run` makes no live API calls—instead, it prints the exact REST endpoints and JSON payloads for the three stages of the Managed Agent lifecycle:
1. **Control Plane Creation (`POST .../agents`)**
2. **Control Plane Updates (`PATCH .../agents/{agent_id}?update_mask=tools`)**
3. **Data Plane Execution (`Interactions API`)**

```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"
uv sync
uv run manage_agent.py --project="${GOOGLE_CLOUD_PROJECT}" --dry-run
```

### Step 2: Provision the Managed Agent in Google Cloud (`--action=provision`)
```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"
uv run manage_agent.py --project="${GOOGLE_CLOUD_PROJECT}" --action=provision
```
This automatically:
1. Registers `https://cloudcli.googleapis.com/mcp` in **Google Cloud Agent Registry** (`agentregistry.googleapis.com/v1alpha/projects/${GOOGLE_CLOUD_PROJECT}/locations/global/services?serviceId=gcloud-remote-mcp`) if not already present, obtaining its canonical `mcpServers` resource URI.
2. Provisions the Managed Agent (`cloud-cli-ops-agent`) via `POST https://aiplatform.googleapis.com/v1beta1/projects/${GOOGLE_CLOUD_PROJECT}/locations/global/agents`, polls the Long-Running Operation (LRO) until `"done": true`, and prints the completed `Agent` resource.

### Step 3: Check Provisioned Agent & Agent Registry Status (`--action=status`)
```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"
uv run manage_agent.py --project="${GOOGLE_CLOUD_PROJECT}" --action=status
```
This verifies:
- `GET .../locations/global/agents/cloud-cli-ops-agent` (full provisioned Agent configuration and `effectiveIdentity`)
- `GET .../locations/global/agents` (list of all Managed Agents in the project)
- `GET https://agentregistry.googleapis.com/v1alpha/projects/${GOOGLE_CLOUD_PROJECT}/locations/global/services/gcloud-remote-mcp` (registered MCP server entry in Google Cloud Agent Registry)

### Step 4: Interact with the Managed Agent via Code (`--action=interact`)
```bash
export GOOGLE_CLOUD_PROJECT="your-project-id"
uv run manage_agent.py \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --action=interact \
  --prompt="Use run_gcloud_command to list the first 3 Compute Engine regions in project ${GOOGLE_CLOUD_PROJECT} (--format=json --limit=3 --quiet) and summarize them."
```
