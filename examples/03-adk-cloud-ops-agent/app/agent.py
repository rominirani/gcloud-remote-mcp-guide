"""ADK Cloud Operations Agent wired to the Google Cloud CLI Remote MCP Server.

Connects to https://cloudcli.googleapis.com/mcp using McpToolset and
StreamableHTTPConnectionParams, with dynamic OAuth 2.0 token refresh via
Application Default Credentials (ADC). Access control is enforced by Google
Cloud IAM permissions on the caller or Service Account.
"""

import os
from typing import Any

from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "YOUR_PROJECT_ID")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
GCLOUD_MCP_URL = "https://cloudcli.googleapis.com/mcp"


def get_dynamic_mcp_headers(ctx: Any = None) -> dict[str, str]:
    """Refreshes Application Default Credentials (ADC) for each MCP request."""
    import google.auth
    import google.auth.transport.requests

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    return {
        "Authorization": f"Bearer {credentials.token}",
        "x-goog-user-project": PROJECT_ID,
    }


gcloud_mcp_tools = McpToolset(
    connection_params=StreamableHTTPConnectionParams(url=GCLOUD_MCP_URL),
    header_provider=get_dynamic_mcp_headers,
    tool_filter=["run_gcloud_command", "run_bq_command"],
)

root_agent = Agent(
    name="cloud_ops_agent",
    model=os.environ.get("ADK_MODEL", "gemini-3-flash-preview"),
    description=(
        "Cloud Operations and BigQuery agent powered by the "
        "Google Cloud CLI Remote MCP Server."
    ),
    instruction=f"""You are a Google Cloud Operations & BigQuery assistant.
You have access to `run_gcloud_command` and `run_bq_command` via the remote MCP server.

When calling `run_gcloud_command` or `run_bq_command`:
1. Set `project="projects/{PROJECT_ID}"`.
2. Include `--project={PROJECT_ID}` in `gcloud` commands (or `--project_id={PROJECT_ID}` in `bq` commands).
3. Use '=' between flag names and values (e.g., `--limit=5`, `--format=json`, `--quiet`).
""",
    tools=[gcloud_mcp_tools],
)
