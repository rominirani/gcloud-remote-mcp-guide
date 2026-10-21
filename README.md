# Google Cloud CLI Remote MCP Server (`cloudcli.googleapis.com/mcp`) — Examples

Ready-to-run examples for integrating Google Cloud's stateless **`gcloud` and `bq` Remote MCP Server** (`https://cloudcli.googleapis.com/mcp`) across raw HTTP/`curl`, Local IDEs (Antigravity & Gemini CLI), the **Google Agent Development Kit (ADK)** using **`gemini-3-flash-preview`**, and **Gemini Enterprise Agent Platform Managed Agents**.

---

## Repository Structure

```text
.
├── README.md                                        # Quickstart guide
└── examples/
    ├── 01-wire-protocol-smoke-test/                 # Direct HTTP / JSON-RPC 2.0 smoke test
    │   ├── README.md
    │   ├── curl_examples.sh                         # Raw curl requests for tools/list & tools/call
    │   └── mcp_probe.py                             # Simple Python smoke test script
    ├── 02-local-ide-config/                         # Local IDE setup (Antigravity / Gemini CLI)
    │   ├── README.md
    │   └── mcp_config.json                          # google_credentials ADC config
    ├── 03-adk-cloud-ops-agent/                      # Google ADK Agent (gemini-3-flash-preview)
    │   ├── README.md
    │   ├── Dockerfile                               # Lean python:3.12-slim container (no Cloud SDK)
    │   ├── pyproject.toml
    │   ├── server.py                                # FastAPI ADK entrypoint for Cloud Run
    │   ├── deploy.sh                                # Cloud Run deployment script
    │   ├── run_live_adk_test.py                     # End-to-end ADK test script
    │   ├── test_cloud_run.py                        # Remote Cloud Run verification script
    │   └── app/
    │       ├── __init__.py
    │       └── agent.py                             # McpToolset + dynamic ADC header_provider
    └── 04-managed-agent/                            # Gemini Enterprise Agent Platform Managed Agents
        ├── README.md
        └── manage_agent.py                          # Agent Registry onboarding + Control/Data Plane script
```

---

## Prerequisites

1. Set your Google Cloud project ID and enable the Cloud CLI Execution API:
   ```bash
   export GOOGLE_CLOUD_PROJECT="YOUR_PROJECT_ID"
   gcloud services enable cloudcli.googleapis.com --project="${GOOGLE_CLOUD_PROJECT}" --quiet
   ```
2. Grant `roles/mcp.toolUser` (`mcp.tools.call` permission) and least-privilege IAM roles (such as `roles/viewer`) to your user identity or Service Account:
   ```bash
   gcloud projects add-iam-policy-binding "${GOOGLE_CLOUD_PROJECT}" \
     --member="user:YOUR_EMAIL@example.com" \
     --role="roles/mcp.toolUser" \
     --quiet
   ```
3. Authenticate local Application Default Credentials (ADC):
   ```bash
   gcloud auth application-default login
   ```

---

## Quickstart Commands

### 1. Run the `curl` or Python Smoke Test
```bash
export GOOGLE_CLOUD_PROJECT="YOUR_PROJECT_ID"
bash examples/01-wire-protocol-smoke-test/curl_examples.sh
# or
python3 examples/01-wire-protocol-smoke-test/mcp_probe.py --project YOUR_PROJECT_ID
```

### 2. Run the ADK Agent (`gemini-3-flash-preview`)
```bash
cd examples/03-adk-cloud-ops-agent
uv sync
export GOOGLE_CLOUD_PROJECT="YOUR_PROJECT_ID"
export GOOGLE_CLOUD_LOCATION="global"
export GOOGLE_GENAI_USE_VERTEXAI="True"
uv run adk web
```

### 3. Preview or Provision a Managed Agent
```bash
python3 examples/04-managed-agent/manage_agent.py --project YOUR_PROJECT_ID --dry-run
```
