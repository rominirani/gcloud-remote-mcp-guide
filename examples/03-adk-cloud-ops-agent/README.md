# Example 03: ADK Cloud Operations & BigQuery Agent

This example implements a **Google Agent Development Kit (ADK)** agent in Python (`gemini-3-flash-preview`) that connects to the **Google Cloud CLI Remote MCP Server** (`https://cloudcli.googleapis.com/mcp`).

## Key Features

1. **Zero Local SDK Installation**: [`Dockerfile`](./Dockerfile) uses a minimal `python:3.12-slim` base image without installing `google-cloud-sdk`.
2. **Dynamic OAuth Token Refresh (`header_provider`)**: `get_dynamic_mcp_headers` in [`app/agent.py`](./app/agent.py) refreshes Application Default Credentials (ADC) before each tool call.
3. **IAM-Driven Access Control**: Instead of maintaining brittle client-side command filters, permissions are enforced directly by Google Cloud IAM roles granted to the caller or Cloud Run Service Account (e.g., `roles/mcp.toolUser` plus read-only roles like `roles/compute.viewer` and `roles/bigquery.dataViewer`).

## Quickstart

### 1. Run Live End-to-End ADK Test
```bash
export GOOGLE_CLOUD_PROJECT="YOUR_PROJECT_ID"
uv run python run_live_adk_test.py
```

### 2. Run Interactive ADK Web Playground
```bash
uv sync
export GOOGLE_CLOUD_PROJECT="YOUR_PROJECT_ID"
export GOOGLE_CLOUD_LOCATION="global"
export GOOGLE_GENAI_USE_VERTEXAI="True"
export ADK_MODEL="gemini-3-flash-preview"
uv run adk web
```

### 3. Deploy to Cloud Run
```bash
export GOOGLE_CLOUD_PROJECT="YOUR_PROJECT_ID"
./deploy.sh
uv run python test_cloud_run.py --url <CLOUD_RUN_SERVICE_URL> --project YOUR_PROJECT_ID
```
